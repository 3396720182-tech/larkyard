## Context

- lark-cli v1.0.86（发版包自带）支持设备码授权：`auth login --no-wait --json --scope "<…>"` 立即返回 `{verification_url, device_code, expires_in}`；之后 `auth login --device-code <码> --json` 轮询直到用户同意或过期（内部最多 600 秒），成功后把用户 token 存进钥匙串。飞书设备码有效期由服务端给（没给时 lark-cli 按 240 秒）。
- 本地检查（不联网）：`auth status --json` 的 `identities.user.status`：`ready` / `needs_refresh`（可用，下次调用自动续期）/ `missing`（没登录、钥匙串里没有、refresh token 过期）/ `not_configured`；`auth check --scope "<…>" --json`：`{ok, granted, missing}` 或 `{ok:false, error:"not_logged_in"|"no_token"}`。
- 报错 JSON（`error.type` / `error.subtype`）：`authentication/*`（token_missing、token_expired、refresh_token_expired、refresh_token_revoked…；`refresh_server_error` 是服务端一时出错）；`authorization/missing_scope|user_unauthorized|token_scope_insufficient`（用户授权缺范围）；`authorization/app_scope_not_applied|app_unavailable|app_disabled`（应用本身没开通，用户授权解决不了）。
- 用户 refresh token 默认 7 天，一直在用会自动续；一周不用就失效（「授权后来失效」）。

## Decisions

### D1 权限范围写死

`userauth.SCOPES`：
- 导入文档 `drive +import`：`docs:document.media:upload`、`docs:document:import`
- 覆盖改过的稿子 `docs +update`：`docx:document:write_only`、`docx:document:readonly`
- 视频传云空间 `drive +upload` 和查链接 `drive metas batch_query`：`drive:file:upload`、`drive:drive.metadata:readonly`

不带 wiki 范围（我们不往知识库导入）。授权链接、`ticket setup --user-auth`、安装向导第 ⑤ 步都用这一组，同事不用自己勾。

自检只查缺了就一定失败的三项（`CHECK_SCOPES`：`docs:document.media:upload`、`docs:document:import`、`drive:file:upload`）：覆盖原文档失败时会改为另导入一份，查链接只是上传没回链接时的兜底，缺这两类不该让已经能正常出稿的同事自检变红、收到链接。

### D2 状态（kv `userauth.state`）

`{"state", "at", "missing": [...]}`，state 取：
- `ok`：status 是 ready / needs_refresh，且 `auth check --scope <CHECK_SCOPES>` 没有缺的；
- `missing`：没登录 / 钥匙串里没有；`expired`：登录过但 refresh token 过期；`scope`：缺范围；
- `unknown`：lark-cli 跑不起来、输出看不懂、钥匙串打不开（比如在沙箱里）——不算没授权，不发链接。

用户身份调用（导入 / 上传）成功时不直接改成 ok（缺的可能是另一项权限，直接改会红绿来回跳），只记下时间并让守护进程现查；现查是 ok、且这次成功晚于失败记录时才转回 ok。视频已经传上去、只是查链接失败的，不算没授权。

谁来更新：守护进程（启动后很快一次，之后每 30 分钟，授权完成后马上）；出稿时遇到授权类失败直接记成没授权（`missing` / `expired` / `scope`，按报错子类型）；终端里的 `ticket doctor` 现查一次。`unknown` 不覆盖已知的结论（worker 里跑 doctor 读不到钥匙串，不能把好的结论冲掉）。

### D3 自检项 `user_auth`

`cli.DOCTOR_IDS` 加 `user_auth`，读 D2 的状态：
- ok：`✅ 飞书用户身份已授权（导入文档、传视频用）`
- missing / expired / scope：`❌ 飞书用户身份没授权 / 授权已失效 / 缺权限…：机器人已私信你授权链接，点开同意即可（也可以在终端执行 ticket setup --user-auth）`
- unknown / 还没查过：`⚠️ …`（不算不通过）

心跳的 `doctor.failed` 由此带上 `user_auth`，控制台据此显示「谁还没授权」。字段、清单都不变。

### D4 发链接（守护进程，`userauth.Lane`）

主循环每拍调 `Lane.tick()`，真正的事在一个后台线程里做（同一时间只有一个），主循环不等 lark-cli。线程里按顺序：

1. 有一条**还没过期的链接**（kv `userauth.link` 里有 device_code 和到期时间）：跑 `auth login --device-code <码> --json`（超时 = 剩余有效期 + 30 秒；lark-cli 最多轮询 600 秒，链接有效期按 10 分钟封顶）等用户同意。退出码 0，或输出 `event=authorization_complete`（只同意了部分权限时 lark-cli 存好 token 但退出码是 3）都算成功 → 现查状态（D2）→ 补发文档（D5）→ 私信一句「✅ 授权好了」；失败 / 过期 → 清掉这条链接，状态不变。守护进程停机时结束这个 lark-cli（不留下和新进程抢同一个设备码的轮询），链接留在 kv 里，重启后没过期的接着等。
2. 否则到点（或被要求）就查状态（D2）。
3. 需要发链接时：`auth login --no-wait --json --scope SCOPES` → 私信主人一张卡片（标题「飞书用户身份需要授权」，说明用来导入文档、传视频，`N 分钟内有效`，按钮「去授权」打开 `verification_url`；文字兜底带链接），设备码和到期时间写进 kv。

什么时候「需要发」（D2 状态不是 ok / unknown 时）：
- **装好后的一次**：本机从来没发过（kv `userauth.first_link_at` 为空）就马上发一次。已经装着、一直没授权的同事升级到这一版后也会收到这一次。
- **出稿失败时**：文档导入或视频传云空间遇到授权类失败（D6）时置 `userauth.need_link`；线程看到它且当前没有没过期的链接、离上一次发链接 ≥30 分钟就发。一次汇报里 3 份文档同时失败只发一条。没发出去（主人没配好、申请链接失败）也按发过记时间，30 分钟后再试；主人没配好只告警一次。

链接只私信本机主人（发件箱占位 `@owner`，守护进程按自己读到的 `TICKETD_OWNER` 发）。设备码不写进告警、事件和诊断包。

### D5 授权完成后补发文档

- 授权类失败时，在新表 `auth_backfill(ticket_id, path, reply_to, created_at, PRIMARY KEY(ticket_id, path))` 记下「哪张单的哪份稿子、结果发到哪条消息下」。
- 授权成功后逐条处理（只处理 7 天内的）：按发布时同样的规则重新核对路径（`publish.candidates`：真实路径在工单目录或授权目录里、不是内部文件、普通文件、大小在限内；文件已删就丢掉）；当前版本已经有链接（后来的汇报已导入成功）就只删记录；否则导入（同 `publish._import`，记 `published` / `doc_targets`）。
- 同一张单导入成功的稿子合成一条消息，回复在原来那条结果下面（`reply_to`）：卡片「📄 授权好了，补上 T00xx 没导入的文档」+ 每份一个「打开」按钮，文字兜底带链接。
- 补发时又遇到授权类失败：保留记录、重新置 `need_link`；网络不通：保留记录，等下一次检查；其他失败：删记录、写告警（原文在告警日志里）。这张单这个路径已经有云文档的 Markdown / 文本稿先覆盖原文档（issue #13），不另导入一份。补发后又变成没授权时不发「授权好了」。

### D6 失败原因归类和中文说明（`userauth.classify`）

| 类别 | 判断 | 卡片上写 |
|---|---|---|
| `auth` | `type=authentication`（除 `refresh_server_error`）；`type=authorization` 且 subtype 是 missing_scope / user_unauthorized / token_scope_insufficient；或原文里有 `need_user_authorization` | 飞书用户身份没授权或已失效。机器人已私信你授权链接，点开同意后会自动补发这份文档 |
| `app` | `type=authorization` 且 subtype 是 app_scope_not_applied / app_unavailable / app_disabled | 飞书应用没开通导入文档的权限，请管理员在飞书开放平台给应用开通后再试 |
| `network` | 现有 `_is_transient` | 网络不通或超时，下次汇报时会自动重试 |
| `other` | 其余 | 飞书没能导入（原因：<lark-cli 的 message，≤60 字>）。可以对机器人说「把诊断包发给管理员」 |

`doc_failures.reason` 存这段中文（卡片和文字兜底直接用），原文仍写告警日志。

### D7 同一原因只报一次

- 授权类失败的指标记成 `doc:auth` / `video:auth`（`kind=media`）：不映射成出错记录（`_problem_of` 只认 `:fail`），匿名统计也不算它（只认 ok / fail）。
- 这个原因只通过自检项 `user_auth` 报一次：状态从「通过」变成「没授权」时，控制台收到一次（控制台改成「新出现的不通过项各记一条」），之后每次出稿失败不再刷。授权好了自检转绿，下次再失效才会再报。

### D8 诊断包

`ticket diag` 多一节「飞书用户身份和最近的上传失败」：D2 状态和检查时间、有没有没过期的授权链接（不带设备码）、`auth_backfill` 里待补发几份；最近 7 天 `doc_failures` 10 条（时间、文件名、原因）；告警日志里最近 15 行导入 / 上传 / 授权相关的原文。整段照常打码。

### D9 终端里的授权

`ticket setup --user-auth` 和安装向导第 ⑤ 步改为 `lark-cli auth login --scope "<SCOPES>"`（原来不带范围，靠用户在授权页手选）。完成后守护进程下一次查状态时转绿；授权成功也触发补发（守护进程查到状态从没授权变成 ok 时同样执行 D5）。

## Risks / Trade-offs

- 设备码有效期短（几分钟）：同事没及时点就过期了。过期后不自动重发（避免刷屏），下一次出稿失败时再发（≥30 分钟一次）；卡片上写明有效期和「过期了在终端执行 ticket setup --user-auth 也行」。
- 别人点了链接会把别人的身份授权到这台电脑：链接只私信主人，和终端授权同一风险面。
- 钥匙串在 launchd 下可读（T0106：本机授权后 260 次导入零失败），在 worker 沙箱里不可读：worker 里的 doctor 显示 ⚠️、不改结论。
