## Why

Larkyard 现在靠 `git clone` + `scripts/install.sh` 安装，修了 bug 要一台台通知 `git pull`、重装，版本长期不一致。多台机器统一管理（控制台，服务端不在本仓库）要做自动更新，但自动更新一旦做错（推了坏版本、更新源被入侵）会同时弄坏或控制所有机器，所以要先把「只装带签名的包、装坏了自己切回」这一层做对。这一层不依赖控制台服务器，可以先在作者自己的两台机器上跑通。

## What Changes

- 新增发版工具：在发布者自己的电脑上，从一个 git tag 打出版本包、生成版本清单、用 Ed25519 私钥（`ssh-keygen -Y sign`）签名。服务器和更新源上没有私钥。
- 新增客户端自动更新，**默认关闭**：只有本机显式配置了更新源和信任的公钥才启用；开源仓库里没有任何默认更新地址。
- 客户端只安装同时满足这些条件的包：签名能被本机信任的公钥验过、包的 SHA-256 和清单一致、通道和本机一致、版本号高于当前（或带签过名的回滚标记）。
- 客户端只在空闲时安装：没有运行中的 worker、没有正在执行的沙箱外脚本、没有正在上传的媒体。
- 版本按目录并排安装、切换指向后重启服务；新版本在限定时间内自检不通过就自动切回上一个版本。
- 新命令：`ticket update status | check | hold | resume | rollback`；`ticket doctor` 显示更新状态。
- 安装方式新增「受管安装」（版本目录 + 指向当前版本的链接），服务模板指向这个链接；不启用自动更新的安装保持原来的就地运行，行为不变。
- 更新源这一期只支持静态文件（本机目录或 HTTPS 静态地址）；控制台、入网、通道推进、心跳上报留给后续变更。

## Capabilities

### New Capabilities
- `release-signing`: 发布者本机打包、生成版本清单、签名，以及回滚清单和换钥清单的格式约定。
- `self-update`: 客户端的启用条件、检查新版本、验签和校验、空闲判定、安装切换、自检和自动回滚、降级保护、暂停更新和本地命令。

### Modified Capabilities
（无。`openspec/specs/` 目前为空，现有行为以 `docs/spec.md` 为准；本变更落地时在 `docs/spec.md` 新增「自动更新」一节，并在 §10「上线与回滚」写「调整」。）

## Impact

- 代码：新增 `ticketd/update.py`（客户端）、`scripts/release.py`（发版工具）；`ticketd/cli.py` 加 `ticket update`、`ticket doctor` 增加一项；`ticketd/config.py` 加更新相关配置项；`ticketd/daemon.py` 的主循环里加低频检查。
- 平台：服务切换和重启涉及 `scripts/install.sh` / `uninstall.sh`、`launchd/`、`systemd/` 模板，macOS 和 Linux 两个分支都要有单测；未启用受管安装时，生成的 plist / unit 逐字节不变。
- 依赖：不新增第三方依赖。验签用系统自带的 `ssh-keygen -Y verify`（OpenSSH ≥ 8.1），不可用时更新功能拒绝启用并在 `ticket doctor` 里写明。
- 数据：`kv` 表记更新状态，`events` 表记更新事件（只加不改，保证回滚到上一个版本时旧代码能读新库）。
- 文档：`docs/spec.md` 新增一节、`config.example.env`、README 配置表、`CHANGELOG.md`。
- 开源用户：不配置更新源就不发出任何更新请求，匿名统计行为不变。
