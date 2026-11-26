## ADDED Requirements

### Requirement: 用量统计只在本人同意 c3 后上报

心跳 SHALL 只在本机 `console.json` 的 `consent_version` 是 c3（或更新）时带 `usage`，且只包含代号和数字。

#### Scenario: c2 设备
- **WHEN** 本机清单是 c2
- **THEN** 心跳带 `problems`、不带 `usage`

#### Scenario: 同意之前的不补报
- **WHEN** 从 c2 换成 c3
- **THEN** 换之前建的单、之前的能力调用、之前结束的轮次都不进 `usage`

### Requirement: 大类在本机判断、只存代号

每张单 SHALL 在建单时得到 7 个代号之一；主会话给的不认识时按本机关键词判断。

#### Scenario: 主会话没给
- **WHEN** `ticket new` 没带 `--cat`，原话是「帮我出一张海报」
- **THEN** `tickets.cat` 是 media

### Requirement: 私信同意要守护进程核对

`ticket console agree` SHALL 只登记消息 id；守护进程取回消息核对通过（本人、同一私聊、私信之后、内容是同意）才换清单，结果私信本人。

#### Scenario: 别人发的「同意」
- **WHEN** 消息的发送人不是 `TICKETD_OWNER`
- **THEN** 清单不变，私信说明原因
