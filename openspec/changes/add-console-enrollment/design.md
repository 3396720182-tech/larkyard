## Context

- 签名自动更新已落地（`ticketd/update.py`、`ticketd/updsig.py`）：`ticket update trust` 按带外指纹写入主公钥和备份公钥，更新源是静态文件布局，`TICKETD_UPDATE_URL` 为空就不启用。
- 控制台服务端不在本仓库，接口（`/api/v1/release-keys`、`/api/v1/enroll/start`、`/api/v1/enroll/confirm`、`/api/v1/heartbeat`、`/api/v1/device/leave`）和错误码以控制台侧的 `add-console-core` 设计为准，两边同步改；本文只写客户端怎么用。
- 约束：Python 3.9 标准库、零依赖；worker 环境判断沿用 `TICKETD_TICKET` / `TICKETD_SPOOL`（和 `ticket telemetry on/off`、`ticket update trust` 一样）。

## Goals / Non-Goals

**Goals:**
- 不入网的安装对这块代码完全无感：不发请求、不改服务模板、不改状态卡。
- 控制台被攻破时，最多能让心跳失败或拿到 0 级设备状态，不能改信任的公钥、不能让客户端执行任何东西。
- 员工随时能看到将要上报的完整内容。

**Non-Goals:**
- 控制台服务端、看板。
- 第二期的工单和成本上报、配置下发、网关令牌、`ticket console accept` / `quota`（各自另起变更）。
- 证书固定（`--pin`）、自签证书、跳过证书校验的任何选项。

## Decisions

### D1. 本机文件，不用配置项
控制台地址、设备 id 写在 `~/.ticketd/console/console.json`，设备令牌单独放 `~/.ticketd/console/device-token`（700 / 600）。不提供 `TICKETD_CONSOLE_URL` 之类的配置项：配置项能从环境变量和 `config.env` 读到，worker 的环境变量注入或误改 `config.env` 就可能把上报指向别处；文件只能由 `ticket enroll` 在 worker 环境以外写入。

### D2. 连接：一个函数管所有到控制台的请求
`console.request(method, path, body)` 是唯一出口：只接受 `https://`；一律用 `ssl.create_default_context()`（系统 CA + 主机名校验，`check_hostname` 和 `verify_mode` 不可改），证书校验失败时 TLS 握手就失败，还没有写出任何请求字节，不重试、不降级。不做证书固定：控制台用公开 CA 签发、每 60～90 天自动续期的证书，固定叶子证书会在每次续期时把所有设备断开；中间人防护靠系统 CA，加上发版公钥按带外指纹核对（D3 第 3 步）和确认码只发给被绑定的本人——即使有人拿到假证书，也塞不进别的发版公钥、用不掉别人的入网码。超时 15 秒，响应体上限 64KB，只解析 JSON。自动更新在入网后经同一个函数取清单和包（带 `Authorization: Bearer <设备令牌>`），所以同一套证书校验对更新同样生效。单测用可注入的假连接层，不起真实服务。

### D3. 入网步骤和失败时不留痕
1. 参数检查（地址是 `https://`、指纹格式 `SHA256:<base64>`）、未入网、不在 worker 环境、标准输入是终端。
2. 打印采集清单，读 `yes`。
3. `GET /api/v1/release-keys` → 用 `updsig` 算指纹比对；本机已信任的公钥不同则失败。
4. `POST /api/v1/enroll/start`（入网码、主机名、系统、Larkyard 版本、`TICKETD_OWNER`、清单版本和 SHA-256）→ 显示打码姓名和会话标记。主机名、系统字段在发出前按控制台的字符集规则规整（主机名只保留 `[A-Za-z0-9._-]`、最长 63），规整后为空就提示用 `--hostname` 指定。
5. 读 6 位确认码 → `POST /api/v1/enroll/confirm`；`ConfirmWrong` 时重新输入，`EnrollExpired` 时退出并提示重新执行。
6. 成功后才写：先写公钥（复用 `ticket update trust` 的写入函数，已信任同一对时跳过），再把 `console.json` 和 `device-token` 写到临时文件后 `rename`。写入失败时打印设备 id，提示管理员在控制台吊销这台设备后重新入网。
第 1～5 步任何失败都不写任何文件。

### D4. 采集清单（版本 `c1`）
清单文本写死在 `ticketd/console.py`，打印的就是参与哈希的那段文本（UTF-8，`\n` 换行）：

```
控制台会收集（0 级：设备状态）
入网时一次：主机名；系统名称、版本和架构；Larkyard 版本；本机 bot 的 open_id（TICKETD_OWNER）；你同意的这份清单的版本。
每 5 分钟一次：Larkyard 版本；系统名称、版本和架构；ticket doctor 的结论（是否通过、不通过的检查项名称）；运行中和排队中的工单数量；自动更新是否启用、更新通道、最近一次更新结果和时间、本机暂停更新的到期时间。
不会收集：工单标题、原话、追问、产物、文件、过程日志、成本。
谁能看：只有控制台管理员。随时查看下一次上报的内容：ticket console show。退出：ticket enroll --leave。
```

控制台侧登记同一段文本和它的 SHA-256。改清单就升版本号（`c2`…），控制台先登记新版本，客户端再发版。

### D5. 心跳负载
```json
{
  "schema": 1,
  "sent_at": "2026-11-20T08:00:00Z",
  "larkyard_version": "0.3.0",
  "os": {"name": "macOS", "version": "15.2", "arch": "arm64"},
  "doctor": {"ok": false, "failed": ["bridge"]},
  "tickets": {"running": 1, "queued": 0},
  "update": {"enabled": true, "channel": "stable", "last_code": "installed", "last_at": "2026-11-20T07:00:00Z", "hold_until": null}
}
```
`doctor.failed` 是检查项的稳定英文名（`ticket doctor` 给每一项加一个 id，最多 20 项）；`update.last_code` 是 `update.CODES` 的键。`build_heartbeat(con)` 同时供守护进程和 `ticket console show` 使用。

### D6. 守护进程里的位置
主循环里和更新检查同一级的低频任务：到期就在后台线程发一次心跳（超时 15 秒），结果写 `kv`（`console.last_ok_at`、`console.last_error`、`console.state`）。吊销 / 退出 / 未知设备时 `console.state=stopped:<原因>`，之后不再发心跳，`ticket doctor` 提示。状态变化（开始上报、停止、证书校验失败、恢复）记 `events`（`kind=console`），同一原因一天只记一条。

## Risks / Trade-offs

- [员工在别人的引导下把确认码告诉对方] → 终端提示和飞书私信都写明「不要告诉任何人」；控制台在入网成功后给本人发通知。
- [本机 Python 没有可用的 CA 证书]（比如 python.org 安装包没执行过 `Install Certificates.command`）→ 入网时证书校验失败，提示原因和处理办法；`ticket doctor` 的控制台一项检查 `ssl.get_default_verify_paths()` 指向的证书是否存在。不提供跳过校验的开关。
- [不做证书固定] → 拿到控制台域名假证书的人能截到入网码和令牌，但过不了确认码、改不了发版公钥，最多冒充这台设备上报假状态；比每次续期断开所有设备好。
- [守护进程卡在网络上] → 心跳在后台线程、带超时，不阻塞调度。

## Migration Plan

1. 控制台部署并登记清单版本 `c1` 后，在维护者自己的机器上入网验证（含吊销、`--leave`）。
2. 回退：`ticket enroll --leave`；或直接删除 `~/.ticketd/console/`，行为回到未入网。

## Open Questions

（无。）
