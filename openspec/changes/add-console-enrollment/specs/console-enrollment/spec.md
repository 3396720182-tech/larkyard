## Purpose

让员工用一条命令把自己的 Larkyard 接入控制台：入网前看清并同意控制台会收集什么；发版公钥只在和带外指纹一致时写入；入网码只有被绑定的本人能用掉。不入网的安装（包括所有开源用户）完全不受影响。

## ADDED Requirements

### Requirement: 默认不启用
仓库里 MUST NOT 有任何控制台的默认地址，控制台地址也 MUST NOT 能通过环境变量或 `config.env` 设置，只能由 `ticket enroll` 写入。没有执行过 `ticket enroll`（或已经 `--leave`）的安装 MUST NOT 发出任何到控制台的请求，状态卡上 MUST NOT 有内部版标识。

#### Scenario: 开源默认安装
- **WHEN** 用户按 README 安装，从未执行 `ticket enroll`
- **THEN** 守护进程运行 24 小时内没有任何到控制台的请求（单测里用一个拒绝一切连接的假网络层确认），`ticket console status` 显示「未入网」

#### Scenario: 试图用环境变量指定控制台
- **WHEN** `config.env` 里写了 `TICKETD_CONSOLE_URL=https://example.com`
- **THEN** 不发出任何请求，`ticket doctor` 提示这个配置项不存在

### Requirement: 入网前打印采集清单并要求同意
`ticket enroll` SHALL 在发出任何网络请求之前，在终端完整打印采集清单（带版本号，内容见 `design.md`），要求员工输入 `yes`（不是 `y`、不能用命令行参数跳过）。标准输入不是终端时 MUST 拒绝入网。入网请求 SHALL 带上清单版本和清单文本的 SHA-256，供控制台留痕。

#### Scenario: 不同意
- **WHEN** 员工看完清单输入 `no` 或直接回车
- **THEN** 命令退出，没有发出任何请求，本机没有写入任何文件

#### Scenario: 在管道里执行
- **WHEN** `echo yes | ticket enroll …`
- **THEN** 命令拒绝执行，提示需要在终端里由本人确认

### Requirement: 发版公钥按带外指纹核对
`ticket enroll` SHALL 先从控制台取两把发版公钥，分别和 `--release-key`、`--backup-key` 给的指纹比对（与 `ticket update trust` 同一套核对和写入逻辑）。任一不一致 MUST 立即失败：不提交入网码、不写入任何公钥或配置。本机已经信任了一对不同的公钥时 MUST 失败并说明，不覆盖。核对通过的公钥 SHALL 只在入网成功之后写入。

#### Scenario: 指纹对不上
- **WHEN** 控制台返回的主公钥指纹和 `--release-key` 给的不同
- **THEN** 入网失败，提示「发版公钥指纹不一致」；控制台没有收到入网码；本机没有写入任何公钥

#### Scenario: 本机已信任同一对公钥
- **WHEN** 员工之前已用 `ticket update trust` 信任了同样的两把公钥
- **THEN** 入网照常进行，信任状态不变

### Requirement: 确认码证明是本人
提交入网码后，客户端 SHALL 显示控制台返回的打码姓名和 4 位会话标记（「确认码已私信给 张*，会话标记 K7QD，请核对私信里的标记一致」），提示员工到飞书查看，并在终端读取 6 位确认码提交；确认码错误时 SHALL 允许重新输入，直到控制台作废这次入网。确认码 MUST NOT 写入日志或事件。

#### Scenario: 用别人的入网码
- **WHEN** B 用发给 A 的入网码在自己电脑上入网
- **THEN** 终端显示「确认码已私信给 A 的打码姓名」，B 拿不到确认码，入网失败，本机没有写入任何东西

#### Scenario: 确认码发不出去
- **WHEN** 控制台返回 `ConfirmDeliveryFailed`
- **THEN** 提示「确认码发不出去，请联系管理员」，本机没有写入任何东西

### Requirement: 证书校验
控制台地址 MUST 是 `https://`。客户端到控制台的所有连接（入网、心跳、退出、入网后的自动更新）SHALL 按系统 CA 校验证书和主机名；校验失败 MUST 在发出任何请求内容之前断开，记「证书校验失败」，MUST NOT 重试为不校验的连接。客户端 MUST NOT 提供跳过校验、固定证书或信任自签证书的选项。

#### Scenario: 证书和地址对不上
- **WHEN** 控制台地址的证书是签给别的域名的（或是自签证书）
- **THEN** 入网失败，提示证书校验失败；本机没有写入任何东西，入网码没有被提交

#### Scenario: 控制台证书自动续期
- **WHEN** 控制台的证书到期前被续期成一张新证书
- **THEN** 心跳照常成功，员工什么都不用做

#### Scenario: 本机 Python 没有 CA 证书
- **WHEN** 本机 Python 找不到可用的 CA 证书
- **THEN** 入网失败，提示原因和处理办法；`ticket doctor` 的控制台一项给出同样的提示

### Requirement: 本机保存的内容
入网成功后客户端 SHALL 把控制台地址、设备 id、入网时间、同意的清单版本写入 `~/.ticketd/console/console.json`，把设备令牌写入 `~/.ticketd/console/device-token`；目录权限 700、文件权限 600。设备令牌 MUST NOT 出现在日志、事件、`ticket console show`、`ticket doctor` 的输出里。已经入网的机器再执行 `ticket enroll` MUST 失败，提示先 `--leave`。

#### Scenario: 重复入网
- **WHEN** 已入网的机器再执行 `ticket enroll <地址> <新入网码> …`
- **THEN** 命令失败，提示先执行 `ticket enroll --leave`

### Requirement: 退出
`ticket enroll --leave` SHALL 先通知控制台（设备标为已退出），再删除本机的设备令牌和控制台配置；控制台不可达时 SHALL 询问是否仍然删除本机文件。退出后 MUST NOT 再发出任何到控制台的请求。信任的发版公钥保留（它们属于自动更新），命令输出里说明这一点。

#### Scenario: 正常退出
- **WHEN** 员工执行 `ticket enroll --leave`
- **THEN** 控制台把设备标为已退出，本机 `~/.ticketd/console/` 被删除，之后没有心跳

### Requirement: 和自动更新的衔接
入网后、本机没有单独配置 `TICKETD_UPDATE_URL` 时，自动更新的更新源 SHALL 是控制台，请求带设备令牌、按同一套规则校验证书；单独配置了 `TICKETD_UPDATE_URL` 时以它为准，`ticket doctor` 注明「更新源不是控制台」。是否真正启用自动更新仍按 `add-signed-self-update` 的启用条件；入网 MUST NOT 自动执行 `ticket update adopt`。控制台返回的任何内容 MUST NOT 改变信任的公钥（换钥仍只认备份密钥签的换钥清单）。

#### Scenario: 入网但没转受管安装
- **WHEN** 员工入网成功，但没执行过 `ticket update adopt`
- **THEN** 心跳照常，自动更新显示「未启用：不是受管安装」，入网命令的最后一行提示怎么启用

### Requirement: worker 不能改控制台设置
在 worker 环境里执行 `ticket enroll`（含 `--leave`）MUST 被拒绝，提示只能由用户本人在终端执行。`ticket console show` 和 `ticket console status` 是只读的，worker 可以执行。

#### Scenario: worker 试图退出
- **WHEN** 一张工单里的智能体执行 `ticket enroll --leave`
- **THEN** 命令被拒绝，本机文件不变，心跳照常
