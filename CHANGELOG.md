# Changelog

## 0.1.0 — 未发布

首个开源版本。代码来自作者私有的 ticketd（36 个提交，加上尚未提交的「结果覆盖状态卡」改动），开源时做了这些调整：

- 个人信息改成配置：主用户 open_id（`TICKETD_OWNER`，没配置时拒绝带 `--sender` 的派单）、工单根目录（默认 `~/larkyard-tickets`）、launchd 标签（默认 `dev.larkyard.ticketd`，可用 `TICKETD_LABEL` 改）。
- 新增 `~/.ticketd/config.env`：一行一个 `KEY=VALUE`，环境变量优先，改了即生效。
- `scripts/install.sh` 支持 `TICKETD_HOME`、`TICKETD_LABEL`、`--print`；首次安装生成 `config.env`。
- 工作区约定拆成通用模板 `workspace/CLAUDE.md.tmpl` + 个人覆盖 `CLAUDE.local.md`，用 `scripts/setup-workspace.sh` 安装。
- 新增中英 README、NOTICE（注明基于 lark-channel-bridge 开发）。
- 去掉含真实消息/会话 id 的实施计划和端到端记录；`docs/spec.md` 去敏后保留。
