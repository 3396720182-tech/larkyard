## ADDED Requirements

### Requirement: 用量统计只在本机清单是 c3 时上报

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

### Requirement: c2 直接升到 c3，只升一次

守护进程 SHALL 在心跳前把清单是 c2、还没自动升过的设备静默换成 c3（SHALL NOT 私信）；本人之后换过清单（含退回 c2）SHALL NOT 再自动升；c1 SHALL NOT 自动升。

#### Scenario: c2 设备升级后第一次心跳
- **WHEN** 本机清单是 c2，控制台认识 c3
- **THEN** 换成 c3，这一次心跳带 `usage`，不私信

#### Scenario: 本人退回 c2
- **WHEN** 自动升过之后本人执行 `ticket console accept c2`
- **THEN** 之后的心跳不再自动升

#### Scenario: 控制台还不认识 c3
- **WHEN** 换清单时控制台回 ConsentUnknown
- **THEN** 本机留在 c2，这次心跳照常发，下一次再试
