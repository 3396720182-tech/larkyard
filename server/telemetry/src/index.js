// Larkyard 匿名统计后端（Cloudflare Worker + D1）。部署步骤见 server/telemetry/README.md。
//
// POST /v1/report   收一份每日报告：严格校验（多一个键整份拒收）、按 (install_id, day) 去重写入。
//                   不读、不存请求的 IP 和 User-Agent；限流用 Cloudflare 的 Rate Limiting 绑定（只在边缘计数，不落库）。
// GET  /v1/summary  最近一次的全体汇总（公开）：只有分布，不含 install_id；每个桶按贡献机器数做小样本保护（见 summarize）。
// 定时任务（每天）：删掉 365 天前的报告；每周一生成一份全体汇总。
import { Invalid, schema, validate } from "./validate.js";

export const RETENTION_DAYS = 365;
export const SUMMARY_WINDOW_DAYS = 7;
export const MIN_BUCKET = 5;      // 某个桶 / 取值的贡献机器少于这么多台，合并进 other
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
// 小样本保护（docs/telemetry.md「公开汇总」）：
// - 每个分布的每个桶都按「有多少台机器贡献过」判断，少于 MIN_BUCKET 台的并进 other。
//   版本、系统这类头部字段一台机器只算一次，计数类字段（轮次结果、上下文桶、媒体耗时……）按贡献机器数判断。
// - 补充合并：other 自己不到 MIN_BUCKET 台时，不把它删掉（删掉的话，公开的总量减去其余各桶就能算出它），
//   而是再把机器最少的桶并进来，直到 other 够 MIN_BUCKET 台。这样公开的每个分布都加得回原来的总量，
//   用总量或兄弟分布相减只能得到 other，而 other 至少有 MIN_BUCKET 台机器。整个分布都凑不够就整个不列。
// - 活跃机器少于 MIN_MACHINES 台：只公开 TOTALS_ONLY 里的总量（分布照样做桶级保护），不公开头部字段。
// - 活跃机器少于 MIN_BUCKET 台：连总量也不公开，只给机器数和报告数。
const HEADER_ENUMS = ["version", "os", "arch", "python", "claude_code", "codex"];
const TOTALS_ONLY = ["tickets_new", "rounds", "round_outcomes", "model_errors", "lark_errors"];
const SUMS = ["tickets_new", "rounds"];

function addBucket(dist, key, n, iid) {
  const b = (dist[key] = dist[key] || { n: 0, machines: new Set() });
  b.n += n;
  b.machines.add(iid);
}

function mergeSmall(dist) {
  // dist：桶 → { n, machines }。返回 桶 → n：贡献机器少于 MIN_BUCKET 台的桶并进 other，
  // other 不够 MIN_BUCKET 台就继续把机器最少的桶并进去（补充合并）；凑不够就返回 {}。结果总是加得回原总量。
  const kept = new Map();
  let other = null;
  const absorb = (b) => {
    other = other || { n: 0, machines: new Set() };
    other.n += b.n;
    for (const m of b.machines) other.machines.add(m);
  };
  for (const [k, b] of Object.entries(dist)) {
    if (b.machines.size >= MIN_BUCKET && k !== "other") kept.set(k, b);
    else absorb(b);
  }
  if (other && other.machines.size < MIN_BUCKET) {
    const order = [...kept.entries()].sort((a, b) => a[1].machines.size - b[1].machines.size || a[1].n - b[1].n
      || (a[0] < b[0] ? -1 : 1));
    for (const [k, b] of order) {
      kept.delete(k);
      absorb(b);
      if (other.machines.size >= MIN_BUCKET) break;
    }
    if (other.machines.size < MIN_BUCKET) return {};
  }
  const out = Object.fromEntries([...kept.entries()].map(([k, b]) => [k, b.n]));
  if (other) out.other = other.n;
  return out;
}

function sumSmall(machinesWithAny, n) {
  // 总量也按贡献机器数判断：只有不到 MIN_BUCKET 台机器有过这类数据时不公开。
  // 分布都是补充合并过的（加得回总量），所以公开总量不会让被并掉的桶露出来
  return machinesWithAny.size >= MIN_BUCKET ? n : undefined;
}

export function summarize(rows, fromDay, toDay, generatedAt) {
  const machines = new Set(rows.map((r) => r.install_id));
  const sums = {}; // 字段 → { n, machines }
  const dists = {}; // 字段 → 桶 → { n, machines }
  const media = {}; // 媒体种类 → { ok/fail: 分布, seconds: 分布, size_mb: 分布 }
  const latest = new Map(); // install_id → 窗口里最后一份报告（头部字段按机器算，不按天算）
  for (const r of rows) {
    const rep = JSON.parse(r.body);
    const iid = r.install_id;
    for (const [k, v] of Object.entries(rep)) {
      if (SUMS.includes(k)) {
        if (v > 0) {
          const s = (sums[k] = sums[k] || { n: 0, machines: new Set() });
          s.n += v;
          s.machines.add(iid);
        }
      } else if (k === "media") {
        for (const [kind, m] of Object.entries(v)) {
          const t = (media[kind] = media[kind] || { result: {}, seconds: {}, size_mb: {} });
          for (const [mk, mv] of Object.entries(m)) {
            if (typeof mv === "number") { if (mv > 0) addBucket(t.result, mk, mv, iid); }
            else for (const [bk, bn] of Object.entries(mv)) if (bn > 0) addBucket(t[mk], bk, bn, iid);
          }
        }
      } else if (schema.fields[k] && schema.fields[k].type === "counts") {
        const d = (dists[k] = dists[k] || {});
        for (const [bk, bn] of Object.entries(v)) if (bn > 0) addBucket(d, bk, bn, iid);
      }
    }
    const prev = latest.get(iid);
    if (!prev || prev.day < rep.day) latest.set(iid, rep);
  }
  const out = {
    window: { from: fromDay, to: toDay },
    generated_at: generatedAt,
    machines: machines.size,
    reports: rows.length,
  };
  if (machines.size < MIN_BUCKET) {
    out.note = `活跃机器少于 ${MIN_BUCKET} 台，不公开任何数字`;
    return out;
  }
  const totals = {};
  for (const [k, s] of Object.entries(sums)) {
    const n = sumSmall(s.machines, s.n);
    if (n !== undefined) totals[k] = n;
  }
  for (const [k, d] of Object.entries(dists)) {
    const m = mergeSmall(d);
    if (Object.keys(m).length) totals[k] = m;
  }
  const mediaOut = {};
  for (const [kind, t] of Object.entries(media)) {
    const o = { ...mergeSmall(t.result) }; // ok / fail 有一边不够 5 台时两边合成 other，只剩总次数
    for (const mk of ["seconds", "size_mb"]) {
      const m = mergeSmall(t[mk]);
      if (Object.keys(m).length) o[mk] = m;
    }
    if (Object.keys(o).length) mediaOut[kind] = o;
  }
  if (Object.keys(mediaOut).length) totals.media = mediaOut;
  if (machines.size < MIN_MACHINES) {
    out.note = `活跃机器少于 ${MIN_MACHINES} 台，只公开总量`;
    out.totals = Object.fromEntries(TOTALS_ONLY.filter((k) => k in totals).map((k) => [k, totals[k]]));
    return out;
  }
  out.totals = totals;
  const header = Object.fromEntries(HEADER_ENUMS.map((k) => [k, {}]));
  for (const [iid, rep] of latest) {
    for (const k of HEADER_ENUMS) addBucket(header[k], rep[k] || "none", 1, iid);
  }
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
