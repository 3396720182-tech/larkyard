# Changelog

## 0.3.13 — 2026-11-28

- 工单快速通道：引用工单卡片的回复，以及「停 T0005」「先别动 T0005」这类指令，由 bridge 直接交给 ticketd，不再先经过主会话（省一次模型调用，响应也更快）。整条消息恰好是「停 / 先别动 / 继续 / 执行 / 换新会话重来」等控制词时执行对应操作，其余当追问转给工单；认不出来的照旧交给主会话。bridge 更新到 fork 提交 804f150。
- 较早接管的机器上，bridge 服务里缺 `LARKYARD_TICKET_BIN`（快速通道和会议待办派单要用）：下一次重启 bridge（含每天的定时重启）时自动补上并重新加载服务，原有的 PATH 和日志位置不变。

## 0.3.12 — 2026-11-28

- 控制台出错记录：一次故障只记一条（T0109）。模型接口出错后，同一张单的自动重试（故障等待里每 15 分钟一次）以前每次都记两条，一次 4 小时的故障刷出二十多条；现在从第一条模型接口错误起、到又交上汇报为止只报第一条，故障等待恢复时补一条「已恢复」（控制台显示持续多久）。本机记录和匿名统计不变。需要控制台同时升级才显示「持续多久」（旧控制台显示原代号 recovered）。
- 心跳里的自检不再带「控制台」这一项（T0109）：电脑睡眠、断网超过 30 分钟醒来后，第一次心跳会自己报「控制台不通过」，是误报。本机 `ticket doctor` 照常显示。
- Windows：主会话约定认得 `C:/Users/…` 这种路径了（T0109）。以前 Windows 电脑的自检一直报「主会话约定和模板不一致」，也不会自动换新版主会话约定；现在和 Mac 一样，没手改过的旧版在守护进程启动时自动换成新版（先备份）。

## 0.3.11 — 2026-11-27

- 缺飞书用户身份授权时，机器人直接私信授权链接（T0107）：结果里的稿子导入成云文档、视频传云空间要用用户身份，以前没授权的同事每次出稿都失败、卡片上是一行英文，自己也不知道。现在守护进程每 30 分钟查一次，一发现没授权就用 bot 私信你一张「去授权」卡片，点开在飞书里同意就好，不用开终端，需要的权限已经选好；授权完成后，之前因为没授权没导入的文档自动补发到原来那张单下面。装好后（或升级后）发现没授权马上发一次；之后授权失效的，出稿失败时再发（30 分钟最多一次）。
- `ticket doctor` 多一项「飞书用户身份」，随心跳报给控制台，管理员能直接看到谁还没授权。
- 结果卡上稿子没能导入的原因改成中文并写清下一步；没授权这个原因不再每次汇报都算一条出错记录（只经自检报一次）。诊断包多一节「飞书用户身份和最近的上传失败」，带失败原文（设备码不带）。
- `ticket setup --user-auth` 授权时带上需要的权限范围，不用在授权页手选。规格见 `docs/spec.md` §5.3「缺飞书用户身份授权时机器人发链接」。

## 0.3.10 — 2026-11-27

- 控制台看得到「大家拿机器人主要做什么、能力用得怎么样、谁连续失败或卡住了」：采集清单升到 `c3`，多报一份用量统计——今天和昨天各大类（生图生视频、找素材、写文档、查资料、做表格、写代码、其他）的工单数和做成几单、各项能力（控制台下发的技能）用了几次和成没成、最近连续没做成几单、正在跑的单最久几分钟没动静。只有数字和写死的代号，不报标题、原话、内容。新入网直接用 c3。
- 大类在本机定：主会话派单时用 `ticket new --cat` 选一个（主会话约定里写了怎么选），没选时按标题和原话的关键词判断。能力用量按过程日志里调用技能脚本的次数和结果算，技能包不用改。
- 已入网、清单是 c2 的机器升级后自动换成 c3（c3 只比 c2 多了上面这份用量统计，算小迭代，不再逐人确认），静默更新、不发私信；想退回在终端里执行 `ticket console accept c2`，退回后不会再被自动升。c1 的机器不动。需要控制台先升级（旧控制台不认识 c3，没升级前留在 c2、下一次心跳再试）。规格见 `docs/spec.md` §12「用量统计和 c2 → c3 自动升级」。

## 0.3.9 — 2026-11-27

- 生图触发体检（T0098）：同事想用生图生视频却用不了时，看到的是一句能照着做的话，不再是笼统的「没开通」。
  - 网关令牌领失败后 5、10、15、30 分钟各再领一次，之后每小时一次（以前一律一小时后再领，刚入网时可能头一个小时都用不了）。
  - 工单里没拿到网关子令牌时，worker 环境多一个 `LARKYARD_GATEWAY_STATE`（`not_enrolled` / `stopped` / `pending`），gen-image 据此说「找管理员入网」「10 分钟后再试」等，不再请同事去终端查。
  - worker 约定：本机没装上生图技能时报 blocked、说清下一步，不自己找 key 或换接口。

## 0.3.8 — 2026-11-26

- 主会话约定跟着版本走：守护进程启动时，bridge 工作目录的 `CLAUDE.md` 如果是没改过的旧版模板（逐字等于发过的某一版），先备份成 `CLAUDE.md.bak-<时间>` 再换成本版；手改过的不动，`ticket doctor` 照旧提示。以前模板只在装机时写一次，之后的新规则到不了已经装好的电脑。规格见 `docs/spec.md` §14「主会话约定自动同步」。
- 主会话约定：生图、生视频一律派单，不在主会话里先试（主会话拿不到网关子令牌，试了必然报「没开通」）；不说「没开通 / 找管理员」，不找 key、不试别的网关。

## 0.3.7 — 2026-11-23

- 生图默认走控制台网关：入网的机器在心跳成功后自动凭设备令牌领一枚网关令牌，存在 worker 读不到的 `~/.ticketd/console/`；worker 每轮只拿到一枚只对这一单有效、这一轮结束就过期的子令牌（环境变量 `LARKYARD_GATEWAY_TOKEN` / `LARKYARD_GATEWAY_URL`）。控制台下发的 gen-image 看到它就走网关：在导演组用导演组额度、用量记到人；不在导演组才用自己配的火山 key；都没有回「还没开通生图，找管理员」。被吊销后自动重新领。`ticket console status` 多一行「生图网关」。需要控制台先升级（旧控制台领不到，本机不受影响）。规格见 `docs/spec.md` §12「生图网关」。
- 控制台下发技能的上限放大到合计 2MB（响应 4MB），给带整套脚本的技能留余量。

## 0.3.6 — 2026-11-23

- 控制台看得到「谁、哪台电脑、哪一步坏了」：采集清单升到 `c2`，入网的机器在心跳里多报一份上次以来的出错记录（时间、工单号、出错环节、错误归类码，都是写死的代号，不含报错原文、标题、原话）。新入网直接用 c2；已入网的机器照旧只报设备状态，本人执行 `ticket console accept` 看过新清单、输入 yes 后才开始报，`ticket console accept c1` 可以改回去。`ticket console status` / `show` 会提示。需要控制台先升级（旧控制台不认识 c2）。
- 诊断包：`ticket diag [工单号]` 在本机生成一份（版本、自检、最近的报错原文、工单的原话 / 汇报 / 过程日志末尾，常见密钥打码，≤48KB），打印全文、不发送；本人看过后 `ticket diag send <文件> [--note 一句话]` 发给控制台管理员。主会话约定：同事说「把诊断包发给管理员」时先生成给他看，明确确认后才发。规格见 `docs/spec.md` §12「出错记录和诊断包」。

## 0.3.5 — 2026-11-22

- 控制台下发技能：入网的机器上，控制台可以按人给 worker 下发技能和它要用的凭据（团队内部工具开给指定的人用）。随心跳检查（摘要变了才领），装在 `~/.ticketd/skills/`，只给 Claude worker 以 `--plugin-dir` 加载，用户自己的 Claude Code 会话不加载；worker 能读不能写。控制台关掉开通、设备被吊销或退出、`ticket enroll --leave` 时本机删掉。`ticket console status` 列出装着的技能。旧版控制台不受影响。规格见 `docs/spec.md` §12「下发技能」。

## 0.3.4 — 2026-11-22

- 安全：worker 读不到、写不了 ticketd 的控制台目录 `~/.ticketd/console/`（设备令牌，以后的网关令牌也放这里）。之前 worker 的沙箱只限制写，被提示词注入的 worker 能读走设备令牌、拿去领网关令牌花额度。Claude worker 的设置里同时加了沙箱禁读禁写和文件工具的 deny 规则，授权目录再宽也不放开；本机入网后不再起 Codex worker（它的沙箱还禁不了读），提示改用 Claude 或先 `ticket enroll --leave`。worker 里的 `ticket doctor` / `ticket console` 改为说明「看不到」。`scripts/selftest.py` 新增 `secrets` 一项实测。规格见 `docs/spec.md` §6「凭据目录」、§12「凭据不进 worker」。

## 0.3.3 — 2026-11-21

- 打包的 bridge 换到 fork 上带会议纪要和会前资料的提交（`vendor/bridge.lock.json`）：
  - 会后纪要改成卡片：讨论要点、结论、待办（负责人、截止，记录里没有的写「未明确」），每条待办一个「派成工单」按钮，管理员点了就在当前会话开一张 Larkyard 工单（同一条只派一次，失败可以再点）。纪要和待办存档在 `<bridge profile>/meeting-data/archive/`，不存字幕和弹幕原文。卡片发不出去时退回原来的富文本。
  - 会前资料：管理员私聊 `/meeting doc <飞书文档链接> [会议号]` 或「会前资料 <链接>」把文档绑到会议（进行中的、指定会议号的、或者在这个会话里让 bot 入会的下一场，12 小时内有效），会中共享的飞书文档自动绑定。用 bot 身份读、按版本存快照；被 @ 时按问题摘相关段落，回答标〔《标题》r版本 §段〕，每次回答记下给了哪几段、实际引用了哪几段。资料不按提问人权限过滤（管理员绑定即同意在这场会里引用）。会议结束后释放绑定，快照保留。
  - bridge 服务环境新增 `LARKYARD_TICKET_BIN`（「派成工单」用）；已装好的机器重新执行一次 `ticket setup` 生效。
- 「会中 @ 写在任意位置」「忽略 bot 自己的弹幕」「轮询拿到 122001 按散会处理」在 0.3.0 已经带上，这次没有改动。
- 工单卡片精简：结果卡、需要你决定的卡和提醒只显示一句结论、需要你做的那一步和文档按钮。worker 写长了或带了代码 / 命令 / 路径时，卡片只露第一句，其余收进默认收起的「详情」（有稿子时提示「完整内容见文档」）；沙箱外执行申请只露一句「要做什么」（脚本开头的注释），完整脚本默认收起。worker 提示词规定 summary / ask 各最多 2 句、技术细节写进 `结果.md`；工作区约定要求主会话回复同样只写结论和下一步。规格见 `docs/spec.md` §5.3、§5.5 和 `openspec/changes/concise-ticket-cards/`。

## 0.3.2 — 2026-11-21

- 离线升级包放在任何路径都能用：
  - `install.sh` 新增 `--from-dir <目录>`：本地发布目录的整条路径按 RFC 3986 转义成 `file://` 地址，空格、中文、`# % ?` 都不会被 curl 误解。原来的 `--from file://…` 照旧，只转空格。
  - `install.sh` 里的变量统一写成 `${VAR}`：变量名后面紧跟中文时，bash 会把中文的字节读成变量名的一部分。
  - 新增 `scripts/upgrade-offline.sh`，作为升级包的入口 `upgrade.sh`：不再拒绝带空格或中文的路径，把同目录的 `feed/` 交给 `install.sh --from-dir`；没有参数时也能正确转发（macOS 的 `/bin/sh` 是 bash 3.2，`set -u` 下 `"${@}"` 会报错）。
  - `release.py bundle --feed <更新源> --out <目录>`：从更新源里一个通道的当前版本做出离线升级包和 zip。打包前用 feed 里的主公钥验签，核对包的摘要、`install.sh` 内置的指纹和发布地址；写进升级包的就是核对过的字节；通道写进 `feed/CHANNEL`，由 `upgrade.sh` 带上。
  - `install.sh` 里的内联 Python 一律 `-I`：在「下载」里执行时，那里的 `pathlib.py` 之类顶替不了标准库。
- `ticket migrate`：
  - 旧代码目录本身带空格也认（含空白的启动参数只要是存在的文件就认，`sh -c "…"` 包装照旧不认）；
  - 只读打开数据库时用转义过的 `file:` 地址，路径里有 `# ?` 也对。

## 0.3.1 — 2026-11-21

- 升级已经在跑的旧安装（`openspec/changes/migrate-existing-install/`，规格见 `docs/spec.md` §14）：新命令 `ticket migrate [--dry-run | --undo]` 和 `install.sh --upgrade-existing [--dry-run]`，把早期版本（自定义服务名、主用户和工单根目录写在代码里）或从 clone 装的 Larkyard 一条命令转成受管安装 + 打包的 bridge。按启动命令认出在跑的 ticketd 服务；用旧代码自己算出它实际在用的主用户和工单根目录，和新版本默认值不同才写进 `config.env`（服务环境里的其他 `TICKETD_*` 也搬过去）；停旧服务、等数据库锁放开、拷数据库快照、服务统一成 `dev.larkyard.ticketd` 经启动器运行、主会话约定里的 `ticket` 路径改到 `current`、等新守护进程的心跳，任何一步失败自动退回；bridge 是 Homebrew / npm 装的就接着 `ticket bridge adopt`（这一步失败只退回 bridge）。`--dry-run` 只读，不打开数据库读写，并打出旧代码目录的版本、最后一次提交和本地改动（`git log -1`、`git status -s`；有本地改动时提醒迁移后不含这些改动）；动手和 `--undo` 在 bridge 驱动的会话里拒绝；有正在运行的工单时不动手。`--undo` 按备份恢复服务文件、配置、主会话约定和 bridge。
- `ticket setup` 发现直接跑旧代码的 ticketd 服务（任何服务名）时拒绝并提示 `ticket migrate`：以前服务名不是 `dev.larkyard.ticketd` 的机器会被装出第二个服务，两个服务抢同一个数据库锁，写死在旧代码里的主用户也不会搬进 `config.env`。

## 0.3.0 — 2026-11-21

- 一行安装、bridge 随包发布（`openspec/changes/vendor-bridge-one-line-install/`，规格见 `docs/spec.md` §13）：
  - `curl -fsSL <发布地址>/install.sh | sh`：按 `install.sh` 里写死的两把发版公钥指纹核对公钥、验签通道清单、核对包，装成受管安装（第一天就能自动更新），下载 Node 和 lark-cli 官方包到 `~/.ticketd`（按 `vendor/runtime.json` 核对 SHA-256），然后进入新的 `ticket setup` 向导：扫码建飞书应用、工作区、bridge / ticketd 服务和每天 04:07 的 bridge 重启窗口、用 bot 身份查应用所有者自动认主人（不对就发 6 位码）、可选的用户身份授权、自检。内部员工的命令多带 `--console <地址> --enroll <入网码>`，装完接着入网，不用手抄指纹。原来「先 npm 装 bridge、clone、install.sh、抄 open_id、setup-workspace」的步骤不再需要（从 clone 安装仍可用）。
  - lark-channel-bridge 打包进发版包的 `vendor/bridge/`：来自我们 fork 的固定提交（`vendor/bridge.lock.json`），原先只打在 bridge 编译产物上的补丁（会中授权、会中提醒、会议号提取、附件批处理、终态折叠、流式宽限、模型选项、会中 @ 任意位置、忽略自己的弹幕、轮询发现散会）已移植进 fork 源码并有单测。发版工具新增 `scripts/build_bridge.py`、`release.py release --bridge`（核对提交和内容摘要）、`release.py publish-installer`；清单格式不变。附第三方许可证声明。
  - bridge 和 ticketd 服务不再写死 nvm 下的 Node 路径；`scripts/install.sh` 在有 `~/.ticketd/runtime/node`、`~/.ticketd/bin/lark-cli` 时优先用它们（没有时生成的文件不变）。
  - Larkyard 自动更新只重启 ticketd，不打断主会话；新版本带的 bridge 由每天 04:07 的 `ticket bridge restart --if-stale` 换上（版本没变不重启），重启后 90 秒内没连上飞书就切回原来的 bridge 并私信你。清理旧版本时保留 bridge 在用的那个。
  - 新命令 `ticket setup [--profile] [--workspace] [--app-id] [--user-auth]`、`ticket bridge status | restart [--if-stale] | adopt [--dry-run | --undo]`；`ticket doctor` 增加 bridge 一项。`ticket bridge adopt` 把分开安装的 bridge 迁移到打包的 bridge（服务名、profile、会话、凭据不变，改前备份，`--undo` 逐字节恢复，迁移后没连上自动退回）。
  - 会重启 bridge 的命令在 bridge 驱动的会话里（环境有 `LARK_CHANNEL=1`）一律拒绝，要在自己的终端里执行；工作区约定里也写明主会话不要执行它们。
  - 修复：macOS 的 `/bin/sh` 在 UTF-8 环境里把紧跟变量名的中文当成变量名的一部分，`uninstall.sh` 的结束提示、`install.sh` 的几条报错会报 unbound variable；脚本里改为 `${变量}`，并加了检查。
- 接入控制台（默认关闭，仓库里没有控制台地址，也没有配置项能设置它）：本人在终端执行 `ticket enroll <地址> <入网码> --release-key SHA256:… --backup-key SHA256:…`，看过采集清单 `c1` 输入 `yes`、按带外指纹核对控制台上的两把发版公钥、输入飞书私信里的 6 位确认码后入网；之后守护进程每 5 分钟上报一次设备状态（版本、系统、`ticket doctor` 结论、运行中 / 排队中的工单数、自动更新状态，不含任何工单内容和成本）。只接受 `https://`、按系统 CA 校验证书，校验失败不发出任何请求内容，没有跳过校验的选项。新命令 `ticket console show`（下一次上报的完整内容）、`ticket console status`、`ticket enroll --leave`；`ticket doctor` 增加控制台一项，每项检查有稳定的英文 id；入网的机器在卡片上多一行内部版标识；没配 `TICKETD_UPDATE_URL` 时自动更新的更新源是控制台。被吊销后停止上报，派单不受影响。规格见 `docs/spec.md` §12 和 `openspec/changes/add-console-enrollment/`。
- 签名自动更新（默认关闭，没有默认更新地址）：配置 `TICKETD_UPDATE_URL`、用 `ticket update trust` 按带外指纹信任主公钥和离线备份公钥、`ticket update adopt` 转成受管安装后，守护进程每小时检查一次更新源，只装签名能被主公钥验过、SHA-256 一致、通道一致、序号比已接受过的大、版本更高（或在有效期内的回滚清单）的包，空闲时安装；新版本试用期（默认 10 分钟）内自检不过，由版本目录外的启动器自动切回上一个版本（新版本的 `bin/ticketd` 有语法错误也能切回），私信你一次并且不再自动装那个版本。更新源发布的预签暂停标记、本机 `ticket update hold` 都能暂停安装。新命令 `ticket update status | check | trust | hold | resume | rollback | adopt [--undo]`，`ticket doctor` 增加更新一项；新发版工具 `scripts/release.py`（发布者本机打包、签三通道清单、回滚清单、暂停标记、换钥清单）。`adopt` 把服务改为「解释器 + `~/.ticketd/app/launcher.py`」，并把 bridge 工作区 `CLAUDE.md` 里的 ticket 路径改为 `~/.ticketd/app/current/bin/ticket`，`adopt --undo` 全部改回；不执行 `adopt` 时 `install.sh` 生成的 plist / unit 和以前逐字节相同。新配置项 `TICKETD_UPDATE_URL`、`TICKETD_UPDATE_CHANNEL`、`TICKETD_UPDATE_EVERY`、`TICKETD_UPDATE_PROBATION`、`TICKETD_UPDATE_STALE_DAYS`。规格见 `docs/spec.md` §11 和 `openspec/changes/add-signed-self-update/`。
- 修复自动更新（外部评测 P1）：版本已经切换、记安装事件时出错，守护进程会继续跑旧代码、恢复接单，也不再请求重启，CLI 和守护进程分别跑新旧两份代码。现在切换成功之后出错也照样重启；守护进程每一拍发现「在试用期、当前运行的不是 `current` 指向的版本」时，停止接新活、空闲后自动重启，新版本的试用期从真正启动时算起。
- 匿名使用统计（默认关闭）：`ticket telemetry status | show | on | off`，开启后每天发一份前一天的汇总（只有计数、区间和归类后的错误码，不发任何内容），`show` 只打印不发送，发过的副本留在 `~/.ticketd/telemetry/sent/`；`DO_NOT_TRACK=1` 或 `TICKETD_TELEMETRY=0` 强制关闭，worker 不能开关。字段表和隐私说明见 `docs/telemetry.md`；后端（Cloudflare Worker + D1）在 `server/telemetry/`，还没部署，默认上报地址为空，开启后只在本机生成报告。报告的生成（要探 Claude Code / Codex 版本）和发送都在后台线程里，不拖住主循环；公开汇总的小样本保护按每个桶的贡献机器数算（不到 5 台机器的桶不单独公开，也不能用总量减出来）；关闭统计时正在生成或发送的报告不会写回、不会在重新开启后带着旧 ID 发出；`ticket telemetry off` 会等正在进行的那次发送结束，返回之后不再发出任何请求。`ticket doctor` 显示统计状态，`install.sh` 结束时提示一次。新配置项 `TICKETD_TELEMETRY`、`TICKETD_TELEMETRY_URL`；`events` 表新增 `code`、`seconds`、`size` 列（模型接口错误归类、飞书错误码、结果的投递方式、媒体上传耗时和大小），老库自动迁移。
- 修复：暂停或停止信号已经发出、压缩进程还没收尾时就点了「继续」，这次压缩仍会被当成普通失败、清零上下文规模，放开后跳过压缩直接续做；普通轮在同样时序下会被记成「未交汇报就退出」。现在发信号时记下打断原因，收尾按这个记录处理。
- 修复工单恢复语义（外部评测）：worker 没能启动时，这一轮的追问不再被当成已投递，回复「继续」后会原样交给 worker；故障等待到期（如压缩会话时一直余额不足）后停止自动重试，等你回复「继续」，原追问保留；暂停或停止打断会话压缩后，放开时会重新压缩，不再直接拿大上下文续做。成本对账按会话分开计算，换会话后新会话首轮比旧会话累计值大时不再少算。`tickets` 表新增 `auto_halted` 列，老库自动迁移。
- 会话压缩遇到临时故障或余额不足时，退避重试压缩 / 进入故障等待，不再拿着大上下文直接续做；确定性失败才照常续做，并在结果卡上提示会话没能压缩。`ticket stats` 增加上下文和自动压缩的统计，有 Codex 单时按执行器分开列成本；`ticket show` 显示上下文规模和压缩次数。
- 自动压缩会话：长单的上一轮上下文超过 `TICKETD_COMPACT_TOKENS`（默认 15 万 token）时，下一轮开始前先用 Claude Code 的 `/compact`（默认 sonnet）压缩会话，并要求保留决定、授权、外部动作和进度。实测 T0005 的真实会话 91 万 → 2.4 万 token，关键信息全部保住。每轮的上下文规模记进轮次表。新配置项 `TICKETD_COMPACT_TOKENS`、`TICKETD_COMPACT_MODEL`。
- 沙箱外执行的脚本不再被守护进程重启打断（macOS）：停服务时留着继续跑，重启后重新认领，跑完的按退出码照常交回 worker。
- 成本对账：`ticket doctor` 和端到端自检会核对每张单实时记的成本和从原始记录重算的是否一致。
- 系统私信的收件人改由守护进程发送时按它自己的配置决定；`ticket doctor --dm` 让守护进程真发一条测试私信并报告结果。`ticket doctor` 还会检查 bridge 工作目录里的主会话约定是否落后于模板（新配置项 `TICKETD_WORKSPACE`）。`setup-workspace.sh` 不再把示例文件拷成 `CLAUDE.local.md`。
- `ticket stats` 和每周概况显示主会话的当前累计成本，超过阈值时建议在那个聊天里发 `/new`。
- 修复：私信主用户时遇到「open_id 不属于本应用」（99992361）会静默重试一整天；现在立即放弃并写明多半是 `config.env` 里的 `TICKETD_OWNER` 不对。新命令 `ticket doctor` 检查当前环境和守护进程看到的配置是否一致（守护进程只读 `config.env`）。
- 端到端自检 `scripts/selftest.py`：用真实的 claude 和沙箱检查沙箱语义、普通工单、沙箱外执行、卡住处置四条路径，不发飞书消息。升级 Claude Code 之后先跑它。
- 长会话成本提示：一张单的会话累计成本超过 `TICKETD_COST_HINT_USD`（默认 $30）时，结果卡上提示后续追问每轮都按整段上下文计费，建议换目标就开新单、或换新会话重来。worker 遇到把工单带向另一个目标的追问会报「不属于本单」。
- 运营指标：worker 成本按轮落库（`ticket ls` / `show` 显示每张单花了多少）；关键事件（卡住、中断、故障等待、沙箱外执行、没送达……）落库；新命令 `ticket stats` 给出最近几天的概况，`--backfill` 补老工单的成本；结果没送达、媒体任务卡住时私信主用户（原来只写本机日志）；每周一 10:00 私信一份运行概况。新配置项 `TICKETD_DIGEST`、`TICKETD_COST_HINT_USD`，新表 `rounds`、`events`、`kv`。
- 卡住分级处置（#16）：没有动作满 30 分钟、且这次命令的进程没在消耗 CPU，ticketd 结束这条命令，worker 收到报错后接着做或汇报（会话和上下文都在）；满 60 分钟仍没进展就中止这一轮、报「需要你决定」，回复「继续」用同一个会话接着做。还在消耗 CPU 的慢命令、等后台任务的都不算卡住。新配置项 `TICKETD_STALL_INTERRUPT`、`TICKETD_STALL_ABORT`。
- worker 的补充环境（#19）：新配置项 `TICKETD_WORKER_PATH`（追加到 PATH）和 `TICKETD_WORKER_ENV`（变量名白名单，值从 `config.env` 取），worker 和沙箱外执行的脚本都生效；`ticket show` 显示 worker 实际的 PATH。代理变量到不了沙箱里的命令（Claude Code 沙箱用自己的代理）。
- 稿子导入云文档失败时，结果卡上写一行原因和本机路径，不再只是少一个按钮、什么也不说（#12）。
- 沙箱外执行通道：worker 遇到沙箱里做不了的步骤（推送、开 PR、部署重启等），把命令写成脚本，用 `ticket report --status blocked --run <脚本>` 申请；结果卡上显示完整脚本，你回复「执行」后主会话执行 `ticket approve`，ticketd 在工单目录里、沙箱外运行汇报时存下的脚本快照，退出码和输出交回 worker 接着做。原来这类事要主会话或你手动在终端里跑。新命令 `ticket approve`，新配置项 `TICKETD_RUN_TIMEOUT`，新表 `runs`。
- `ticket grant` 能放开文件系统以外的沙箱限制（#20）：`--local-net` 让 worker 连本机端口、在本机起服务监听；`--socket <路径>` 放开一个 unix socket（如 docker.sock）。无头浏览器授权了也跑不起来，走沙箱外执行通道。只对 Claude 单生效，下一轮起作用。worker 提示词和任务卡说明了什么时候该请用户放开。新表 `net_grants`。
- 故障自动续跑：断网、接口故障（短重试用完后）、余额不足、认证失败时，工单不再直接失败，而是每 15 分钟自动续跑一次，最长 24 小时；只在开始时推一次「⏳ 等待」，网络恢复或充值后自动接着做，随时引用回复「继续」可马上重试。新配置项 `TICKETD_OUTAGE_RETRY_EVERY`、`TICKETD_OUTAGE_RETRY_HOURS`，`tickets` 表新增 `outage_since` 列。
- 停止 / 暂停时交代停在哪一步、这一轮写了哪些文件、怎么接着做、怎么在终端接管（#17）。
- 已停止的单可以用 `ticket release` 恢复，沿用同一个会话；主会话听到「恢复 T0001」就执行（#18）。
- 新命令 `ticket restart <id>`：会话被不可恢复的错误卡住时换新会话重来，工单号和目录不变，原话和历次追问自动带上；因 4xx 请求错误失败时，失败卡上会提示这条路（#14）。
- 修复：每轮追问都把同一份稿子重新导入成一份新的云文档（#13）。现在同一张单同一路径的 Markdown / 文本稿改过之后覆盖原来那份云文档，链接不变；其他格式或覆盖失败时才另导入，名字带上时间。新表 `doc_targets`。
- worker 任务卡写明这张单的沙箱能做什么、不能做什么，并给出设置文件路径；提示词加了「被拒绝长什么样」的对照和「先做对照实验再下结论」的规矩，减少把网络抖动、网络盘问题误判成权限问题（#21）。另外提示 worker 读 PDF 前先转成文本。
- 修复：worker 把长任务放到后台、结束本轮等任务通知时，10 分钟后会被标成「疑似卡住」，用户以为卡死停单，后台任务一起被杀。现在 ticketd 跟踪 Claude Code 的后台任务事件，这段时间状态卡显示「⏳ 等后台任务」和任务描述，不判卡住；任务结束后 worker 自动接着做。`tickets` 表新增 `bg_wait` 列，老库自动迁移。
- 修复：模型接口余额不足、认证失败时，结果只写「worker 未交汇报就退出（code=1）」。现在写明「模型接口余额不足/认证失败」和报错原文，附「处理好后回复继续」，并且不做无用的重试；后台任务还在跑时马上推一条提醒，不再等到 10 分钟后的「疑似卡住」。
- 修复：过程日志把出错的一轮写成「本轮结束：success」，现在写「本轮结束：出错（…）」（#15）。
- 修复：断网 / DNS 失败（`ENOTFOUND`、`EAI_AGAIN`、「Can't reach the API server」）不算临时故障，工单直接失败不重试（#10）。
- 修复：长时间断网后状态卡刷新失败满 30 次，整单再也不发状态卡。现在只放弃当前那张卡，之后的轮次照常发卡（#11）。
- 修复：稿子导入云文档失败时，告警只截了 lark-cli 输出的末尾（全是升级提示），看不到真正的错误。现在取 JSON 里的 `error` 字段（#12）。
- `ticket report` 交终态汇报时检查图片/视频产物：不在工单目录内或不存在的当场报错，提示 worker 复制进工单目录后重报（原来这些图在结果卡里被静默丢掉）。

- 媒体通道改成有并发上限的任务池：一个大视频在传时，其他工单的图片和稿子不再排在它后面（原来媒体通道只有一个线程，逐行串行）。默认最多 2 个任务同时进行，其中传视频的最多 1 个；同一工单仍按顺序。新配置项 `TICKETD_MEDIA_WORKERS`、`TICKETD_MEDIA_VIDEO_WORKERS`。媒体任务卡住 15 分钟以上、或调度线程 2 分钟没响应时告警，卡住的任务让出名额。
- 修复：一次大视频上传（100MB 约 7 分钟）会卡住整个守护进程，派单、状态卡、心跳和其他工单的投递都停下。现在图片/视频上传、稿子导入云文档和视频消息在守护进程里的独立线程（媒体通道）里做，主循环只发已经准备好的消息；结果卡的发送和覆盖状态卡仍在主循环里。
- 修复：发件箱把所有 SQLite `OperationalError` 都当成「库被锁」，最长重试 24 小时，而且每次都会挡住整个投递队列。现在只有 SQLITE_BUSY / SQLITE_LOCKED 按锁等待退避；磁盘 I/O、只读、库满、缺表缺列等故障告警「数据库故障」，只让出错的那一条退避，连续 3 次且等满 10 分钟才改发文字兜底；结果已发出但写不进库时不再每轮重发。
- 新增去敏检查 `scripts/check_private.py`：`--install-hooks` 给克隆装上 pre-commit / pre-push 钩子，提交和推送前拦住密钥 / 令牌、私钥、真实飞书 id 和文档链接、本机用户目录、非 noreply 邮箱（文件内容、提交信息、作者和提交者都查）；只打印位置，不回显内容。个人规则放 `~/.ticketd/private-patterns`，不进仓库。CI 里由 `tests/test_check_private.py` 检查整个仓库。

- 修复：被回复的那条消息已经撤回时（飞书 230011），结果和状态卡会一直发不出去，最后被丢弃。现在这一单还没发出的消息改为直接发到会话里，卡片和视频原样改投。
- 修复：发件箱在上传媒体之后遇到意外异常（如 lark-cli 输出非 UTF-8）会把结果直接丢掉。现在 lark-cli 输出按容错方式解码，标记失败之前也会先试一次文字兜底。
- 修复：读产物时，如果拿不到已打开文件的真实路径（Linux 上 `/proc` 不可用），并发把中间目录换成符号链接可以读到工单目录外的文件。现在从工单目录起逐级 `openat` + `O_NOFOLLOW` 打开，不再依赖事后核对。
- 断网时媒体不再很快降级：要失败满 3 次、且已经等了 10 分钟，图片才换成灰字、视频才改另发消息（原来断网约 35 秒就降级）。限流等可重试的飞书错误也按退避处理，不再当成卡片被拒。
- 同一工单里同一个视频再次汇报时复用之前的云空间链接，不重复上传（新表 `drive_links`）。云空间视频上限从 20MB 放宽到 100MB，超时按文件大小放宽。
- 一行结果卡在一个 tick 里开始新上传的时间上限为 60 秒，超时后保存进度、下个 tick 继续，减少守护进程主循环被卡住的时间。

- 支持 Linux（含 WSL2）：`scripts/install.sh` 在 Linux 上装成 systemd 用户服务（`systemd/ticketd.service.in`），macOS 仍是 launchd，生成的 plist 不变。`uninstall.sh` 同样按平台处理。WSL1、WSL2 里用了 Windows 盘上的 node / lark-cli / claude、没有 systemd 用户实例时拒绝安装；缺 bwrap / socat、Ubuntu 24.04 的 AppArmor 限制、没开 linger 时给出提示。
- Linux 上查看和清理 worker 进程直接读 `/proc`（新文件 `ticketd/procs.py`、`ticketd/host.py`），不依赖 ps / lsof；按环境变量标记找残留进程不再受「看不到系统程序环境变量」的限制，按目录补的那一道把「父进程是 PID 1 或 ticketd 的祖先（如 `systemd --user`）」当作被收养。
- Linux 上 worker 沙箱的可写目录换成 lark-cli 在 Linux 的数据目录（`~/.local/share/lark-cli`）和 `/tmp`，不存在的附带路径不写（bubblewrap 绑不上不存在的路径），不写 macOS 才有的 `allowMachLookup`。macOS 上生成的设置不变。
- 新环境变量 `TICKETD_PYTHON`：指定 Python 解释器（默认 `/usr/bin/python3`，不存在时退回 PATH 上的 `python3`），`bin/ticket`、`bin/ticketd`、`install.sh` 都认；Linux 上写进服务环境，macOS 上显式指定时写进 plist。安装时检查 Python ≥ 3.9。
- README（中英）新增「Linux / WSL2」安装说明和已知限制。
- 新增 GitHub Actions 工作流：在 ubuntu-latest 和 macos-latest 上跑全部单测，并在 Ubuntu 上真装、重装、卸载一次 systemd 用户服务。
- 结果卡里直接看图：图片产物（png/jpg/jpeg/webp/gif，最多 6 张）由发件箱上传成 `img_key` 后显示在卡里，结果覆盖状态卡和另发新消息两条路都生效。只上传工单目录里的普通文件（不跟符号链接、不认硬链接、≤10MB、核对文件头），交给 lark-cli 的是私有副本。
- 结果卡里看视频：`.mp4` 产物以用户身份传到用户自己的云空间（`lark-cli drive +upload --as user`），卡里放可点击的封面和「▶ 播放视频」按钮；封面用同目录同名的图片。云空间上传失败时改为卡片后另发一条视频消息（`outbox` 新增 `media_json` 列，老库自动迁移）。
- 新配置项：`TICKETD_VIDEO_FOLDER`（视频上传到的云空间文件夹，空为根目录）。
- 结果卡改版（画廊风）：有媒体时媒体在最上面，单图大图、多图两列网格带编号；下面一行结论、「👉 需要你」色块、链接和稿子按钮，其余细节和本机路径收进默认折叠的「详情」。没有媒体的卡按文档排版。worker 提示词要求摘要第一句写结论。
- 发送链路加固：结果卡遇到任何飞书业务错误都马上改发文字兜底；结果覆盖状态卡被拒时改发新卡；卡片/视频重试满 24 小时前先试一次文字兜底；上传媒体遇到网络/超时先退避重试 3 次，不立刻降级。
- 修复：macOS 15 上 `ps -E` 看不到系统程序（sh、sleep…）的环境变量，worker 留下的后台进程逃过了清理。现在另按目录补一道：已被 launchd 收养、没有控制终端、当前目录在工单目录内的进程也会被清理。
- `scripts/validate_cards.py` 新增画廊卡样例，`--with-images` 会先传两张小图，把含图片的卡也送去校验。
- 新增执行器：工单可以交给 Codex CLI 跑（`ticket new --executor codex`，或设 `TICKETD_EXECUTOR=codex`），默认仍是 Claude Code。新文件是 `ticketd/executors.py` 和 `prompts/worker.codex.md`；`tickets` 表新增 `executor` 列，老库会自动迁移，老单按 claude 处理。
- Codex worker 跑在 Codex 自带的 Seatbelt 沙箱里，可写范围和 Claude worker 一致；授权仓库的 `.git` 默认放开（`TICKETD_CODEX_GIT_WRITABLE`）。在专用 `CODEX_HOME`（`TICKETD_CODEX_HOME`）里写 execpolicy 规则，禁止发飞书消息。
- 新配置项：`TICKETD_EXECUTOR`、`TICKETD_CODEX`、`TICKETD_CODEX_MODEL`、`TICKETD_CODEX_HOME`、`TICKETD_CODEX_GIT_WRITABLE`。
- `ticket show` 显示执行器和对应的终端接管命令。
- `scripts/install.sh`：如果 PATH 上有 codex，就把它所在目录写进 launchd 的 PATH。
- 工作区约定：用户明确说用 codex 时，派单加 `--executor codex`。
- 临时故障识别补充了 Codex 网关断流时的报错。
- 测试：新增 `tests/fakes/fake_codex.py`、codex-cli 0.134.0 的真实输出样本（已脱敏）和 `tests/test_codex.py`。

## 0.1.0 — 未发布

首个开源版本。代码来自作者私有的 ticketd（36 个提交，加上尚未提交的「结果覆盖状态卡」改动），开源时做了这些调整：

- 个人信息改成配置：主用户 open_id（`TICKETD_OWNER`，没配置时拒绝带 `--sender` 的派单）、工单根目录（默认 `~/larkyard-tickets`）、launchd 标签（默认 `dev.larkyard.ticketd`，可用 `TICKETD_LABEL` 改）。
- 新增 `~/.ticketd/config.env`：一行一个 `KEY=VALUE`，环境变量优先，改了即生效。
- `scripts/install.sh` 支持 `TICKETD_HOME`、`TICKETD_LABEL`、`--print`；首次安装生成 `config.env`。
- 工作区约定拆成通用模板 `workspace/CLAUDE.md.tmpl` + 个人覆盖 `CLAUDE.local.md`，用 `scripts/setup-workspace.sh` 安装。
- 新增中英 README、NOTICE（注明基于 lark-channel-bridge 开发）。
- 去掉含真实消息/会话 id 的实施计划和端到端记录；`docs/spec.md` 去敏后保留。
