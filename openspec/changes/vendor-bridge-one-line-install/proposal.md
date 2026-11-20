## Why

新机器从零装好 Larkyard 现在要 15 步，其中 8 步来自 lark-channel-bridge：装 nvm 和 Node、`npm i -g` 装 bridge、在 bridge 里扫码建应用、装 lark-cli 并绑定、找 bridge 的工作目录、抄 open_id 填 `TICKETD_OWNER`。另外有三个长期隐患：

- bridge、`secrets-getter`、ticketd 服务的 PATH 都写死了 nvm 下的 Node 路径，nvm 一切换版本三者一起坏；
- Larkyard 走签名自动更新，bridge 走 `npm i -g`，两套升级；
- 维护者机器上给 bridge 编译产物打的补丁（会中授权、会中提醒、会议号提取、附件批处理、终态折叠等）不在任何源码里，`npm i -g` 一升级就全没了。

架构不变：bridge 仍然驱动主会话，主会话的能力（改文件、流式回复、会议智能体）全部保留。这一变更只精简部署。

## What Changes

- **bridge 随发版包一起打包（vendoring）**：bridge 源码取自我们的 fork，固定在 `vendor/bridge.lock.json` 记录的提交上；补丁已移植进 fork 源码并有测试。`scripts/build_bridge.py` 按这个提交构建 bridge（JS 产物 + 按锁文件装的生产依赖 + 第三方许可证声明），`release.py release --bridge <目录>` 把它放进签名包的 `vendor/bridge/`。清单格式不变：bridge 被包的 SHA-256 一起覆盖。
- **运行时固定目录**：`vendor/runtime.json`（跟随仓库、进签名包）写死 Node 和 lark-cli 的版本和各平台官方包的 SHA-256。Node 装到 `~/.ticketd/runtime/`，lark-cli 装到 `~/.ticketd/bin/`，不再依赖 nvm 和 npm。
- **一行安装** `install.sh`（仓库根目录，发布到发布地址）：`curl -fsSL <发布地址>/install.sh | sh`。按内置的发版公钥指纹核对公钥、验签通道清单、核对包，装成受管安装（第一天就能自动更新），装 Node 和 lark-cli，然后进入 `ticket setup`。内部员工的命令多带 `--console <地址> --enroll <入网码>`，装完接着 `ticket enroll`，不用再手抄指纹。
- **`ticket setup` 向导**：调用打包进来的 bridge 扫码建飞书应用（已有 profile 就跳过）→ 工作区（默认 `~/larkyard-workspace`，渲染工作区约定）→ 写 bridge 服务、ticketd 服务和每天 04:07 的 bridge 重启任务（路径全部指向 `~/.ticketd`，不写死 nvm）→ 自动认主人（用 bot 身份查应用 owner，显示名字让你确认；不对就在飞书里给 bot 发一个 6 位码）→ 可选的用户身份授权 → 自检。
- **`ticket bridge`**：`status`；`restart [--if-stale]`（切到当前版本带的 bridge、确保运行时、重启、确认长连接已连上，失败就切回原来的 bridge）；`adopt [--dry-run] / --undo`（从现在分开安装的 bridge 迁移：服务名、profile、会话、凭据都不动，只换启动命令和 `secrets-getter`、把每天的重启任务改成 `ticket bridge restart`，改前备份，`--undo` 原样恢复）。
- **bridge 什么时候重启**：Larkyard 自己更新时照旧只重启 ticketd，不打断主会话；新版本带的 bridge 由每天 04:07 的重启任务（`ticket bridge restart --if-stale`）换上，版本没变就不重启。`ticket doctor` 增加「bridge」一项。
- 上游边界改为「基于我们的 fork，固定提交，每季度或有需要的修复时 rebase；通用修复同时给上游提 PR，不等合并」。

## Capabilities

### New Capabilities
- `vendored-bridge`: fork 固定提交和构建、进签名包、运行时清单、bridge 链接和按需重启、重启后健康检查和切回、清理旧版本时保留 bridge 在用的版本、`ticket bridge status`、`ticket doctor` 一项。
- `one-line-install`: `install.sh` 的核对和安装步骤、内置指纹、`ticket setup` 向导（应用、工作区、服务、认主人、用户授权、自检）、非终端时的行为、入网衔接。
- `bridge-adopt`: 从分开安装的 bridge 迁移到打包的 bridge、备份和 `--undo`、不动 profile 和凭据。

### Modified Capabilities
（无。`openspec/specs/` 目前为空，现有行为以 `docs/spec.md` 为准；本变更落地时在 `docs/spec.md` 新增「bridge 内化与一行安装」一节，并调整 §3、§11 里和 bridge 相关的约定。）

## Impact

- 代码：新增 `install.sh`、`scripts/build_bridge.py`、`ticketd/bridge.py`、`ticketd/setup.py`、`vendor/bridge.lock.json`、`vendor/runtime.json`、`launchd/bridge*.plist.in`、`systemd/bridge*.in`；`scripts/release.py` 加 `--bridge` 和 `publish-installer`；`ticketd/update.py` 清理时保留 bridge 在用的版本、新版本带的 bridge 不同时记下待重启；`ticketd/cli.py` 加 `ticket setup`、`ticket bridge`，`ticket doctor` 加一项；`scripts/install.sh` 优先用 `~/.ticketd/runtime` 和 `~/.ticketd/bin` 里的 Node 和 lark-cli（不存在时生成的文件不变）。
- 依赖：Larkyard 本身不新增依赖（Python 标准库）。发版机器构建 bridge 要 git、Node 和 pnpm；员工机器只要 `curl`、`tar`、`/usr/bin/ssh-keygen`、Python 3.9+ 和已登录的 Claude Code。
- 签名包：多出 bridge 约 45 MB（JS 产物加生产依赖）；Node 和 lark-cli 不进包，由安装时按 SHA-256 下载。
- 开源用户：同样一行安装（不带 `--enroll`）；不想用一行安装的，原来「自己装 bridge + clone + `scripts/install.sh`」的方式继续可用。
- 不改 `~/.lark-channel` 里的 profile、会话和凭据；不改 bridge 的服务名，`lark-channel-bridge status/stop` 的习惯照常（命令改由打包的 bridge 提供）。
