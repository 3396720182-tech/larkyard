## Why

T0106 排查：同事的结果卡上「文档导入失败 / 视频上传失败」反复出现，原因是那台电脑没做飞书**用户身份**授权（或授权后来失效）。文档导入、视频传云空间是 ticketd 里仅有的两处用用户身份的地方。

- 安装向导第 ⑤ 步默认跳过，`ticket doctor` 也不查用户身份：装完自检全绿，出稿才失败，同事自己不知道。
- 补救要同事自己开终端执行 `ticket setup --user-auth`、在授权页手选权限；大多数同事没有终端习惯。
- 结果卡上是一行英文 `upload media failed: need_user_authorization (user: ou_…)`，看不懂、也不知道下一步。
- 同一个原因每次汇报都报一次，一张单在控制台刷出 ×2 / ×3；诊断包里也看不到原因原文。

## What Changes

用户和主会话逐条定下的四条（2026-11-27），只做这些：

1. **机器人直接发授权链接**：哪台电脑缺用户授权，守护进程就用 bot 私信这台电脑的主人（`TICKETD_OWNER`）一张卡片，点「去授权」在飞书里同意即可，不用开终端；授权完成后，把这次因为没授权而没导入的文档补发到原来那张单下面。
2. **发链接时机**：装好后自检一发现没授权就马上发一次（不等出稿失败）；授权后来失效的，出稿失败时再发一次。
3. **顺手修**：授权链接预先带好需要的权限范围（同事不用自己勾）；结果卡上的英文报错改成中文并写清下一步；同一原因只报一次（不再一张单刷多条）；诊断包带上失败原因。
4. **控制台直接显示「谁还没授权」**：自检多一项 `user_auth`，随心跳的自检结论上报；控制台首页「需要处理」单独列出（控制台侧见 larkyard-console 的 `show-user-auth`）。

## Non-goals

- 不改入网流程，不加审批，不加新页面以外的管控。
- 不改采集清单（自检不通过的检查项名称本来就在 c1 里）。
- 视频不补发：传云空间失败时本来就退回成 bot 发的视频消息，同事已经收到视频。
- 安装向导第 ⑤ 步仍然可选（只把权限范围带上）；没授权的由守护进程发链接兜住。

## Capabilities

### New Capabilities
- `user-auth-link`：用户身份检查、授权链接、补发文档、失败原因中文化、只报一次、诊断包原因。

### Modified Capabilities
- `console-heartbeat`：`doctor.failed` 可能出现新的检查项 `user_auth`（字段和清单不变）。

## Impact

- 新增 `ticketd/userauth.py`；改 `ticketd/publish.py`、`ticketd/outbox.py`、`ticketd/daemon.py`、`ticketd/cli.py`（doctor）、`ticketd/setup.py`、`ticketd/diag.py`、`ticketd/templates.py`。
- 新表 `auth_backfill`（`publish.SCHEMA`，CREATE IF NOT EXISTS，不用迁移）；`kv` 里 `userauth.*`。
- 匿名统计：因为没授权导致的失败记成 `doc:auth` / `video:auth`，不再算进 `media.doc.fail` / `media.video.fail`（字段不变，统计只认 ok / fail）。
- `docs/spec.md` §5.3（稿子导入）加「调整」，CHANGELOG。
- 控制台侧（私有仓库）同时改：先上控制台、后发客户端都行（控制台不认识的检查项照样能显示原名）。
