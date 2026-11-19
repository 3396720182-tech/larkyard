// 匿名统计报告的严格校验：和客户端（ticketd/telemetry.py）用同一份白名单 ticketd/telemetry_schema.json。
// 多一个键、类型不对、字符串不在枚举里，整份拒收。报错只说哪个字段，不回显内容。
import schema from "../../../ticketd/telemetry_schema.json" with { type: "json" };

export { schema };

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;

export class Invalid extends Error {}

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const isInt = (v) => typeof v === "number" && Number.isInteger(v);
const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);

function checkCount(path, n) {
  if (!isInt(n) || n < 0 || n > schema.max_count) throw new Invalid(`${path} 不是合法计数`);
}

function checkCounts(path, v, keys) {
  if (!isObj(v)) throw new Invalid(`${path} 不是对象`);
  for (const [k, n] of Object.entries(v)) {
    if (!keys.includes(k)) throw new Invalid(`${path} 有白名单以外的键`);
    checkCount(`${path}.${k}`, n);
  }
}

function checkField(path, spec, v) {
  switch (spec.type) {
    case "const":
      if (!isInt(v) || v !== spec.value) throw new Invalid(`${path} 取值不对`);
      return;
    case "uuid":
      if (typeof v !== "string" || !UUID_RE.test(v)) throw new Invalid(`${path} 不是 UUIDv4`);
      return;
    case "day":
      if (typeof v !== "string" || !DAY_RE.test(v)) throw new Invalid(`${path} 不是日期`);
      return;
    case "pattern":
      // Python 的 re.match 只锚开头；白名单里的模式都自带 ^…$，两边语义一致
      if (typeof v !== "string" || !new RegExp(spec.pattern).test(v)) throw new Invalid(`${path} 格式不对`);
      return;
    case "enum":
      if (typeof v !== "string" || !spec.values.includes(v)) throw new Invalid(`${path} 不在枚举里`);
      return;
    case "int":
      if (!isInt(v) || v < spec.min || v > spec.max) throw new Invalid(`${path} 不是合法整数`);
      return;
    case "bool":
      if (typeof v !== "boolean") throw new Invalid(`${path} 不是布尔值`);
      return;
    case "counts":
      checkCounts(path, v, spec.keys);
      return;
    case "object":
      checkObject(path, spec.fields, v);
      return;
    case "media":
      if (!isObj(v)) throw new Invalid(`${path} 不是对象`);
      for (const [k, x] of Object.entries(v)) {
        if (k === "ok" || k === "fail") checkCount(`${path}.${k}`, x);
        else if (k === "seconds" || k === "size_mb") checkCounts(`${path}.${k}`, x, schema.media[k]);
        else throw new Invalid(`${path} 有白名单以外的键`);
      }
      return;
    default:
      throw new Invalid(`未知类型 ${spec.type}`);
  }
}

function checkObject(path, fields, v) {
  if (!isObj(v)) throw new Invalid(`${path || "报告"} 不是对象`);
  for (const k of Object.keys(v)) {
    if (!own(fields, k)) throw new Invalid(`${path || "报告"} 有白名单以外的键`);
  }
  for (const [k, spec] of Object.entries(fields)) {
    if (own(v, k)) checkField(path ? `${path}.${k}` : k, spec, v[k]);
    else if (spec.required) throw new Invalid(`缺少 ${k}`);
  }
}

export function validate(report) {
  checkObject("", schema.fields, report);
}
