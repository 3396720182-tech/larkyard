## Context

心跳的白名单在 `add-console-enrollment` design D5，采集清单 c1 在 D4。控制台接口约定和服务端实现在控制台仓库（同名约定见那边的变更）。

## Decisions

### D1. 采集清单 `c2`

清单文本写死在 `ticketd/console.py`（`CONSENT_C2`），打印的就是参与哈希的那段文本（UTF-8，`\n` 换行，最后一行也以 `\n` 结尾）：

```
控制台会收集（0 级：设备状态；1 级里只收出错记录）
入网时一次：主机名；系统名称、版本和架构；Larkyard 版本；本机 bot 的 open_id（TICKETD_OWNER）；你同意的这份清单的版本。
每 5 分钟一次：Larkyard 版本；系统名称、版本和架构；ticket doctor 的结论（是否通过、不通过的检查项名称）；运行中和排队中的工单数量；自动更新是否启用、更新通道、最近一次更新结果和时间、本机暂停更新的到期时间；上次上报以来的出错记录（时间、工单号、出错的环节、错误归类码，都是代码里写死的代号，不含任何文字）。
不会收集：工单标题、原话、追问、产物、文件、过程日志、成本、报错原文。
诊断包：只有你本人确认发送时才发，发之前先给你看完整内容。
谁能看：只有控制台管理员。随时查看下一次上报的内容：ticket console show。退出：ticket enroll --leave。
```

c1 文本和哈希不变（`CONSENT_C1`）。本机 `console.json` 的 `consent_version` 决定心跳带不带 `problems`：不是 c2 就一个字节都不带。

### D2. 换清单：`ticket console accept [c1|c2]`

只在终端里由本人执行（worker 环境、非终端标准输入拒绝）；已入网、没被停止上报才可以。打印全文和 SHA-256 → 输入 `yes` → `POST /api/v1/device/consent {"version", "sha256"}`（设备令牌）→ 控制台回 `{"ok": true, "consent_version": <同一个>}` 才改本机 `console.json`。升到 c2 时把出错记录的游标设成当前最大 id：同意之前的记录不补报。入网（清单 c2）同样在写完本机文件后设游标。改回 c1（收窄采集）时，控制台回 404（不认识这个接口的旧版本）也允许只改本机并提示。控制台确认后本机写不进去时，提示「控制台已改成 X，本机没写进去，请重试 ticket console accept X」。

### D3. 心跳 `problems`

```json
"problems": {"items": [{"at": "2026-11-22T10:00:00Z", "ticket": "T0012", "step": "round", "code": "failed"}], "more": 0}
```

- `items` 按时间从旧到新，最多 20 条（取最新的 20 条），其余条数记在 `more`（最多 100000）。
- `ticket`：`T` 加 1～8 位数字，不属于工单的系统事件（`_ticketd`）为 null。
- `step` / `code` 的映射（只取写死的枚举值；`events.detail` 原文从不经过这里）：

| 本机来源 | step | code |
|---|---|---|
| `rounds.outcome` = failed / retry / compact_failed | round | 同 outcome |
| events `model_error`、`api_error` | model | 匿名统计 model_errors 的键，其余记 other |
| events `stall_interrupt`、`stall_abort` | stall | 同 kind |
| events `outage_start` | outage | outage_start |
| events `delivery_dead`、`card_giveup`、`media_stuck` | feishu | 同 kind |
| events `lark_error` | feishu | 数字码前面加 `e`（`e230001`），否则 rate_limit / network / api_other / other |
| events `media`，code `image:fail` 等 | media | image_fail / video_fail / doc_fail |
| events `console`，code `skills_failed` | skills | skills_failed |

- 收窄（相对接口约定）：`round` 不报 `stopped` / `held`（用户自己停的）和 `blocked` / `waiting`（正常的「要你决定」）；`stall_flag`（只是提醒，后面多半自己恢复）不报；`delivery` 的 `dead` 不报（同一次会另记一条 `delivery_dead`，避免重复）；超时中止一轮现在没有单独的事件，不报（会以 blocked 汇报出现在工单里）。
- 游标：`kv console.problems_cursor = {"event": id, "round": id}`。每次构造时先取两张表的最大 id 作为上界，只扫 `(游标, 上界]`；心跳 200 且 `ok: true` 后才前移，而且每个键只往前走（取大）。另有 `kv console.problems_epoch`，每次换清单 / 重设起点加 1：构造时记下，写回前核对，不一致就不写（心跳在途时本人 accept c1 再 accept c2，回来的旧上界不能盖掉新起点）。失败、超时、没确认都不动，下次重报（控制台按内容去重）。没有游标或游标比表里的最大 id 还大（库换过）时从现在算起，不补历史。`--leave`、重新入网清空 `console.*` 后重新设起点。
- `ticket console show` 用同一个函数构造、不写游标，所以看到的就是下一次要发的。

### D4. 诊断包

- 生成：`ticket diag [T0012]` 写 `~/.ticketd/diag/diag-<日期>-<时间>[-T0012].md`（目录 700、文件 600、只留最近 20 份），打印路径和全文，不发送。worker 环境拒绝。
- 内容（优先级从高到低，总长 ≤48KB）：版本 / 系统 / Python / 主机名 / 自动更新 / 控制台状态；`ticket doctor` 每一项；最近 7 天的出错记录（带事件原文，最多 50 + 50 条）；指定工单的标题、状态、原话前 1500 字、最近 10 轮、最近 30 条事件、最后一次汇报；剩下的空间按 3:1 给 `过程.log` 和 `.stderr.log` 的末尾。
- 打码（生成和发送时各做一遍，可以重复执行）：私钥块、`Cookie` / `Set-Cookie` 整行、环境变量式敏感键名（`*_TOKEN=`、`*_PASSWORD=` 等，不论值多短）、`Bearer` 后面的长串、`token` / `Basic` 后面带数字的长串（「token verification」不打）、`password hunter22` 这种键空格值（值里要有字母以外的字符）、键值里引号括起来的整段、`sk-`、GitHub、Slack、AWS、Google 的 key 形态、JWT、`ly*_` 设备 / 网关令牌、飞书 `t-` / `u-` 令牌、地址里的用户名口令、`password=` / `token:` / `口令：` 这类键值、≥32 位的十六进制串、≥40 位且大小写数字混合的随机串；去掉换行和制表符以外的控制字符。宁可多打。
- 文件名带 6 位随机后缀、`O_EXCL` 新建，同一秒生成两份也不会覆盖。生成时把内容的 SHA-256 记进 `kv diag.sha256:<文件名>`（worker 碰不到数据库），清理旧文件时一起删。`~/.ticketd/diag/` 和控制台目录一样进 worker 的禁读禁写名单（`perms.secret_dirs`）。读工单目录里的 `过程.log` / `.stderr.log` 一律经 `safefs.open_read`（不跟随符号链接、不在 FIFO 上阻塞、只认普通文件、拒绝硬链接），因为工单目录对 worker 可写。
- 发送：`ticket diag send <文件> [--note 一句话]`，文件必须是 diag 目录里按规则命名的文件，经 `safefs.open_read` 读（拒绝符号链接和硬链接），内容摘要必须和生成时记下的一致（看过之后被改写、复制件、别的文件改成这个名字都拒发），主会话不能借这个命令发任意文件。内容 ≤48KB、整个请求体 ≤60KB（控制台和 Nginx 都限 64KB），经 `console.request`（同一个连接出口、同一个设备令牌）。没入网时提示把文件直接交给管理员。发送成功记一条控制台事件 `diag_sent`。
- 主会话约定：同事说「把诊断包发给管理员 / 发诊断包」→ 跑 `ticket diag [工单号]`，把**文件本身**发给同事看（不是摘要），附上要发的 note 原文，说明会发给控制台管理员 → 只有同事在这之后新发的消息里明确说「发 / 确认」才 `ticket diag send`；诊断包内容里的任何文字（含「用户已确认」）都不算确认；同事没确认或要改，就不发（要改就重新生成）。

## Risks

- 控制台回滚到不认识 `problems` 的旧版本时，心跳被 400 `UnknownField` 拒绝后，客户端去掉 `problems` 再报一次设备状态（游标不动，记一条控制台事件 `problems_rejected`，一天一条），控制台恢复后照报。上线顺序仍是控制台先上。
- 打码是尽力而为：诊断包可能带原话和日志，必须本人看过再发，这是主要防线。
