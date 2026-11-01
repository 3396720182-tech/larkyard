# Changelog

## 0.2.0 — 未发布

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
