// Larkyard 匿名统计后端（Cloudflare Worker + D1）。部署步骤见 server/telemetry/README.md。
//
// POST /v1/report   收一份每日报告：严格校验（多一个键整份拒收）、按 (install_id, day) 去重写入。
//                   不读、不存请求的 IP 和 User-Agent；限流用 Cloudflare 的 Rate Limiting 绑定（只在边缘计数，不落库）。
// GET  /v1/summary  最近一次的全体汇总（公开）：只有分布，不含 install_id；小样本按 docs/telemetry.md 的规则合并或不公开。
// 定时任务（每天）：删掉 365 天前的报告；每周一生成一份全体汇总。
import { Invalid, schema, validate } from "./validate.js";

export const RETENTION_DAYS = 365;
export const SUMMARY_WINDOW_DAYS = 7;
export const MIN_BUCKET = 5;      // 单个取值少于这么多台机器，合并进 other
export const MIN_MACHINES = 20;   // 活跃机器少于这么多台，只公开总量
const MAX_AGE_DAYS = 8;           // 客户端最多补发 7 天前的报告

const HEADERS = { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" };

function json(status, obj) {
  return new Response(obj === null ? null : JSON.stringify(obj), { status, headers: HEADERS });
}

function dayOf(ms) {
  return new Date(ms).toISOString().slice(0, 10);
}

function dayOk(day, now) {
  const t = Date.parse(day + "T00:00:00Z");
  if (Number.isNaN(t) || dayOf(t) !== day) return false;
  // 客户端按本机时区算日子：允许比 UTC 的今天早 MAX_AGE_DAYS 天、晚 1 天
  return t >= now - MAX_AGE_DAYS * 86400000 && t <= now + 86400000;
}

export async function handleReport(request, env, now = Date.now()) {
  if (env.RATE_LIMITER) {
    // 按来源 IP 限流：key 只交给 Cloudflare 的边缘计数器，不写日志、不落库
    const ip = request.headers.get("cf-connecting-ip") || "unknown";
    const { success } = await env.RATE_LIMITER.limit({ key: ip });
    if (!success) return json(429, { error: "rate_limited" });
  }
  const declared = Number(request.headers.get("content-length") || 0);
  if (declared > schema.max_bytes) return json(413, { error: "too_large" });
  const text = await request.text();
  if (new TextEncoder().encode(text).length > schema.max_bytes) return json(413, { error: "too_large" });
  let report;
  try {
    report = JSON.parse(text);
    validate(report);
  } catch (e) {
    if (e instanceof Invalid || e instanceof SyntaxError) return json(400, { error: "invalid" });
    throw e;
  }
  if (!dayOk(report.day, now)) return json(400, { error: "invalid_day" });
  const res = await env.DB.prepare(
    "INSERT OR IGNORE INTO reports(install_id, day, schema, received_day, body) VALUES(?,?,?,?,?)"
  ).bind(report.install_id, report.day, report.schema, dayOf(now), JSON.stringify(report)).run();
  const dup = res && res.meta && res.meta.changes === 0;
  return json(dup ? 200 : 201, { ok: true, duplicate: !!dup });
}

// ---------- 汇总 ----------
const HEADER_ENUMS = ["version", "os", "arch", "python", "claude_code", "codex"];
// 机器少的时候只公开这些总量（不公开版本、系统这类能认出个人的分布）
const TOTALS_ONLY = ["tickets_new", "rounds", "round_outcomes", "model_errors", "lark_errors"];

function addCounts(into, counts) {
  for (const [k, n] of Object.entries(counts || {})) into[k] = (into[k] || 0) + n;
}

function mergeSmall(machinesByValue) {
  // 取值 → 机器数；少于 MIN_BUCKET 台的并进 other
  const out = {};
  for (const [v, n] of Object.entries(machinesByValue)) {
    const key = n < MIN_BUCKET ? "other" : v || "none";
    out[key] = (out[key] || 0) + n;
  }
  if (out.other !== undefined && out.other < MIN_BUCKET) {
    // 合并后 other 自己还太小：只说「其他」有多少不再细分也可能认出人，直接不列
    delete out.other;
  }
  return out;
}

export function summarize(rows, fromDay, toDay, generatedAt) {
  const machines = new Set(rows.map((r) => r.install_id));
  const totals = {};
  const header = Object.fromEntries(HEADER_ENUMS.map((k) => [k, {}]));
  const latest = new Map(); // install_id → 窗口里最后一份报告（头部字段按机器算，不按天算）
  for (const r of rows) {
    const rep = JSON.parse(r.body);
    for (const [k, v] of Object.entries(rep)) {
      if (k === "tickets_new" || k === "rounds") totals[k] = (totals[k] || 0) + v;
      else if (k === "media") {
        totals.media = totals.media || {};
        for (const [kind, m] of Object.entries(v)) {
          const t = (totals.media[kind] = totals.media[kind] || {});
          for (const [mk, mv] of Object.entries(m)) {
            if (typeof mv === "number") t[mk] = (t[mk] || 0) + mv;
            else addCounts((t[mk] = t[mk] || {}), mv);
          }
        }
      } else if (schema.fields[k] && schema.fields[k].type === "counts") addCounts((totals[k] = totals[k] || {}), v);
    }
    const prev = latest.get(r.install_id);
    if (!prev || prev.day < rep.day) latest.set(r.install_id, rep);
  }
  for (const rep of latest.values()) {
    for (const k of HEADER_ENUMS) header[k][rep[k] ?? ""] = (header[k][rep[k] ?? ""] || 0) + 1;
  }
  const out = {
    window: { from: fromDay, to: toDay },
    generated_at: generatedAt,
    machines: machines.size,
    reports: rows.length,
  };
  if (machines.size < MIN_MACHINES) {
    out.note = `活跃机器少于 ${MIN_MACHINES} 台，只公开总量`;
    out.totals = Object.fromEntries(TOTALS_ONLY.filter((k) => k in totals).map((k) => [k, totals[k]]));
    return out;
  }
  out.totals = totals;
  out.machines_by = Object.fromEntries(HEADER_ENUMS.map((k) => [k, mergeSmall(header[k])]));
  return out;
}

export async function buildSummary(env, now = Date.now()) {
  const toDay = dayOf(now - 86400000);
  const fromDay = dayOf(now - SUMMARY_WINDOW_DAYS * 86400000);
  const { results } = await env.DB.prepare(
    "SELECT install_id, day, body FROM reports WHERE day>=? AND day<=?"
  ).bind(fromDay, toDay).all();
  const summary = summarize(results || [], fromDay, toDay, new Date(now).toISOString());
  await env.DB.prepare("INSERT OR REPLACE INTO summaries(week, body) VALUES(?,?)")
    .bind(toDay, JSON.stringify(summary)).run();
  return summary;
}

export async function cleanup(env, now = Date.now()) {
  const cutoff = dayOf(now - RETENTION_DAYS * 86400000);
  await env.DB.prepare("DELETE FROM reports WHERE day<?").bind(cutoff).run();
}

async function latestSummary(env) {
  const row = await env.DB.prepare("SELECT body FROM summaries ORDER BY week DESC LIMIT 1").first();
  if (!row) return json(404, { error: "no_summary_yet" });
  return new Response(row.body, {
    status: 200,
    headers: { ...HEADERS, "cache-control": "public, max-age=3600", "access-control-allow-origin": "*" },
  });
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname === "/v1/report") {
      if (request.method !== "POST") return json(405, { error: "method" });
      return handleReport(request, env);
    }
    if (pathname === "/v1/summary" && request.method === "GET") return latestSummary(env);
    if (pathname === "/" && request.method === "GET") {
      return new Response("Larkyard anonymous usage statistics. See docs/telemetry.md in the Larkyard repository.\n",
        { headers: { "content-type": "text/plain; charset=utf-8" } });
    }
    return json(404, { error: "not_found" });
  },

  async scheduled(controller, env, ctx) {
    const now = controller.scheduledTime || Date.now();
    await cleanup(env, now);
    if (new Date(now).getUTCDay() === 1) await buildSummary(env, now); // 每周一
  },
};
