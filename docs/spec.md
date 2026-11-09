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
▲                │  ticket new / send / stop / hold / release / grant / show / ls
│                ▼
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

## 3. 路径与部署

| 用途 | 路径 |
|---|---|
| 代码 | 本仓库（Python 3 标准库，零依赖） |
| 个人配置 | `~/.ticketd/config.env`（`TICKETD_HOME` 可改；同名环境变量优先） |
| 状态库与服务日志 | `~/.ticketd/db/ticketd.db`、`~/.ticketd/logs/` |
| 工单目录 | `$TICKETD_WORKROOT/<编号>-<短名>/`（默认 `~/larkyard-tickets`，编号如 `T0001`） |
| launchd（macOS） | `~/Library/LaunchAgents/<TICKETD_LABEL>.plist`（默认 `dev.larkyard.ticketd`），`KeepAlive=true`，`RunAtLoad=true` |
| systemd（Linux / WSL2） | `~/.config/systemd/user/<TICKETD_LABEL>.service`，`Restart=always`，`WantedBy=default.target`（见 §3.1） |

plist / unit 中注入当前 bridge profile 的环境变量（`LARK_CHANNEL=1`、`LARK_CHANNEL_HOME`、`LARK_CHANNEL_PROFILE`、`LARK_CHANNEL_CONFIG`、`LARKSUITE_CLI_CONFIG_DIR`），值与 bridge 注入的一致；同时注入包含 nvm node 和 `~/.local/bin` 的 `PATH`。这些变量只设置，不 unset，也不改用普通 profile。

### 3.1 Linux / WSL2（2026-11-02，T0016）

用户选了「先做 Linux 支持，Windows 通过 WSL2 来用」。目标是开源版在 Linux（含 WSL2）上能装能跑，macOS 行为不变（生成的 plist、worker 沙箱设置、ps / lsof 调用都和之前逐字节一致）。

- **平台差异只放四处**：`ticketd/host.py`（平台判断、lark-cli 数据目录、`/tmp` 的真实路径）、`ticketd/procs.py`（查看进程）、`ticketd/perms.py`（沙箱设置）、`scripts/install.sh` / `uninstall.sh`（服务）。单测通过 mock `host.system()` 在任一系统上测两个平台的分支；CI（`.github/workflows/tests.yml`）在 ubuntu-latest 和 macos-latest 上跑全部单测，并在 Ubuntu 上真装一次 systemd 用户服务。
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

**发送链路加固（T0012 对抗审查 F1/F2）**：① 结果卡发送遇到**任何** `error.type == "api"` 的飞书业务错误（不再只认 `PERMANENT_CODES` 白名单）都当「卡片被拒」，马上改发文字（幂等键 `-t`）；结果覆盖状态卡时遇到业务错误，不在状态卡上反复重试，改发一张新的结果卡（再被拒就走文字兜底）；卡片/视频行重试满 24 小时、标 dead 之前，先试一次文字兜底，送到了就算 sent。② 发卡前上传图片/视频遇到网络/超时（错误信息含「调用 lark-cli 失败」或 `"type": "network"`）：**不降级**，只把已成功的 img_key / 云空间链接写回发件箱（链接记在视频占位的 `ticketd_video_url` 上，下次只补封面、不重传视频），一行里第一次瞬时失败就停止后续上传，整行按 attempts 退避重试；连续 `MEDIA_MAX_ATTEMPTS`（3）次后才降级（图换灰字、视频另发）。文件不合格、飞书明确拒收等非瞬时失败照旧立即降级。

**恢复（真实守护进程 + 故障注入实测后）**：① 回复目标被撤回（飞书 230011）：这一单还没发出的消息全部改为直接发到会话（`reply_to` 换成 `oc_` 会话 id，`lark-cli im +messages-send --chat-id`），卡片/视频原样改投，不退成文字；状态卡同样改投。② lark-cli 标了 `"retryable": true` 的业务错误（如限流）按瞬时失败退避，不当成卡片被拒。③ 降级改为「失败满 3 次**且**这一行已等了 `MEDIA_DEGRADE_AFTER`（600 秒）」（原来断网约 35 秒就降级）。④ 每行每个 tick 开始新上传的时间上限 `MEDIA_BUDGET`（60 秒），超了把进度写回、下个 tick 接着传。⑤ 云空间视频按「工单 + 内容 sha256」缓存链接（表 `drive_links`，7 天），同一个视频再汇报不重复上传；上限 100MB，超时按大小放宽（每 MB 加 3 秒）。⑥ 发送异常的毒丸行标 dead 之前先试一次文字兜底；lark-cli 输出按 `errors="replace"` 解码。已知：上传进行中守护进程被强杀时，重启后会再传一次，云空间里多一个文件。

**媒体通道与 SQLite 异常分类（2026-11-04，T0022，回应外部代码审查）**：① `MEDIA_BUDGET` 只决定还开不开始下一次上传，一次云空间上传本身可以跑到 `_drive_timeout`（100MB 约 420 秒），稿子导入每个最长 90 秒，原来这段时间整个主循环（派单、状态卡、心跳、其他工单的投递）都停着。现在发件箱分两条通道：守护进程里的独立线程 `daemon.MediaLane` 用自己的数据库连接跑 `outbox.flush(lane="media")`，只做**慢的准备**——结果卡里图片/视频占位的上传（写回发件箱）和稿子导入（进 `published` 缓存）；视频消息（lark-cli 上传和发送是同一条命令）整条在这里发。主循环跑 `flush(lane="fast")`，负责其余所有发送，包括把结果覆盖到状态卡：这一步必须和 `card.refresh` 在同一线程里，否则两边 PATCH 同一张卡会互相覆盖；主循环里只用缓存里的稿子链接，不导入。同一工单仍按 id 顺序（每单只取最早一条待发消息，它没准备好，同单后面的消息就等着）；两条通道用进程内认领集合互斥，认领后再按库里最新状态核一次。导入失败的稿子不会让一行一直留在媒体通道（进程内记一次，重启后再试）。`MEDIA_BUDGET` 保留，只用于媒体通道里各行轮流。媒体线程意外退出时主循环下一轮重启它并告警；停止服务时最多等 5 秒，不等正在进行的上传。② `flush` 原来把所有 `sqlite3.OperationalError` 都当「库被锁」，24 小时内每 30 秒重试且 `break` 掉整个队列。现在 `db.is_busy` 只认 SQLITE_BUSY / SQLITE_LOCKED（Python 3.11+ 看错误码，3.9 看 sqlite 的固定报错文字），锁等待照旧退避；磁盘 I/O、只读、库满、缺表缺列等故障告警「数据库故障」，这一行单独退避（30 秒起翻倍、最多 10 分钟，同时记在内存里，库写不进去也生效），不挡其他工单；同一行连续 3 次且已等满 10 分钟仍出错才按毒丸处理（先文字兜底）。库连退避都写不进去时本轮暂停这条通道；已经发出去但结果写不进库（含毒丸的文字兜底）时只在内存里退避，到点用同一幂等键重试，不再每轮重发。稿子缓存读写遇到数据库错误不再当成「导入失败」吞掉，交给上面的分类。

**调整：媒体通道改成有并发上限的任务池（2026-11-04，T0023，回应外部代码审查 P2）**：T0022 的媒体通道只有一个线程、逐行串行，解决了「一个慢上传拖住整个守护进程」，没解决「一个慢上传拖住其他工单的媒体交付」：A 传 100MB 视频的 7 分钟里，B 的图片、C 的稿子也要排队。现在 `daemon.MediaLane` 是一个调度线程加若干任务线程：调度线程每秒用 `outbox.media_jobs` 取出要准备媒体、没人在处理、不在退避中的行，认领后各开一个任务线程跑 `outbox.prepare`（每个任务自己的数据库连接，写入仍是短事务，做完就关）。① 并发上限 `TICKETD_MEDIA_WORKERS`（默认 2）；其中传视频的（视频消息，或卡片里还有没拿到云空间链接的视频占位，`outbox.is_video_job`）另有上限 `TICKETD_MEDIA_VIDEO_WORKERS`（默认 1，不超过总数），限制带宽、内存和飞书限流。默认配置下一个大视频在传时，另一个名额留给图片和稿子。② 同一工单仍严格按顺序：候选仍是每单最早一条待发消息，调度时再按工单去重（同一工单同时最多一个任务，含下面说的卡死任务）。③ 轮转：候选按「上次开始准备的时间」排序，从没处理过的先上；一张多视频卡用完 60 秒预算回到队列后排在别的行后面（原来总是 id 小的先上）。④ 某个任务遇到网络/超时或库被占用，调度暂停一轮（`interval`，默认 1 秒）再派新任务，对应原来单线程里的「本轮不再试其余行」。⑤ 卡死检测：任务跑满 900 秒（正常最长约 9 分钟：100MB 上传 420 秒 + 查链接、封面）还没结束，告警并让出它的名额，这一行保持认领、不会被重复处理，等它结束（再告警一次）或重启服务；调度线程 120 秒没转一圈，主循环告警（恢复时也告警一次）。Python 线程杀不掉，所以只能让出名额、告警，不能强停。⑥ 停止服务时最多等 5 秒（调度线程和任务线程共用），不等传完的上传，行为同前。`outbox.flush(lane="media")` 保留为单线程版本（测试用）。

**调整：稿子导入失败写在卡片上（2026-11-08，issue #12）**：原来导入失败只写告警，结果卡上没有「打开」按钮也没有任何说明。现在失败原因按文件版本（路径 + 修改时间 + 大小）记进 `doc_failures` 表；发结果时，没有缓存链接、但当前版本有失败记录的稿子，在稿子按钮的位置加一行灰字「📄 <文件名> 没能导入云文档：<原因>。本机文件：<路径>」，文字兜底里同样带上。原因取 lark-cli 报错 JSON 里的 `message`（网络 / 超时统一写「调用 lark-cli 失败（网络或超时）」），最多 80 字。读失败记录是只读的，表不存在时当作没有失败。

**调整：改过的稿子覆盖原云文档（2026-11-07，issue #13）**：稿子缓存键是「路径 + 修改时间 + 大小」，文件一改就重新 `drive +import` 一份新文档，另一台机器上一份 `结果.md` 被导入了 7 次，云空间里堆满同名文档。现在新表 `doc_targets(ticket_id, path, url)` 记每张单每个稿子路径当前对应的云文档：同一张单同一路径的稿子再次汇报且内容变了时，`.md` / `.markdown` / `.txt` 用 `lark-cli docs +update --as user --command overwrite --doc-format markdown --content @<文件>` 整篇覆盖原文档（链接不变，旧版本在飞书文档历史里，旧卡片上的链接也指向最新版）；其他格式（docx / xlsx / pptx）或覆盖失败（原文档被删、没有权限等，写告警）时另导入一份，名字后面加「（MM-DD HH:MM）」以便区分，并把它记成新的当前文档。

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
- 申请：worker 把命令写成工单目录里的脚本（≤8KB、UTF-8），`ticket report --status blocked --summary … --run <脚本>`。CLI 在沙箱里读出脚本，连内容一起写进待收件；守护进程收汇报时存进 `runs` 表（`status=pending`，记 `report_id`）。没带 `--ask` 时默认问「要我在沙箱外执行上面这段脚本吗？同意就回复「执行」…」。`--run` 只能配 `blocked`。
- 展示：结果卡在「👉 需要你」下面放一个默认展开的面板，标题「🖥️ 申请在沙箱外执行（Rn · 路径）」，正文是完整脚本（plain_text，原样含缩进），下面一行说明批准后会发生什么；文字兜底消息里同样带脚本。`ticket show` 显示「沙箱外执行 Rn（路径）：待批准 / 已批准 / 执行中」。
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
- 文件工具：生成的 settings 只对上述目录开放 `Edit` 和 `Write`；`Read`、`Glob`、`Grep`、`WebFetch`、`WebSearch` 全部放开。
- shell：启用 Claude Code sandbox，文件写入只允许上述目录加临时目录。
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
- **调整（2026-11-09）成本对账**：`metrics.reconcile_costs` 从每张不在运行中的单的 `.stream.jsonl` 按同样的规则重算成本，和 `tickets.cost_usd` 比对（误差 1 美分以内）。`ticket doctor` 报对不上的单，`scripts/selftest.py` 在工单类检查之后也核对一遍。重启、异常退出、换会话、重复读事件，任何一处记账出错都会在这里露出来。首次在线上核对 33 张单全部一致。
- **调整（2026-11-09）主会话成本**：`ticket stats` 和每周概况加一行主会话（bridge 会话）的当前累计成本，按 bridge 日志里每轮的 `usage` 事件（`costUsd` 是会话累计值，取每个聊天最近一次；私聊 / 群聊 N，不写聊天 id）；任一 ≥ `TICKETD_COST_HINT_USD` 时建议在那个聊天里发 `/new`。日志是上游格式，只读，读不到就不显示这一行。起因：复盘里同一个主会话累计到 $35。

## 8. 容错

| 情况 | 处理 |
|---|---|
| 模型接口或网络临时故障（进程退出，stderr 含 429/5xx/超时/连接错误；**调整 2026-11-07**：断网 / DNS 失败 `ENOTFOUND`、`EAI_AGAIN`、「Can't reach the API server」也算，issue #10） | 重试 3 次，间隔 10s、40s、160s：会话已建立则 `--resume` 并发「上一轮因网络中断，请继续」；首轮未建立会话则用同一 `--session-id` 重跑任务卡 |
| worker 没交汇报就退出 | 记 `failed`，附最后一段 assistant 文本 |
| 模型接口账户级故障（报错含余额不足、认证失败等，见 `worker.ACCOUNT_MARKERS`）（**调整 2026-11-07**） | 不重试（重试不会好）。记 `failed`：「模型接口余额不足/认证失败，这一轮没做完：<报错原文>」，`--ask`「处理好之后引用这条消息回复「继续」」。原来只写「worker 未交汇报就退出（code=1）」，T0005/T0015/T0017 的用户看不出要做什么。过程日志里 `is_error` 的一轮写「本轮结束：出错（…）」，不再照抄 subtype 写 success（issue #15） |
| **调整（2026-11-07）故障等待**：短重试用完的临时故障，以及账户级故障 | 不再直接失败，进入故障等待（`tickets.outage_since` 记开始时刻）：每 `TICKETD_OUTAGE_RETRY_EVERY`（默认 900 秒）自动续跑一次（会话已建立则加一条 `retry` 输入，否则重投原输入），最长 `TICKETD_OUTAGE_RETRY_HOURS`（默认 24 小时，0 = 关掉故障等待）。进入时记一份 `waiting` 汇报推给用户一次：账户级故障带 `--ask`「处理好之后会自动接着做（每 N 分钟试一次，最晚到 MM-DD HH:MM）；也可以引用回复「继续」马上重试」，网络故障只在摘要里说明、不打扰用户做事；之后的自动重试只写过程日志。任一轮交了汇报、用户追问、恢复、换会话都清掉故障等待；用户追问还会清掉重试时间点，马上投递。超过时限记 `failed`（账户级沿用上一行的文案；网络故障写「自动重试 N 小时仍未恢复」并带「恢复后回复继续」）。起因：余额不足、断网时工单直接失败，要人发现后手动「继续」，另一台机器上隔了 3 小时和 6.5 小时才被发现（issue #10） |
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
