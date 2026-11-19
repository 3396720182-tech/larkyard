// 后端单测：用 Node 自带的 node:sqlite 模拟 D1，不需要 wrangler、不连 Cloudflare。
// 运行：cd server/telemetry && npm test（Node 22.5+）
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";

import worker, { buildSummary, cleanup, handleReport, MIN_MACHINES, summarize } from "../src/index.js";
import { validate } from "../src/validate.js";

const here = new URL(".", import.meta.url);
const EXAMPLE = JSON.parse(readFileSync(new URL("../../../docs/telemetry-example.json", here), "utf-8"));
const MIGRATION = readFileSync(new URL("../migrations/0001_init.sql", here), "utf-8");
const NOW = Date.parse("2026-11-19T06:00:00Z");

function fakeD1() {
  const db = new DatabaseSync(":memory:");
  db.exec(MIGRATION);
  const stmt = (sql, args = []) => ({
    bind: (...a) => stmt(sql, a),
    run: async () => ({ meta: { changes: Number(db.prepare(sql).run(...args).changes) } }),
    all: async () => ({ results: db.prepare(sql).all(...args) }),
    first: async () => db.prepare(sql).get(...args) ?? null,
  });
  return { prepare: (sql) => stmt(sql), raw: db };
}

function env(extra = {}) {
  return { DB: fakeD1(), ...extra };
}

function post(body, headers = {}) {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return new Request("https://larkyard-telemetry.example.workers.dev/v1/report", {
    method: "POST", body: text, headers: { "content-type": "application/json", ...headers },
  });
}

function report(over = {}) {
  return { ...structuredClone(EXAMPLE), ...over };
}

test("示例报告通过校验", () => {
  validate(EXAMPLE);
});

test("白名单以外的键、类型、枚举整份拒收", () => {
  const bad = [
    { title: "x" }, { os: "macOS on somebody-mac" }, { events: { stall_flag: 1, secret: 1 } },
    { events: { stall_flag: "1" } }, { events: { stall_flag: -1 } }, { events: { stall_flag: 1.5 } },
    { config: { model: "opus" } }, { media: { image: { ok: 1, name: 1 } } }, { install_id: "x" },
    { schema: 2 }, { version: "0.2.0-somebody" }, { day_cost_usd: "12.3" }, { __proto__: { x: 1 }, extra: 1 },
  ];
  for (const b of bad) assert.throws(() => validate(report(b)), JSON.stringify(b));
  const r = report();
  delete r.install_id;
  assert.throws(() => validate(r));
});

test("收报告：写入、去重、不存 IP 和 UA", async () => {
  const e = env();
  const req = () => post(report(), { "cf-connecting-ip": "203.0.113.7", "user-agent": "larkyard" });
  let res = await handleReport(req(), e, NOW);
  assert.equal(res.status, 201);
  res = await handleReport(req(), e, NOW);
  assert.equal(res.status, 200);
  assert.equal((await res.json()).duplicate, true);
  const rows = e.DB.raw.prepare("SELECT * FROM reports").all();
  assert.equal(rows.length, 1);
  const dump = JSON.stringify(rows);
  assert.ok(!dump.includes("203.0.113.7"));
  assert.ok(!dump.includes("larkyard\""));
  assert.equal(rows[0].received_day, "2026-11-19");
});

test("坏请求：400 / 413 / 405 / 太老的日子", async () => {
  const e = env();
  assert.equal((await handleReport(post("{not json"), e, NOW)).status, 400);
  assert.equal((await handleReport(post(report({ title: "x" })), e, NOW)).status, 400);
  assert.equal((await handleReport(post(report({ day: "2026-10-01" })), e, NOW)).status, 400);
  assert.equal((await handleReport(post(report({ day: "2026-02-30" })), e, NOW)).status, 400);
  assert.equal((await handleReport(post("x".repeat(9000)), e, NOW)).status, 413);
  const get = new Request("https://x.workers.dev/v1/report");
  assert.equal((await worker.fetch(get, e)).status, 405);
  assert.equal(e.DB.raw.prepare("SELECT COUNT(*) n FROM reports").get().n, 0);
});

test("限流：超过就 429，不写库", async () => {
  const e = env({ RATE_LIMITER: { limit: async () => ({ success: false }) } });
  assert.equal((await handleReport(post(report()), e, NOW)).status, 429);
  assert.equal(e.DB.raw.prepare("SELECT COUNT(*) n FROM reports").get().n, 0);
});

test("定时任务：删 365 天前的报告", async () => {
  const e = env();
  e.DB.raw.prepare("INSERT INTO reports VALUES(?,?,?,?,?)").run("a", "2025-11-01", 1, "2025-11-02", "{}");
  e.DB.raw.prepare("INSERT INTO reports VALUES(?,?,?,?,?)").run("a", "2026-11-01", 1, "2026-11-02", "{}");
  await cleanup(e, NOW);
  assert.deepEqual(e.DB.raw.prepare("SELECT day FROM reports").all().map((r) => r.day), ["2026-11-01"]);
});

function uuid(i) {
  return `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`;
}

test("汇总：机器少时只公开总量，不含 install_id", async () => {
  const e = env();
  for (let i = 0; i < 6; i++) {
    await handleReport(post(report({ install_id: uuid(i), day: "2026-11-17" })), e, NOW);
  }
  const s = await buildSummary(e, NOW);
  assert.equal(s.machines, 6);
  assert.ok(s.note);
  assert.equal(s.machines_by, undefined);
  assert.deepEqual(Object.keys(s.totals).sort(), ["lark_errors", "model_errors", "round_outcomes", "rounds", "tickets_new"]);
  assert.equal(s.totals.rounds, 54);
  assert.ok(!JSON.stringify(s).includes("00000000-0000-4000"));
  const res = await worker.fetch(new Request("https://x.workers.dev/v1/summary"), e);
  assert.equal(res.status, 200);
  assert.equal((await res.json()).machines, 6);
});

test("汇总：不到 5 台机器时连总量也不公开", () => {
  const rows = [0, 1, 2].map((i) => ({ install_id: uuid(i), day: "2026-11-17", body: JSON.stringify(report({ install_id: uuid(i) })) }));
  const s = summarize(rows, "2026-11-12", "2026-11-18", "t");
  assert.equal(s.machines, 3);
  assert.equal(s.totals, undefined);
});

test("汇总：分布按每个桶的贡献机器数做小样本保护", () => {
  // 外部复查的场景：20 台里只有 1 台贡献了 >400k 上下文桶，不能单独公开 >400k: 1
  const rows = [];
  for (let i = 0; i < MIN_MACHINES; i++) {
    const over = {};
    if (i === 0) {
      over.ctx_tokens = { "50-150k": 7, ">400k": 1 };
      over.round_outcomes = { ready: 5, failed: 1 };
      over.media = { video: { fail: 1, seconds: { ">120": 1 }, size_mb: { ">100": 1 } } };
      over.tickets_by_executor = { codex: 1 };
    }
    rows.push({ install_id: uuid(i), day: "2026-11-17", body: JSON.stringify(report({ install_id: uuid(i), ...over })) });
  }
  // 少于 20 台：只有总量，但总量里的分布也要做桶级保护
  const few = summarize(rows.slice(0, MIN_MACHINES - 1), "a", "b", "t");
  assert.equal(few.totals.round_outcomes.failed, undefined);
  assert.equal(few.totals.ctx_tokens, undefined);
  const s = summarize(rows, "2026-11-12", "2026-11-18", "t");
  const dump = JSON.stringify(s);
  assert.equal(s.totals.ctx_tokens[">400k"], undefined);
  assert.equal(s.totals.ctx_tokens.other, undefined); // 并进 other 后 other 也只有 1 台，不列
  assert.equal(s.totals.ctx_tokens["50-150k"], 7 * MIN_MACHINES);
  assert.equal(s.totals.round_outcomes.failed, undefined);
  assert.equal(s.totals.media.video, undefined);
  assert.equal(s.totals.tickets_by_executor.codex, undefined);
  assert.ok(!dump.includes(">400k"));
  assert.ok(!dump.includes(">120"));
  // 同一个桶有 5 台机器贡献就公开
  for (let i = 1; i < 5; i++) {
    const r = JSON.parse(rows[i].body);
    r.ctx_tokens = { ">400k": 1 };
    rows[i].body = JSON.stringify(r);
  }
  assert.equal(summarize(rows, "a", "b", "t").totals.ctx_tokens[">400k"], 5);
});

test("汇总：机器够多时公开分布，少于 5 台的取值合并进 other", () => {
  const rows = [];
  for (let i = 0; i < MIN_MACHINES + 5; i++) {
    const os = i < 3 ? "wsl" : i < 6 ? "other" : "macos";
    rows.push({ install_id: uuid(i), day: "2026-11-17", body: JSON.stringify(report({ install_id: uuid(i), os })) });
  }
  const s = summarize(rows, "2026-11-12", "2026-11-18", "t");
  assert.equal(s.machines, 25);
  assert.deepEqual(s.machines_by.os, { macos: 19, other: 6 }); // wsl 3 台 + other 3 台 → other 6
  assert.equal(s.totals.media.image.ok, 75);
  assert.deepEqual(s.totals.media.image.seconds, { "<5": 75 });
});

test("其他路径 404", async () => {
  assert.equal((await worker.fetch(new Request("https://x.workers.dev/admin"), env())).status, 404);
});
