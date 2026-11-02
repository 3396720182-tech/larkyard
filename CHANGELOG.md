# Changelog

## 0.2.0 — 未发布

- 支持 Linux（含 WSL2）：`scripts/install.sh` 在 Linux 上装成 systemd 用户服务（`systemd/ticketd.service.in`），macOS 仍是 launchd，生成的 plist 不变。`uninstall.sh` 同样按平台处理。WSL1、WSL2 里用了 Windows 盘上的 node / lark-cli / claude、没有 systemd 用户实例时拒绝安装；缺 bwrap / socat、Ubuntu 24.04 的 AppArmor 限制、没开 linger 时给出提示。
- Linux 上查看和清理 worker 进程直接读 `/proc`（新文件 `ticketd/procs.py`、`ticketd/host.py`），不依赖 ps / lsof；按环境变量标记找残留进程不再受「看不到系统程序环境变量」的限制，按目录补的那一道把「父进程是 PID 1 或 ticketd 的祖先（如 `systemd --user`）」当作被收养。
- Linux 上 worker 沙箱的可写目录换成 lark-cli 在 Linux 的数据目录（`~/.local/share/lark-cli`）和 `/tmp`，不存在的附带路径不写（bubblewrap 绑不上不存在的路径），不写 macOS 才有的 `allowMachLookup`。macOS 上生成的设置不变。
- 新环境变量 `TICKETD_PYTHON`：指定 Python 解释器（默认 `/usr/bin/python3`，不存在时退回 PATH 上的 `python3`），`bin/ticket`、`bin/ticketd`、`install.sh` 都认；Linux 上写进服务环境，macOS 上显式指定时写进 plist。安装时检查 Python ≥ 3.9。
- README（中英）新增「Linux / WSL2」安装说明和已知限制。
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
