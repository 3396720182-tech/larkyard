## ADDED Requirements

### Requirement: 诊断包先给本人看
`ticket diag [工单号]` SHALL 只在本机生成诊断包文件（权限 600）并打印全文，MUST NOT 发送。诊断包 SHALL 不超过 48KB，常见密钥形态打码。

#### Scenario: 生成
- **WHEN** 执行 `ticket diag T0001`
- **THEN** `~/.ticketd/diag/` 下多一份带 T0001 的文件，输出里有全文和「确认后执行 ticket diag send」的提示，控制台没有收到请求

#### Scenario: 打码
- **WHEN** 过程日志里有 `Authorization: Bearer <长令牌>` 和 `password=xxxx`
- **THEN** 诊断包里这两处都是「‹已打码›」

### Requirement: 只发本人确认过的诊断包
`ticket diag send <文件>` SHALL 只接受 diag 目录里按规则命名的普通文件，发送前再打一遍码，经设备令牌发到 `POST /api/v1/diagnostics`。worker 环境 MUST 拒绝。未入网时 SHALL 提示把文件直接交给管理员。主会话 SHALL 在同事明确确认后才执行发送。

#### Scenario: 发任意文件被拒
- **WHEN** 执行 `ticket diag send ~/.ssh/id_ed25519`
- **THEN** 报错「只能发送 ticket diag 生成的诊断包」，没有发出请求
