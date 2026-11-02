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
                 │  ticket new / send / stop / hold / release / grant / show / ls
                 ▼
          SQLite（~/.ticketd/db/ticketd.db） ◀──轮询（1s）── ticketd（launchd 托管）
                                                        ├─ worker：claude -p（每单一个会话 + 目录）
                                                        └─ 发件箱：lark-cli 回复原消息，失败重试
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
| launchd | `~/Library/LaunchAgents/<TICKETD_LABEL>.plist`（默认 `dev.larkyard.ticketd`），`KeepAlive=true`，`RunAtLoad=true` |

plist 中注入当前 bridge profile 的环境变量（`LARK_CHANNEL=1`、`LARK_CHANNEL_HOME`、`LARK_CHANNEL_PROFILE`、`LARK_CHANNEL_CONFIG`、`LARKSUITE_CLI_CONFIG_DIR`），值与 bridge 注入的一致；同时注入包含 nvm node 和 `~/.local/bin` 的 `PATH`。这些变量只设置，不 unset，也不改用普通 profile。

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

**上传云空间失败**（用户身份不可用、超过 20MB 等）时退回视频消息：卡片里放封面 + 一行说明，并在卡片之后补排一条发件箱消息（`outbox.media_json`，`report_id` 为空，id 更大所以排在卡片后面）：`lark-cli im +messages-reply --as bot --video ./video.mp4 --video-cover ./cover.<ext>`，没有封面时退成 `--file`。安全规则同图片：只认工单目录内、非符号链接、非硬链接、文件头是 mp4（`ftyp`）的普通文件（云空间 ≤20MB，视频消息 ≤30MB），交给 lark-cli 的是私有临时副本（固定文件名 `video.mp4`），超时 180 秒。本地检查不过或飞书永久拒收时，改发一行文字「🎬 … 的视频没能直接发出，文件在：…」（幂等键加 `-t`）；网络失败照常退避重试（同一幂等键），连续失败 3 次后改发兜底文字（每次尝试会阻塞守护进程最多 180 秒，并挡住同单后面的消息）。读文件时除了检查路径，还用 F_GETPATH（Linux 为 /proc/self/fd）核对实际打开的文件仍在工单目录内，防止中间某级目录被换成符号链接（图片同样适用）。视频消息的 message_id 也写入 `bindings`，引用它同样能追问。

**结果卡改版（画廊风）**：worker 可控文字仍一律 plain_text。① **有图片/视频的卡（画廊）**：媒体占主体放最上面——视频是一个可点容器（不裁剪的圆角大封面 + 通栏「▶ 播放视频」按钮，点哪都打开云空间播放）；单张图不裁剪、圆角、可点开放大；多张图两列网格、不裁剪，每张下面一个圆形编号（`<number_tag>`，内容是我们生成的数字，方便回复「2」），奇数张补空列；卡里已有视频时，其余图作三列小缩略图跟在后面。媒体下面只留**一行结论**（摘要第一句，`heading-4`，最多 2 行），再下面是「👉 需要你」浅色块（blocked 橙、其余蓝），然后链接按钮和稿子按钮，最后一个默认收起的「详情」面板（摘要其余部分 + 本机文件路径）。② **没有媒体的卡（文档）**：结论放大 → 需要你 → 细节 → 链接 / 稿子按钮 → 收起的「📎 本机文件」。页脚过程轨迹不再加分割线。图片占位可以嵌在 column 里，发件箱递归替换，上传失败的图换成一行灰字「（图 N 未能显示）」。

- **调整（2026-11-01，T0005）**：worker 运行期间每个 tick 都收一次 `.reports/`，终态汇报马上发出，不再等进程退出。起因：T0004 的 worker 03:51 已汇报，claude 进程却在等后台子代理，到 04:09 才退出，期间结果没发、`ticket show` 一直显示 running。现在还加了一条：已有终态汇报、且最后一个 stream 事件是 `result`（本轮已结束、进程只是挂着）持续 `TICKETD_LINGER_GRACE`（默认 60 秒）后，结束该进程。

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
| `ticket grant <id> <dir>` | 增加可写目录，加一条 `grant` 输入，重新生成权限配置 |

只有用户在对话里明确说了，主会话才执行 stop、hold、release 和 grant。

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
- 卡片的 message_id 写入 `bindings`，引用卡片追问能路由到工单。
- 主动推新消息只有三种：终态汇报（同 §5.3）、需要用户决策（blocked 或带 `--ask`）、疑似卡死（运行中 `TICKETD_STALL_SECONDS`（默认 600 秒）没有任何工具调用，含子代理；每次安静期只推一次，只提醒不杀）。

## 8. 容错

| 情况 | 处理 |
|---|---|
| 模型接口或网络临时故障（进程退出，stderr 含 429/5xx/超时/连接错误） | 重试 3 次，间隔 10s、40s、160s：会话已建立则 `--resume` 并发「上一轮因网络中断，请继续」；首轮未建立会话则用同一 `--session-id` 重跑任务卡 |
| worker 没交汇报就退出 | 记 `failed`，附最后一段 assistant 文本 |
| 单轮 worker 进程运行超过 2 小时 | 先 SIGTERM，10 秒后 SIGKILL；记 `blocked`：「运行超过 2 小时已中止，是否继续？」 |
| 发件箱发送失败 | 退避重试，从 5 秒起倍增，最长 300 秒。同一工单的消息按顺序发，前一条没发出去，后一条就等着。同一条连续失败 3 次，写入 `~/.ticketd/logs/alerts.log`，然后继续重试；飞书返回目标永久不可达时标为 `dead` |
| 服务或电脑重启 | 启动时把所有 `running` 工单改为待续做，插入一条 `recovery` 输入：「服务重启打断了上一轮。先检查工单目录和已发出的消息，确认哪些外部动作已完成，再决定是否重做。」 |
| 服务崩溃 | launchd 自动拉起。所有动作都是先落库再执行，所以不会丢 |

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
