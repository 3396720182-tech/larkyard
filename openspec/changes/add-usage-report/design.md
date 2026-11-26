## Context

c1（add-console-enrollment D4）、c2（add-console-problem-reports D1）不变。控制台那边的接收、存储和页面在控制台仓库的 add-oversight。

## Decisions

### D1. 采集清单 `c3`

清单文本写死在 `ticketd/console.py`（`CONSENT_C3`），和控制台 `console/consent.py` 的 `C3` 逐字一致（两边对照 SHA-256 `0bf2d5b8f4e4ac0c40df730cb8480ed789d9faf9f75bced7d4c7e92392841b26`）：

```
控制台会收集（0 级：设备状态；1 级里只收出错记录和用量统计）
入网时一次：主机名；系统名称、版本和架构；Larkyard 版本；本机 bot 的 open_id（TICKETD_OWNER）；你同意的这份清单的版本。
每 5 分钟一次：Larkyard 版本；系统名称、版本和架构；ticket doctor 的结论（是否通过、不通过的检查项名称）；运行中和排队中的工单数量；自动更新是否启用、更新通道、最近一次更新结果和时间、本机暂停更新的到期时间；上次上报以来的出错记录（时间、工单号、出错的环节、错误归类码，都是代码里写死的代号，不含任何文字）；每天各大类的工单数和做成的数量（大类由本机判断，是代码里写死的 7 个代号：生图生视频、找素材、写文档、查资料、做表格、写代码、其他）；每天各项能力的调用次数、成功次数和失败归类码；最近连续没做成的工单数；正在运行的工单里最久没有进展的分钟数。
不会收集：工单标题、原话、追问、产物、文件、过程日志、成本、报错原文。
诊断包：只有你本人确认发送时才发，发之前先给你看完整内容。
谁能看：只有控制台管理员。随时查看下一次上报的内容：ticket console show。退出：ticket enroll --leave。
```

`reports_problems(v)`：c2 起带 `problems`；`reports_usage(v)`：c3 起带 `usage`。从低版本换上来时，出错记录的游标、用量统计的起点（kv `console.usage_since`）设成当时：同意之前的不补报。

### D2. 大类

7 个代号：media 生图生视频、asset 找素材、doc 写文档、research 查资料、sheet 做表格、code 写代码、other 其他。建单时定、存 `tickets.cat`，之后不变：

1. 主会话 `ticket new --cat <代号>`（工作区规则里写了怎么选）；
2. 没给或不认识：`usage.classify(标题, 原话)` 按关键词，先命中的算，都不中为 other。

原话、标题只在本机参与判断，上报的只有代号。

### D3. 能力调用

调度器读过程日志时：Bash 工具调用的命令里出现 `<技能根>/current/skills/<名字>/scripts/` 或 `…/bin/`、名字在本机装着的技能里、命令不是查看文件（cat、ls 等）时记下 tool_use_id → 技能名；看到对应的 tool_result 时记一行 `cap_calls`：`is_error` 为假算成功；为真时归类码取输出里的「Exit code N」→ `exit_N`，取不到记 `tool_error`。不存命令和输出原文。只调 Skill 工具（加载说明）不算一次调用。

### D4. 心跳 `usage`

```json
"usage": {"cats": [{"day": "2026-11-27", "cat": "media", "n": 3, "ok": 2}],
          "caps": [{"day": "2026-11-27", "cap": "asset-search", "n": 5, "ok": 4, "fails": [{"code": "exit_2", "n": 1}]}],
          "streak_fail": 0, "stuck_min": 0}
```

- 天是本机本地日期；每次报今天和昨天的累计数（控制台按设备、天、类覆盖），`cats` 最多 14 条，`caps` 最多 20 条、每条最多 3 个归类码。
- 「做成」：这张单有过一轮的结果是 ready。昨天以前建的单之后才做成，不再更新。
- `streak_fail`：按每张单最后一次有结论的轮次（ready / failed / blocked / waiting / dropped）从新到旧数 failed，遇到 ready 停；blocked、waiting、dropped 跳过。最多往回看 50 张单，只看同意 c3 之后的轮次。
- `stuck_min`：状态 running、没在等后台任务的单里，距最后一次动作（没有就用开轮时间）最久的分钟数。

### D5. c2 → c3 直接升级（管理员决定，2026-11-27）

c3 只比 c2 多了用量统计（写死的代号和数字，不收任何文字），算小迭代，不再逐人确认。

- 守护进程每次心跳前：本机清单是 c2、kv `console.consent_auto` 不是当前最新版 → 走和 `accept` 相同的换清单流程（`switch_consent`）换成 c3，私信本人一句说明：多了什么、`ticket console show` 看下一次上报、`ticket console accept c2` 退回。这一次心跳就按 c3 报；用量起点设成换的时刻，之前的不补报。
- 只升一次：`switch_consent`（自动或本人 `accept`）每次都把 `console.consent_auto` 记成当前最新版。本人退回 c2 后不再动；c3 入网后退回 c2 的也不动。
- 控制台还不认识 c3（ConsentUnknown）：本机留在 c2，记一条事件（一天一条），不挡这次心跳，下一次再试。
- c1 不自动升：c1 → c3 还多了出错记录，仍要本人在终端 `ticket console accept`。
- 先前设计过「机器人私信清单全文、本人引用回复同意、守护进程去飞书核对」，管理员认为对小迭代太重，不做。

### D6. 旧控制台

心跳收到 400 UnknownField 且带了 `usage`：去掉 `usage` 再发；还不行且带 `problems`，再去掉 `problems`。各记一条事件。换清单时控制台回 ConsentUnknown：提示控制台要先升级，本机不变。
