## Context

- 现状：一份 git 克隆就是安装本身，`scripts/install.sh` 把仓库路径（`@REPO@`）写进 launchd plist / systemd unit，服务直接跑仓库里的 `bin/ticketd`。没有发版流程，`__version__` 只在 CHANGELOG 里手工对齐。
- 约束：Python 3.9 标准库、零依赖（`CLAUDE.md`）；平台差异只能放在 `host.py`、`procs.py`、`perms.py` 和安装脚本里（`docs/spec.md` §3.1）；macOS 行为和生成的 plist 不能顺带改掉。
- 数据库迁移目前只加列 / 加表，回滚到旧版本时旧代码能读新库，这是自动回滚能成立的前提。
- 动机和范围见 `proposal.md`；行为以 `specs/release-signing`、`specs/self-update` 为准。

## Goals / Non-Goals

**Goals:**
- 更新源（以后是控制台）被完全控制时，也推不进一个发布者没签过的版本。
- 坏版本最多影响一个通道，且能在 10 分钟内自己回到上一个版本。
- 不启用时，代码路径对开源用户是惰性的：不发请求、不改服务模板。

**Non-Goals:**
- 控制台、入网、设备令牌、心跳和通道推进（后续变更，服务端不在本仓库）。
- 增量更新、差分包、P2P 分发。
- 更新 lark-channel-bridge、Claude Code、Codex 等外部程序。
- 系统级安装（`/opt`、root 权限）。

## Decisions

### D1. 验签用 `ssh-keygen -Y verify`，不自己实现 Ed25519
标准库没有 Ed25519。可选：自己用纯 Python 实现（约 200 行，易错、难审计）、引入 `cryptography`（破坏零依赖）、调 OpenSSH（macOS 和主流 Linux 自带，8.1 起支持 `-Y`）。选 OpenSSH：签名格式（SSHSIG）有公开规范，签和验用同一个工具，发布者可以把私钥放在 ssh-agent 或硬件密钥里。信任文件用 `allowed_signers` 格式，分两个文件：`main.allowed_signers`（主公钥，principal `larkyard-release`）和 `backup.allowed_signers`（备份公钥，principal `larkyard-rekey`）。普通清单、回滚清单、暂停标记只拿主文件验，换钥清单只拿备份文件验，命名空间固定为 `larkyard-release`，防止拿别处的 SSH 签名冒充。写入信任公钥时用 `ssh-keygen -lf` 算指纹，和命令行给的 `--fingerprint` 比较（对抗审查 B1）。

### D2. 受管安装：`~/.ticketd/app/versions/<版本>/` + `current` 符号链接
可选：就地 `git pull`（需要 git 和仓库写权限，不能验签）、覆盖解包（失败时半新半旧）、并排目录 + 链接（切换是一次原子 `rename`）。选并排目录。服务模板的启动命令指向版本目录外的启动器（见 D6），启动器再 `exec` `~/.ticketd/app/current/bin/ticketd`。`ticket update adopt` 把一份现有克隆转成受管安装：解包当前 tag 的签名包到版本目录、安装启动器、重新生成服务模板，并把 bridge 工作区 `CLAUDE.md` 里的 `@LARKYARD@/bin/ticket` 路径改写为 `~/.ticketd/app/current/bin/ticket`（复用 `scripts/setup-workspace.sh` 的渲染，改之前备份；`--undo` 全部改回，对抗审查 S12）。这是唯一会改服务模板和工作区的入口；不执行它的安装保持就地运行，`install.sh` 默认行为不变。

### D3. 更新源是静态文件布局
```
<源>/channels/<通道>/manifest.json(.sig)   这个通道当前发布的清单（release / rollback）
<源>/pause.json(.sig)                       可选，当前窗口的暂停标记（所有通道共用）
<源>/rekey.json(.sig)                       可选，换钥清单
<源>/packages/larkyard-<版本>.tar.gz
```
支持 `file://` 和 `https://`。这样第一期客户端可以在两台机器上用一个本地目录或任意静态托管跑通，后续控制台只要按同样的路径提供文件，客户端不用改。HTTPS 只是传输层，安全不依赖它（依赖签名）。

### D4. 更新源没有私钥：推进、回滚、暂停都是「发布预签文件」（对抗审查 B2、B3）
更新源以后是控制台，控制台被攻破是主要威胁，所以它不能有任何签名能力。三个通道的清单在发版时一起签；回滚清单、换钥清单只在发布者本机签；暂停标记按 7 天窗口预签 12 周。备选方案「推进时在本机对新通道重签」也安全，但每次推进都要发布者回到自己电脑上，选预签。代价：控制台上的「一键暂停」依赖还有没过期的预签标记，控制台在剩不到 2 周时提醒发布者补签。

重放：每份清单带发布者本机递增的 `seq`，客户端记已接受过的最大序号（`kv` 的 `update.max_seq`，只增不减），比它小或相等的清单一律当作旧清单。回滚清单靠「新序号 + `expires_at`」才能装回旧版本，过期后重放无效。冻结（一直给你看旧清单）这一期只做提示：本机通道当前清单的签名时间早于 `TICKETD_UPDATE_STALE_DAYS`（默认 30）天时，`ticket doctor` 标黄。

### D5. 空闲判定复用现有状态
空闲 = 调度器没有运行中的 worker（`tickets.state='running'`）、`runs` 没有 `running`、发件箱没有处理中的媒体上传、没有压缩中的会话。判断和安装都在守护进程主循环里做，安装期间调度器不启动新 worker（短暂持有一个「安装中」标记），所以不存在「判定空闲后刚好派出一张单」的竞态。

### D6. 自检和回滚：版本目录外的启动器（对抗审查 B4）
原方案让 `bin/ticketd` 兼任启动器，但它在版本目录里：新版本的 `bin/ticketd` 或它导入的模块有语法错误时，永远走不到回滚分支，服务每 10 秒重启一次、无限循环。所以启动器放在 `~/.ticketd/app/launcher.py`：只用标准库、不导入 `ticketd` 包，状态放独立的 `~/.ticketd/app/update-state.json`（不读数据库，免得依赖新版本的表结构）。

流程：切换前写 `update-state.json = {from, to, deadline, starts: 0}`。启动器每次被 launchd / systemd 拉起时先读它：`starts` 加一；已过 `deadline` 且未确认、或期限内 `starts >= 3`，就把 `current` 改回 `from`、写「已回滚」，再 `exec` 旧版本。新版本守护进程自检通过后把状态改为「已确认」。启动器本身更新分两步：版本包里带新启动器，新版本确认之后才替换 `launcher.py`（原子 rename），替换失败保留旧的。

### D7. 重启服务的方式
macOS：`launchctl kickstart -k gui/<uid>/<label>`；Linux：`systemctl --user restart <label>`。放在 `host.py` 旁新增的平台分支里，两个平台都 mock 测试。由守护进程自己发起重启，重启前按 §8 的停机流程结束（此时已是空闲，没有 worker 要停）。Linux 上 `KillMode=mixed` 会杀 cgroup 里剩下的进程，因为空闲判定包含「没有沙箱外脚本」，不会误杀。

### D8. 配置项
`TICKETD_UPDATE_URL`（空 = 关闭）、`TICKETD_UPDATE_CHANNEL`（默认 `stable`）、`TICKETD_UPDATE_EVERY`（3600）、`TICKETD_UPDATE_PROBATION`（600）、`TICKETD_UPDATE_STALE_DAYS`（30）。信任的公钥放 `~/.ticketd/update/main.allowed_signers`、`backup.allowed_signers`（权限 600，归用户）。抖动为 0 到间隔 1/6 的均匀随机数。全部加进 `config.py`，worker 环境不能改（沿用 `TICKETD_TICKET` / `TICKETD_SPOOL` 判断，和 `ticket telemetry on/off` 一样）。

## Risks / Trade-offs

- [启动器本身有 bug，自动回滚失效] → 启动器保持最小、不依赖版本化代码、全分支单测，并加用例「新版本 `bin/ticketd` 有语法错误 → 600 秒内切回」；`ticket update rollback` 和手工把 `current` 链接改回去作为兜底，写进 README。
- [`ssh-keygen` 版本差异] → 启动时探测 `-Y verify` 是否可用，不可用就拒绝启用；CI 两个平台都跑一次真实签名和验签。
- [新版本带了不兼容的数据库迁移，回滚后旧代码读不了] → 发版约定只加列 / 加表（`docs/spec.md` 写明），发版工具检查 `db.py` 相对上一个 tag 是否出现 `DROP` / `RENAME` 并警告。
- [冻结攻击] → 只做到「过期提示」，彻底解决要等控制台的心跳能对比版本（后续变更）。
- [主私钥泄露] → 攻击者能签普通清单，但签不了换钥清单（要备份私钥），抢不走信任；发布者发布暂停标记，再用备份私钥换钥（对抗审查 S11）。泄露期间攻击者签的清单序号可能很大，换钥后旧主公钥签的东西全部失效，不影响。
- [两把私钥都丢了] → 只能在每台机器上手工 `ticket update trust` 换公钥，README 写明。
- [预签暂停标记被提前发布] → 控制台被攻破时可以发布暂停标记让大家停更新，这是可以接受的（最坏是停更，不是装坏版本）。

## Migration Plan

1. 发布者生成主签名密钥和离线备份密钥，在自己两台机器上用 `ticket update trust` 按指纹写入两把公钥。
2. 在两台机器上 `ticket update adopt`，转为受管安装；`ticket doctor` 全部通过。
3. 用一个本地目录做更新源，按 `tasks.md` 第 6 组的步骤逐项验证（正常升级、三种拒装、坏版本自动回滚、暂停）。
4. 回退：`ticket update adopt --undo` 把服务模板改回指向原克隆，删除 `TICKETD_UPDATE_URL`，行为回到当前版本。

## Open Questions

- 发布者的主私钥放 ssh-agent 还是硬件密钥：不影响格式和客户端，发版工具两种都支持。
