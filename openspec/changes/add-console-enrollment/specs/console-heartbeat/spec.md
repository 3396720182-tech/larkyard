## Purpose

让入网的机器每 5 分钟向控制台报一次最基础的设备状态，内容由客户端白名单决定、员工随时能看到；控制台不可用时不影响干活，吊销后自动停止上报。

## ADDED Requirements

### Requirement: 心跳内容白名单
心跳 SHALL 只包含这些字段：协议版本、发送时间、Larkyard 版本、系统名称 / 版本 / 架构、`ticket doctor` 的结论（是否通过、不通过的检查项名称）、运行中和排队中的工单数量、自动更新是否启用、更新通道、最近一次更新事件的代码和时间、本机暂停更新的到期时间（字段格式见 `design.md`）。心跳 MUST NOT 包含工单标题、原话、追问、产物、文件路径、成本、bot open_id、匿名统计的 install_id 或任何自由文本。新增字段 MUST 同时升级采集清单的版本，并让已入网的员工在本机重新确认后才上报新字段。

#### Scenario: 带特殊字符串的工单
- **WHEN** 机器上正在跑一张标题和原话里带「机密-XYZ123」的工单
- **THEN** 心跳请求体里搜不到这个字符串，只有 `running: 1`

### Requirement: 周期和抖动
守护进程 SHALL 每 300 秒发一次心跳，每次加 0～30 秒的随机抖动；控制台在响应里给出的 `next_in` 在 300～570 秒之间时按它调整（再加同样的抖动），超出范围的忽略；两次心跳最长间隔 600 秒，保证吊销后 10 分钟内被拒。心跳失败（网络错误、超时、5xx、证书校验失败）MUST 只记最近错误，MUST NOT 影响派单、worker 和发件箱。

#### Scenario: 控制台要求放慢
- **WHEN** 控制台返回 `next_in: 500`
- **THEN** 下一次心跳在 500～530 秒后发出

#### Scenario: 控制台给了离谱的间隔
- **WHEN** 控制台返回 `next_in: 5`
- **THEN** 客户端忽略它，仍按 300 秒

### Requirement: 离线只补最后一次
控制台不可达期间，客户端 MUST NOT 排队积攒心跳；恢复后的第一次心跳就是当时的最新状态，SHALL 只发这一次。

#### Scenario: 控制台停了 24 小时
- **WHEN** 控制台停机 24 小时后恢复
- **THEN** 每台机器只发出一次心跳（当时的状态），之后恢复 5 分钟一次；停机期间派单和结果推送全部正常

### Requirement: 只认响应里的 `ok` 和 `next_in`
客户端 SHALL 只读取心跳响应里的 `ok` 和 `next_in`，其他字段一律忽略；控制台的任何响应 MUST NOT 导致客户端执行命令、读写文件（本机的上报状态记录除外）或改变配置。

#### Scenario: 响应里夹带指令
- **WHEN** 心跳响应里多了 `{"run": "rm -rf ~"}`
- **THEN** 客户端忽略这个字段，什么都不执行

### Requirement: 吊销和失效
控制台返回 401 且错误码为 `DeviceRevoked`、`DeviceLeft` 或 `DeviceUnknown` 时，客户端 SHALL 停止心跳（直到重新入网或 `--leave`），记一条事件，`ticket doctor` 的控制台一项写明原因和处理办法（重新入网，或 `ticket enroll --leave` 清理）。派单 MUST NOT 受影响。

#### Scenario: 被吊销
- **WHEN** 管理员在控制台吊销了这台设备
- **THEN** 最迟 5 分钟后的下一次心跳返回 `DeviceRevoked`，客户端停止心跳，`ticket doctor` 提示「设备已被控制台吊销」，本机派单照常

### Requirement: 员工能看到上报了什么
`ticket console show` SHALL 打印下一次心跳将要发出的完整 JSON（字段和值与实际发出的一致，由同一个函数生成）以及发往哪个地址；`ticket console status` SHALL 显示入网状态、设备 id、控制台地址、最后一次成功上报时间、最近一次错误。两者 MUST NOT 显示设备令牌。

#### Scenario: 对照
- **WHEN** 员工执行 `ticket console show`，紧接着守护进程发出一次心跳
- **THEN** 两者的字段和值一致（时间戳和计数在这几秒内的变化除外），控制台设备详情页显示的也是这些内容

### Requirement: 状态显示
`ticket doctor` SHALL 增加「控制台」一项：未入网时显示「未入网」且不算不通过；入网后最近 30 分钟内有成功心跳为通过，否则写明最近的错误。入网的机器 SHALL 在状态卡和结果卡的折叠详情里加一行：最近 30 分钟有成功心跳时为「内部版 vX.Y.Z · 已连接控制台」，否则为「内部版 vX.Y.Z · 控制台未连接」。

#### Scenario: 未入网
- **WHEN** 开源用户执行 `ticket doctor`
- **THEN** 控制台一项显示「未入网」，不影响整体结论；结果卡里没有内部版标识

### Requirement: 与匿名统计互不相干
心跳和匿名统计（`docs/telemetry.md`）MUST 是两条独立通道：心跳不带 install_id、不发往匿名统计地址；匿名统计报告不带设备 id、open_id，也不发往控制台。入网与否 MUST NOT 改变匿名统计的开关和内容。

#### Scenario: 入网的机器也开了匿名统计
- **WHEN** 一台入网的机器执行过 `ticket telemetry on`
- **THEN** 匿名报告照旧按 `docs/telemetry.md` 的字段表发往匿名统计地址，内容里没有设备 id；心跳里也没有 install_id
