## ADDED Requirements

### Requirement: 采集清单 c2 和本人确认
新入网 SHALL 使用清单 `c2`。已入网、本机清单是 `c1` 的设备 MUST NOT 上报出错记录，直到本人在终端执行 `ticket console accept`、看过 c2 全文并输入 `yes`、控制台确认后，本机才改记 c2。`ticket console accept c1` SHALL 让设备改回不上报出错记录。worker 环境和非终端标准输入 MUST 拒绝 accept。

#### Scenario: c1 设备不带出错记录
- **WHEN** 本机清单是 c1，本机有失败的轮次和模型接口错误
- **THEN** 心跳里没有 `problems` 字段，`ticket console status` 和 `show` 提示「执行 ticket console accept 可开启」

#### Scenario: 升级到 c2
- **WHEN** 本人执行 `ticket console accept` 并输入 yes，控制台回 `consent_version: c2`
- **THEN** 本机记 c2；同意之前的出错记录不上报，之后新出的出错记录随下一次心跳上报

#### Scenario: 不同意
- **WHEN** 本人没有输入 yes
- **THEN** 没有发出请求，本机清单不变

### Requirement: 心跳里的出错记录只有代号
清单是 c2 时，心跳 SHALL 带 `problems`：最多 20 条 `{at, ticket, step, code}`（按时间从旧到新）和超出条数 `more`。`step`、`code` MUST 只取代码里写死的枚举值，`ticket` 只能是工单号或 null；事件原文、工单标题、原话 MUST NOT 出现在心跳里。

#### Scenario: 带特殊字符串的报错
- **WHEN** 一轮失败的事件原文和工单标题里带「机密-XYZ123」
- **THEN** 心跳里有一条 `{step: "round", code: "failed", ticket: "T0001"}`，请求体里搜不到这个字符串

### Requirement: 出错记录不丢不重
游标 SHALL 只在控制台确认收到（200 且 `ok: true`）后前移；发送失败或没确认时下一次心跳 SHALL 重报同样的记录。`ticket console show` MUST NOT 前移游标。

#### Scenario: 控制台暂时不可用
- **WHEN** 有 2 条出错记录时心跳失败，下一次心跳成功
- **THEN** 第二次心跳里仍有这 2 条，之后的心跳里不再出现
