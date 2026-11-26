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

### D5. 机器人私信同意

- 守护进程：心跳成功后，本机清单不是最新、还没私信过这一版（kv `console.consent_offer`）且配了主用户 → 私信一次：为什么、多了什么、清单全文和哈希前 16 位、怎么同意（引用这条私信回复「同意」）、怎么退回。发不出去或过期才重发。
- 主会话：用户在那个私聊里说「同意」→ `ticket console agree --message <这条消息 id>`。命令只登记（kv `console.consent_agree`），不发请求。
- 守护进程下一次心跳前核对（按原值取走，只核对一次，不通过要本人重新回复）：用机器人身份取回这条消息和那条私信（`GET /im/v1/messages/:id`），要求：发送人 = `TICKETD_OWNER` 且 `sender_type` 是 user；同一个 chat_id；`parent_id` 或 `root_id` 是那条私信（必须引用它，私聊里回别的问题的「同意」不算）；在私信之后 30 天内；晚于本机最近一次换清单（kv `console.consent_switched_at`，防止拿旧的「同意」把本人退回的清单再换回去）；文本消息去掉空白和标点后是「同意」这几种说法之一。通过就走和 `accept` 相同的换清单流程（`switch_consent`），结果私信本人；不通过私信原因。
- 私信发不出去（发件箱 dead）或发出超过 30 天（回复不再算数）时重发一次；新增内容按本机现在的版本写（c1 设备会说明出错记录也是新增）。
- 剩下的风险（写明，不再加码）：本机上以用户身份运行的程序（被带偏的主会话、绕开 worker 检查的进程）如果有用户身份的发消息权限，可以用 `lark-cli --as user` 以本人名义引用那条私信发「同意」。终端里的 `accept` 也挡不住本机上以用户身份运行的程序（伪造终端即可），所以两条路的信任边界一样：防的是模型被一句话带偏，不防本机已经被控制。
- 理由：主会话是模型，可能被消息里的文字带偏；换不换只看飞书那边这条消息是不是本人在那个私聊里发的，模型伪造不了。

### D6. 旧控制台

心跳收到 400 UnknownField 且带了 `usage`：去掉 `usage` 再发；还不行且带 `problems`，再去掉 `problems`。各记一条事件。换清单时控制台回 ConsentUnknown：提示控制台要先升级，本机不变。
