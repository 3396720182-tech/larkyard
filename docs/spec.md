# ticketd 设计规格（v1）

> 状态：v1 已实现，并在作者本机持续服务真实工单。正文里的「调整（Txxxx）」是上线后的迭代记录，Txxxx 是作者自己用 Larkyard 派出的工单编号。
> 本文写于开源之前，「主用户」「我」指部署者本人；路径、标签等以仓库当前的默认值为准（见 §3 和 `config.example.env`）。

## 1. 目标与非目标

**目标**

1. 长任务不再卡住飞书对话：bridge 会话（主会话）秒级派单后立即空闲。
2. 结果送达不依赖流式卡片：由本地服务主动推送，失败重试。
3. 多任务并行（上限 3）。
4. 任务状态持久化：bridge、服务或电脑重启后可恢复。
5. worker 写权限受限，过程可随时查看（非黑盒）。

**非目标（v1 不做）**

- 飞书多维表格面板（v2 再考虑）。
- 主动发现 / 规则驱动的主动工作（C 档）。
- 跨用户授权体系：v1 只为主用户（`config.env` 里的 `TICKETD_OWNER`）发起的消息派单。

## 2. 架构

```
用户 ──飞书──▶ bridge 会话（主会话：只派单）
▲   bridge 本身：会议纪要卡「派成工单」── ticket new ──┐
│                │  ticket new / send / stop / hold / release / grant / approve / show / ls
│                ▼◀──────────────────────────────────┘
│         SQLite（~/.ticketd/db/ticketd.db） ◀──轮询（1s）── ticketd（launchd / systemd 托管）
│                                                       ├─ worker：Claude Code（claude -p）或 Codex（codex exec），按单路由
│                                                       │          每单一个会话 + 目录 + 沙箱，汇报写进 .reports/
│                                                       ├─ 状态卡：原地刷新，结果覆盖
│                                                       └─ 发件箱：处理产物，再用 lark-cli 发回飞书
│                                                          ├─ 图片放进卡里；视频以用户身份传云空间（按内容缓存链接）；稿子导入云文档
│                                                          ├─ 断网退避重试；媒体失败满 3 次且已等 10 分钟才降级
│                                                          ├─ 回复原消息；原消息已撤回（230011）则改发到会话
└──◀── 结果卡 / 提醒 ──────────────────────────────────────┘
```

| 单元 | 职责 | 不负责 |
|---|---|---|
| `ticket` CLI | 主会话和 worker 操作工单的唯一入口；只读写数据库 | 不启动进程，不发飞书消息 |
| `ticketd` 服务 | 调度 worker、并发与项目锁、发件箱、超时、崩溃恢复、过程日志 | 不做任何判断类决策 |
| 主会话（bridge 里的我） | 判断：开新单 / 转给已有单 / 停止 / 暂停 / 闲聊；写开工确认句 | 不做技术工作，不给 worker 写计划 |
| worker | 读资料、做事、验证，用 `ticket report` 收尾 | 不直接给用户发飞书消息 |

`ticket` CLI 和 `ticketd` 之间只通过 SQLite 通信：不开端口，不用 socket。SQLite 用 WAL 模式加 busy_timeout，以支持并发写入。

上图只画派单和送达。v1 之后加的几块挂在同一个守护进程上，不改这条主路径：签名自动更新和启动器（§11）、控制台接入（§12，默认关闭）、打包的 bridge 和一行安装（§13）、升级旧安装（§14）、匿名统计（§7.4，默认关闭）。发版和更新的总图见 README「组成」。

## 3. 路径与部署

| 用途 | 路径 |
|---|---|
| 代码 | 本仓库（Python 3 标准库，零依赖） |
| 个人配置 | `~/.ticketd/config.env`（`TICKETD_HOME` 可改；同名环境变量优先） |
| 状态库与服务日志 | `~/.ticketd/db/ticketd.db`、`~/.ticketd/logs/` |
| 工单目录 | `$TICKETD_WORKROOT/<编号>-<短名>/`（默认 `~/larkyard-tickets`，编号如 `T0001`） |
| launchd（macOS） | `~/Library/LaunchAgents/<TICKETD_LABEL>.plist`（默认 `dev.larkyard.ticketd`），`KeepAlive=true`，`RunAtLoad=true` |
| systemd（Linux / WSL2） | `~/.config/systemd/user/<TICKETD_LABEL>.service`，`Restart=always`，`WantedBy=default.target`（见 §3.1） |
| 打包的 bridge、运行时（§13） | `~/.ticketd/bridge/current` → `../app/versions/<版本>/vendor/bridge`；`~/.ticketd/runtime/node`、`~/.ticketd/bin/lark-cli` |
| bridge 服务（§13） | `~/Library/LaunchAgents/ai.lark-channel-bridge.bot.<profile>.plist` / `~/.config/systemd/user/lark-channel-bridge.bot.<profile>.service`（服务名和 bridge 自己装的相同）；每天的重启窗口 `dev.larkyard.bridge-restart` |

plist / unit 中注入当前 bridge profile 的环境变量（`LARK_CHANNEL=1`、`LARK_CHANNEL_HOME`、`LARK_CHANNEL_PROFILE`、`LARK_CHANNEL_CONFIG`、`LARKSUITE_CLI_CONFIG_DIR`），值与 bridge 注入的一致；同时注入包含 node、lark-cli 所在目录和 `~/.local/bin` 的 `PATH`。这些变量只设置，不 unset，也不改用普通 profile。**调整（§13）**：`~/.ticketd/runtime/node`、`~/.ticketd/bin/lark-cli` 存在时 PATH 用它们（固定目录，不跟着 nvm 切版本走），不存在时和以前一样从 PATH 上找。

### 3.1 Linux / WSL2（2026-11-02，T0016）

用户选了「先做 Linux 支持，Windows 通过 WSL2 来用」。目标是开源版在 Linux（含 WSL2）上能装能跑，macOS 行为不变（生成的 plist、worker 沙箱设置、ps / lsof 调用都和之前逐字节一致）。

- **平台差异只放四处**：`ticketd/host.py`（平台判断、lark-cli 数据目录、`/tmp` 的真实路径）、`ticketd/procs.py`（查看进程）、`ticketd/perms.py`（沙箱设置）、`scripts/install.sh` / `uninstall.sh`（服务）。**调整（§13）**：加上 `ticketd/bridge.py`（bridge 服务和每天的重启窗口：launchd / systemd 两套）。单测通过 mock `host.system()` 在任一系统上测两个平台的分支；CI（`.github/workflows/tests.yml`）在 ubuntu-latest 和 macos-latest 上跑全部单测，并在 Ubuntu 上真装一次 systemd 用户服务。
- **服务**：systemd 用户服务，模板 `systemd/ticketd.service.in`，环境变量和 plist 一致。对应关系：`KeepAlive`→`Restart=always`，`ThrottleInterval=10`→`RestartSec=10`，`ExitTimeOut=45`→`TimeoutStopSec=45`。`KillMode=mixed`：停服务时只给 ticketd 发 SIGTERM，由它按 §8 停掉 worker，之后 cgroup 里剩下的进程（含 worker 留下的后台进程）统一 SIGKILL。`LANG=C.UTF-8` 而不是 `zh_CN.UTF-8`：后者在很多 Linux 上没有生成，Python 会退回 ASCII，中文路径全坏。`TICKETD_PYTHON` 写进 unit（macOS 上只在显式指定时写进 plist，不指定时 plist 不变）。`StandardOutput=append:` 要 systemd ≥ 240。unit 放在 systemd 用户实例自己的 `XDG_CONFIG_HOME`（从 `systemctl --user show-environment` 取，默认 `~/.config`）下，不看当前 shell 的。直接拒绝安装的：WSL1（没有 systemd，Claude Code 沙箱也不支持）、WSL2 里 node / lark-cli / claude / codex 解析到 `/mnt/<盘符>/` 下的 Windows 版、没有 systemd 用户实例（WSL2 要在 `/etc/wsl.conf` 开 systemd）、路径里有空白和 `% $ " \`（systemd 会把它们当成特殊字符，不值得逐个转义）。只警告的：缺 bwrap / socat、AppArmor 限制用户命名空间、没开 linger。
- **进程（§8 的孤儿清理、重启恢复）**：Linux 上直接读 `/proc`，不依赖 ps / lsof（精简系统常常没有 lsof，procps 的 ps 也没有 `-E`）。按环境变量标记时用 `/proc/<pid>/environ` 按整条精确匹配 `TICKETD_SPOOL=<spool>`，不受 macOS 15 那种「系统程序的环境变量看不到」的限制。按目录补的那一道里，「被收养」在 Linux 上指父进程是 PID 1 或 ticketd 自己的某个祖先进程：worker 是 ticketd 的子进程，它留下的孤儿只会被 PID 1 或 worker 祖先里的 subreaper 收养，ticketd 作为用户服务时就是 `systemd --user`；按祖先判断而不按进程名猜，没开 systemd 的 WSL（`init(Ubuntu)`、`SessionLeader`）也认得出。只看本用户的进程，跳过僵尸；父进程在 PID 命名空间之外（ppid 为 0）的不算被收养。和 macOS 一样有一个残余风险：用户自己在工单目录里启动、又脱离了终端的后台程序（比如在工单目录里 `code .` 起的编辑器主进程、tmux 服务端）也会被当成残留清掉。
- **worker 沙箱（§6）**：Claude Code 和 Codex 在 Linux 上都用 bubblewrap。lark-cli 在 Linux 上不用系统钥匙串，凭据和主密钥是 `$LARKSUITE_CLI_DATA_DIR/lark-cli`（没设为 `~/.local/share/lark-cli`）里的文件，这个目录替代 `~/Library/Application Support/lark-cli` 进入 allowWrite 和 Codex 的可写根；`/tmp` 替代 `/private/tmp`；不写 `allowMachLookup`（mach 服务只有 macOS 有）。`failIfUnavailable` 保持开着：缺 bubblewrap / socat 时 worker 起不来，而不是裸跑。bubblewrap 绑定不存在的路径会让命令起不来，所以 Linux 上 lark-cli 目录、`$TMPDIR` 这类附带路径不存在时不写进 allowWrite / 可写根（工单目录和授权目录照写；macOS 不过滤）。Codex 的 `<grant>/.git` 可写根照旧按 `TICKETD_CODEX_GIT_WRITABLE` 加。
- **还没实测（只有单测和 CI）**：bubblewrap 沙箱下 worker 的读写边界、Codex 在 Linux 上是否也把 `.git` 设为只读、worker 沙箱里 lark-cli 的只读操作、WSL2 关掉终端后发行版是否会被停掉。这些需要在真实 Linux / WSL2 上按 §9 V1 补测，结论写回 §6。

## 4. 数据模型

```sql
tickets(
  id TEXT PRIMARY KEY,            -- T0001
  slug TEXT, title TEXT,
  chat_id TEXT, origin_message_id TEXT,
  request_text TEXT,              -- 用户原话，原样保存
  router_note TEXT,               -- ≤30 字，可空
  ack_text TEXT,                  -- 开工确认句（主会话撰写）
  workdir TEXT,
  session_id TEXT,                -- claude 会话 UUID
  state TEXT,                     -- queued|running|idle|held|stopped|failed
  last_report_id INTEGER,
  created_at, updated_at
)
grants(ticket_id, path, created_at)                -- 额外可写目录
bindings(message_id PRIMARY KEY, ticket_id)        -- 原消息 / 确认 / 结果消息 → 工单
inputs(id, ticket_id, kind, text, source_message_id, status, created_at)
       -- kind: assignment|followup|recovery|grant|release
       -- status: pending|delivered
reports(id, ticket_id, status, summary, artifacts_json, ask, created_at)
       -- status: ready|blocked|failed|dropped|waiting|progress
outbox(id, ticket_id, reply_to, text, status, attempts, next_at, last_error, sent_message_id)
       -- status: pending|sent|dead
locks(path PRIMARY KEY, ticket_id)                 -- 项目写锁
```

## 5. 工单生命周期

### 5.1 派单

主会话判断消息需要超过 1 分钟时执行：

```
ticket new --chat <chat_id> --message <om_id> --title <标题> --slug <短名> \
  --request-file <原话> --ack "开工：动 X，预期 Y" [--note <≤30字>] [--grant <dir>]...
```

- CLI 写入 `tickets`、`grants`、`bindings(origin)`、`inputs(assignment)`，然后打印确认文案，由 bridge 会话原样作为回复发出。**调整（T0007）**：确认文案只有一行「🎫 已开单 T0001「标题」，进度看下方状态卡」；开工句（作为「目标」）和可写目录只写在状态卡上，不再在两条消息里重复。
- **调整（2026-11-01，写计划时）**：确认、停止、暂停、放开类消息都由 bridge 会话自己回复，ticketd 发件箱只发 worker 汇报，避免同一事件出两条消息。用户引用 bridge 确认消息追问时，靠消息里的工单号关联（§5.4）。
- 不到 1 分钟的小事，主会话直接做，不开单。

### 5.2 调度

服务每秒检查一次：

1. **启动条件**：工单 `queued`，或者 `idle` 且有 `pending` 输入；运行中的 worker 少于 3 个；该单所有 grant 路径都能拿到写锁。拿不到锁的单继续排队。
2. **首次运行**：建工单目录，执行
   `claude -p --session-id <uuid> --output-format stream-json --verbose --append-system-prompt-file prompts/worker.md --settings <生成的权限 JSON> --permission-mode dontAsk "<任务卡>"`，
   cwd 为工单目录。
3. **续做**：执行 `claude -p --resume <uuid> ... "<输入卡>"`，把所有 `pending` 输入按顺序合成一张卡。
4. **结束处理**：输入在进程启动时标为 `delivered`。进程退出后读取最新汇报：
   - 有汇报：状态转为 `idle`（`ready`、`waiting`、`blocked`、`dropped`）或 `failed`。
   - 没有汇报：记一条 `failed` 汇报，附上 stream 里最后一段 assistant 文本。
   - 释放写锁。
   - **调整（2026-11-19，外部评测）启动失败不吞输入**：输入改为在 worker 进程真的起来（`Popen` 返回）之后才标 `delivered`，和记 pid 在同一个事务里；原来在 `Popen` 之前就标掉，worker 没起来时追问已算投递，用户回复「继续」后任务卡里没有它。起不来（任务卡写不进、程序不存在等）或起来之后记库失败（杀掉进程）时，输入保持 `pending`，记 `failed`「无法启动 worker」并带「回复继续」，工单置 `auto_halted=1` 停止自动调度（不会每个 tick 再试一次、再报一次）。
   - **停止自动调度（`tickets.auto_halted`，2026-11-19）**：`failed` 的单原本只要还有 `pending` 输入就会被调度器选中。现在 `auto_halted=1` 的单不参与自动调度，留着的输入原样保留；用户追问、放开 / 恢复、授权、换会话、批准沙箱外执行时清零，留着的输入和新消息一起投递。
5. **任务卡内容**：原话、路由备注、可写目录、工单号、工单目录、汇报方式。不含任何主会话写的计划。
   - **调整（2026-11-07，issue #21）**：Claude 单的任务卡多一行沙箱事实（只能写可写目录和临时目录、出网放开、本机端口 / unix socket / bind 被拒）和这张单的设置文件路径（`~/.ticketd/settings/<id>.json`）；worker 提示词加一节「被拒绝长什么样」，要求把失败归因到沙箱或系统权限之前先在工单目录里做一次对照。起因：另一台机器上 worker 把网络盘的间歇 `EINTR` 当成 TCC，烧掉一整轮并让用户去改系统设置。不做「blocked 但被拒操作为 0」的核对：`permission_denials` 只统计 Claude Code 权限规则的拒绝，沙箱在系统层的 EPERM 不计入，拿它核对会误报。

### 5.2.1 执行器（2026-11-01，T0004）

每张单在建单时定下执行器（`tickets.executor`，`ticket new --executor claude|codex`，默认 `TICKETD_EXECUTOR`，未设为 `claude`），之后不变——会话不能跨执行器迁移。主会话只在用户明确说「用 codex」时加 `--executor codex`。

- 代码在 `ticketd/executors.py`。调度器只认 Claude stream-json 的事件形状；Codex 的 `codex exec --json` 事件由 `CodexNormalizer` 翻译成同一形状（`thread.started`→`system/init`，`item.started/completed`→`tool_use`/`tool_result`，`agent_message`→assistant 文本，`turn.completed/failed`→`result`），所以卡死检测、状态卡、linger、重试逻辑两边共用。`.stream.jsonl` 存的始终是原始输出。
- **Codex 首轮**：`codex exec -C <工单目录> --json --skip-git-repo-check <沙箱 -c 参数> --disable shell_snapshot -c developer_instructions=<worker.md + worker.codex.md> -`（任务卡走 stdin）。
- **Codex 续做**：`codex exec resume <thread_id> --json …同上… -`。`exec resume` 不接受 `-s/-C/--add-dir`，沙箱参数一律走 `-c`。
- **会话 ID**：Codex 不能预先指定。建单时仍写一个 uuid 占位；首个 `thread.started` 到达时回写真实 `thread_id` 并置 `session_started=1`。首轮在拿到 thread_id 之前临时失败：按「会话未建立」重跑任务卡。
- **重启恢复的 pid 校验**：Codex 认「工单目录」或「thread_id」任一出现在命令行里（首轮进程拿到 thread_id 后命令行里仍只有 `-C <工单目录>`）。
- 配置：`TICKETD_CODEX`（codex 路径；launchd 的 PATH 里没有 nvm，要写绝对路径）、`TICKETD_CODEX_MODEL`、`TICKETD_CODEX_HOME`（ticketd 专用 CODEX_HOME，放 execpolicy 规则；空则沿用 `~/.codex` 且不写规则）、`TICKETD_CODEX_GIT_WRITABLE`（默认 1）。

### 5.3 汇报（worker 收尾）

```
ticket report --status ready|blocked|failed|dropped|waiting \
  --summary "<一句话结果>" [--artifact <路径或链接>]... [--ask "<一个具体问题>"]
```

工单号从环境变量 `TICKETD_TICKET` 读取。服务把每份汇报转成一条发件箱消息，按状态套用模板，回复用户原消息：

- `✅ 完成：…` / `⚠️ 受阻：… → 需要你：…` / `❌ 失败：…` / `⏳ 等待：…`
- `dropped` 默认不发消息，只记入日志。

**调整（T0017）**：本轮有状态卡时，结果卡直接覆盖到状态卡上，不另发消息，详见 §7.1。**调整（T0007）**：终态汇报发的是一张结果卡片（新消息，有推送提醒；header 按状态着色，正文是一句话结果、「👉 需要你」、产物，页脚是阶段轨迹）。飞书永久拒收卡片时改发同内容的文字兜底（幂等键加 `-t`），结果不会丢。结果消息发出后，它的 message_id 写入 `bindings`。`progress` 只记库、刷新状态卡（§7.1），不发消息；带 `--ask` 的 progress 才推消息。

**调整（T0005）**：结果卡片直接显示图片产物（png/jpg/jpeg/webp/gif，最多 6 张，每张上方一行文件名；📎 路径列表照旧）。模板里放带 `ticketd_upload` 的占位，发件箱发卡前用 `lark-cli im images create --as bot` 上传换成 `img_key`，并把换好的卡写回发件箱（重试不重复上传）。守护进程不在沙箱里而路径由 worker 控制，所以只上传工单目录内、非符号链接、非硬链接、≤10MB、文件头确认是图片的普通文件，且上传的是读出字节的私有副本。任一张上传失败只删掉这张图并记告警，结果照常发出。本轮有状态卡时，换好图片的结果卡同样由发件箱覆盖到状态卡上（§7.1）；状态卡自己的刷新（`card.refresh`）不经发件箱，不显示图片。

**调整（T0010）**：支持视频产物（`.mp4`，每份汇报最多 2 个），在结果卡里点开播放。飞书卡片 JSON 2.0 没有视频组件，所以模板放 `ticketd_video` 占位；发件箱发卡前先以**用户身份**把视频传到用户自己的云空间（`lark-cli drive +upload --as user --file ./video.mp4 --name <工单号>-<文件名>`，可用 `TICKETD_VIDEO_FOLDER` 指定文件夹；文件归用户所有，用户本人能打开），返回里没有链接时再用 `drive metas batch_query --as user`（`with_url`）查，只接受 `https://*.feishu.cn|larksuite.com` 的链接；然后把占位换成可点击的封面 + 「▶ 播放视频」按钮（open_url），点开走飞书云空间的播放器。封面约定为**与视频同目录同名的图片**（`视频-720p.mp4` → `视频-720p.jpg/.jpeg/.png/.webp`），按图片的规则上传；同名封面图即使也列为产物也不再重复显示。换好的卡写回发件箱，重试不重复上传；本轮有状态卡时同样覆盖到状态卡上。

**上传云空间失败**（用户身份不可用、超过 100MB 等；超过 20MB 时 lark-cli 自动分片上传，超时按大小放宽）时退回视频消息：卡片里放封面 + 一行说明，并在卡片之后补排一条发件箱消息（`outbox.media_json`，`report_id` 为空，id 更大所以排在卡片后面）：`lark-cli im +messages-reply --as bot --video ./video.mp4 --video-cover ./cover.<ext>`，没有封面时退成 `--file`。安全规则同图片：只认工单目录内、非符号链接、非硬链接、文件头是 mp4（`ftyp`）的普通文件（云空间 ≤20MB，视频消息 ≤30MB），交给 lark-cli 的是私有临时副本（固定文件名 `video.mp4`），超时 180 秒。本地检查不过或飞书永久拒收时，改发一行文字「🎬 … 的视频没能直接发出，文件在：…」（幂等键加 `-t`）；网络失败照常退避重试（同一幂等键），连续失败 3 次后改发兜底文字（每次尝试会阻塞守护进程最多 180 秒，并挡住同单后面的消息）。读文件时从工单目录起逐级 `openat + O_NOFOLLOW` 打开（`safefs.open_read_under`），检查之后中间某级目录被换成符号链接时直接打开失败；打开后再用 F_GETPATH（Linux 为 /proc/self/fd）核对一次作为第二道防线（图片同样适用）。视频消息的 message_id 也写入 `bindings`，引用它同样能追问。

**结果卡改版（画廊风）**：worker 可控文字仍一律 plain_text。① **有图片/视频的卡（画廊）**：媒体占主体放最上面——视频是一个可点容器（不裁剪的圆角大封面 + 通栏「▶ 播放视频」按钮，点哪都打开云空间播放）；单张图不裁剪、圆角、可点开放大；多张图两列网格、不裁剪，每张下面一个圆形编号（`<number_tag>`，内容是我们生成的数字，方便回复「2」），奇数张补空列；卡里已有视频时，其余图作三列小缩略图跟在后面。媒体下面只留**一行结论**（摘要第一句，`heading-4`，最多 2 行），再下面是「👉 需要你」浅色块（blocked 橙、其余蓝），然后链接按钮和稿子按钮，最后一个默认收起的「详情」面板（摘要其余部分 + 本机文件路径）。② **没有媒体的卡（文档）**：结论放大 → 需要你 → 细节 → 链接 / 稿子按钮 → 收起的「📎 本机文件」。页脚过程轨迹不再加分割线。图片占位可以嵌在 column 里，发件箱递归替换，上传失败的图换成一行灰字「（图 N 未能显示）」。

**调整（2026-11-21，T0057）卡片正文只放结论和需要你做的那一步**：用户反馈「输出一大堆代码不知道要我干啥」，技术内容有文档按钮，想看的人会去点。① worker 提示词规定 `--summary`、`--ask` 各最多 2 句，不写命令、代码、路径、提交号、日志、原因分析和审查细节，这些写进 `结果.md` 用 `--artifact` 附上（有文档按钮）。② 模板兜底（`templates.brief`）：摘要第一句照旧作结论；其余部分只有「一句、≤80 字、不含技术内容」时才在正文显示，否则收进默认收起的「详情」。「需要你」≤2 句、≤120 字、不含技术内容时原样显示，否则只露第一句（代码块之前，超长截断）并标「（完整说明见下方「详情」）」，全文进「详情」；dropped 的说明同理。有稿子（会变成文档按钮）时「详情」标题写「详情 · 完整内容见文档」。技术内容的判断（`templates.technical`）：代码块、行内代码、路径（`/x/…`、`~/x/`、`./x/`、带扩展名的相对路径，网址不算；「需要你」里的路径不算，要授权的目录就是问题本身）、`--参数`、命令提示符开头的行、提交号。摘要里有代码块时，结论取代码块之外的第一句。③ 需要你决定的文字提醒（`ping_message`）只带问题第一句。④ 文字兜底消息（卡片发不出去时）不折叠，照旧全文。状态卡运行中的内容（阶段、最近动作、目标、可写范围）不变。

**发送链路加固（T0012 对抗审查 F1/F2）**：① 结果卡发送遇到**任何** `error.type == "api"` 的飞书业务错误（不再只认 `PERMANENT_CODES` 白名单）都当「卡片被拒」，马上改发文字（幂等键 `-t`）；结果覆盖状态卡时遇到业务错误，不在状态卡上反复重试，改发一张新的结果卡（再被拒就走文字兜底）；卡片/视频行重试满 24 小时、标 dead 之前，先试一次文字兜底，送到了就算 sent。② 发卡前上传图片/视频遇到网络/超时（错误信息含「调用 lark-cli 失败」或 `"type": "network"`）：**不降级**，只把已成功的 img_key / 云空间链接写回发件箱（链接记在视频占位的 `ticketd_video_url` 上，下次只补封面、不重传视频），一行里第一次瞬时失败就停止后续上传，整行按 attempts 退避重试；连续 `MEDIA_MAX_ATTEMPTS`（3）次后才降级（图换灰字、视频另发）。文件不合格、飞书明确拒收等非瞬时失败照旧立即降级。

**恢复（真实守护进程 + 故障注入实测后）**：① 回复目标被撤回（飞书 230011）：这一单还没发出的消息全部改为直接发到会话（`reply_to` 换成 `oc_` 会话 id，`lark-cli im +messages-send --chat-id`），卡片/视频原样改投，不退成文字；状态卡同样改投。② lark-cli 标了 `"retryable": true` 的业务错误（如限流）按瞬时失败退避，不当成卡片被拒。③ 降级改为「失败满 3 次**且**这一行已等了 `MEDIA_DEGRADE_AFTER`（600 秒）」（原来断网约 35 秒就降级）。④ 每行每个 tick 开始新上传的时间上限 `MEDIA_BUDGET`（60 秒），超了把进度写回、下个 tick 接着传。⑤ 云空间视频按「工单 + 内容 sha256」缓存链接（表 `drive_links`，7 天），同一个视频再汇报不重复上传；上限 100MB，超时按大小放宽（每 MB 加 3 秒）。⑥ 发送异常的毒丸行标 dead 之前先试一次文字兜底；lark-cli 输出按 `errors="replace"` 解码。已知：上传进行中守护进程被强杀时，重启后会再传一次，云空间里多一个文件。

**媒体通道与 SQLite 异常分类（2026-11-04，T0022，回应外部代码审查）**：① `MEDIA_BUDGET` 只决定还开不开始下一次上传，一次云空间上传本身可以跑到 `_drive_timeout`（100MB 约 420 秒），稿子导入每个最长 90 秒，原来这段时间整个主循环（派单、状态卡、心跳、其他工单的投递）都停着。现在发件箱分两条通道：守护进程里的独立线程 `daemon.MediaLane` 用自己的数据库连接跑 `outbox.flush(lane="media")`，只做**慢的准备**——结果卡里图片/视频占位的上传（写回发件箱）和稿子导入（进 `published` 缓存）；视频消息（lark-cli 上传和发送是同一条命令）整条在这里发。主循环跑 `flush(lane="fast")`，负责其余所有发送，包括把结果覆盖到状态卡：这一步必须和 `card.refresh` 在同一线程里，否则两边 PATCH 同一张卡会互相覆盖；主循环里只用缓存里的稿子链接，不导入。同一工单仍按 id 顺序（每单只取最早一条待发消息，它没准备好，同单后面的消息就等着）；两条通道用进程内认领集合互斥，认领后再按库里最新状态核一次。导入失败的稿子不会让一行一直留在媒体通道（进程内记一次，重启后再试）。`MEDIA_BUDGET` 保留，只用于媒体通道里各行轮流。媒体线程意外退出时主循环下一轮重启它并告警；停止服务时最多等 5 秒，不等正在进行的上传。② `flush` 原来把所有 `sqlite3.OperationalError` 都当「库被锁」，24 小时内每 30 秒重试且 `break` 掉整个队列。现在 `db.is_busy` 只认 SQLITE_BUSY / SQLITE_LOCKED（Python 3.11+ 看错误码，3.9 看 sqlite 的固定报错文字），锁等待照旧退避；磁盘 I/O、只读、库满、缺表缺列等故障告警「数据库故障」，这一行单独退避（30 秒起翻倍、最多 10 分钟，同时记在内存里，库写不进去也生效），不挡其他工单；同一行连续 3 次且已等满 10 分钟仍出错才按毒丸处理（先文字兜底）。库连退避都写不进去时本轮暂停这条通道；已经发出去但结果写不进库（含毒丸的文字兜底）时只在内存里退避，到点用同一幂等键重试，不再每轮重发。稿子缓存读写遇到数据库错误不再当成「导入失败」吞掉，交给上面的分类。

**调整：媒体通道改成有并发上限的任务池（2026-11-04，T0023，回应外部代码审查 P2）**：T0022 的媒体通道只有一个线程、逐行串行，解决了「一个慢上传拖住整个守护进程」，没解决「一个慢上传拖住其他工单的媒体交付」：A 传 100MB 视频的 7 分钟里，B 的图片、C 的稿子也要排队。现在 `daemon.MediaLane` 是一个调度线程加若干任务线程：调度线程每秒用 `outbox.media_jobs` 取出要准备媒体、没人在处理、不在退避中的行，认领后各开一个任务线程跑 `outbox.prepare`（每个任务自己的数据库连接，写入仍是短事务，做完就关）。① 并发上限 `TICKETD_MEDIA_WORKERS`（默认 2）；其中传视频的（视频消息，或卡片里还有没拿到云空间链接的视频占位，`outbox.is_video_job`）另有上限 `TICKETD_MEDIA_VIDEO_WORKERS`（默认 1，不超过总数），限制带宽、内存和飞书限流。默认配置下一个大视频在传时，另一个名额留给图片和稿子。② 同一工单仍严格按顺序：候选仍是每单最早一条待发消息，调度时再按工单去重（同一工单同时最多一个任务，含下面说的卡死任务）。③ 轮转：候选按「上次开始准备的时间」排序，从没处理过的先上；一张多视频卡用完 60 秒预算回到队列后排在别的行后面（原来总是 id 小的先上）。④ 某个任务遇到网络/超时或库被占用，调度暂停一轮（`interval`，默认 1 秒）再派新任务，对应原来单线程里的「本轮不再试其余行」。⑤ 卡死检测：任务跑满 900 秒（正常最长约 9 分钟：100MB 上传 420 秒 + 查链接、封面）还没结束，告警并让出它的名额，这一行保持认领、不会被重复处理，等它结束（再告警一次）或重启服务；调度线程 120 秒没转一圈，主循环告警（恢复时也告警一次）。Python 线程杀不掉，所以只能让出名额、告警，不能强停。⑥ 停止服务时最多等 5 秒（调度线程和任务线程共用），不等传完的上传，行为同前。`outbox.flush(lane="media")` 保留为单线程版本（测试用）。

**调整：稿子导入失败写在卡片上（2026-11-08，issue #12）**：原来导入失败只写告警，结果卡上没有「打开」按钮也没有任何说明。现在失败原因按文件版本（路径 + 修改时间 + 大小）记进 `doc_failures` 表；发结果时，没有缓存链接、但当前版本有失败记录的稿子，在稿子按钮的位置加一行灰字「📄 <文件名> 没能导入云文档：<原因>。本机文件：<路径>」，文字兜底里同样带上。原因取 lark-cli 报错 JSON 里的 `message`（网络 / 超时统一写「调用 lark-cli 失败（网络或超时）」），最多 80 字。读失败记录是只读的，表不存在时当作没有失败。

**调整：改过的稿子覆盖原云文档（2026-11-07，issue #13）**：稿子缓存键是「路径 + 修改时间 + 大小」，文件一改就重新 `drive +import` 一份新文档，另一台机器上一份 `结果.md` 被导入了 7 次，云空间里堆满同名文档。现在新表 `doc_targets(ticket_id, path, url)` 记每张单每个稿子路径当前对应的云文档：同一张单同一路径的稿子再次汇报且内容变了时，`.md` / `.markdown` / `.txt` 用 `lark-cli docs +update --as user --command overwrite --doc-format markdown --content @<文件>` 整篇覆盖原文档（链接不变，旧版本在飞书文档历史里，旧卡片上的链接也指向最新版）；其他格式（docx / xlsx / pptx）或覆盖失败（原文档被删、没有权限等，写告警）时另导入一份，名字后面加「（MM-DD HH:MM）」以便区分，并把它记成新的当前文档。

**调整：缺飞书用户身份授权时机器人发链接（2026-11-27，T0107）**：行为以 `openspec/changes/add-user-auth-link/` 为准。稿子导入、视频传云空间是仅有的两处用用户身份的地方，安装向导第 ⑤ 步默认跳过、`ticket doctor` 不查，同事装完自检全绿、出稿才失败，卡片上是一行英文，同一原因每次汇报都报一次（T0106）。现在（`ticketd/userauth.py`）：
- **查**：守护进程启动后约 20 秒查一次、之后每 30 分钟查一次（`lark-cli auth status --json` + `auth check --scope "docs:document.media:upload docs:document:import drive:file:upload" --json`，本地、不联网；只查缺了就一定失败的三项），结论记在 `kv userauth.state`（ok / missing / expired / scope / unknown）；出稿遇到授权类失败时直接记成没授权，之后只有重新授权过（token 授权时间更晚）或下一次用户身份调用成功才转回 ok。查不到（钥匙串打不开等）不覆盖已知结论。`ticket doctor` 多一项 `user_auth`（没授权是 ❌，查不到是 ⚠️），随心跳的 `doctor.failed` 上报控制台；终端里的 `ticket doctor` 会现查一次。
- **发链接**：`lark-cli auth login --no-wait --json --scope "docs:document.media:upload docs:document:import docx:document:write_only docx:document:readonly drive:file:upload drive:drive.metadata:readonly"` 拿到 `verification_url` 和设备码，用 bot 私信主人（`@owner`）一张「去授权」卡片（文字兜底带链接，写明几分钟内有效）；守护进程在后台线程里 `auth login --device-code <码> --json` 等用户同意（最多到链接过期，重启后没过期的接着等）。什么时候发：本机从没发过时一发现没授权就发一次（装好后的那一次，已经装着的同事升级后也会收到）；之后只在出稿遇到授权类失败、没有没过期的链接、离上次 ≥30 分钟时再发，一次汇报多份失败只发一条。设备码只在 kv 里，不进告警、事件和诊断包。
- **补发**：授权类失败的稿子记进 `auth_backfill(ticket_id, path, reply_to)`；授权完成（链接里同意，或终端授权后被查到）后，7 天内的按发布规则（`publish.candidates`）重新核对路径再导入，每张单一条卡片回复在原结果下面（每份一个「打开」按钮），已经有链接的不重复导入；主人另收到一句「授权好了」。授权后还缺范围（应用没开通）时私信说明缺哪几项。
- **原因说中文**：`doc_failures.reason` 改为按 lark-cli 的 `error.type / subtype` 归类的中文（`userauth.reason_text`）：授权类「飞书用户身份没授权或已失效。机器人已私信你授权链接，点开同意后会自动补发这份文档」；应用没开通「请管理员在飞书开放平台给应用开通」；网络「下次汇报时会自动重试」；其余「飞书没能导入文档（原因：≤60 字）。可以对机器人说「把诊断包发给管理员」」。原文照旧写 `alerts.log`。
- **只报一次**：授权类失败的指标记 `doc:auth` / `video:auth`（不映射成出错记录，匿名统计只认 ok / fail 也不算它），这个原因只经自检项 `user_auth` 报一次。
- **诊断包**多一节「飞书用户身份和最近的上传失败」：状态、有没有没过期的链接（不带设备码）、待补发几份、最近 7 天 `doc_failures` 10 条、`alerts.log` 里导入 / 上传 / 授权相关的最近 15 行原文。
- `ticket setup --user-auth` 和向导第 ⑤ 步的 `auth login` 带上同一组范围，成功后让守护进程马上查一次。

- **调整（2026-11-01，T0005）**：worker 运行期间每个 tick 都收一次 `.reports/`，终态汇报马上发出，不再等进程退出。起因：T0004 的 worker 03:51 已汇报，claude 进程却在等后台子代理，到 04:09 才退出，期间结果没发、`ticket show` 一直显示 running。现在还加了一条：已有终态汇报、且最后一个 stream 事件是 `result`（本轮已结束、进程只是挂着）持续 `TICKETD_LINGER_GRACE`（默认 60 秒）后，结束该进程。
- **调整（2026-11-07，运行复盘）等后台任务不算卡住**：Claude Code 的 worker 可以把长任务放到后台（Bash `run_in_background`、后台子代理），然后结束本轮去等；任务结束时它发 `system/task_notification`，进程唤醒模型接着做，所以进程在 `result` 之后还挂着。原来调度器把这段等待当成「没有新动作」，10 分钟后标「疑似卡住」：T0005 第 20 轮在后台出 19 个镜头时被标卡住，用户以为卡死停了单，后台出图一起被杀，只好另开 T0018 收拾残局。现在调度器按 `task_started` / `task_notification` 跟踪还没结束的后台任务；本轮已结束（最后一个事件是 `result`）、没交终态汇报、还有后台任务时，不判卡住，把任务描述写进 `tickets.bg_wait`，状态卡显示「⏳ 等后台任务」和「本轮已结束，等后台任务：<描述> · N 分钟前开始等，结束后自动接着做」，`ticket show` 同样显示。`task_notification` 也算一次动作，唤醒后卡住计时从这里重新算。单轮超时照旧兜底。已交终态汇报时 linger 规则不变（后台任务照旧被结束）。worker 提示词相应说明了这种用法。
- **调整（同上）等后台任务时模型接口报错**：本轮 `result` 是 `is_error`（如网关返回 403 余额不足）而后台任务还在跑时，进程要等任务结束才会退出。原来用户 10 分钟后只看到「疑似卡住」，几十分钟后才看到失败。现在马上推一条提醒（每轮一次）：报错原文、是不是账户级故障、后台任务还在跑，赶在任务结束、模型被唤醒之前处理好就能接着做。
- **调整（同上）产物在汇报时先检查**：worker 在沙箱里执行 `ticket report`（终态，不含 progress）时，图片/视频产物不在工单目录内或不存在的，当场报错并提示复制进工单目录后重报。原来这些图在发件箱被静默丢掉（T0005 两次选图卡里一张图都没有，产物在授权目录里）。安全检查仍以发件箱为准，这里只是提前告诉 worker。

### 5.4 追问与续做

- 用户引用某条消息时，主会话执行 `ticket lookup --message <quoted_id>`：命中就执行 `ticket send <Txxxx> --message <om_id> --text-file <原话>`；没命中则看被引用内容里有没有工单号（如 T0001），有就转给该单；都没有就按新消息处理。
- 没有引用的消息，主会话只判断新活还是闲聊，**不按话题相似度去猜工单**。
- worker 判断追问超出本单范围时，报 `dropped`，并附 `--ask` 说明原因。服务据此回复：「这条超出 T0001 范围（原因），不引用直接发我即可开新单。」
  （限制：服务无法唤醒主会话，所以退回的追问由用户重新发起。）

### 5.5 停止、暂停、授权

| 命令 | 效果 |
|---|---|
| `ticket stop <id>` | 结束 worker 进程，状态改为 `stopped`，回复「已停止」。stopped 是终态，之后的引用追问会被拒绝，需要开新单 |
| `ticket hold <id>` | 结束当前进程（会话保留），状态改为 `held`；新输入只排队不投递。追问和补充线索都不会解除暂停 |
| `ticket release <id>` | 状态回到 `idle`，加一条 `release` 输入：「用户已放开暂停」，然后投递排队中的输入 |
| `ticket restart <id>`（**2026-11-07 新增**，issue #14） | 换新会话从头续做：新的 session_id、`session_started=0`，工单号、目录、授权不变；加一条 `fresh` 输入（说明换了会话、先读 `结果.md` / `过程.log`，并附上历次追问，最多最近 10 条），首轮任务卡照常带上原话。运行中或排队中拒绝 |
| `ticket grant <id> <dir>` | 增加可写目录，加一条 `grant` 输入，重新生成权限配置 |
| `ticket approve <id>`（**2026-11-07 新增**） | 批准这张单最新一份待批准的沙箱外执行申请（见下面的调整）。工单要在 `idle` / `failed`；没有待批准的申请时报错 |
| `ticket grant <id> --local-net` / `--socket <路径>`（**2026-11-07 新增**，issue #20） | 文件系统以外的沙箱授权，记在 `net_grants` 表，加一条 `grant` 输入，下一轮生效。`--local-net` → `sandbox.network.allowLocalBinding: true`；`--socket` → `sandbox.network.allowUnixSockets`（路径是链接时连同解析后的路径一起写）。一次只授权一样；只对 Claude 单生效（Codex 沙箱没有对应开关，CLI 会提示） |

只有用户在对话里明确说了，主会话才执行 stop、hold、release、restart 和 grant。

**调整（2026-11-08，issue #19）worker 的补充环境**：worker 的环境从守护进程继承（launchd / systemd 给的最小环境），PATH 比用户登录 shell 短、没有代理和镜像源设置，worker 找不到 docker 之类的工具、下载被代理 407。新增 `TICKETD_WORKER_PATH`（追加到 PATH，已有的目录不重复）和 `TICKETD_WORKER_ENV`（变量名白名单，值先取服务环境、再取 `config.env`；没有值的跳过），由 `worker.user_env` 统一加到 worker 和沙箱外执行的脚本上；`ticket show` 显示 worker 实际的 PATH。不跑登录 shell 取环境（Codex 那边已经因为 shell 快照踩过坑），只做显式的白名单。实测（Claude Code 2.1.316）：普通变量能原样进到沙箱里的命令；`HTTP(S)_PROXY`、`ALL_PROXY`、`NO_PROXY` 等在沙箱里被换成 Claude Code 自己的本机代理，所以代理变量只对 claude 进程本身和沙箱外执行的脚本有效。

**调整（2026-11-07）沙箱外执行通道**：运行复盘里 55 次 blocked/waiting 有 22 次是 worker 请主会话或用户「在沙箱外替我跑一下」（git push / 开 PR、无头 Chromium 渲染、部署重启），主会话临时代跑，没有记录，也没有限制。现在把它做成一条显式的通道：
- 申请：worker 把命令写成工单目录里的脚本（≤8KB、UTF-8），`ticket report --status blocked --summary … --run <脚本>`。CLI 在沙箱里读出脚本，连内容一起写进待收件；守护进程收汇报时存进 `runs` 表（`status=pending`，记 `report_id`）。没带 `--ask` 时默认问「要我在沙箱外执行这段脚本吗？同意就回复「执行」…」。`--run` 只能配 `blocked`。
- 展示：结果卡在「👉 需要你」下面放一个默认展开的面板，标题「🖥️ 申请在沙箱外执行（Rn · 路径）」，正文是完整脚本（plain_text，原样含缩进），下面一行说明批准后会发生什么；文字兜底消息里同样带脚本。**调整（2026-11-21，T0057）**：卡片上只露一句「🖥️ 申请在沙箱外执行：<要做什么>」（取脚本开头第一行说明注释；没写时提示「批准前请展开看完整脚本」）和批准后会发生什么，完整脚本收进默认**收起**的面板「查看完整脚本（Rn · 路径 · N 行）」（`element_id=run_script`，徽标、稿子按钮不放进它）。起因是用户嫌卡片里一大段代码看不懂要干什么。代价是用户可能不展开就批准：那一句说明是 worker 写的，worker 提示词要求它和脚本一致、脚本里不藏别的动作；文字兜底消息照旧带完整脚本。`ticket show` 显示「沙箱外执行 Rn（路径）：待批准 / 已批准 / 执行中」。
- 批准：只有用户明确同意时，主会话才执行 `ticket approve <id>`（同 grant）。新的终态汇报把之前没批准的申请标成 `superseded`；worker 开新一轮（用户追问等）时标成 `expired`，用户只能批准最新、仍然有效的那份。
- 执行：守护进程每个 tick 取 `approved` 且工单在 `idle`/`failed` 的申请，把**数据库里的快照**写到 `~/.ticketd/runs/R<n>.sh`（目录 0700、文件 0600，worker 写不到），`bash` 运行，cwd 为工单目录，环境为守护进程的环境加 `TICKETD_TICKET`、`TICKETD_RUN`，输出写到工单目录的 `沙箱外执行-R<n>.log`。执行期间工单 `state=running`（不改 `started_at`，状态卡保持上一轮的结果），不会同时起 worker；所以批准和执行之间、执行当中都没有 worker 代码在跑，执行的文件就是 worker 汇报时留下的。超过 `TICKETD_RUN_TIMEOUT`（默认 1800 秒）或用户停止 / 暂停时先 SIGTERM、10 秒后 SIGKILL（整个进程组）。
- 交回：结束后加一条 `run_result` 输入（退出码、用时、是否被中止、完整日志路径、输出末尾 3000 字），工单回到 `idle`，worker 下一轮接着做；被停止 / 暂停的，输入排队到恢复后。快照删掉。服务重启时还在 `running` 的申请标成 `interrupted`（命令行能对上时先杀掉进程组），交代给 worker「结果未知，先核对实际到了哪一步」。
- **调整（2026-11-09）活过守护进程重启**：原来服务停止时把正在跑的脚本 SIGTERM 掉、重启后一律记 `interrupted`（结果未知）；可部署就要重启守护进程，正在推送 / 部署的脚本会被打断在一半。现在脚本由包装命令运行（跑完把退出码写到 `~/.ticketd/runs/R<n>.exit`），守护进程停止时不杀它（它本来就在自己的进程组里）；重启后 `recover` 重新认领：进程还在（命令行对得上）就接着盯超时和停止 / 暂停，已经跑完的按退出码文件照常把结果交回 worker；只有既不在跑、也没有退出码文件的才记 `interrupted`。macOS 实测：launchd 停掉任务后，独立进程组里的子进程照常跑完。Linux 上 systemd 停服务会杀掉整个 cgroup（`KillMode=mixed`），脚本活不下来，重启后按 `interrupted` 交代。
- 安全边界：这条通道让用户批准过的脚本以用户身份在沙箱外运行，把关只靠用户看过脚本再说「执行」。用户看到的是脚本本身，脚本调用的工单目录里的其他文件不在审批范围内（卡片上写明）；worker 提示词要求脚本里不藏和说明不符的动作、不调用之后还会改的文件。

**调整（2026-11-07，issue #20）沙箱的本机网络和 unix socket 授权**：原来 `grant` 只能给可写目录，worker 连本机数据库、docker.sock、在本机起服务、无头 Chrome（要在系统临时目录里 bind ProcessSingleton 的 socket）都只能报 blocked 让用户自己去终端跑。字段和语义在 Claude Code 2.1.316（macOS）上实测：默认设置下连本机端口、连 unix socket、本机 bind、在临时目录 bind socket 全是 `Operation not permitted`；`allowLocalBinding: true` 后连本机端口和本机 bind 都通（它一次放开 bind/listen、入站和连 `localhost:*` 的所有端口，**不能按端口细分**，所以没有做 `--port`）；`allowUnixSockets` 里的路径（含子路径）下 socket 的连接和 bind 都通。无头 Chrome（2026-11-08 用正式版 Chrome 在 worker 沙箱里实测）：默认设置下死在 ProcessSingleton 的 socket bind（EPERM，和 #20 描述一致）；授权 `getconf DARWIN_USER_TEMP_DIR` 的 socket 后这一步能过，但随后写 `~/Library/Application Support/Google/Chrome/Crashpad` 被拒、并在 `base/mac/mac_util.mm` 的一处检查上以 EPERM 崩溃（关崩溃上报、改 HOME 都无效），所以**授权解决不了无头浏览器**，渲染走沙箱外执行通道。未实测：Linux（bubblewrap 下 worker 在独立网络命名空间里，`allowLocalBinding` 是否能连到宿主机 localhost 未知）。

**调整（2026-11-07，issue #17 #18 #14）**：
- 停止 / 暂停有交代：`ticket stop` / `ticket hold` 的输出（主会话原样回复给用户）多几行——停在哪个阶段、最近一步和多久前（运行中才有）、这一轮在工单目录里写了哪些文件（跳过隐藏文件和 `过程.log`，最多列 6 个）、怎么接着做、终端接管命令。信息都是 ticketd 已有的，不问 worker。原来停掉之后什么都不说，用户不知道能捡回什么。
- stopped 不再是绝对终态：`ticket release` 也能恢复已停止的单（回到 `idle`，加一条 `release` 输入，内容提醒 worker 上一轮是被停掉的、先核对做到哪一步），沿用同一个会话。引用追问仍然会被拒绝（提示先恢复），所以「停」的语义不变，只是多了一条显式的回头路。原来卡住时用户只能用 stop 救场，同一件事被迫开两张单、丢会话。
- worker 因不可恢复的 4xx（`API Error: 400/404/413/422`，如网关不支持 PDF 类型的 tool_result）失败时，失败卡带 `--ask` 提示可以说「换新会话重来」。

### 5.6 查询

| 命令 | 内容 |
|---|---|
| `ticket ls` | 列出所有工单：编号、标题、状态、可写目录、运行时长、最近汇报 |
| `ticket show <id>` | 工单详情，加上 `过程.log` 的最后 N 行 |

用户问进度时，主会话读这两个命令的输出，用一两句回答，不打扰 worker。

## 6. 权限边界

- 默认只能写工单目录，加上 `grants` 里的目录。
- 文件工具：生成的 settings 只对上述目录开放 `Edit` 和 `Write`；`Read`、`Glob`、`Grep`、`WebFetch`、`WebSearch` 除凭据目录外全部放开。
- shell：启用 Claude Code sandbox，文件写入只允许上述目录加临时目录。
- **凭据目录**（2026-11-22，T0064 调整）：`$TICKETD_HOME/console/`（设备令牌；以后 ticketd 代持的网关令牌也放这里，见 §12）worker 读不到也写不了，授权目录再宽也不例外。Claude worker 的设置里同时写 `sandbox.filesystem.denyRead` / `denyWrite`（管 Bash 及其子进程）和 `permissions.deny` 的 `Read` / `Edit` / `Write` / `NotebookEdit(//<目录>/**)`（管内置文件工具，`Grep` / `Glob` 跟 `Read` 规则走）；`TICKETD_HOME` 经软链时字面路径和真实路径都写。每轮写设置前先建好这个目录（700、空目录不算入网），因为 Linux 的 bubblewrap 只能遮住已经存在的路径。worker 里的 `ticket doctor` / `ticket console` 不碰这个目录，只说明看不到。Codex 的 `workspace-write` 不限制读，所以**本机入网后不起 Codex worker**（报「无法启动 worker」，提示改用 Claude 或先退出控制台）；Codex 0.134 的权限配置（permission profile）有禁读项，等实测后再放开。为什么改：这条在 T0046 是已知限制，网关上线后拿设备令牌能领网关令牌、花额度，性质变了（T0047）。实测：`scripts/selftest.py --only secrets`。
- worker 的 `ticket report` 写入工单目录下的 `.reports/` 待收件（环境变量 `TICKETD_SPOOL`），由 ticketd 按工单目录归属入库；worker 对数据库目录没有写权限。lark-cli 的 deny 规则只是防误发的护栏，不是安全边界。
- 未预先允许的操作，在 `dontAsk` 模式下会被直接拒绝，不会挂起。worker 应报 `blocked`，并用 `--ask` 说明需要写哪个目录。
- worker 禁用给用户发消息的命令：`lark-cli im +messages-send`、`lark-cli im +messages-reply`。对外沟通只走 `ticket report` 这一个渠道。读飞书文档等只读 lark-cli 操作允许。
- **V1 实测结论（2026-11-01，Claude Code 2.1.300，均已验证）**：
  - `--permission-mode dontAsk` 下未允许的操作立即拒绝，不挂起，并会出现在 result 事件的 `permission_denials` 里。
  - Edit/Write 规则必须写**真实绝对路径**，格式为 `Edit(//Users/...)`。macOS 的 `/tmp` 实际是 `/private/tmp` 的软链，规则对不上；工单根目录不放在 `/tmp` 下就没有这个问题。
  - 沙箱 `sandbox.filesystem.allowWrite` 对 shell 及其子进程（包括 python）都生效，`allowUnsandboxedCommands:false` 可防止绕开。
  - 沙箱默认禁止所有外网访问，需要配置 `sandbox.network.allowedDomains:["*"]`。
  - lark-cli 在沙箱里需要两项配置：一是 allowWrite 加上 `~/Library/Application Support/lark-cli` 和 `$LARKSUITE_CLI_CONFIG_DIR`（令牌刷新要写）；二是 `sandbox.network.allowMachLookup` 加上 `com.apple.trustd.agent`、`com.apple.trustd`、`com.apple.SecurityServer`、`com.apple.securityd`（TLS 证书校验要用）。配好后 lark-cli 可以正常读消息，**不需要把它排除出沙箱**。
  - `Bash(lark-cli im +messages-send:*)` 这类 deny 规则生效。
  - launchd 环境注入 §3 的环境变量后，`lark-cli im +messages-reply --as bot` 发送成功。发件箱使用 `--idempotency-key <outbox-id>` 防止重复发送。
- **Codex 执行器（2026-11-01，T0004，codex-cli 0.134.0 实测）**：
  - 沙箱：`sandbox_mode=workspace-write`，`writable_roots` = 工单目录 + grants + lark-cli 配置目录，`network_access=true`，`approval_policy=never`（越权直接失败、不挂起）。实测：工单目录、grant、/tmp 可写；可写根以外、家目录被拒；联网可用；`.reports/` 可写。
  - **grant 下的 `.git` 是只读的**（Codex 自带保护），授权仓库里 `git commit` 失败。默认（`TICKETD_CODEX_GIT_WRITABLE=1`）把 `<grant>/.git` 也列为可写根，实测 commit 成功、越界写仍被拒；设为 `0` 则保留 Codex 的只读保护。
  - 防误发：execpolicy 规则写在 `$TICKETD_CODEX_HOME/rules/ticketd.rules`，`lark-cli im +messages-send/+messages-reply` 等实测被拒。规则按词精确匹配，写不出 `/open-apis/im` 前缀，所以 `lark-cli api` 的 POST/PUT/PATCH/DELETE 全部拦住（比 Claude 宽）。被规则拒绝的命令不出现在事件流里。
  - 必须 `--disable shell_snapshot`：否则 Codex 重放用户登录 shell 的环境，PATH 被改写，`shell_environment_policy` 不生效。关掉后再用 `shell_environment_policy.exclude=["*TOKEN*","*KEY*","*SECRET*"]` 挡住守护进程环境里的凭据。
  - lark-cli 只读 API 在 Codex 沙箱里失败（exit=3），原因待查；同一命令在 Claude worker 沙箱里也失败（`keychain Get failed: keychain not initialized`，lark-cli 1.0.86），与执行器无关。

## 7. 可观测性

- 服务把 worker 的 stream-json 原样保存到 `<工单目录>/.stream.jsonl`，同时转写成人话写入 `<工单目录>/过程.log`。每行一个事件：时间、动作、对象、结果摘要。
- 终端接管：`claude --resume <session_id>`。session_id 用 `ticket show` 查。Codex 单为 `codex resume <thread_id>`（配了专用 CODEX_HOME 时要带上 `CODEX_HOME=…`），`ticket show` 会按执行器给出完整命令。
- `ticket show` / `ticket ls` 显示与状态卡同一套口径：显示状态、阶段、用时、最近一步、最后动作。

### 7.1 状态卡（2026-11-01，T0005；T0007、T0017 调整）

**T0017 调整（以此为准，覆盖下面 T0007/T0005 里不一致的条目）**：用户嫌「开单回执 + 状态卡 + 结果卡」三条冗余，否决了「撤回状态卡」，要求结果原地覆盖。

- 终态汇报（结果卡，含稿子按钮）直接 PATCH 覆盖到本轮的状态卡上，不再另发结果消息；一张单在聊天里只占「开单回执 + 一张卡」。覆盖不产生推送提醒（用户已知情并选择覆盖）。覆盖成功后发件箱那一行记 `sent`，`sent_message_id` 就是状态卡的 id；卡片 `card_closed=1`、`card_round=本轮`，之后不再改动（视图里的 form 为 `result`）。
- 「本轮的状态卡」（`card.live_card`）：还开着的卡；或已收起但属于本轮（同一轮又交了一份终态汇报，再覆盖一次）。超过 13.5 天的卡不算。
- 没有本轮的状态卡时，结果照旧作为新消息推送：追问轮次跑得短（没到 `TICKETD_ROUND_CARD_DELAY`，没发本轮的卡；上一轮的卡保持显示上一轮的结果）、卡片没发出去、卡片放弃了。
- 覆盖失败：临时失败按发件箱的退避重试（仍然覆盖，不改发）；永久失败（卡被删等）把 `card_wanted` 置 0、写 alerts.log，这份结果改发新消息。
- 需要用户回答的（`blocked`，或带 `--ask` 且不是 `dropped`）：覆盖之后另推一条短文字提醒「⚠️ T0001 需要你决定：<问题>」+「详情已更新在上方状态卡，引用这条回复即可」（其他状态带 --ask 时是「✅ T0001 完成 · 👉 需要你：<问题>」）（回复用户最近一条消息，绑定到工单，引用它即可回答）。结果本身作为新消息发出时自带提醒，不再另推。
- 运行中带 `--ask` 的 progress 照旧推一条文字消息（是问题）。
- 疑似卡死：本轮有开着的状态卡时只在卡上标「⚠️ 疑似卡住」（橙色 header、红字），不另推消息——它不需要用户回答，真卡死到单轮超时会报 `blocked`，届时按上一条提醒；没有状态卡时照旧推文字提醒。

**T0007 调整**：

- 一张单在聊天里只占：bridge 的一行开单回执 + 一张状态卡 + 一张结果卡。结果只出现一次。
- 状态卡运行中显示：header（状态 · 已用时长，按状态着色）、阶段轨迹（`✓ 读代码 → ✓ 写代码 → ● 审查`，最多 4 段）、「最近：<人话动作> · N 分钟前」（Bash 用它的 description，文件操作写「改 card.py」，不展示原始命令行；卡住时变红）、页脚的目标和可写目录（家目录显示成 `~`）。
- 结果卡送达后，状态卡收起成一行灰色标题「✅ 完成 · 用时 N 分钟 · 结果见下方 ↓」，只留阶段轨迹，并标记 `card_closed=1`，之后不再改动。结果刚进发件箱的 30 秒内不刷卡，避免闪一下。
- 没有单独推送的终态（不带 `--ask` 的 dropped、结果发送失败）直接写在状态卡上。
- 追问等后续轮次默认不发新状态卡，只推结果卡；跑满 `TICKETD_ROUND_CARD_DELAY`（默认 120 秒）还没结果，才在这一轮的消息下面另发一张状态卡。
- 改卡片结构后跑 `scripts/validate_cards.py`：用 cardkit「创建卡片实体」接口校验所有卡片变体（不发消息）。注意它不校验 header 颜色、图标 token、背景色等枚举值，所以只用已验证过的能力。

**T0005 原始设计**：


- 新单（`card_wanted=1`）由 ticketd 回复原消息发一张交互卡片（Card 2.0，`update_multi:true`），之后只用 `PATCH /open-apis/im/v1/messages/:id` 原地编辑，编辑不产生推送。不用流式卡片（服务端 10 分钟后失效）。上线前的老单不发卡。
- 卡片内容：显示状态与用时（header）、阶段（worker `progress` 自述，带自述时间）、最近一步和最后动作时间（ticketd 从 stream 观测）、终态汇报与「需要你」、开工句和可写目录。worker 可控的文字一律用 `plain_text`，不经 Markdown 解析。
- 刷新节奏：里程碑变化（状态、阶段、汇报）立即刷新，同一张卡最小间隔 10 秒；运行中每 `TICKETD_CARD_HEARTBEAT`（默认 150 秒）心跳刷新一次；终态刷一次后不再动。
- 顺序：卡片没发出之前，同单的结果消息最多等 120 秒，保证卡片在结果前面。单在卡片发出前就结束了，就不再补发卡片。
- 失败：退避重试（5 秒起倍增，最长 300 秒）；永久失败或 24 小时发不出则放弃这张卡，写 `alerts.log`；卡片失败不影响结果消息。
  - **调整（2026-11-07，issue #11）**：已发出的卡刷新失败满 30 次、但不是永久失败（多半是长时间断网）时，只放弃这一张卡：收起（`card_closed=1`），这一轮的结果改发新消息；`card_wanted` 不再置 0，这一单之后用户发起的轮次照常另发新卡。原来断网 3 小时后整单再也没有状态卡。永久失败、超过 14 天编辑窗口照旧整单放弃。
- 卡片的 message_id 写入 `bindings`，引用卡片追问能路由到工单。
- 主动推新消息只有三种：终态汇报（同 §5.3）、需要用户决策（blocked 或带 `--ask`）、疑似卡死（运行中 `TICKETD_STALL_SECONDS`（默认 600 秒）没有任何工具调用，含子代理；每次安静期只推一次，只提醒不杀）。

### 7.2 运营指标（2026-11-08，运行复盘后新增）

起因：这次运行复盘里的问题——3 条结果没送达、40% 的 blocked 是沙箱、余额耗尽、一张单花了 $88——都是手工翻库、翻 alerts.log、逐个解析 `.stream.jsonl` 才看到的；告警只写在本机，T0016 的结果丢了没人知道。现在把这件事变成系统自己的例行动作（`ticketd/metrics.py`）：

- **成本落库**：Claude 的 result 事件里 `total_cost_usd` 是**会话累计值**（实测：同一会话后一轮的值包含前面所有轮）。调度器按「这次值 − 上次记的值」换算成新增成本（值变小说明换了会话，`ticket restart` 也会把 `session_cost` 清零），累加到 `tickets.cost_usd`，当前会话累计记在 `tickets.session_cost`；每轮结束在 `rounds` 表记一行（开始、结束、成本、结果：本轮最后一份终态汇报的状态，没有汇报时为 retry / stopped / held / failed）。Codex 的事件不带成本，记 0。老工单可以用 `ticket stats --backfill` 从 `.stream.jsonl` 补（只补没有轮次记录的单，可重复执行）。
- **事件落库**（`events` 表）：疑似卡住、卡住后中断命令、卡住后中止一轮、进入故障等待、模型接口报错（后台任务还在跑时）、申请 / 批准沙箱外执行、结果没送达、媒体任务卡住、状态卡放弃。
- **紧急事件私信主用户**：结果（或任何一条消息）彻底没送达、媒体任务卡住，发一条「🚨 …」私信给 `TICKETD_OWNER`（`lark-cli im +messages-send --user-id`，不进任何群；发件箱里工单号记为 `_ticketd`）。同一类一小时最多一条，其余只落库；系统消息自己没送达不再嵌套通知。
- **`ticket stats [--days N]`**：最近 N 天（默认 7）的概况：新开工单、跑了几轮、worker 成本、每轮结果分布、最花钱的 3 张单、会话已很长的单（`session_cost` ≥ `TICKETD_COST_HINT_USD`）、需要你的次数（其中申请 / 批准沙箱外执行、授权）、你回复的中位 / 最慢时间、失败次数和最近原因、事件计数、没送达的消息和本机告警条数。
- **长会话成本提示**：终态汇报（dropped 除外）时，这张单当前会话的累计成本 ≥ `TICKETD_COST_HINT_USD`（默认 30 美元）就在结果卡页脚上方加一行「💰 这张单的会话已累计约 $N，之后每次追问都按整段上下文计费。换了目标就开新单；同一件事要接着做，可以说「换新会话重来 Txxxx」」，文字兜底同样带上。起因：T0005 一张单 $88，后期每次追问 $5～8。worker 提示词同时把「追问把工单带向另一个目标」算作不属于本单（报 dropped，让用户开新单）。
- **每周概况**：守护进程按 `TICKETD_DIGEST`（默认 `mon 10:00`，空 = 不发）把最近 7 天的 `ticket stats` 私信给主用户。每个时刻只发一次（`kv` 表记 `digest_last`，跨重启不重发）；第一次运行只记下时刻、不补发。

- **调整（2026-11-09）配置两处不一致**：第一份周报没发出去，发件箱里静默重试了 51 次，报 `99992361 open_id cross app`。根因：`TICKETD_OWNER` 在 `~/.claude/settings.json` 的环境变量里是对的（Claude Code 会话、主会话里的 `ticket` 命令都读它，所以派单一直正常），在 `~/.ticketd/config.env` 里却是迁移带来的旧应用下的 open_id；守护进程由 launchd 启动，只读 `config.env`。前一晚的测试私信是从 Claude Code 会话里塞进发件箱的，用的是环境变量里的值，没测到守护进程自己的路径。修复：① 99992361 算永久失败，立即标 dead；② 私信主用户本身失败时，告警写明多半是 `config.env` 里的 `TICKETD_OWNER` 不对；③ 新命令 `ticket doctor`：列出当前环境和 `config.env` 不一致的 `TICKETD_*`、只在环境里有而守护进程看不到的项、`config.env` 里有没有有效的主用户、守护进程心跳。

- **调整（2026-11-09）守护进程视角**：系统私信放进发件箱时收件人记成占位 `@owner`，发件箱发送时才按**发送进程**（守护进程）的 `TICKETD_OWNER` 解析并写回；没有有效主用户时标 dead 并告警。`ticket doctor --dm` 放一条测试私信、等守护进程发出后报告结果——不管从哪个会话发起，测的都是守护进程的真实路径。`ticket doctor` 另外把本仓库的 `workspace/CLAUDE.md.tmpl` 渲染一遍，和 bridge 工作目录（`TICKETD_WORKSPACE`，没配时尽力读 bridge 配置里的默认工作目录）的 `CLAUDE.md` 比对，不一致时提示用 `setup-workspace.sh --force` 同步。`setup-workspace.sh` 新建 `CLAUDE.local.md` 时不再拷示例文件（示例里的「（例）」句子会被主会话当成真规则），只写一句说明。
- **调整（2026-11-09）成本对账**：`metrics.reconcile_costs` 从每张不在运行中的单的 `.stream.jsonl` 按同样的规则重算成本，和 `tickets.cost_usd` 比对（误差 1 美分以内）。`ticket doctor` 报对不上的单，`scripts/selftest.py` 在工单类检查之后也核对一遍。重启、异常退出、换会话、重复读事件，任何一处记账出错都会在这里露出来。首次在线上核对 33 张单全部一致。**调整（2026-11-19，外部评测）**：重算时按 `session_id`（result 事件自带，没有就用最近一个 init 的）分开累计，不再只靠「累计值变小」识别换会话：旧会话 $2、新会话首轮 $5 时原来算成 $5，实际 $7。没有 `session_id` 的老记录沿用原规则。
- **调整（2026-11-09）主会话成本**：`ticket stats` 和每周概况加一行主会话（bridge 会话）的当前累计成本，按 bridge 日志里每轮的 `usage` 事件（`costUsd` 是会话累计值，取每个聊天最近一次；私聊 / 群聊 N，不写聊天 id）；任一 ≥ `TICKETD_COST_HINT_USD` 时建议在那个聊天里发 `/new`。日志是上游格式，只读，读不到就不显示这一行。起因：复盘里同一个主会话累计到 $35。

### 7.3 自动压缩会话（2026-11-09）

起因：长单的成本几乎全部来自上下文规模。T0005 后半程平均每轮读缓存 1700 万 token、重写 91 万 token，输出可以忽略；一张单 $88，后期每轮 $5～8。主会话那边，9 天 $78.7 里只有 $27 是纯派单 / 转发 / 查询，$49 是主会话自己干活（部署、推送、替 worker 在沙箱外跑），所以没有先做「程序路由入口」，先压 worker 的上下文。

做法：调度器从每个 assistant 事件的 usage（输入 + 缓存读 + 缓存写）记下这一轮的上下文峰值，存进 `rounds.ctx_tokens` 和 `tickets.ctx_tokens`。新一轮开始时，如果是 Claude 单、会话已建立、上一轮峰值 ≥ `TICKETD_COMPACT_TOKENS`（默认 150000，0 = 关），先跑一轮 `claude -p --resume <会话> --model <TICKETD_COMPACT_MODEL，默认 sonnet> "/compact <保留要求>"`：不投递输入、不放行任何工具、不要求汇报；不改 `started_at`（卡片按它分轮），`last_action_at` 从开始压缩算。看到 `compact_boundary` 算成功；成功失败都把 `ctx_tokens` 清零，下一个 tick 照常投递这一轮的输入（压缩失败不卡工单、也不反复压），并记事件 `compact` / `compact_failed`、轮次结果 `compact` / `compact_failed`。压缩的成本照常记账（会话累计值继续往上走）。开了自动压缩时，结果卡上的「会话已很长」成本提示不再显示。

保留要求（`worker.COMPACT_PROMPT`）逐条列出：用户的决定和限制（尤其「不要做 / 不能再做」）、授权和审批（含沙箱外执行的申请和结果）、已经执行过的外部动作及结果、未完成事项和下一步、最初的目标和验收标准、关键文件和函数、工单号和目录、结果.md 里的进度；可以丢掉读过的文件全文、命令原始输出、探索过程。

实测（不动核心代码先做的实验）：haiku 上 9.8 万 → 2.5 万 token，不带保留要求时丢了「已部署、不能再部署」这条决定，带上之后 haiku 和 sonnet 都保住了；T0005 真实会话复制一份（`--fork-session`，原会话不动）用 sonnet 压缩：91 万 → 2.4 万 token，压缩约 $2.04，之后一轮约 $0.07；6 道核对题（目标、用户对参考方案的限制、三个授权目录、860 / 654 / 240 积分、11-03 的编号挑图要求、当前进度）全部答对。用较便宜的模型压缩 Opus 会话没有问题，续做照常用原模型。`scripts/selftest.py` 的 compact 项用真实 claude 验证整条链路：ready → compact → ready，压缩后记得暗号和「不能再推送」。

**调整（2026-11-09，外部审查 A～D）**：
- **A 压缩失败分两类**：临时故障（网络、429、5xx）和账户级故障（余额、认证）时保留 `ctx_tokens`，按普通轮次的规则退避重试（`TICKETD_RETRY_DELAYS`）或进入故障等待（`_outage`，余额不足时照常推一次 waiting），这一轮的输入原样留着、也不加「上一轮中断」的 retry 输入——不拿几十万 token 的上下文去续做（那一轮多半也会失败，成功了也是在烧钱）。只有确定性失败才清零后照常续做。
- **B 成本提示两个量不混用**：会话累计成本只增不减，上下文规模才决定下一轮多贵。没开自动压缩时按累计成本提示（原行为）；开了自动压缩时，只有这张单最近一次压缩失败才在结果卡上提示「会话没能自动压缩，仍然很长（上一轮约 N 万 token）」。
- **C 用上上下文数据**：`ticket stats` 加一行「上下文：每轮平均 N 万 token（最大 Txxxx M 万）；自动压缩 K 次（失败 F 次，花 $C），压缩前一轮平均 X 万 → 压缩后一轮平均 Y 万 token」；`ticket show` 显示上一轮的上下文和压缩次数。每轮记自己真实的开始时刻（压缩轮不改 `tickets.started_at`，原来 `rounds.started_at` 会和前一轮重复）。
- **调整（2026-11-19，外部评测）暂停 / 停止打断压缩**：压缩轮被用户暂停或停止时保留 `ctx_tokens`，这一轮的结果记 `held` / `stopped`，不算压缩失败；放开后 `start` 重新判断，仍超阈值就先压缩。原来落到「确定性失败」分支把 `ctx_tokens` 清零，放开后直接拿大上下文跑普通轮。
- **D 按执行器分开**：窗口里有不止一种执行器时，`ticket stats` 分开列 Claude / Codex 的轮数和成本，并注明 Codex 不报成本、不自动压缩，避免直接拿来比。

### 7.4 匿名使用统计（2026-11-19，T0035 设计、T0038 实现）

起因：Larkyard 分发给别人用之后，维护者要能统一看到「运行得怎么样」，定位常见故障、决定优化顺序。原则：只收运行状况，绝不收用户在做什么；默认关闭。面向用户的说明和完整字段表在 `docs/telemetry.md`。

- **开关**（`ticketd/telemetry.py`）：`ticket telemetry on` 在 `kv` 里记 `telemetry_consent=<同意的 schema 版本>`、`telemetry_id=<随机 UUIDv4>`、`telemetry_since=<开启的第二天>`；`off` 删掉所有 `telemetry_*` 键和 `pending/` 里的报告（`sent/` 副本留给用户自己删）。`DO_NOT_TRACK=1` 或 `TICKETD_TELEMETRY=0`（环境变量或 `config.env`）优先级最高，`on` 也拒绝。`CI=true` 时不发送。worker 里（有 `TICKETD_TICKET` / `TICKETD_SPOOL`）执行 `on` / `off` 报错，只读的 `status` / `show` 可以。
- **数据来源**：只读数字和枚举列：`rounds`（时长、成本、结果、上下文）、`tickets.executor / created_at`、`reports.status / ask`（只看有没有）、`events.kind / code / seconds / size`、`grants` / `net_grants` 的时间和种类。**不读**标题、原话、汇报摘要、`events.detail`、`outbox.last_error`、路径。所有字符串经白名单映射，不认识的记 `other`。
- **新埋点**：`events` 加 `code`（只放枚举值）、`seconds`、`size` 三列。`worker.error_code` 把模型接口报错归成 `balance / auth / rate_limit / overloaded / network / server_5xx / client_4xx / other`，在 worker 没交汇报就退出、压缩失败、后台任务时报错时记 `model_error` / `api_error`；`outbox.lark_error_code` 把飞书发送失败归成已知错误码 / `rate_limit / network / api_other / other`，每次失败记 `lark_error`；结果消息送达记 `delivery`（`sent / card_patched / fallback_text / card_patch_fallback / video_fallback / dead`）；图片上传、视频上传 / 视频消息、稿子导入成功或非网络失败时记 `media`（`image:ok` 这类码 + 耗时 + 字节数）。网络 / 超时这类会重试的媒体失败不记，免得一次断网记成几十次失败。这几类事件不进 `ticket stats` 的「事件」一行（`metrics.DETAIL_KINDS`）。版本号放在 `ticketd/__init__.py` 的 `__version__`。
- **白名单**：`ticketd/telemetry_schema.json`，客户端（Python）和后端（`server/telemetry/src/validate.js`）共用。生成后、写入 `pending/` 前、发送前各校验一次；多一个键、类型不对、字符串不在枚举里整份丢弃，写 `alerts.log`（只写哪个字段，不回显内容）。
- **生成与发送**：守护进程主循环每轮调用 `telemetry.maybe_send`（和 `maybe_digest` 同一位置）。每天 00:00 之后再加 0～6 小时随机延迟（每天抽一次，记在 `kv` 的 `telemetry_due`）；主循环里只读几个 `kv`（开没开、到没到点、下次什么时候试），到点后起一个后台线程（同一时刻最多一个，用自己的数据库连接）：给最近 7 天里开启过、还没生成过的日子（到昨天为止）生成报告写进 `~/.ticketd/telemetry/pending/<day>.json`（生成时要跑 `claude --version` / `codex --version`，各 5 秒超时、每 6 小时最多问一次，所以不能放在主循环），然后用 `urllib` POST（10 秒超时），结果写 `telemetry/last.json`。和关闭的竞态：生成开始时记下当前 install_id，每份报告写入前后、每份发送前都核对「仍开启且 ID 没变」，不是就停（刚写的那份删掉）；`pending/` 里 ID 不是当前 ID 的报告（上一轮授权留下的）直接删掉不发；关闭后线程不再写 `telemetry_*` kv。「核对同意 → 发请求 → 搬到 sent/」和「核对同意 → 写待发报告」都在跨进程文件锁 `~/.ticketd/telemetry.lock`（flock）里做，`turn_off` 先删同意记录，再拿这把锁删待发报告：正在进行的那次请求发完（最多 10 秒）它才返回，发送方下一次拿到锁核对时看到已关闭就停，所以 `ticket telemetry off` 返回之后不会再有请求发出。顺序不能反过来：flock 不保证先来先得，先拿锁再删的话，发送方放锁后可能马上又抢到锁、在 off 删同意之前发出下一份（macOS CI 上出现过）。成功移到 `sent/`（最多留 90 份）；后端 4xx 拒收就删掉不重发；网络失败、429、5xx 留在 `pending/`，至少隔 30 分钟再试，每天最多 6 次，超过 7 天的删除。失败不告警、不私信，只在 `ticket telemetry status` 里显示。
- **上报地址**：`TICKETD_TELEMETRY_URL`，默认值 `telemetry.DEFAULT_URL` 在后端部署前为空：开启后只在本机生成报告、不发送，`status` 和 `on` 都会说明。
- **字段变更**：白名单扩一次 `schema` 加一，`docs/telemetry.md` 写明新增了什么；用户同意的是某个版本，没再次 `on` 之前按老版本发（v1 是第一版，按版本裁剪字段的代码等出现 v2 时再写）。
- **后端**（`server/telemetry/`，维护者本人的 Cloudflare 账号，第一期 `*.workers.dev`）：`POST /v1/report` 严格校验、8KB 上限、只收最近 8 天、按 `(install_id, day)` 去重写入 D1，不读不存 IP 和 User-Agent，不开请求日志，按 IP 用 Rate Limiting 绑定限流（边缘计数，不落库）；每天定时删 365 天前的报告，每周一生成全体汇总，`GET /v1/summary` 公开，不含 install_id。小样本保护按每个桶的贡献机器数算：任何分布（版本、系统这类头部字段，以及轮次结果、上下文、成本、错误码、媒体耗时 / 大小等计数字段）里贡献机器少于 5 台的桶并进 `other`；`other` 自己不够 5 台时不删（删了就能用总量减出来），而是继续把机器最少的桶并进去直到够 5 台，凑不够整个分布不列，所以公开的每个分布都加得回总量；活跃机器少于 20 台只公开总量（工单数、轮次数、轮次结果、错误码，同样做桶级保护），不公开头部字段；少于 5 台只公开 `machines`、`reports` 两个总数。部署清单见 `server/telemetry/README.md`。
- **入口提示**：`install.sh` 结束时打印两行说明（不交互、不自动开启），`ticket doctor` 显示一行统计状态。

## 8. 容错

| 情况 | 处理 |
|---|---|
| 模型接口或网络临时故障（进程退出，stderr 含 429/5xx/超时/连接错误；**调整 2026-11-07**：断网 / DNS 失败 `ENOTFOUND`、`EAI_AGAIN`、「Can't reach the API server」也算，issue #10） | 重试 3 次，间隔 10s、40s、160s：会话已建立则 `--resume` 并发「上一轮因网络中断，请继续」；首轮未建立会话则用同一 `--session-id` 重跑任务卡 |
| worker 没交汇报就退出 | 记 `failed`，附最后一段 assistant 文本 |
| 模型接口账户级故障（报错含余额不足、认证失败等，见 `worker.ACCOUNT_MARKERS`）（**调整 2026-11-07**） | 不重试（重试不会好）。记 `failed`：「模型接口余额不足/认证失败，这一轮没做完：<报错原文>」，`--ask`「处理好之后引用这条消息回复「继续」」。原来只写「worker 未交汇报就退出（code=1）」，T0005/T0015/T0017 的用户看不出要做什么。过程日志里 `is_error` 的一轮写「本轮结束：出错（…）」，不再照抄 subtype 写 success（issue #15） |
| **调整（2026-11-07）故障等待**：短重试用完的临时故障，以及账户级故障 | 不再直接失败，进入故障等待（`tickets.outage_since` 记开始时刻）：每 `TICKETD_OUTAGE_RETRY_EVERY`（默认 900 秒）自动续跑一次（会话已建立则加一条 `retry` 输入，否则重投原输入），最长 `TICKETD_OUTAGE_RETRY_HOURS`（默认 24 小时，0 = 关掉故障等待）。进入时记一份 `waiting` 汇报推给用户一次：账户级故障带 `--ask`「处理好之后会自动接着做（每 N 分钟试一次，最晚到 MM-DD HH:MM）；也可以引用回复「继续」马上重试」，网络故障只在摘要里说明、不打扰用户做事；之后的自动重试只写过程日志。任一轮交了汇报、用户追问、恢复、换会话都清掉故障等待；用户追问还会清掉重试时间点，马上投递。超过时限记 `failed`（账户级沿用上一行的文案；网络故障写「自动重试 N 小时仍未恢复」并带「恢复后回复继续」）。起因：余额不足、断网时工单直接失败，要人发现后手动「继续」，另一台机器上隔了 3 小时和 6.5 小时才被发现（issue #10）。**调整（2026-11-19，外部评测）**：到期时同时置 `auto_halted=1`，并按重试的规则留好输入（会话已建立加一条 `retry`，否则原输入改回 `pending`；压缩轮的输入本来就没投递）。原来压缩轮遇到余额不足到期后只标 `failed`，留着的追问让调度器下一拍又选中它，一直重试压缩 |
| 单轮 worker 进程运行超过 2 小时 | 先 SIGTERM，10 秒后 SIGKILL；记 `blocked`：「运行超过 2 小时已中止，是否继续？」 |
| **调整（2026-11-08，issue #16）卡住分级处置** | 原来疑似卡住只在卡上标一下，要等 2 小时单轮超时，实际每次都靠用户「停」救场。现在分三级，都从最后一个动作算起：① `TICKETD_STALL_SECONDS`（600）标「疑似卡住」（不变）；② `TICKETD_STALL_INTERRUPT`（1800，0 = 关）：结束**这次工具调用起的进程**（worker 的后代里启动时间不早于最近一次工具调用的；后台任务、MCP 服务更早，不碰），claude 本身不动——实测（Claude Code 2.1.316，`-p`）这样模型会收到工具报错（exit 144）接着做或交汇报；给 claude 发 SIGINT 则会拒绝当前工具并直接以 `error_during_execution` 结束这一轮、交不了汇报，所以不用；③ `TICKETD_STALL_ABORT`（3600，0 = 关）：仍没进展（进程卡在不可中断的 I/O 里杀不掉、或模型自己卡住没有子进程）就按超时的路子结束进程，报 `blocked`「卡在「<最近一步>」超过 N 分钟没有进展，已中止这一轮」，`--ask` 提示处理好环境后回复「继续」用同一个会话接着做。旁证：标出疑似卡住之后每 30 秒采样一次这些进程的累计 CPU（`procs.tree`：macOS 用 `ps -o etime,time`，Linux 读 `/proc/<pid>/stat`），两次采样之间用了 ≥3% 单核就算还在干活（在跑的慢命令），静止计时从头算；②③ 都要求「没有动作满 N 秒」且「CPU 静止满 N − 600 秒」。等后台任务（`bg_wait`）和已交终态汇报的进程不进这条链。 |
| 发件箱发送失败 | 退避重试，从 5 秒起倍增，最长 300 秒。同一工单的消息按顺序发，前一条没发出去，后一条就等着。同一条连续失败 3 次，写入 `~/.ticketd/logs/alerts.log`，然后继续重试；飞书返回目标永久不可达时标为 `dead` |
| 服务或电脑重启 | 启动时把所有 `running` 工单改为待续做，插入一条 `recovery` 输入：「服务重启打断了上一轮。先检查工单目录和已发出的消息，确认哪些外部动作已完成，再决定是否重做。」 |
| 服务崩溃 | launchd / systemd（`Restart=always`）自动拉起。所有动作都是先落库再执行，所以不会丢 |

## 9. 验证

前一步不通过，不进入下一步。

- **V1 实测**：
  - sandbox 能否挡住工单目录以外的 shell 写入；
  - 文件工具在授权目录以外能否被拒；
  - launchd 环境下 lark-cli 能否发消息、能否读钥匙串；
  - `--resume` 能否带着上下文续做；
  - stream-json 的格式能否解析。
- **V2 单元测试**（`python3 -m unittest`）：用一个假 claude 脚本代替真 worker，覆盖排队、并发上限、项目锁、汇报转消息、发件箱顺序和重试、超时、重启恢复、hold 和 release、stop 之后拒绝续做。
- **V3 端到端**：真派一张小单（在工单目录写一个文件），走完 确认 → 结果推送 → 引用追问 → 续做 → stop。
- **V4 自检（2026-11-08 新增）**：`scripts/selftest.py` 在临时目录里起调度器，用真实的 claude（默认 haiku）和真实的沙箱跑四组检查，飞书一侧换成假 lark-cli、不发消息：① 沙箱语义（默认拒绝连本机端口、连 / bind unix socket、本机 bind；`allowLocalBinding`、`allowUnixSockets` 分别放开对应的项）；② 普通工单跑完一轮、成本落库；③ 沙箱外执行：申请 → 批准 → 执行 → 输出交回 worker → ready；④ 卡住处置：前台挂住的命令被结束，worker 收到报错后自己汇报。首次运行（2026-11-08，Claude Code 2.1.316）9 项全过，worker 成本约 $0.02。升级 Claude Code 之后先跑这个，替代 §6 的手工复核。

## 10. 上线与回滚

**上线**（V3 通过后）：

1. 在 bridge 工作区的 `CLAUDE.md` 增加「派单规则」一节（现在由 `scripts/setup-workspace.sh` 从 `workspace/CLAUDE.md.tmpl` 生成），内容是 §5.1、§5.4、§5.5、§5.6 里主会话的部分；
2. 删除「过程中的可见性」里那条 10 分钟心跳的过渡规则；
3. 修改 `CLAUDE.md` 前先备份。

**回滚**：

```
scripts/uninstall.sh
cp <工作区>/CLAUDE.md.bak-<时间> <工作区>/CLAUDE.md
```

`~/.ticketd/` 和工单目录保留，不删。

**调整（2026-11-20，T0042，自动更新）**：多了一种「受管安装」。执行过 `ticket update adopt` 的安装，代码在 `~/.ticketd/app/versions/<版本>/`，服务经版本目录外的启动器 `~/.ticketd/app/launcher.py` 运行 `~/.ticketd/app/current/bin/ticketd`，bridge 工作区 `CLAUDE.md` 里的 ticket 路径是 `~/.ticketd/app/current/bin/ticket`。它的回滚有三层：自动（新版本试用期内自检不过，启动器切回上一个版本）、`ticket update rollback`（空闲时切回本机上一个版本）、`ticket update adopt --undo`（退回就地运行，服务和工作区都改回原克隆）。守护进程和命令都起不来时手工把 `current` 链接改回旧版本目录再重启服务（README「自动更新」一节）。没有执行 `adopt` 的安装，上线和回滚和上面完全一样。

## 11. 自动更新（2026-11-20，T0042）

行为以 `openspec/changes/add-signed-self-update/specs/release-signing/spec.md`（发版和签名）和 `specs/self-update/spec.md`（客户端）为准，设计取舍见同目录的 `design.md`。这里记实现上的约定。

**默认关闭**：`TICKETD_UPDATE_URL` 空（仓库里没有默认值）时，守护进程只做两件不发请求的事：读 `update-state.json` 发切回后的私信、确认试用期。启用要同时满足：配了 `file://` 或 `https://` 更新源、`~/.ticketd/update/` 下有 `main.allowed_signers` 和 `backup.allowed_signers`、当前运行的代码就是 `~/.ticketd/app/current` 指向的版本目录、`ssh-keygen -Y verify` 能验过内置的一份探测签名。

**格式**（`ticketd/updsig.py`，发版工具和客户端共用）：清单是 `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)` 的 UTF-8 字节，签名针对这份字节，客户端解析后重新序列化必须逐字节相同。签名是 `ssh-keygen -Y sign -n larkyard-release` 的 SSHSIG（`.sig` 文件）。

| 字段 | release / rollback | pause | rekey |
|---|---|---|---|
| `format`（1）、`product`（`larkyard`）、`kind`、`seq`（正整数）、`signed_at`（UTC，`YYYY-MM-DDTHH:MM:SSZ`） | ✓ | ✓ | ✓ |
| `version`、`package`（`larkyard-<版本>.tar.gz`）、`sha256`、`size`、`channel`、`min_from`、`commit`、`notes` | ✓ | | |
| `expires_at` | 仅 rollback（≤ 签名后 7 天） | | |
| `not_before`、`not_after`（窗口 7 天） | | ✓ | |
| `new_main_key`、`old_main_key`（`ssh-ed25519 <base64>`） | | | ✓ |

**更新源布局**：`channels/<通道>/manifest.json(.sig)`、`pause.json(.sig)`、`rekey.json(.sig)`、`packages/larkyard-<版本>.tar.gz`。文件不存在（`file://` 找不到、HTTPS 404）算「没有发布」，别的失败记「检查失败」。清单最大 64KB，包最大 200MB。

**检查顺序**（`ticketd/update.py` 的 `check`）：换钥清单（只用 `backup.allowed_signers` 的 `larkyard-rekey` 身份验）→ 暂停标记（主公钥验，窗口内才生效，无效当不存在）→ 本机通道清单：主公钥验签 → 格式和产品名 → 通道 → 序号（`< update.max_seq` 或等于但内容不同都是「旧清单」；等于且就是上次接受的那份不算重放）→ rollback 查 `expires_at`、release 拒绝降级 → 已是这个版本 → `min_from` → 已拉黑 → 下载包、边下边核对大小和 SHA-256 → 记为待安装、`update.max_seq` 提到这份清单的序号。

**序号只增不减**，例外只有换钥：换钥清单（序号大于上一次处理过的换钥清单、`old_main_key` 正是本机当前信任的主公钥）验过后，主公钥换成新的，`update.max_seq` 设为换钥清单的序号、丢弃待安装的包。理由：旧主公钥签的东西从此全部验不过，不存在重放；主私钥泄露期间攻击者可能签出序号极大的清单把 `max_seq` 推上去，不重置的话发布者之后签的清单都会被当成旧清单。`ticket update trust` 换了主公钥时同理清零。

**事件**：`events.kind='update'`，`code` 是 `update.CODES` 里的枚举（检查失败、验签失败、校验失败、通道不符、旧清单、拒绝降级、回滚已过期、已下载、已安装、已确认、更新失败已回滚、换钥、换钥清单无效……）。同一种同一原因一天只记一条。`update` 列在 `metrics.DETAIL_KINDS` 里：不进 `ticket stats` 的事件行，匿名统计不读（统计字段不变）。状态在 `kv`：`update.max_seq`、`update.accepted`（上次接受的清单摘要）、`update.pending`、`update.blocked`、`update.hold_until`、`update.pause`、`update.rollback_request`、`update.last_check_at` / `last_ok_at` / `last_error`、`update.channel_signed_at`、`update.rekey_seq`、`update.rekey_inflight`（换钥写了公钥、还没重置序号时进程退出，下次检查补完）。

**安装和切换**：守护进程主循环每轮调用 `Updater.tick`。检查在后台线程里做（不拖主循环）。有待安装、没有本地暂停 / 更新源暂停、空闲（调度器没有运行中的 worker 和压缩、没有 `approved` / `running` 的沙箱外执行、媒体通道没有在跑的任务、`tickets` 没有 `running`）时：调度器置 `update_hold`（这之后不启动新 worker 和沙箱外脚本）→ 再核对一次包的 SHA-256 → 解包到 `versions/.<版本>.tmp-<pid>`（只解普通文件和目录，拒绝绝对路径、`..`、链接、设备文件和不在 `larkyard-<版本>/` 下的成员；检查 `bin/ticketd`、`ticketd/launcher.py` 存在、`__version__` 一致）→ rename 成 `versions/<版本>` → 写 `update-state.json = {status: probation, from, to, deadline, starts: 0, token}` → `current` 原子切换（相对链接，`rename`）→ 守护进程按正常停机流程退出，退出码 75。launchd（`KeepAlive`）/ systemd（`Restart=always`）重新拉起，经启动器跑新版本。没有用 D7 设想的 `launchctl kickstart` / `systemctl restart`：退出由服务管理器拉起两个平台行为一致，也不需要守护进程知道自己的服务名。**调整（外部评测 P1）**：`current` 切换成功是提交点。原来切换之后记「已安装」事件出错时 `install` 整体抛出，调用方当成没装上、放开 `update_hold` 接着接单，磁盘上却已是新版本的试用期，旧进程不再请求重启，下一拍看到试用期也只是等着——CLI 走 `current` 跑新代码、守护进程跑旧代码，新版本从没被拉起，直到旧进程哪天退出时启动器按超时把它切回、拉黑。现在提交点之后的错误只打日志不抛出；`install` 仍抛出时调用方按磁盘核对（试用期的 `to` 是这个版本、`current` 已指向它、本进程还在跑别的版本目录）就照样重启，`update_hold` 不放开（手工切回早已按同样原则处理）。另加自愈：主循环每一拍看到状态是 `probation`、本进程跑的是某个版本目录、`current` 却指向另一个版本目录（`update.stale_code`）时，记 `stale_code` 事件、置 `update_hold` 不再接新活，空闲后按退出码 75 重启；新版本一次都没被拉起过（`starts` 为 0、没有 `timed_out`）时，等空闲期间试用期剩不到一半就重算一次、重启前再重算一次（`deadline`、`window`、`boot`）：等空闲的时间不算进新版本的试用期，旧进程这时因任何原因退出，启动器都不会把没跑过的新版本按超时切回、拉黑。重启前在状态里记下当时的 `starts`（`healed_starts`，确认时清掉）；下一个进程如果还是旧代码而 `starts` 没涨，说明服务没经过启动器拉起，不再补发，免得反复重启；这一步记状态失败就不重启，连试 3 拍仍失败就放弃补发、放开 `update_hold`。从克隆就地运行、或跑的就是 `current` 的进程不算；等待期间状态离开试用期（比如被手工处理）就放开 `update_hold`。

**启动器**（`ticketd/launcher.py`，`adopt` 时拷到 `~/.ticketd/app/launcher.py`）：只导入 `fcntl json os signal sys time`。每次被拉起：状态是 `probation` 就 `starts` 加一；看门狗记了 `timed_out` 或 `starts >= 3` 就把 `current` 切回 `from`、记 `rolled_back`（`from` 目录不在了记 `rollback_failed`，接着跑新版本）；墙钟已过 `deadline`、看门狗没动手、而且开机时刻和切换时记下的 `boot` 不同（试用期里关机重启了），重新给一次完整试用期（`window` 秒，记 `extended`），第二次再过期按超时切回；没重启过（新版本在 `deadline` 和看门狗动手之间自己退出）直接按超时切回。守护进程收到停机信号正常退出时记 `clean_exit`，下次启动不计入 `starts`。然后 `exec /bin/sh current/bin/ticketd`。试用期内另 fork 一个看门狗（脱离标准输入输出），按单调时钟（电脑睡眠时不计时）等到剩余试用期 + 30 秒还是同一个 `token` 的试用期，就在状态里记 `timed_out`、SIGTERM 守护进程、60 秒后 SIGKILL，服务管理器重新拉起时启动器按超时切回。状态文件写临时文件 + fsync + rename + 目录 fsync，启动器、看门狗、确认、安装的读改写都在 `update-state.json.lock` 的 flock 里做（看门狗在锁里重读，已确认就不动手；看门狗记了 `timed_out` 之后不再接受确认）。替换启动器同样 fsync。`deadline` = 切换时刻 + max(试用期 − 120 秒, 试用期 / 2)，保证「看门狗动手 + 最长停机 + 拉起节流」都落在试用期之内。试用期下限 180 秒。

**自检和确认**：新版本守护进程转满 min(60 秒, 试用期 / 3)、主循环没有在报错、数据库可读写、心跳新鲜、当前运行的就是 `current` 指向的版本，就把状态改为 `confirmed`（记 `previous`），再做启动器两步更新的第二步（新版本里的 `launcher.py` 能编译、内容不同才原子替换）、清理旧版本目录（保留最近 3 个和当前、上一个）。只看新版本自己会弄坏的东西；`config.env` 的问题（如没填 `TICKETD_OWNER`）旧版本一样有，不算自检失败。

**切回之后**：旧版本守护进程启动时看到 `rolled_back` / `rollback_failed` 且没私信过，就私信主用户一次（「更新到 X 失败（原因），已回到 Y」）、记事件、把 X 加进 `update.blocked`。`ticket update rollback` 记一个请求，守护进程空闲时切回本机上一个更低的版本（不进试用期），把切走的版本拉黑。

**受管安装**：`ticket update adopt`（空闲时、工作树干净）把克隆里被跟踪的文件拷进 `versions/<当前版本>/`，拷启动器，`current` 指向它，用 `TICKETD_APP=~/.ticketd/app` 跑那个版本目录里的 `scripts/install.sh`：plist 的 `ProgramArguments` / unit 的 `ExecStart` 换成「解释器 + 启动器」，PATH 里的 `bin` 换成 `current/bin`；不设 `TICKETD_APP` 时生成的文件逐字节不变（`tests/fixtures/service/*.golden` 由改动前的脚本生成）。bridge 工作区 `CLAUDE.md` 里的 `<克隆>/bin/ticket` 改为 `~/.ticketd/app/current/bin/ticket`（改前备份），`setup-workspace.sh` 在受管安装里运行时也写 `current` 路径，`ticket doctor` 的主会话约定检查按 `current` 路径比对。文件内容从 HEAD 的 git 对象取（`git ls-tree` + `cat-file`，不读工作树；git 调用关掉 fsmonitor、钩子等会执行命令的配置，清掉 `GIT_*` 环境变量）。CLAUDE.md 里只替换完整的 `<克隆>/bin/ticket`（后面不跟字母、数字、`.`、`-`），克隆路径的两种写法（真实路径、经过符号链接的路径）都认，记下实际替换的那种（`workspace_root`），`--undo` 换回它。服务名取 `TICKETD_LABEL`（默认 `dev.larkyard.ticketd`），这个名字的 plist / unit 不存在就拒绝（多半是当初用自定义服务名装的，这次没设）；`TICKETD_HOME`、`LARK_CHANNEL_HOME`、`LARK_CHANNEL_PROFILE`、`TICKETD_PYTHON` 从已装的服务文件里读，shell 里的同名变量不混进去，记进 `update-state.json`（`label`、`service_env`，之后每次重写状态都带上），重装都按它来；服务里的 `TICKETD_HOME` 和当前的不同就拒绝。`install.sh` 失败（含 Ctrl-C）时先按原样从克隆重装服务，再改回工作区；`install.sh` 的受管模式渲染后检查入口确实是启动器，不是就不装。`adopt --undo` 用原克隆的 `install.sh`（同样的服务名和环境）重装服务、把工作区路径改回、删掉 `current` 和启动器（版本目录留着）；原克隆的版本比正在运行的低时提示退回后跑的是旧代码（数据库迁移只增不删，旧代码能读新库）。

**worker**：在 worker 环境里（有 `TICKETD_TICKET` 或 `TICKETD_SPOOL`）`ticket update trust | hold | resume | rollback | adopt` 一律拒绝；`status`、`check` 可以。信任公钥文件在 `~/.ticketd/update/`，不在 worker 沙箱的可写范围里。命令层的拒绝只防误用（环境变量可以去掉），真正的边界是沙箱：worker 写不了 `~/.ticketd`，也就改不了信任公钥、`kv` 和 `update-state.json`。

**更新源的读法**：https 只跟随到 https 的重定向；`file://` 只读普通文件（FIFO、设备文件会永远阻塞）；下载有总时长上限（单次读的超时挡不住慢速滴流）。清单的 JSON 拒绝 `NaN` / `Infinity`，`signed_at` 必须是规定格式，版本号整串匹配（结尾带换行不行）。`ssh-keygen` 先用 `/usr/bin/ssh-keygen`，不按 PATH 找。接受清单时序号和摘要在一个事务里写。暂停标记的窗口最长 7 天、`not_before < not_after`，否则当作无效；换钥和 `trust` 换了主公钥时清掉已记下的暂停。换钥写新公钥之前先丢掉待安装的包，`update.rekey_inflight` 还在时不安装，下次检查不论 `rekey.json` 还在不在都补完。

**撤回**：检查时接受了一份序号更大、版本低于待安装版本的清单（通常是指回当前版本的回滚清单），就丢掉待安装的包、记 `withdrawn`。安装时本地包和清单对不上（磁盘、并发写）只丢掉待装记录，下次检查重新下载，不拉黑；下载的临时文件按进程和线程分开。

**序号和预签**：回滚清单、暂停标记、发布清单共用 `release.py` 的一个序号计数。签过回滚清单之后，早先预签、还没放出去的发布清单序号更小，客户端会按旧清单拒绝，要重新签。

**没做的**（后续变更）：控制台、入网、心跳上报、按心跳对比版本来发现冻结攻击（这一期只在 `ticket doctor` 里对超过 `TICKETD_UPDATE_STALE_DAYS` 天没换过的清单标黄）。

## 12. 控制台接入（2026-11-20，T0046）

行为以 `openspec/changes/add-console-enrollment/specs/console-enrollment/spec.md`（入网、退出、连接）和 `specs/console-heartbeat/spec.md`（心跳）为准，设计取舍见同目录的 `design.md`。控制台服务端不在本仓库。这里记实现上的约定（`ticketd/console.py`）。

**默认关闭**：没有配置项、没有环境变量、仓库里没有地址。只有 `~/.ticketd/console/console.json`（地址、设备 id、入网时间、同意的清单版本）和 `device-token` 都在时才算入网；守护进程每拍只看一眼这两个文件，不在就不发请求。`ticket doctor` 发现环境里有 `TICKETD_CONSOLE*` 时提示「没有这个配置项」。

**连接**：所有到控制台的请求（入网、心跳、退出、入网后的自动更新）只经过 `console._open(method, url, headers, body, timeout) -> (status, headers, bytes)`：`ssl.create_default_context()`（系统 CA + 主机名校验，不可关闭；最低 TLS 1.2），先在 TCP socket 上做完 TLS 握手再交给 `HTTPSConnection` 写请求，所以证书校验失败时一个请求字节都没写出；不跟随重定向，不重试。15 秒是整个请求的总时限（到点由定时器断开连接，不只是单次读写的超时）；握手之后的 TLS 错误算网络错误，不报成证书校验失败。`https://` 检查、响应体上限（64KB；自动更新的包按清单里的大小）、JSON 解析和错误码映射在 `_open` 之外。错误按响应里的 `error.code` 判断，不按 HTTP 状态码；401 + `DeviceRevoked` / `DeviceLeft` / `DeviceUnknown` 停止上报（`kv console.state = stopped:<code>`），之后不再发心跳，直到 `--leave` 后重新入网；同时写 `~/.ticketd/console/stopped`，经控制台的自动更新也随之停止。

**入网**（`ticket enroll <地址> <入网码> --release-key SHA256:… --backup-key SHA256:… [--hostname 名字]`）：worker 环境（`TICKETD_TICKET` / `TICKETD_SPOOL`）和非终端标准输入都拒绝；已有 `~/.ticketd/console/` 时拒绝（先 `--leave`）。打印清单 `c1` 和它的 SHA-256（清单文本 UTF-8、`\n` 换行、最后一行也以 `\n` 结尾，打印的就是参与哈希的文本）→ 输入 `yes` → `GET /api/v1/release-keys` 按指纹核对两把公钥（本机已信任另一对时中止）→ `POST /api/v1/enroll/start` → 输入 6 位确认码 → `POST /api/v1/enroll/confirm`（`ConfirmWrong` 和网络失败可以重输，其余错误退出）→ 先写公钥（复用 `update.trust`，已信任同一对时跳过），再写令牌、最后写 `console.json`（目录 700、文件 600、临时文件 + rename）。确认之前任何一步失败，本机都不写任何文件。

**退出**（`ticket enroll --leave`，同样只能在终端里由本人执行）：`POST /api/v1/device/leave` 后删除 `~/.ticketd/console/` 和 `kv` 里的 `console.*`；控制台不可达时问一句是否仍然删除；本机文件不完整（本来就发不出请求）时直接删除；删不掉时报错，不说「已删除」。信任的发版公钥保留。

**心跳**：`console.build_heartbeat(con)` 是唯一的负载来源（`ticket console show` 打印的就是它），字段见 `design.md` D5。`doctor.failed` 用 `cli.DOCTOR_IDS` 里的稳定 id（改名要同步控制台）；其中成本对账要重读所有过程记录，心跳里一小时最多做一次，其余时候用上次的结论。守护进程主循环里的 `console.Heartbeat.tick()`：入网后先在 0～30 秒内报第一次，之后每 300 秒 + 0～30 秒抖动；控制台回的 `next_in` 只在 300～570 之间才采用；发送在后台线程（超时 15 秒），同一时间只有一个。结果写 `kv`（`console.last_ok_at`、`console.last_error`、`console.state`），状态变化记 `events`（`kind=console`，属于细分事件，不进匿名统计）。设备令牌不进任何输出、日志和事件。

**显示**：`ticket console status`；`ticket doctor` 的控制台一项（未入网是 ℹ️，不算问题；吊销、30 分钟没有成功上报、本机文件不完整、找不到 CA 证书是 ❌）。入网的机器在状态卡页脚、结果卡的收起详情里多一行「内部版 v<版本> · 已连接控制台 / 控制台未连接」（最近 30 分钟有成功心跳 = 已连接）。

**自动更新**：本机没配 `TICKETD_UPDATE_URL`、但已入网时，更新源是 `<控制台地址>/api/v1/updates`（静态文件布局和 §11 相同），经 `console._open` 带 `Authorization: Bearer <设备令牌>` 取清单和包；配了 `TICKETD_UPDATE_URL` 时以它为准。一次更新检查开始时就定下走哪条路，检查中途退出控制台，剩下的请求失败，不会改走普通连接去请求控制台。

**凭据不进 worker**（2026-11-22，T0064 调整，原「已知限制」）：原来 worker 的沙箱只限制写、不限制读，被提示词注入的 worker 能读到 `~/.ticketd/console/device-token`；网关上线后，拿到设备令牌就能领网关令牌、用这台设备主人的额度。现在 Claude worker 对 `~/.ticketd/console/` 禁读禁写，入网的机器上不起 Codex worker（§6「凭据目录」）。规矩：
- 设备令牌、网关令牌只放在 `console_dir()` 下，不进环境变量、命令行、工单目录和任何输出。
- 网关令牌由 ticketd 代持；worker 每单只拿一个短时效、只对这一单有效的子令牌（ticketd 自己签发，失效时间不超过单轮时长上限 + 10 分钟），worker 侧的生图工具只认子令牌。实现见下面「生图网关」（2026-11-23，T0078）。
- 沙箱外执行（`--run`）的脚本不受这条限制：用户批准前要看脚本有没有读控制台目录。

worker 判断靠环境变量，worker 去掉变量再用伪终端可以发起 `enroll`（写不了 `~/.ticketd`，最后会失败）。启用条件（信任公钥、受管安装、验签工具）不变。

**下发技能**（2026-11-22，T0069）：入网的机器上，控制台可以按人给 worker 下发技能（Agent Skills 格式：目录 + `SKILL.md` + 可选脚本）和技能要用的凭据，用来把团队内部的工具（比如只在公司内网、只有一把共用口令的服务）开给指定的人。实现在 `ticketd/skills.py`，控制台一侧不在本仓库。
- **只给 worker 用**：装在 `$TICKETD_HOME/skills/current/`，这个目录是一个 Claude Code 插件（`.claude-plugin/plugin.json`，插件名 `larkyard`），Claude worker 每轮以 `--plugin-dir` 加载（技能名带 `larkyard:` 前缀；没装任何技能时不加这个参数）。用户自己的 Claude Code 会话和 bridge 主会话不加载。worker 能读（技能脚本要读凭据），不能写：外层 `$TICKETD_HOME/skills/` 进沙箱的 `denyWrite` 和文件工具的 `Edit/Write/NotebookEdit` deny 规则，授权目录再宽也不放开（否则一张单能改掉别的单要加载的技能）；每轮写设置前先建好空目录（Linux 的 bubblewrap 只能遮住已存在的路径）；外层目录一直留着，换装和删除都只动里面的 `current/` 和临时的 `.new-*` / `.old-*`（bubblewrap 的只读绑定挂在目录上，换掉外层本身，正在跑的 worker 看到的新目录就没有只读保护了）。Codex worker 不加载（入网的机器上本来就不起 Codex）。
- **布局**（`current/` 里）：`skills/<名字>/…`（644）、`secrets/<名字>/<键>`（目录 700、文件 600）、`state.json`（控制台给的摘要、技能名、更新时间）。技能脚本按「本技能目录往上两级的 `secrets/<名字>/`」找凭据。
- **同步**：心跳成功时响应里多一个 `skills`（控制台算的 64 位十六进制摘要；`ok` / `next_in` 之外唯一会读的字段）。和 `state.json` 里的不同才 `GET /api/v1/skills`（设备令牌，响应上限见下面「校验」），校验、验签后在外层目录里写好整套新的、两次 rename 换掉 `current/`；列表为空也照样换（只剩 `state.json`，不再加 `--plugin-dir`），所以没开通的机器只在第一次心跳后领一次。校验：最多 8 个技能、每个最多 64 个文件、合计 2MB（0.3.7 起，原来 512KB；响应上限 4MB，原来 1MB），名字 `[a-z0-9-]`，路径每段不能以点开头、最多 4 层，凭据是可见 ASCII。
- **删除**：控制台给空列表；心跳被拒停止上报（吊销 / 退出 / 不认识，含自动更新发现的；删失败时之后每拍再试）；`ticket enroll --leave`；本机不再入网（手工删了控制台目录）时守护进程下一拍删掉（含换到一半留下的 `.new-*` / `.old-*`）。
- **不动**：心跳响应里没有 `skills`（旧版控制台）或格式不对、领取失败、内容不合规、签名验不过、写盘失败：本机保持原样，记一条控制台事件（`skills_failed`），心跳结果不受影响。
- **看得到**：`ticket console status` 列出装着的技能和更新时间；装上、删掉都记控制台事件。
- **签名**：每个技能都要有发布者的签名，针对 `{"name", "files"}` 的确定性 JSON（`updsig.canonical`），用入网时按指纹信任的 main 发版公钥（`main.allowed_signers`，身份 `larkyard-release`）验，命名空间 `larkyard-skill`（版本清单的签名冒充不了技能）；有一个验不过整批不装，没有信任的公钥也不装。控制台只分发、改不了技能内容；凭据是数据，不签。
- **信任边界**：被攻破的控制台能做的是：给开通的人换凭据、停发、或者重新下发以前签过的旧版本（签名里没有版本号，接受这个风险）；不能塞没签过的内容。所以技能脚本只从下发的凭据里读口令这类数据，不读会改变行为的配置（比如服务地址）。换 main 公钥（§11 换钥）后旧签名全部失效：发布者要用新钥重签，在那之前本机保留原来装的（含旧凭据）。只给 worker、不给用户自己的会话；采集清单不变（领取不上报任何新内容）。

**生图网关**（2026-11-23，T0078）：入网的机器自动接控制台的模型网关（火山图片 / 视频模型，按人记额度），同事什么都不用做。实现在 `ticketd/gateway.py`；控制台一侧见 larkyard-console 的 openspec `add-genimage-gateway`。
- **领网关令牌**：心跳成功后，本机没有网关令牌就凭设备令牌 `POST /api/v1/gateway-token`，把响应里的 `gateway_token`、`base_url`、`sub_id`、`sub_key` 存进 `console_dir()/gateway.json`（600，worker 禁读禁写）。没领到（旧版控制台 404、响应里没有 `sub_key`、网络问题、限流）时记控制台事件 `gateway_failed`，最多每小时再领一次（`kv console.gateway_claim_next`）。领到后每小时用网关令牌 `GET /api/v1/gateway/me` 查一次：401（被管理员吊销、同一设备在别处重新领过、人员离职）就删掉，下次心跳马上重新领；其他错误不动。响应里没有 `sub_key`（旧版控制台）时一天才再领一次。所以控制台的「吊销网关令牌」对还在入网的设备等于换发（防令牌泄露）；要停掉一个人用网关，在导演组里把这个人移出导演组或吊销设备。退出控制台时随控制台目录一起删；停止上报后不再签子令牌。
- **子令牌**：worker 每轮启动时守护进程在本机签（不联网）：`lys_1.<sub_id>.<工单号>.<到期 Unix 秒>.<base64url(HMAC-SHA256(sub_key, 前面这一串))>`，到期 = 现在 + min(单轮时长上限 + 10 分钟, 12 小时)。放进 worker 的环境变量 `LARKYARD_GATEWAY_TOKEN`，网关地址放 `LARKYARD_GATEWAY_URL`（守护进程自己环境里的同名变量不继承）。控制台用令牌摘要和 server-hmac 推出同一把 `sub_key` 验签（库里不存钥匙），用量记到子令牌绑定的工单号；网关令牌失效时子令牌一起失效。子令牌不能提交导演组申请（申请流程已取消）。沙箱外执行的脚本拿不到子令牌。
- **谁能用**：控制台「网关」页的导演组决定。gen-image（控制台对所有入网的人下发，见上面「下发技能」）看到子令牌就先查 `/api/v1/gateway/me`：在导演组走网关；不在导演组、但本机配了自己的火山 key 才用自己的；都没有回「还没开通生图，找管理员」。
- **采集清单不变**：领令牌不上报新内容；走网关的调用是同事主动使用的服务，网关按人、按工单号记用量（T0060 起就是这样）。
- **调整（2026-11-27，T0098 生图触发体检）**：
  - 领失败后不再一律等一小时：连续失败第 1～4 次后分别隔 5、10、15、30 分钟再领，之后每小时一次（`kv console.gateway_claim_fails` 记连续失败次数，领到或被吊销时清零；领到时 `console.gateway_claim_next` 也清零）。原因：刚入网时领失败一次，同事头一个小时就用不了生图；控制台限每台设备每小时 10 次，头一个小时最多领 5 次。旧版控制台（没有 `sub_key`）仍是一天一次。
  - 没有子令牌时 worker 环境里设 `LARKYARD_GATEWAY_STATE`：`not_enrolled`（没入网）、`stopped`（已停止上报）、`pending`（已入网、还没领到）；领到时不设（守护进程环境里的同名变量不继承）。worker 读不到控制台目录、`ticket console status` 在 worker 里看不到入网状态，gen-image 以前只能请同事去终端查；现在按它说一句能照着做的话（找管理员入网 / 10 分钟后再试 / 发诊断包）。值只是这三个代号，不含令牌和路径。
  - worker 约定（`prompts/worker.md`「生图、生视频」）：本机没有 `larkyard:gen-image` 技能时不自己写代码调别的接口、不找 key，报 blocked，按 `LARKYARD_GATEWAY_STATE` 写下一步。

**出错记录和诊断包**（2026-11-23，T0073 调整）：行为以 `openspec/changes/add-console-problem-reports/` 为准。管理员要能看到「谁、哪台电脑、哪一步坏了」，但不碰同事的工作内容。
- **清单 c2**：`console.CONSENTS` 里 c1、c2 并存（c1 文本和哈希不变）。新入网用 c2；本机 `console.json` 的 `consent_version` 决定心跳带不带出错记录。已入网的 c1 设备只有本人在终端执行 `ticket console accept`（打印全文 → `yes` → `POST /api/v1/device/consent`，控制台确认后才改本机）才升级；`ticket console accept c1` 改回。c1 设备的 `ticket console status` / `show` 提示怎么开启。
- **心跳 `problems`**（只在 c2 下出现）：`{"items": [{at, ticket, step, code}…最多 20 条], "more": n}`。来源是本机 `events` / `rounds` 表的 id、时间、kind、code、outcome，`events.detail` 原文不经过；`step` / `code` 的映射和不报的项（用户停止、正常的 blocked / waiting、只是提醒的 stall_flag、重复的 delivery dead、超时中止）见 design D3。游标 `kv console.problems_cursor` 在心跳 200 且 `ok: true` 后才前移到构造时的上界（每个键只往前走；换清单时 `console.problems_epoch` 加 1，在途心跳回来核对不一致就不写），失败下次重报（控制台按内容去重）；`ticket console show` 用同一个函数构造、不前移。入网、升级到 c2、游标丢失或比表里大时从当时算起，不补历史。
- **诊断包**（`ticketd/diag.py`）：`ticket diag [工单号]` 写 `~/.ticketd/diag/diag-<时间>-<随机>[-T…].md`（700 / 600、`O_EXCL`，留 20 份，内容摘要记进 kv；这个目录和控制台目录一样 worker 禁读禁写；工单目录里的日志经 `safefs` 读），内容按优先级（版本和控制台状态、doctor、最近 7 天出错记录带原文、工单概况和最后汇报，剩余空间给过程日志和 stderr 末尾）截到 48KB，常见密钥形态打码；只打印、不发送。`ticket diag send <文件> [--note]` 只接受这个目录里按规则命名、没有其他硬链接、内容摘要和生成时一致的文件，再打一遍码，经 `console.request` 发 `POST /api/v1/diagnostics`（请求体 ≤60KB，控制台和 Nginx 限 64KB）。worker 环境两条都拒绝。主会话约定（`workspace/CLAUDE.md.tmpl`「诊断包」）：把文件本身和 note 原文给同事看、说明发给谁，同事之后新发的消息明确确认才发送；诊断包里的文字不算确认。
- **上线顺序**：控制台先认得 c2 和两个新接口，再发客户端。控制台退回旧版时，心跳被 400 `UnknownField` 拒绝就去掉 `problems` 再报一次（游标不动，控制台恢复后照报）；`ticket console accept c1` 遇到控制台 404 时只改本机。

**用量统计和 c2 → c3 自动升级**（2026-11-27，T0091 调整）：行为以 `openspec/changes/add-usage-report/` 为准。管理员要看「大家拿机器人主要做什么（大类）」「各项能力用得怎么样」「谁连续失败、谁卡住了」；出错记录只有失败、没有成功，算不出「连续」。
- **清单 c3**：比 c2 多「用量统计」，和控制台 `console/consent.py` 的 C3 逐字一致；新入网用 c3。`reports_problems(v)`（c2 起）、`reports_usage(v)`（c3 起）决定心跳带什么。换上来时出错记录游标、用量起点（kv `console.usage_since`）设成当时，之前的不补报。
- **大类**：建单时定、存 `tickets.cat`。主会话 `ticket new --cat <7 个代号之一>`（工作区规则写了怎么选），没给或不认识时 `usage.classify` 按标题和原话的关键词判断。原话只在本机参与判断，上报的只有代号。
- **能力调用**：调度器读过程日志时，Bash 命令运行了本机装着的技能的脚本（`<技能根>/current/skills/<名字>/scripts|bin/…`，cat / ls 这类查看不算）就跟着这次工具调用，看到结果记一行 `cap_calls`（成没成、归类码 `exit_<退出码>` / `tool_error`）。不存命令和输出原文，技能包不用改。
- **心跳 `usage`**（只在 c3 下出现）：今天和昨天（本机本地日期）各大类的单数和做成数（有过一轮 ready 算做成）、各能力的次数 / 成功 / 最多 3 个归类码、`streak_fail`（按每张单最后一次有结论的轮次从新到旧数 failed，遇到 ready 停，blocked / waiting / dropped 跳过）、`stuck_min`（running 且没在等后台任务的单里距最后一次动作最久的分钟数）。控制台不认识 `usage`（400 UnknownField）时去掉它再报，还不行再去掉 `problems`。
- **c2 → c3 自动升级**：管理员决定 c3 只比 c2 多了用量统计（写死的代号和数字，不收文字），算小迭代，不再逐人确认。本机是 c2、还没自动升过（kv `console.consent_auto` 不是当前最新版）时，守护进程每次心跳前走和 `accept` 相同的换清单流程换成 c3，静默更新、不私信（管理员要求；事件里记一条，`ticket console show` 看内容、`ticket console accept c2` 退回）；这一次心跳就按 c3 报。只升一次：任何一次换清单（自动或本人 `accept`）都记下 `console.consent_auto`，本人退回 c2 后不再动。控制台还不认识 c3（ConsentUnknown）就留在 c2、记事件（一天一条），下一次心跳再试，不挡心跳。c1 不自动升（出错记录要本人在终端确认）。先前设计的「机器人私信清单全文、引用回复同意、守护进程去飞书核对」整套不做了。
- **上线顺序**：控制台先上线认识 c3 和 `usage` 的版本（控制台仓库 add-oversight），再发客户端；反过来新同事入网会被拒（控制台不认识 c3）。

## 13. bridge 内化与一行安装（2026-11-20，T0052）

行为以 `openspec/changes/vendor-bridge-one-line-install/specs/`（`vendored-bridge`、`one-line-install`、`bridge-adopt`）为准，设计取舍见同目录的 `design.md`。架构不变：bridge 仍驱动主会话；这一节只管 bridge 怎么来、怎么装、怎么换。

**来源和构建**：`vendor/bridge.lock.json` 写 fork 地址、40 位提交号和版本号。`scripts/build_bridge.py` 只按这个提交构建（检出后核对 `HEAD` 和 `package.json` 版本），输出 `bin/`、`dist/`、`package.json`、`LICENSE`、扁平的生产依赖（pnpm `node-linker=hoisted`，去掉 `.bin`、`.pnpm`、`.modules.yaml` 和依赖里的链接）、`THIRD_PARTY_NOTICES.md`（每个依赖的许可证文件，没有就写 `license` 字段，两者都没有就失败）、`BRIDGE.json`（`repo`、`commit`、`version`、`tree_sha256`）。`tree_sha256` = 按路径排序的「路径、权限（644/755）、内容 SHA-256」逐行再取 SHA-256，`BRIDGE.json` 本身不算，遇到链接或特殊文件就失败；构建、发版和本机共用 `bridge.tree_digest`。没有选单文件打包：`qrcode-terminal` 的八进制转义在 ESM 严格模式下编不过，飞书 SDK 读 `__dirname/../package.json`。

**发版**：tag 带锁文件时 `release.py release` 必须给 `--bridge`，`BRIDGE.json` 的提交等于锁文件、`tree_sha256` 等于目录实际内容才打包；文件放在包内 `vendor/bridge/` 下，按现有规则固定时间和权限（同一 tag、同一 bridge 字节相同），和仓库里被跟踪的文件重名就拒绝。清单字段不变，老客户端照常装（多出来的只是普通文件，`update.extract` 本来就只收普通文件和目录）。实测包约 5.6 MB（bridge 解开约 44 MB）。`publish-installer` 把 `install.sh`（填好默认发布地址）和 `keys/main.pub`、`keys/backup.pub` 放到更新源根目录，脚本里的指纹必须就是这两把（换钥后先改仓库里的 `install.sh`）。

**运行时**：`vendor/runtime.json`（被跟踪、随包签名）写 Node 和 lark-cli 的版本、四个平台（`darwin-arm64`、`darwin-x64`、`linux-arm64`、`linux-x64`）官方包的地址、镜像和 SHA-256。`bridge.ensure_runtime(<版本目录>)`：`runtime/node-v<版本>-<平台>/bin/node` 不在就下载（只跟随 https 重定向、总时长 900 秒、大小 200 MB）、核对 SHA-256、只解出 `bin/node` 和 `LICENSE`，再原子换 `runtime/node` 链接；lark-cli 同理，`bin/lark-cli` 指向 `runtime/lark-cli-<版本>/lark-cli`。下载按 `Content-Length` 判断是否完整：连接中途断开时 urllib 不一定报错、只是提前读到结尾（实测出现过），不完整就同一个地址重试，最多 3 次，再换镜像。SHA-256 才是信任来源，地址不是。

**bridge 服务**：入口是 `~/.ticketd/runtime/node/bin/node ~/.ticketd/bridge/current/bin/lark-channel-bridge.mjs run --profile <p>`，服务名、日志位置和 bridge 自己装的一样（`lark-channel-bridge status/stop` 照常），环境里多一个 `LARKYARD_MANAGED=1` 用来认出是 Larkyard 写的。PATH：固定目录的 Node、`~/.ticketd/bin`、claude（和 codex）所在目录、系统目录。macOS 用 `plistlib` 生成，Linux 的 unit 拒绝含空白和 `% $ " \` 的路径。bridge 每次启动按自己的 Node 和入口重写 `~/.lark-channel/secrets-getter`，所以也指向固定目录；首次启动时 PATH 上有 lark-cli，bridge 自己执行 `config bind`（不在终端里也会做，只是不会用 npm 装 lark-cli）。

**换 bridge**：`bridge/current` 是单独的链接，不直接用 `app/current`：Larkyard 自己更新（切 `current`）时正在跑的 bridge 不受影响。新版本确认后 `update.confirm` 调 `bridge.note_pending`：带的 bridge 和在用的不同（`commit` + `tree_sha256`）就记 `kv bridge.pending`，`ticket doctor` 显示待生效。每天 04:07 的 `ticket bridge restart --if-stale`：相同就什么都不做；不同就先 `ensure_runtime`（失败不切换）→ 切链接、`state.json` 记 `previous` → 服务管理器重启（launchd `kickstart -k`、systemd `restart`）→ 最多 90 秒看 bridge 当天和前一天的日志里重启之后有没有 `ws/connected` 或 `supervisor/profile-online` → 没有就切回 `previous`、再重启、私信主用户、`state.json` 记失败（`ticket doctor` 标红）。不在白天自动重启：bridge 没有「有没有进行中的回复」的对外接口，从外面猜空闲会打断主会话。Larkyard 切回旧版本后，下一个窗口发现不同，bridge 也跟着换回。`update.prune` 保留 `bridge/current` 和 `previous` 指向的版本目录。

**一行安装**：`install.sh`（POSIX sh）只做核对：两把公钥按内置指纹（或 `--release-key` / `--backup-key`）核对 → `ssh-keygen -Y verify` 验通道清单 → 用 Python 检查清单字段 → 下载包、核对大小和 SHA-256 → 用系统 `tar` 只解出包里的 `ticketd/` → `python3 -m ticketd.bootstrap`。任何一步不对就停，`~/.ticketd` 下不写东西。发布地址只收 `https://`（`file://` 用于测试，会警告）。`bootstrap` 用包里同一份 `updsig` 再验一遍，然后：解包到 `versions/<版本>`（`update.unpack`）、拷启动器、切 `current`、写 `update-state.json`（`confirmed`）→ 主公钥或备份公钥和本机信任的不同才 `update.trust` → 序号不小于 `update.max_seq` 就按 `update._accept` 记下这份清单（守护进程第一次检查不会把它当新版本）→ `config.env` 不存在就从示例生成，只补没写过的 `TICKETD_UPDATE_URL` / `TICKETD_UPDATE_CHANNEL` → `ensure_runtime` → 没有 `bridge/current` 就建。已是这个版本只补缺的步骤；本机版本更高就不降级。之后 `install.sh` 从 `/dev/tty` 运行 `ticket setup`（没有终端就提示以后再跑）；带 `--enroll` 时接着 `ticket enroll <--console> <码> --release-key <内置指纹> --backup-key <内置指纹>`。脚本里的变量后面紧跟中文时写 `${变量}`：macOS 的 `/bin/sh`（bash 3.2）在 UTF-8 环境里会把中文字节当成变量名的一部分（`tests/test_install.py` 检查所有 `.sh`）。

**`ticket setup`**（`ticketd/setup.py`）：交互走 `Term`（标准输入不是终端时改读 `/dev/tty`，都没有就只提示）。① `~/.lark-channel/config.json` 已有这个 profile 就跳过，否则把终端交给打包的 bridge `profile create <p> --agent claude --workspace <工作区> [--app-id]`，没有 `active-profile` 再 `profile use`。② 工作区（默认 `~/larkyard-workspace`，已有 profile 时用它的 `workspaces.default`）没有 `CLAUDE.md` 就用 `setup-workspace.sh` 渲染，有就不动。③ bridge 服务文件不存在或是 Larkyard 写的才写并加载（分开安装的不动，提示 `adopt`）；没有每天的重启窗口就装；最多 90 秒等 bridge 连上；ticketd 服务用 `update._install_sh`（受管模式，`LARK_CHANNEL_PROFILE` 传进去；已有服务时沿用它的服务名和环境）；调整（2026-11-21，T0056）：写任何服务之前发现直接跑旧代码的 ticketd 服务（任何服务名）就拒绝，提示 `ticket migrate`（§14）。④ `TICKETD_OWNER` 已有值就跳过；否则最多 60 秒（等 bridge 完成绑定）用 bot 身份 `GET /open-apis/application/v6/applications/<app_id>?user_id_type=open_id` 取 `owner.owner_id`，再试 `GET /open-apis/contact/v3/users/<id>` 取名字，确认后写入；查不到或回答不是，生成 6 位码，读 bridge 日志里提示之后、`preview` 含这个码（前后不是数字）的 `enter` 事件的 `sender`，最多 5 分钟，再确认一次。`config_put` 只填空值或追加，不覆盖已有值。⑤ 默认不做用户授权（`--user-auth` 单独做；调整 2026-11-27，T0107：`auth login` 带上导入文档、传视频要用的权限范围；没授权的由守护进程私信授权链接，见 §5.3「缺飞书用户身份授权时机器人发链接」）。⑥ `ticket doctor`、`ticket doctor --dm`。worker 环境里拒绝。

**迁移**（`ticket bridge adopt`）：前提是受管安装、当前版本带 bridge、bridge 服务文件存在且不是 Larkyard 写的、启动命令里有 `lark-channel-bridge`，否则说明原因退出、不写任何东西。先列出要改的文件，在终端输入 `yes`（`--dry-run` 只列）。用户自己装的每日重启任务按「定时运行、命令里有 `lark-channel-bridge` 和 `restart`」认（launchd 的 `StartCalendarInterval`；systemd 同名 `.timer`），改写成同一个服务名、同一时刻的 `ticket bridge restart`（不带 `--if-stale`，和原来一样每天重启）；没有就新建 `dev.larkyard.bridge-restart`（`--if-stale`）。改前把 bridge 服务、`secrets-getter`、重启任务（含 timer）、ticketd 服务拷到 `~/.ticketd/bridge/adopt-backup/<时间>/`，`state.json` 的 `adopted` 记下每个文件的原路径和备份路径、新建的文件、服务名；先记再改，任何一步失败（含 90 秒内没连上）都自动 `--undo`。`--undo` 先确认备份都在（缺一个就什么都不改），再逐个原子拷回、删掉新建的、重新加载重启任务、ticketd 和 bridge 服务。`~/.lark-channel` 里只碰 `secrets-getter`。

**自检**：`ticket bridge status`；`ticket doctor` 的 `bridge` 一项（加进 `cli.DOCTOR_IDS`）：Larkyard 管理且没有待生效 ✅；待生效 ⚠️；分开安装、没找到服务 ℹ️；服务入口指向不存在的文件、启动命令写死 nvm / npm 路径、上次重启失败 ❌（PATH 里有 nvm 下的 claude / codex 不算：Node 在它们前面）。

**worker**：`ticket setup`、`ticket bridge restart | adopt` 在 worker 环境里拒绝；真正的边界仍是沙箱（worker 写不了 `~/.ticketd`、LaunchAgents、`~/.lark-channel`）。

**对抗审查后的调整**（三路独立审查，修完的）：
- 会重启 bridge 的命令（`ticket bridge restart | adopt`、`--undo`、`ticket setup`）在环境里有 `LARK_CHANNEL=1` 时拒绝：bridge 给它驱动的会话注入这个变量，在主会话里执行会把会话自己杀掉（launchd 停 bridge 时连同子进程），后面的确认和切回都来不及跑。`--dry-run`、`status` 不受限。每天的重启任务的环境里没有这个变量。
- `restart`：服务管理器重启失败也把链接切回、记失败（否则下一个窗口当成「已是这个版本」不再重启）；Larkyard 还在试用期时不换 bridge；在线确认读服务里的 `LARK_CHANNEL_HOME`。launchd 加载前先 `launchctl enable`（`lark-channel-bridge stop` 会 disable）。
- `adopt`：ticketd 服务还在直接跑克隆（不是经 `launcher.py`）就拒绝，先 `ticket update adopt`；有 `running` 的工单就拒绝（要重启 ticketd）；认不出「每天一次」的重启任务（数组、只有分钟）就拒绝；重启任务改写成 `ticket bridge restart --profile <p>`，环境带 `LARK_CHANNEL_HOME`；每一步记 `done`，退回只重新加载真正改过的服务；新建的文件在写之前记进 `created`。
- PATH：bridge 服务是「固定目录 + claude / codex 所在目录 + 原来的目录」（迁移时接上原服务的 PATH，新装时接上当前 PATH 上 node 所在的目录）；`scripts/install.sh` 用固定目录的 Node 时也把原来 node 所在的目录接在后面。否则主会话和 worker 找不到 npm、npx 和 npm 全局装的工具（打包的 Node 只有 `bin/node`）。
- `setup` 重复执行：bridge 服务定义没变就不重新加载，ticketd 服务已经是受管启动器、PATH 已用固定目录就不重装，已有（含用户自己的、迁移改写过的）重启任务就不再建。
- 认主人的码改成 6 位、只认私聊、整句就是「认主人 <码>」（bridge 在权限检查之前就记 `enter`，群聊和陌生人的消息也在里面），确认默认「否」。
- `bootstrap`：本机已是受管安装、版本更低时不切换（交给守护进程按试用期流程装，否则跳过试用期、守护进程还在跑旧代码）；先装运行时再切 `current`；通道上是回滚清单也能装；只在 `config.env` 的通道就是这次的通道时记下清单（别的通道的序号会让守护进程把本通道清单当旧清单）；带 `--enroll` 时不写 `TICKETD_UPDATE_URL`（入网后从控制台更新）。`install.sh` 先探测 `ssh-keygen -Y`（OpenSSH 8.1+），`--help` 不再读 `$0`。
- `publish-installer` 只发提交里的 `install.sh`（工作树要干净），发布地址不能含 `$ \` " \\` 和空白。`bridge.lock.json` 不是合法 JSON 时报错，不当成没有锁文件。
- 留着的：会议里没授权的人的字幕和弹幕仍会进入授权用户那次 run 的上下文（会议智能体本来就要读会议内容）；`ticket doctor` 的 bridge 一项只看文件和上次重启结果，不实时确认在线；Linux 上 unit 目录用当前 shell 的 `XDG_CONFIG_HOME`（和 `scripts/install.sh` 问 systemd 的做法不同，两者不一致时要手动对齐）。

**调整（2026-11-21，T0054）**：bridge 服务环境里多一个 `LARKYARD_TICKET_BIN=~/.ticketd/app/current/bin/ticket`（跟着版本走的 `current`，和每天的重启任务用的是同一个路径）。fork 上的会议纪要卡片有「派成工单」按钮（`/meeting todo <存档号>#<序号>`，只有管理员能点），bridge 用它执行 `ticket new`；没有这个变量时 bridge 退回 PATH 上的 `ticket`，而服务的 PATH 里没有它。已装好的机器重新执行一次 `ticket setup`（服务定义变了才重新加载 bridge）或 `ticket bridge adopt` 之后生效。

**没实测的**（单测和临时 HOME 全流程只用了替身）：真实租户上的扫码建应用、bot 身份查应用所有者、真实 launchd 下的 bridge 重启和在线确认、Linux 上的整套服务。

## 14. 升级已有安装（2026-11-21，T0056）

行为以 `openspec/changes/migrate-existing-install/specs/migrate-existing/spec.md` 为准，取舍见同目录 `design.md`。

**为什么要单独一条路径**：`install.sh` → `bootstrap` 只管文件（解包、切 `current`、写受管状态），不碰服务。之后把服务改成经启动器运行的只有 `ticket setup` 第 ③ 步和 `ticket update adopt`；两者都按 `TICKETD_LABEL`（默认 `dev.larkyard.ticketd`）找服务，也不知道旧代码里写死了什么。实测（T0056，用 0.3.0 的代码和替身服务管理器）：服务名是早期自定义名（如 `com.example.ticketd`）的机器上，`setup` 第 ③ 步照默认名再装一个 `dev.larkyard.ticketd`（两个服务抢 `daemon.lock`），`ticket bridge adopt` 找不到默认名的 ticketd 服务就跳过第 4 步、不拦；服务名相同的机器被 `setup` 静默改成经启动器运行，但主会话约定里的 `ticket` 路径和写死在代码里的主用户都没人管。

**`ticket migrate`**（`ticketd/migrate.py`）：
- 认服务：服务目录里启动命令有一项（含空白的只认存在的文件路径，`sh -c "…"` 包装不认）以 `/bin/ticketd` 结尾、或就是 `~/.ticketd/app/launcher.py`，且 `TICKETD_HOME`（没有就是 `~/.ticketd`）等于本机数据目录的服务，名字不限；服务名取 plist 的 `Label`（文件名可能不同）；多于一个就拒绝。
- 对照配置：`python3 -I` 跑旧代码的 `config.owner()` / `config.workroot()`，环境只给服务里的变量加 `HOME` 和系统 PATH（守护进程看到的就是这些），30 秒超时；和新版本按 `config.env` 会用的值不同才写（主用户为空不写，路径比较前展开 `~`）。服务环境里其他 `TICKETD_*`（`TICKETD_HOME` 等四个服务变量除外）`config.env` 里没有就搬过去。冲突时以旧代码在用的为准，计划里写出原值。
- 只读计划：数据库只用 `mode=ro` 打开查 `running` 的工单（读不了也算挡路）；旧代码用 `python3 -I -B` 跑（不写 `__pycache__`）；`cli.main` 在打开数据库之前处理 `migrate`。挡路：多个服务、正在运行的工单、受管安装版本比要装的旧（交给守护进程的试用期流程）、服务跑克隆但已有更旧的 `current`（`bootstrap` 不会切它）、定不出主用户（旧代码读不出、`config.env` 里也没有：迁移后会拒绝所有派单）、已经迁移过、默认服务名的文件已被别的服务占用。服务跑旧代码目录时计划里打出它的版本号、`git log -1`、`git status -s`（`GIT_OPTIONAL_LOCKS=0`，不回写索引；没有 git——macOS 没装命令行工具时 `/usr/bin/git` 是会弹窗的占位程序，先看 `xcode-select -p`——或不是 git 目录就只给版本号），有本地改动时提醒迁移后跑的是发布版、不含这些改动（不挡：改动留在旧目录）。
- 动手：备份旧服务文件、`config.env`、主会话约定到 `~/.ticketd/migrate/backup/<时间>/`，`~/.ticketd/migrate/state.json` 先记再改、每步记 `done` → 卸载旧服务，`flock` 拿得到 `daemon.lock` 才算旧进程退出（最多 90 秒）→ SQLite 备份接口拷库 → 按键写 `config.env`（记原值；更新源和通道只在没有时写）→ `bootstrap.finish_db`（一行安装推迟的信任公钥、记下清单）→ 服务名不同就删掉旧服务文件 → `update._install_sh(current, app, {label: dev.larkyard.ticketd, env: 旧服务的四个服务变量})` → 受管状态记 `label`、`service_env`、`migrated_from` → `update._rewrite` 改主会话约定里的 `<旧代码目录>/bin/ticket`（含经符号链接的写法）→ 90 秒内要有新心跳。停服务前再查一次正在运行的工单。以上任何一步失败（含 Ctrl-C）按 `done` 退回，记录记 `rolled-back`（可以直接重来）；这次 `install.sh` 才装的 `current`（`--installed-now`）也撤掉。然后 bridge 服务不是 Larkyard 写的、启动命令里有 `lark-channel-bridge`、重启任务都是「每天一次」就调 `bridge.adopt`（Homebrew / npm / 其他只影响计划里的说明）；它失败时由它自己退回，`migrate` 记 `bridge: false`、提示 ticketd 已升级好。
- `--undo`：有正在运行的工单、或原来的代码目录不在了（旧服务要跑它）就不动；先 `bridge.undo_adopt`（做过的话），再卸载新服务并删掉它的文件（服务名不同时）、改回主会话约定、按键改回 `config.env`（迁移前没有这个文件且改完为空就删掉）、放回旧服务文件并加载；这次安装才建的 `current` 和 `launcher.py` 撤掉（版本目录留着），受管状态记 `unmigrated`。数据库不改回。
- worker 里一律拒绝；动手和 `--undo` 在 `LARK_CHANNEL=1` 时拒绝。

**`install.sh --upgrade-existing [--dry-run]`**：核对公钥、签名、包之后、写 `~/.ticketd` 之前，用包里的代码 `python3 -m ticketd.migrate --check --target <版本>`；不通过就停。`--dry-run` 到此为止。否则从 `/dev/tty` 读 `yes` → `bootstrap --upgrade-existing`（照常解包、运行时、切 `current`，但不写 `config.env`、不打开数据库：旧守护进程还在跑，信任公钥和记下清单写进 `app/bootstrap-db.json` 留给 `migrate`）→ `ticket migrate --yes --update-url <地址> --channel <通道> [--installed-now]`（`--installed-now`：安装前没有 `current`，迁移没成或退回时撤掉）。`<地址>`：`--from` 是 `https://` 就用它；是本地发布目录（`file://`）时改用脚本内置的正式发布地址（没有就不写）——守护进程由 launchd 启动，读「下载」这类目录会被系统隐私权限拦，目录也可能被删。`file://` 地址里的空格换成 `%20`（curl 不收）。`LARK_CHANNEL=1`（非 `--dry-run`）在核对之前就拒绝；不和 `--enroll` 一起用。

**`ticket setup`**：在扫码、写工作区和服务之前（`Wizard.check_existing`），发现直接跑旧代码的 ticketd 服务（任何服务名）就拒绝，提示 `ticket migrate`。

**对抗审查后的调整**（两路独立审查，已修）：`bootstrap` 原先在旧守护进程还在跑时就打开数据库（建表加列，快照拍到的已是改过的库）、在 `migrate` 备份之前从示例生成 `config.env`——改为升级模式推迟；自动退回后记录卡在 `applying`、重跑被当成「已经迁移过」；`current` 已被撤掉时提示里的退回命令不存在；主用户读不出时只警告（改为挡路），`config.env` 里的空值被当成「已设置」；服务名取文件名而不是 `Label`；`launcher.py` 匹配过宽；确认之后不再查正在运行的工单；bridge 那一步的非 `BridgeError` 异常让记录停在中间；`TICKETD_HOME` 带 `~` 原样传给 `install.sh`；`setup` 的拦截在扫码之后；更新源写成「下载」里的目录。

**验证**：`tests/test_migrate.py`（两种布局 × 两个平台、退回逐字节、各种失败退回）。临时 HOME 全流程（工单目录里的模拟脚本，不进仓库）：早期私有版（真实的旧代码和未提交改动、旧代码自己的 `install.sh` 装的服务）和 clone 装的 Larkyard 0.2.0 各一遍，真实签名包、真实 `install.sh`、真实守护进程，launchd 是按 plist 拉起进程的替身；只读检查后 HOME 下文件和库结构不变；升级后派单、新单落在原工单根目录；退回后旧代码在迁移过的库上照常运行。**没实测**：真实 launchd、真实 bridge 重启和在线确认（替身只写一条「已连上」日志）、Linux 上的整套服务。

**离线升级包**（0.3.2）：`release.py bundle --feed <更新源> --out <目录> [--channel stable] [--readme <文件>]…` 从更新源取一个通道当前的清单、签名、包，加上 `install.sh`、两把公钥、`feed/CHANNEL`（通道名）和入口 `upgrade.sh`（= `scripts/upgrade-offline.sh`），放进 `larkyard-<版本>-upgrade/` 和同名 zip（zip 里记 unix 权限位，解压后可执行位保留）。打包前核对：清单用 feed 里的 `keys/main.pub` 验签，包名合规，包的大小和摘要对得上清单，`install.sh` 内置的两个指纹就是这两把公钥的，`install.sh` 已填发布地址；写进升级包的就是核对过的那几份字节（不从更新源重读）；只打发版清单（回滚清单有有效期）；目标已存在、说明文件和升级包里的文件重名都拒绝，失败不留临时文件。`install.sh` 也在 zip 里，所以 zip 本身的完整性靠传递渠道（`bundle` 打印 zip 的 sha256），不另放 `install.sh.sha256`。`upgrade.sh` 不检查路径：`DIR` 用 `CDPATH='' cd -- … && pwd -P` 求出，通道取 `feed/CHANNEL`（不认识就用 stable），`exec /bin/sh "${DIR}/feed/install.sh" --from-dir "${DIR}/feed" --channel "${CHANNEL}" --upgrade-existing ${1+"$@"}`（`"${@}"` 在 bash 3.2 + `set -u` + 无参数时报 unbound variable）。`install.sh --from-dir <目录>`：找到 Python 之后用 `pathlib.Path(目录).resolve().as_uri()` 转成 `file://` 地址（整条路径按 RFC 3986 转义），不和 `--from` 一起用；`--from file://…` 保留原来只转空格的行为。`install.sh` 里的 shell 变量一律写 `${VAR}`（单引号里的 awk 字段、`"$@"` `$*` 除外）；内联的 Python（`-c`、`- <<PYEOF`）一律 `-I`，当前目录（比如「下载」）里的 `pathlib.py` / `json.py` 顶替不了标准库（`-m ticketd.*` 先 `cd` 进自己解出的临时目录）。测试 `tests/test_offline_upgrade.py`：升级包放在 `下载 含空格`、`a#b %20c?d`、`升级 包（副本）` 下，HOME 和 TMPDIR 也带空格，跑 `--dry-run`（没有旧服务、有旧克隆服务两种）、`--no-setup` 安装、无参数转发、当前目录里放了顶替标准库的模块、`early` 通道的升级包。

**留着的**：主会话约定只改 `ticket` 路径，内容不和模板同步（手写的约定可能有私人内容；`ticket doctor` 会提示，由人决定是否 `setup-workspace.sh --force`）——**调整（2026-11-26，T0084）见下面「主会话约定自动同步」**；Node 和 lark-cli 仍要在线下载；`--undo` 之后再迁移会重新认服务、重新备份。

### 主会话约定自动同步（调整 2026-11-26，T0084）

**为什么**：主会话约定只在装机时渲染一次，之后升级不变，模板里新加的规则到不了已经装好的电脑。实测 11-26：发布者自己的电脑停在 11-08 那一版，两位同事的主会话不知道「生图一律派单」，在自己的会话里跑生图工具，拿不到网关子令牌（§12 只发给工单），就回同事「这台电脑还没开通，找管理员加导演组」，还拿聊天里别的网关的令牌去试；同事已经在导演组，却一直出不了片。

**怎么做**（`ticketd/workspace.py`）：守护进程每次启动（升级后由启动器拉起的那次也算）看一眼 bridge 工作目录的 `CLAUDE.md`：
- 和本版模板渲染结果一致：不动。
- 把根目录换回 `@LARKYARD@` 后，逐字等于 `workspace/CLAUDE.md.history` 里某一版发过的模板（sha256）：说明没人手改过，备份成 `CLAUDE.md.bak-<时间>`（`O_EXCL`、保留原权限），换成本版模板。根目录先试本机已知的（`current`、本仓库，路径里有空格也认得），再试从 `<根>/bin/ticket` 认出来的；换的时候沿用文件里原来的根目录（它的 `bin/ticket` 不在了才用 `current`），只换内容、不换主会话用的那份 `ticket`。根目录混着两种写法、是符号链接或硬链接、备份后文件又被改了（备份随之删掉）：不动。
- 只在受管安装里做：开发时从克隆或 worktree 起的守护进程不碰真实的主会话约定（单测也清掉 `TICKETD_WORKSPACE`）。非受管时 doctor 提示用 `setup-workspace.sh --force`。
- 其他（手改过、没有工作目录）：不动；`ticket doctor` 照旧提示（没改过的旧版提示「下次启动自动换」）。私人约定一直写在 `CLAUDE.local.md`，不受影响。
- 改模板时把改之前那一版的摘要追加进 `CLAUDE.md.history`；`tests/test_workspace.py` 在完整克隆里核对每个提交过的模板都在里面。

**模板这次加的规则**：生图、生视频、改图一律派单，不在主会话里先试；不说「没开通 / 找管理员」，不找 key、不试别的网关。gen-image 那边配合：不在工单里时报 `NotInTicket`（「这里不是 Larkyard 工单，要派单，不代表没开通」），探测命令不再去探模型。

