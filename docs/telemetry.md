# Larkyard 匿名使用统计

Larkyard 可以每天发一份匿名的运行统计给维护者，用来发现常见故障、决定先优化什么。**默认关闭**，只有你执行 `ticket telemetry on` 才会开始发。统计里只有数字和分类，没有你的任何内容。

> 维护者部署后会在这里填上：上报地址 `<后端域名>`、公开数据 `<公开数据链接>`、联系方式 `<联系方式>`。在那之前代码里的默认上报地址是空的：开启后报告只在本机生成，不会发出去。

## 怎么开、怎么关、怎么看

```
ticket telemetry status      # 现在是开还是关，上次发了什么、发成功没有
ticket telemetry show        # 打印下一份要发的报告（只打印，不发送；关闭状态下也能看）
ticket telemetry show --sent # 看已经发过的报告（本机副本在 ~/.ticketd/telemetry/sent/）
ticket telemetry on          # 开启
ticket telemetry off         # 关闭，并删除本机的匿名 ID 和还没发出的报告
```

设置环境变量 `DO_NOT_TRACK=1` 或 `TICKETD_TELEMETRY=0`（也可以写进 `~/.ticketd/config.env`）会强制关闭，`ticket telemetry on` 也打不开。检测到 `CI=true` 时也不发送。worker 不能开关它（工单里执行会被拒绝），只有你本人能决定。

## 发什么

每天一份，大小约 1KB，内容是前一天（本机时间 00:00～24:00）的汇总，每天 00:00 之后随机延迟 0～6 小时发出：

- Larkyard、Python、Claude Code、Codex 的版本（只到次版本号），系统类型（macOS / Linux / WSL）、系统主版本、CPU 架构。
- 几个配置有没有改过默认值（只报「是 / 否」，不报值），比如有没有指定模型。不报模型名。
- 新开了几张工单、跑了几轮、每轮结果（完成、需要你决定、失败……）各多少次。
- 每轮用时、成本、上下文大小的**区间分布**，比如「10～30 分钟的有 3 轮」。不报精确数字。
- 故障分类计数：卡住、故障等待、模型接口错误的类型（余额不足、认证失败、限流、网络……）、飞书错误码（如 230011）。
- 结果是怎么送到的：卡片、改发文字、没送达，各多少次。
- 图片 / 视频 / 文档上传的成功失败次数，以及耗时和大小的区间分布。
- 一个随机的匿名 ID。它在你开启时随机生成，关闭时删除，再次开启会换一个新的。它和你的电脑、账号、飞书身份都没有关系，作用只是统计「有多少台机器在用」。

开启当天和开启之前的数据不上报：从开启后的第二天起算。

## 不发什么

- 任何内容：需求、追问、汇报、工单标题、文档、聊天记录。
- 文件名、路径、链接。
- 飞书的 open_id、chat_id、message_id，也不发它们的哈希。
- 报错的原文。只发归类后的错误码。
- 模型名、网关地址、代理设置、环境变量的值。
- 主机名、用户名、IP 地址、MAC 地址，也不发它们的哈希。

每个字段的取值都写死在代码里，不认识的值一律记成 `other`。报告发出前会再校验一遍，出现字段表以外的内容就整份丢弃；后端用同一份字段表再校验一次，多一个键就整份拒收。

## 数据去哪、留多久、谁能看

- 发到 `<后端域名>`，由 Larkyard 的维护者本人持有（Cloudflare Workers + D1），原始报告只有维护者能看，不与任何第三方共享。服务器不保存 IP 地址和 User-Agent，也不开请求日志；防刷用 Cloudflare 自带的按 IP 限流，只在边缘计数，不落库。
- 原始报告保留 365 天，之后自动删除。
- 全体汇总每周公开一次：<公开数据链接>。公开的只有所有机器合在一起的分布，不会按机器拆开。每个取值、每个区间都按「有几台机器贡献过」判断：少于 5 台机器的并进 `other`；`other` 自己也不到 5 台时，再把机器最少的格子一起并进去，直到够 5 台，凑不够就整个分布不列（比如只有你一台机器出现过「上下文超过 400k」，这一格不会出现在公开汇总里，也没法用总量减出来）。活跃机器少于 20 台时只公开总量（工单数、轮次数、轮次结果、错误码），少于 5 台时什么数字都不公开。
- 想删除已经上报的数据：执行 `ticket telemetry status` 看到你的匿名 ID，按 <联系方式> 提交，维护者按这个 ID 删除。

## 字段有变化时

每次增加字段，报告的版本号（`schema`）会加一，并在这一页说明加了什么、为什么。你之前同意的是旧版本，升级后仍按旧版本的字段发送，直到你再次执行 `ticket telemetry on` 同意新版本。只删字段或收窄区间不需要重新同意。

## 完整字段表（schema v1）

机器可读的白名单：[`ticketd/telemetry_schema.json`](../ticketd/telemetry_schema.json)（客户端和后端共用）。完整示例：[`telemetry-example.json`](./telemetry-example.json)。某类数据为 0 就不写。

| 字段 | 取值 | 说明 |
|---|---|---|
| `schema` | `1` | 报告格式版本 |
| `install_id` | 随机 UUIDv4 | 开启时生成，关闭时删除 |
| `day` | `YYYY-MM-DD` | 统计的是哪一天（不带时刻） |
| `version` | 如 `0.2.0`；不是正式版本号时为 `dev` | Larkyard 版本 |
| `os` / `os_major` / `arch` | `macos` `linux` `wsl` `other` / 整数 / `arm64` `x86_64` `other` | 系统类型、主版本、CPU 架构 |
| `python` / `claude_code` / `codex` | 如 `3.9` / `2.1` / 空 | 只到次版本，没装为空 |
| `config` | `max_workers`（1～8）、`digest_on`、`compact_on`、`video_folder_set`、`worker_env_set`、`model_set` | 只报是否改过默认值 |
| `tickets_new` / `tickets_by_executor` | 计数 / `claude` `codex` | 当天新开的工单 |
| `rounds` / `round_outcomes` | 计数 / `ready` `blocked` `failed` `waiting` `dropped` `retry` `stopped` `held` `compact` `compact_failed` `other` | 当天结束的轮次 |
| `round_minutes` | `<2` `2-10` `10-30` `30-60` `60-120` `>120` | 每轮用时（分钟）的区间计数 |
| `round_cost_usd` | `0` `<0.1` `0.1-1` `1-5` `5-20` `>20` | 每轮成本（美元）区间，只算 Claude |
| `day_cost_usd` | `0` `<1` `1-5` `5-20` `20-50` `>50` | 当天 worker 成本合计的区间 |
| `rounds_per_ticket` | `1` `2-3` `4-10` `>10` | 当天有轮次结束的工单各自累计到第几轮 |
| `ctx_tokens` | `<50k` `50-150k` `150-400k` `>400k` | 每轮上下文峰值的区间计数 |
| `concurrency_max` | 整数 | 当天同时在跑的最大工单数 |
| `events` | `stall_flag` `stall_interrupt` `stall_abort` `outage_start` `api_error` `run_requested` `run_approved` `delivery_dead` `media_stuck` `card_giveup` `compact` `compact_failed` `other` | 关键事件计数 |
| `model_errors` | `balance` `auth` `rate_limit` `overloaded` `network` `server_5xx` `client_4xx` `other` | 模型接口错误归类 |
| `lark_errors` | `230001` `230002` `230011` `230025` `99992361` `rate_limit` `network` `api_other` `other` | 飞书 / lark-cli 发送失败归类 |
| `needs_user` | `blocked` `waiting` `ask` | 需要你的次数 |
| `grants` | `fs` `local_net` `socket` | 建单之后的授权次数（不带路径） |
| `response_minutes` | `<10` `10-60` `60-240` `>240` | 你多快回应 blocked / waiting |
| `delivery` | `sent` `card_patched` `fallback_text` `card_patch_fallback` `video_fallback` `dead` | 结果怎么送到的 |
| `media` | `image` / `video` / `doc` 各有 `ok`、`fail` 计数，`seconds`（`<5` `5-30` `30-120` `>120`）、`size_mb`（`<1` `1-10` `10-100` `>100`）区间计数 | 上传 / 导入 |

## 改字段的评审清单（给开发者）

- 字段是不是计数 / 区间 / 枚举；枚举有没有兜底 `other`。
- 有没有可能间接带出原文（比如「把错误码换成错误消息前 20 个字」就不行）。
- 改 `ticketd/telemetry_schema.json`、升 `schema`、在本页加一节「vN 新增了什么、为什么」。
- 更新 `tests/test_telemetry.py`：「样本数据里塞进标题、路径、open_id，生成的报告里一个也找不到」的用例要覆盖新字段；后端测试 `server/telemetry/test/` 一起跑。
