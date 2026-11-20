## Context

- 签名自动更新已落地（`docs/spec.md` §11）：包是「tag 里被跟踪的文件」打成的可复现 tar.gz，清单签名覆盖包的 SHA-256；受管安装在 `~/.ticketd/app/versions/<版本>`，`current` 是链接，启动器负责试用期和切回。
- 控制台入网已落地（§12）：`ticket enroll` 要带两把发版公钥的指纹。
- bridge（lark-channel-bridge，MIT）：`run --profile <p>` 前台运行；`profile create <p> --agent --workspace [--app-id]` 在终端扫码建应用；首次运行时如果 PATH 上有 `lark-cli` 就自己执行 `config bind --source lark-channel`（只在交互终端里才会用 npm 装 lark-cli）；每次运行都按自己的 `process.execPath` 和入口重写 `~/.lark-channel/secrets-getter`；服务名 `ai.lark-channel-bridge.bot.<p>`（launchd）/ `lark-channel-bridge.bot.<p>.service`（systemd）。
- 维护者本机的 bridge 是 npm 全局安装的 0.7.0 加编译产物补丁。fork（`larkyard/vendor` 分支）在上游 0.7.1 上：会议三个修复（已向上游提 PR）+ 会中 @ 任意位置、忽略自己的弹幕、轮询发现散会 + 本机补丁移植（会中授权、会中提醒、会议号提取、附件批处理、终态折叠、30 秒流式宽限、模型选项），每项都有单测，原先针对编译产物写的冒烟脚本和新旧产物逐项对照都一致。
- 约束：Larkyard 仍是 Python 3.9 标准库零依赖；`install.sh` 是 POSIX sh；平台差异只放在 `host.py` / `procs.py` / `perms.py` / 安装脚本和本变更新增的 `bridge.py`。

## Goals / Non-Goals

**Goals:**
- 员工：一条命令 + 扫一次码 + 确认主人名字。
- Larkyard、bridge、Node / lark-cli 的版本一起签、一起装、一起回滚（bridge 在下一个重启窗口跟上）。
- 不写死任何 nvm / npm 路径。
- 本机这种「分开安装」的机器能迁移，也能原样退回。

**Non-Goals:**
- 不改架构：bridge 仍驱动主会话；不让 ticketd 托管 bridge 进程；「主会话独立」另议。
- 不把 bridge 编成单个可执行文件（Node SEA），不做精简 fork。
- 不做 Windows。
- 控制台侧怎么托管 `install.sh` 和静态更新源（控制台仓库的事）。

## Decisions

### D1. fork 固定提交，构建在发版机器上做
`vendor/bridge.lock.json`：`{"repo": <fork 地址>, "commit": <40 位>, "version": <package.json 里的版本>}`。`scripts/build_bridge.py --out <目录>`：在缓存目录里 clone / fetch fork、检出这个提交（核对 `HEAD` 和 `package.json` 版本）、`pnpm install --frozen-lockfile`、`pnpm build`，然后组装输出目录：`package.json`、`LICENSE`、`bin/`、`dist/`、生产依赖（另起一个目录 `pnpm install --prod --frozen-lockfile --ignore-scripts --config.node-linker=hoisted`，得到 npm 式扁平 `node_modules`，删掉 `.bin`、`.pnpm`、`.modules.yaml` 这类元数据）、`THIRD_PARTY_NOTICES.md`（逐个依赖抄 `LICENSE*` / `NOTICE*` 文本；没有许可证文件的依赖记下 `package.json` 的 `license` 字段；两者都没有就构建失败）、`BRIDGE.json`（`repo`、`commit`、`version`、`tree_sha256`：按路径排序的「路径 + 权限 + 内容哈希」再哈希）。输出目录里有链接、设备文件就失败。

为什么不打成单文件：试过把依赖全部内联进一个 ESM 文件，`qrcode-terminal` 用了严格模式不允许的八进制转义，飞书 SDK 在模块里读 `__dirname/../package.json`，都要逐个绕；扁平 `node_modules` 就是 npm 全局安装时的形态，线上跑了两个多月，风险最低。代价是包大约多 45 MB。

### D2. 进签名包，清单格式不变
`release.py release --tag vX --key K --bridge <目录>`：tag 里有 `vendor/bridge.lock.json` 时必须给 `--bridge`，`BRIDGE.json` 的 `commit` 必须等于锁文件里的提交，`tree_sha256` 必须和目录实际内容一致；文件按路径排序加到 `larkyard-<版本>/vendor/bridge/` 下，mtime、属主、权限按现有规则固定，同样可复现。清单字段一个不加：bridge 由包的 SHA-256 覆盖，老客户端照常验证和安装（多出来的只是普通文件）。`update.extract` 已经只接受普通文件和目录，包里没有链接。

### D3. 运行时清单和固定目录
`vendor/runtime.json`（被跟踪，随包签名）：

```json
{"node": {"version": "24.22.3", "assets": {"darwin-arm64": {"url": "https://nodejs.org/dist/v24.22.3/node-v24.22.3-darwin-arm64.tar.gz", "sha256": "…"}, …}},
 "lark_cli": {"version": "1.0.86", "assets": {"darwin-arm64": {"url": "https://github.com/larksuite/cli/releases/download/v1.0.86/lark-cli-1.0.86-darwin-arm64.tar.gz", "sha256": "…"}, …}}}
```

`ticketd/bridge.py` 的 `ensure_runtime()`：
- Node：`~/.ticketd/runtime/node-v<版本>-<平台>/bin/node` 不在就下载（只跟随 https 重定向、总时长上限、大小上限 200 MB）、核对 SHA-256、只解出 `bin/node` 和 `LICENSE`（拒绝绝对路径和 `..`），装好后 `runtime/node` 原子换成指向它的相对链接。系统里有没有 Node、装没装 nvm 都不影响。
- lark-cli：同样下载到 `~/.ticketd/runtime/lark-cli-<版本>/lark-cli`，`~/.ticketd/bin/lark-cli` 换成指向它的链接。用户 PATH 上已有 lark-cli 也不动它；Larkyard 的服务只用 `~/.ticketd/bin` 里的这份。
- 已经是这个版本就什么都不做（只核对文件存在且可执行）。

国内网络访问 GitHub 慢时，`runtime.json` 的每个资源可以有 `mirrors`（同一个 SHA-256），按顺序试；SHA-256 才是信任来源，地址不是。

### D4. bridge 用哪一份：单独的链接
`~/.ticketd/bridge/current` → `../app/versions/<版本>/vendor/bridge`（相对链接）。bridge 服务启动的是 `~/.ticketd/runtime/node/bin/node ~/.ticketd/bridge/current/bin/lark-channel-bridge.mjs run --profile <p>`，不直接指向 `app/current`：

- Larkyard 自己更新（`current` 切换）时，正在跑的 bridge 不受影响，主会话不断；
- 新版本带的 bridge 由 `ticket bridge restart --if-stale` 换上：比较 `bridge/current` 和 `app/current/vendor/bridge` 的 `BRIDGE.json`（`commit` + `tree_sha256`），不同才切换和重启；
- `update.prune` 清理旧版本目录时保留 `bridge/current` 指向的那个版本。

`~/.ticketd/bridge/state.json` 记 `version`、`commit`、`tree_sha256`、`switched_at`、`previous`（上一个版本目录）、`managed_label`（Larkyard 写的服务名）、`adopted`（D7）。

### D5. 什么时候重启 bridge
- Larkyard 自动更新：只重启 ticketd（和现在一样）。新版本确认后如果带的 bridge 和在用的不同，记 `kv bridge.pending = <版本>`，`ticket doctor` 显示「新 bridge 待生效：下一个重启窗口（每天 04:07）或 `ticket bridge restart`」。
- 每天 04:07：`ticket bridge restart --if-stale`。新装的机器用这个；`adopt` 的机器原来就有无条件的每日重启任务，改成 `ticket bridge restart`（行为不变，只是换成固定路径、顺带换上新 bridge）。
- 不在白天自动重启 bridge：bridge 没有「当前有没有进行中的回复」的对外接口，从外面猜空闲会误伤正在流式回复的主会话。
- 重启流程：`ensure_runtime()`（失败就不切换，保持原样）→ 切换链接（记 `previous`）→ 服务管理器重启（launchd `kickstart -k`，systemd `restart`）→ 最多等 90 秒，看 bridge 当天日志里重启之后有没有 `ws connected` / `profile-online` → 没有就把链接切回 `previous` 再重启一次、记事件 `bridge rollback`、私信主用户一次。`--if-stale` 且版本没变时什么都不做（不重启）。

这就是「Larkyard 回滚连上 bridge」的落法：Larkyard 切回旧版本后，下一个重启窗口 `--if-stale` 发现在用的 bridge 和 `current` 带的不同，换回旧 bridge。

### D6. `install.sh`（一行安装）
仓库根目录的 POSIX sh 脚本，发布时由 `release.py publish-installer --url <发布地址> --to <更新源目录>` 填进默认地址并拷到更新源根目录，同时把两把公钥放到 `keys/main.pub`、`keys/backup.pub`。脚本里写死两把发版公钥的指纹（仓库里一份、发布地址一份，可以互相比对，这就是「控制台以外的渠道」）。

步骤：
1. 环境：macOS / Linux；`curl`、`tar`、`/usr/bin/ssh-keygen`（Linux 上找不到再用 PATH 上的）、Python ≥ 3.9（macOS 没有就提示 `xcode-select --install`）、`claude` 在 PATH 上（找不到只警告：服务 PATH 会在 `ticket setup` 里再找一次）。
2. 取 `<发布地址>/keys/main.pub`、`backup.pub`，用 `ssh-keygen -lf` 算指纹，必须等于内置值（或 `--release-key` / `--backup-key` 给的值）。
3. 取 `channels/<通道>/manifest.json` 和 `.sig`，用主公钥 `ssh-keygen -Y verify -n larkyard-release -I larkyard-release` 验签。
4. 用 Python 解析清单（`kind=release`、`product=larkyard`、通道一致、`package` 名字合规），下载包、核对大小和 SHA-256。
5. 用系统 `tar` 解包到临时目录，交给包里的 `python3 -m ticketd.bootstrap`：它再验一遍（同一份 `updsig` 代码）、按 `update.extract` 的规则解到 `~/.ticketd/app/versions/<版本>`、拷启动器、`current` 指向它、`ticket update trust` 写入两把公钥、记下已接受的清单（`update.max_seq`、`update.accepted`）、`config.env` 不存在就生成并写 `TICKETD_UPDATE_URL` / `TICKETD_UPDATE_CHANNEL`、`ensure_runtime()`、建 `bridge/current`。已经是受管安装且版本不低于这个包时只做缺的步骤（可重复执行）。
6. `exec ~/.ticketd/app/current/bin/ticket setup … < /dev/tty`。没有终端（CI、远程脚本）时停在这里，提示之后在终端里执行 `ticket setup`。
7. 带 `--enroll` 时，`ticket setup` 结束后执行 `ticket enroll <--console 地址> <入网码> --release-key <内置指纹> --backup-key <内置指纹>`。

`curl | sh` 时脚本自己的标准输入是管道：所有交互都从 `/dev/tty` 读。

### D7. `ticket setup`
顺序：① 飞书应用 → ② 工作区 → ③ 服务 → ④ 认主人 → ⑤ 用户身份（可选）→ ⑥ 自检。每一步先看是否已经做过，做过就跳过（可重复执行）。

- ① 读 `~/.lark-channel/config.json`：已有这个 profile（默认 `claude`，`--profile` 可改）就跳过；否则执行打包的 bridge `profile create <p> --agent claude --workspace <工作区>`（`--app-id` 时用已有应用，App Secret 由 bridge 在不回显的提示里读），标准输入输出直接接终端。
- ② 工作区默认 `~/larkyard-workspace`（已有 profile 时用它的 `workspaces.default`）；目录不存在就建；`CLAUDE.md` 不存在时用 `setup-workspace.sh` 渲染，已存在就不动（提示 `ticket doctor` 会比较模板）。
- ③ 服务：bridge 服务按 D4 写（服务名同 bridge 自己的；该名字的服务文件已存在、又不是 Larkyard 写的，就停下提示用 `ticket bridge adopt`），PATH = `~/.ticketd/runtime/node/bin:~/.ticketd/bin:<claude 所在目录>[:<codex 所在目录>]:/usr/bin:/bin:/usr/sbin:/sbin`（Linux 多 `/usr/local/bin`）；ticketd 服务用现有的 `scripts/install.sh`（受管模式，`LARK_CHANNEL_PROFILE` 传进去）；每天 04:07 的重启任务（`dev.larkyard.bridge-restart`，launchd `StartCalendarInterval` / systemd timer）。
- ④ 认主人：`config.env` 已有 `TICKETD_OWNER` 就跳过。否则等 bridge 完成 `lark-cli config bind`（最多 60 秒），用 bot 身份 `GET /open-apis/application/v6/applications/<app_id>?user_id_type=open_id` 取 `owner.owner_id`（bridge 运行时就是这样认 owner 的），再试 `GET /open-apis/contact/v3/users/<id>` 取名字（没有通讯录权限就只显示 open_id 后 6 位）。终端问「是你吗」，确认就写入。查不到、或者用户说不是：生成 4 位码，请用户在飞书里给 bot 私聊发「认主人 <码>」，读 bridge 当天日志里 `enter` 事件（`preview` 含这个码、时间在提示之后）的 `sender`，最多等 5 分钟，再确认一次写入。
- ⑤ 「现在授权用户身份吗？（传视频、导入文档要用）」默认否；是就执行 `lark-cli auth login`。以后 `ticket setup --user-auth` 单独做这一步。
- ⑥ `ticket doctor`，再 `ticket doctor --dm` 让守护进程按自己的配置私信你一条；提示在飞书里给 bot 说句话确认主会话能回。

worker 环境（`TICKETD_TICKET` / `TICKETD_SPOOL`）里 `ticket setup`、`ticket bridge restart|adopt` 一律拒绝；`ticket bridge status` 可以。

### D8. `ticket bridge adopt`
前提：受管安装、`current` 带 `vendor/bridge`、bridge 服务文件存在（按 `~/.lark-channel/active-profile` 或 `--profile` 找）、它不是 Larkyard 写的。
1. 读服务文件：入口、`LARK_CHANNEL_HOME`、日志路径；入口不是 `lark-channel-bridge` 就拒绝（不认识的启动方式不碰）。
2. `ensure_runtime()`，建 `bridge/current`。
3. 备份到 `~/.ticketd/bridge/adopt-backup/<时间>/`：bridge 服务文件、`~/.lark-channel/secrets-getter`、每日重启任务（有的话）、ticketd 服务文件；`state.json` 记下原路径。
4. 写新的 bridge 服务（同一个服务名、同一个 `LARK_CHANNEL_HOME` 和日志路径）、重写 `secrets-getter`（bridge 下次启动也会按新路径重写一遍，内容一致）、每日重启任务改成 `ticket bridge restart`（保留原时刻）、ticketd 服务用 `scripts/install.sh` 重装（PATH 换成固定目录）。
5. 服务管理器重新加载 bridge 服务（会重启 bridge，打断正在进行的那一轮回复）：所以 `adopt` 默认先打印要改的文件和备份位置、要求输入 `yes`；`--dry-run` 只打印。重启后的健康检查同 D5，不通过自动执行 `--undo`。
6. `--undo`：把备份原样拷回、重新加载这几个服务、删掉 `adopted` 记录。`~/.lark-channel` 里除 `secrets-getter` 以外的任何文件（profile、会话、凭据、日志）都不读写。

### D9. 上游边界
CLAUDE.md「上游边界」改为：bridge 基于 fork 的固定提交；补丁只进 fork 源码（带测试），不再改编译产物；rebase 节奏为每季度或有需要的修复时；通用修复同时给上游提 PR，不等合并。

## Risks / Trade-offs

- **扫码建应用的非交互参数没在真实租户上走过**：`profile create` 的 `--agent`、`--workspace`、`--app-id` 覆盖首次配置的全部问题，按源码确认过，但没在新租户上跑；缺参数时 bridge 会在终端里问，向导把终端原样交给它，不会卡死。
- **bot 身份查应用 owner 在沙箱里测不了**（钥匙串），单测用假 lark-cli；失败时自动走 4 位码。
- **公司租户要管理员审批权限**：向导替代不了，自检里 `ticket doctor --dm` 失败时说明找谁审批。
- **bridge 健康检查靠日志关键字**（`ws connected` / `profile-online`）：bridge 改了日志措辞会误判为失败而切回；切回本身是安全方向，并且会私信说明。
- **包变大约 45 MB**：对 200 MB 上限还有余量；以后可以考虑裁剪依赖。
- **GitHub 在部分网络下慢**：`runtime.json` 支持镜像地址，信任只看 SHA-256。

## Migration Plan

1. fork 的 `larkyard/vendor` 分支推上去，`vendor/bridge.lock.json` 指向它的提交。
2. 发一个带 `vendor/bridge` 的版本（`build_bridge.py` + `release.py release --bridge`），`publish-installer` 发布 `install.sh` 和公钥。
3. 已受管的机器（比如维护者本机）自动更新到这个版本后，在终端里执行 `ticket bridge adopt`（会重启一次 bridge）；有问题 `ticket bridge adopt --undo`。
4. 新机器直接一行安装。
