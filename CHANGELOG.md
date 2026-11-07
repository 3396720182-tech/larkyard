# Changelog

## 0.2.0 — 未发布

- 修复：worker 把长任务放到后台、结束本轮等任务通知时，10 分钟后会被标成「疑似卡住」，用户以为卡死停单，后台任务一起被杀。现在 ticketd 跟踪 Claude Code 的后台任务事件，这段时间状态卡显示「⏳ 等后台任务」和任务描述，不判卡住；任务结束后 worker 自动接着做。`tickets` 表新增 `bg_wait` 列，老库自动迁移。
- 修复：模型接口余额不足、认证失败时，结果只写「worker 未交汇报就退出（code=1）」。现在写明「模型接口余额不足/认证失败」和报错原文，附「处理好后回复继续」，并且不做无用的重试；后台任务还在跑时马上推一条提醒，不再等到 10 分钟后的「疑似卡住」。
- 修复：过程日志把出错的一轮写成「本轮结束：success」，现在写「本轮结束：出错（…）」（#15）。
- 修复：断网 / DNS 失败（`ENOTFOUND`、`EAI_AGAIN`、「Can't reach the API server」）不算临时故障，工单直接失败不重试（#10）。

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
