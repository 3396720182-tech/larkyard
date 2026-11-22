## Why

管理员看不到同事用 Larkyard 时遇到了什么问题：心跳（清单 c1）只有版本、系统、自检结论、工单数量和更新结果，哪张单失败、卡在哪一步、模型接口报了什么类别的错，控制台都收不到。当初这样做是为了不把同事的工作内容传上来。

管理员定的界线（2026-11-22）：控制台默认只自动收「谁、哪台电脑、哪一步坏了」；要看细节时，同事本人在飞书里把诊断包发给管理员，发之前自己能看到发的是什么。

## What Changes

- 采集清单 `c2`：在 c1 的基础上多一项「上次上报以来的出错记录（时间、工单号、出错的环节、错误归类码，都是写死的代号，不含任何文字）」和「诊断包只在本人确认后发送」。新入网用 c2；已入网的 c1 设备照旧只报 0 级，本人执行 `ticket console accept` 看过 c2 全文、输入 `yes` 后才升级（PRD §7.1：1 级数据要本人重新确认）。可以改回 c1。
- 心跳多一个可选字段 `problems`（只在本机清单是 c2 时出现）：最多 20 条 `{at, ticket, step, code}` 和超出的条数 `more`。来源是本机 `events` / `rounds` 表，只取 id、时间、kind、code、outcome，事件的原文（detail）不进心跳。游标在控制台确认收到（200 且 ok）后才前移，失败下次重报。
- 新命令 `ticket console accept [c1|c2]`、`POST /api/v1/device/consent`。
- 新命令 `ticket diag [工单号]`：在 `~/.ticketd/diag/` 生成诊断包（版本、系统、自检、最近出错记录带原文、指定工单的概况、最近几轮、事件、最后一次汇报、过程日志末尾），常见密钥形态打码，≤48KB，打印全文，不发送。`ticket diag send <文件> [--note 一句话]` 发到 `POST /api/v1/diagnostics`（只接受 diag 目录里生成的文件）。
- 主会话约定（`workspace/CLAUDE.md.tmpl`）：同事说「把诊断包发给管理员」时先生成、给同事看、说明发给谁，同事明确确认后才发送。
- 控制台侧（私有仓库）同时加接口和页面；上线顺序：控制台先上，客户端再发版。

## Capabilities

### New Capabilities
- `console-problem-reports`: 清单 c2、`ticket console accept`、心跳 `problems` 字段的内容和游标。
- `console-diagnostics`: `ticket diag` 生成、打码、本人确认后发送。

### Modified Capabilities
- `console-heartbeat`（`add-console-enrollment`）：白名单多一个只在 c2 下出现的字段（「新增字段要升清单版本并让本人重新确认」照做）。

## Impact

- `ticketd/console.py`、新增 `ticketd/diag.py`、`ticketd/cli.py`、`workspace/CLAUDE.md.tmpl`、`docs/spec.md` §12、`tests/test_console.py`、新增 `tests/test_problem_reports.py`。
- 匿名统计不变（它不读这些新字段）。
