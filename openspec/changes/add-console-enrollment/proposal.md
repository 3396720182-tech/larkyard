## Why

多台 Larkyard 统一管理时，需要一个控制台（服务端不在本仓库）知道「哪台机器是谁的、跑的是哪个版本、是不是在正常工作」，签名自动更新（`add-signed-self-update`）也要从这个控制台拿发版公钥和更新。客户端这一侧要做的是：一条命令入网、入网时按带外指纹核对发版公钥、之后定期上报最基础的设备状态。开源用户不入网就完全不受影响：仓库里没有任何控制台地址，不入网不发任何请求。

## What Changes

- 新命令 `ticket enroll <控制台地址> <入网码> --release-key SHA256:<主公钥指纹> --backup-key SHA256:<备份公钥指纹>`：
  - 先在终端完整打印「控制台会收集什么」（采集清单，带版本号），员工输入 `yes` 才继续；
  - 从控制台取两把发版公钥，和命令行给的指纹核对，不一致就失败、什么都不写、也不提交入网码；
  - 提交入网码后，控制台给入网码绑定的那个人发飞书私信确认码，员工在终端输入后入网完成，拿到设备令牌；
  - 两把公钥按 `ticket update trust` 同一套逻辑写入；设备令牌存本机（权限 600）。
- 证书：控制台地址必须是 `https://`，按系统 CA 校验证书和主机名，没有任何跳过校验的选项，也不做证书固定（控制台用公开 CA 签发、定期自动续期的证书，固定会在每次续期时断开）。
- `ticket enroll --leave`：通知控制台并删除本机设备令牌和控制台配置，不再上报。
- 心跳：入网后守护进程每 5 分钟上报一次设备状态，字段白名单写死在客户端；控制台不可用时只保留最新一份，恢复后补发一次；看到「设备已吊销」就停止上报。
- `ticket console show`：打印下一次心跳的完整内容（和实际发出的完全一样）；`ticket console status`：入网状态、最后一次成功上报、最近的错误。
- `ticket doctor` 增加一项「控制台」；入网的机器在状态卡和结果卡的折叠详情里加一行「内部版 vX.Y.Z · 已连接控制台」。
- 入网后，没有单独配置 `TICKETD_UPDATE_URL` 时，自动更新的更新源是控制台（带设备令牌，同一套证书校验）；是否启用自动更新仍按 `add-signed-self-update` 的条件（受管安装等）。
- worker（工单里的智能体）不能入网、退出或改控制台配置。
- 本变更**只有 spec**：控制台侧的接口以控制台仓库 `add-console-core` 的 design「客户端接口」一节为准，审过之后再写代码。

## Capabilities

### New Capabilities
- `console-enrollment`: 默认不启用、入网命令、采集清单和同意、发版公钥核对、确认码、证书校验、本机文件、退出、和自动更新的衔接、worker 限制。
- `console-heartbeat`: 心跳内容白名单、周期和抖动、离线补发、吊销处理、`ticket console show / status`、`ticket doctor` 和内部版标识、与匿名统计互不相干。

### Modified Capabilities
（无。`openspec/specs/` 目前为空，现有行为以 `docs/spec.md` 为准；本变更落地时在 `docs/spec.md` 新增「控制台接入」一节。）

## Impact

- 代码：新增 `ticketd/console.py`（连接、证书校验、入网、心跳、本机文件）；`ticketd/cli.py` 加 `ticket enroll`、`ticket console`；`ticketd/daemon.py` 主循环加心跳；`ticketd/card.py` 详情加一行；`ticketd/update.py` 在入网后用控制台作为更新源并带设备令牌。
- 依赖：不新增第三方依赖（`ssl`、`http.client`、`hashlib` 均为标准库）。
- 数据：`kv` 记最后一次成功上报和最近错误，`events` 记入网、退出、吊销、证书校验失败等事件（只加不改）。
- 文档：`docs/spec.md`、README 新增「控制台接入（只在执行过 `ticket enroll` 后启用）」一段；`docs/telemetry.md` 不变。
- 开源用户：不执行 `ticket enroll` 就不发出任何到控制台的请求，状态卡上没有内部版标识，匿名统计行为不变。
