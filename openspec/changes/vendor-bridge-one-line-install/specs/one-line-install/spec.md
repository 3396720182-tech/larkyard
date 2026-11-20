## ADDED Requirements

### Requirement: 一行安装核对发布者
`install.sh` SHALL 内置两把发版公钥（主公钥、备份公钥）的 SHA-256 指纹，可以用 `--release-key` / `--backup-key` 覆盖。它 SHALL 从发布地址取两把公钥并核对指纹，用主公钥验证通道清单的签名，核对清单的种类、产品名和通道，下载包并核对大小和 SHA-256；任何一步不通过 MUST 停止，并且 MUST NOT 写入 `~/.ticketd` 下的任何文件。发布地址只接受 `https://`（测试用的 `file://` 除外）。

#### Scenario: 发布地址被换了公钥
- **WHEN** 发布地址上的 `keys/main.pub` 被换成另一把
- **THEN** 指纹不符，安装停止，本机没有任何改动

#### Scenario: 包被替换
- **WHEN** 清单验签通过，但下载到的包 SHA-256 不对
- **THEN** 安装停止，临时文件删除

### Requirement: 装成受管安装
核对通过后 SHALL 装成受管安装：版本目录、启动器、`current` 链接、信任两把公钥、记下已接受的清单序号、`config.env` 里的更新源和通道（已有值不覆盖）、Node 和 lark-cli 运行时、`bridge/current`。重复执行 SHALL 只补缺的步骤；本机已经是更高版本时 MUST NOT 降级。

#### Scenario: 第一天就能自动更新
- **WHEN** 一行安装完成后发布了新版本
- **THEN** 守护进程按现有规则检查、下载、安装，不用再执行 `ticket update trust` 或 `ticket update adopt`

#### Scenario: 重复执行
- **WHEN** 已经装好的机器再执行一次一行安装
- **THEN** 不重复建应用、不覆盖 `config.env` 里已有的值、不改工作区里已有的 `CLAUDE.md`

### Requirement: setup 向导
`ticket setup` SHALL 依次完成：飞书应用（没有 profile 时调用打包的 bridge 扫码建应用，或用 `--app-id` 指定的已有应用）、工作区（默认 `~/larkyard-workspace`，渲染工作区约定）、服务（bridge 服务、ticketd 服务、每天 04:07 的 bridge 重启任务）、认主人、可选的用户身份授权、自检。已完成的步骤 SHALL 跳过。交互 SHALL 从终端读；标准输入和 `/dev/tty` 都不是终端时 MUST 不做交互步骤并提示在终端里执行 `ticket setup`。worker 环境里 MUST 拒绝。

#### Scenario: curl | sh
- **WHEN** 用户执行 `curl -fsSL <发布地址>/install.sh | sh`
- **THEN** 扫码、确认主人的输入从终端读到，不会把管道里的脚本内容当成回答

#### Scenario: 已有分开安装的 bridge
- **WHEN** bridge 的服务文件已存在且不是 Larkyard 写的
- **THEN** 向导不覆盖它，提示用 `ticket bridge adopt` 迁移，其余步骤照常

### Requirement: 自动认主人
`config.env` 没有 `TICKETD_OWNER` 时，向导 SHALL 用 bot 身份查应用信息取应用所有者的 open_id，尽量查到名字，在终端显示后由用户确认才写入。查不到或用户回答「不是」时 SHALL 生成 6 位码，请用户给 bot 私聊发「认主人 <码>」，从 bridge 日志里认出发送人（只认提示之后、内容含这个码的消息），再确认一次才写入。最多等 5 分钟，超时 MUST 不写入并说明怎么手动填。

#### Scenario: 应用是自己扫码建的
- **WHEN** 应用所有者就是员工本人
- **THEN** 终端显示「应用所有者：张三（…a1b2c3），是你吗？」，回车后写入 `TICKETD_OWNER`，不用抄 open_id

#### Scenario: 公司统一建的应用
- **WHEN** 员工回答不是
- **THEN** 终端显示 6 位码，员工在飞书私聊 bot 发送后，终端显示认出的发送人并再确认一次

### Requirement: 内部员工入网衔接
`install.sh` 带 `--console <地址> --enroll <入网码>` 时，SHALL 在 `ticket setup` 完成后执行 `ticket enroll`，指纹参数取 `install.sh` 内置（或命令行覆盖）的值，员工 MUST 仍然在终端里输入 `yes` 和 6 位确认码。

#### Scenario: 控制台下发的命令
- **WHEN** 员工执行控制台给的 `curl … | sh -s -- --console https://… --enroll LY-…`
- **THEN** 装完、扫码、确认主人后直接进入入网流程，命令里不需要指纹

### Requirement: 已是受管安装的机器交给自动更新
本机已是受管安装、版本低于发布的版本时，一行安装 MUST NOT 自己切换版本（那样会跳过试用期和自动切回），SHALL 提示由守护进程按现有流程安装；只记下和 `config.env` 里同一通道的清单。

#### Scenario: 老机器重跑一行安装
- **WHEN** 本机是受管安装 0.3.0，发布的是 0.4.0
- **THEN** `current` 仍指向 0.3.0，输出说明 0.4.0 由守护进程自动安装
