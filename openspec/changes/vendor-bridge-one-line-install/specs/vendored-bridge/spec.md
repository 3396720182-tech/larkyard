## ADDED Requirements

### Requirement: bridge 来自 fork 的固定提交
Larkyard SHALL 在 `vendor/bridge.lock.json` 里记录 bridge 的来源仓库、40 位提交号和版本号。`scripts/build_bridge.py` SHALL 只按这个提交构建：检出后的 `HEAD` 和 `package.json` 版本与锁文件不一致时 MUST 失败。构建产物 SHALL 包含 `bin/`、`dist/`、`package.json`、`LICENSE`、按锁文件安装的扁平生产依赖、`THIRD_PARTY_NOTICES.md` 和 `BRIDGE.json`（`repo`、`commit`、`version`、`tree_sha256`）；产物里出现链接或设备文件、或某个依赖既没有许可证文件也没有 `license` 字段时 MUST 失败。

#### Scenario: 锁文件和检出不一致
- **WHEN** 锁文件写的提交在 fork 上不存在，或检出后 `package.json` 的版本和锁文件不同
- **THEN** 构建失败，不产生输出目录

#### Scenario: 许可证声明
- **WHEN** 构建成功
- **THEN** `THIRD_PARTY_NOTICES.md` 里每个生产依赖都有一节，含名称、版本和许可证文本（或 `license` 字段）

### Requirement: bridge 随签名包发布，清单格式不变
`release.py release` 在 tag 带 `vendor/bridge.lock.json` 时 MUST 要求 `--bridge <目录>`，并核对目录里 `BRIDGE.json` 的 `commit` 等于锁文件、`tree_sha256` 等于目录实际内容，不一致就拒绝打包。bridge 文件 SHALL 放在包内 `larkyard-<版本>/vendor/bridge/` 下，按现有规则固定时间、属主和权限（同一 tag、同一 bridge 目录两次打包字节相同）。清单字段 MUST NOT 增加；客户端按现有流程验证和安装。

#### Scenario: 给错了 bridge
- **WHEN** 发布者用另一个提交构建的 bridge 目录打包
- **THEN** `release.py` 拒绝，提示锁文件里的提交和目录里的不一致

#### Scenario: 老客户端收到新包
- **WHEN** 还没有本变更代码的受管安装收到带 `vendor/bridge/` 的新版本
- **THEN** 照常验签、安装、试用期确认；bridge 不受影响，直到执行 `ticket bridge adopt` 或 `ticket setup`

### Requirement: 运行时按清单装在固定目录
`vendor/runtime.json` SHALL 写明 Node 和 lark-cli 的版本，以及每个支持平台（`darwin-arm64`、`darwin-x64`、`linux-x64`、`linux-arm64`）官方包的地址和 SHA-256（可附镜像地址）。安装 SHALL 把 Node 放在 `~/.ticketd/runtime/node-v<版本>-<平台>/`，由 `~/.ticketd/runtime/node` 指向；lark-cli 放在 `~/.ticketd/runtime/lark-cli-<版本>/`，由 `~/.ticketd/bin/lark-cli` 指向。下载 MUST 只跟随 https 重定向、有总时长和大小上限，SHA-256 不符 MUST 丢弃并失败，解包 MUST 拒绝绝对路径和 `..`。Larkyard 写的服务定义、`secrets-getter` 和 ticketd 服务的 PATH MUST NOT 含 nvm 或 npm 全局目录。

#### Scenario: 用户用 nvm 换了 Node 版本
- **WHEN** 用户 `nvm uninstall` 了以前装 bridge 用的 Node
- **THEN** bridge、lark-cli 取凭据、ticketd 发消息都不受影响

#### Scenario: 下载被篡改
- **WHEN** 镜像返回的 Node 包 SHA-256 和 `runtime.json` 不一致
- **THEN** 不解包、不切换 `runtime/node`，换下一个地址；都不对就报错，原来的运行时保持不变

### Requirement: bridge 只在重启窗口换新
bridge 服务 SHALL 运行 `~/.ticketd/bridge/current` 指向的版本目录里的 bridge，不直接运行 `app/current` 下的。Larkyard 自动更新 MUST NOT 重启 bridge。新版本确认后带的 bridge 与在用的不同（按 `BRIDGE.json` 的 `commit` 和 `tree_sha256`）时 SHALL 记下待生效，由每天 04:07 的 `ticket bridge restart --if-stale` 换上；版本相同时这个任务 MUST NOT 重启 bridge。`update.prune` MUST NOT 删除 `bridge/current` 指向的版本目录。

#### Scenario: 白天自动更新
- **WHEN** 下午收到一个只改了 ticketd 的新版本
- **THEN** ticketd 重启，bridge 不重启，主会话正在进行的回复不中断

#### Scenario: 新版本带了新 bridge
- **WHEN** 新版本确认后带的 bridge 提交不同
- **THEN** `ticket doctor` 显示新 bridge 待生效；当晚 04:07 切换并重启 bridge

#### Scenario: 清理旧版本
- **WHEN** 已经更新了 5 个版本，bridge 仍在用第 1 个版本带的 bridge
- **THEN** 第 1 个版本目录保留

### Requirement: 重启后确认在线，不在线就切回
`ticket bridge restart` SHALL 先确保运行时（失败就不切换），切换 `bridge/current` 并记下原来的目标，重启服务，最多等 90 秒确认 bridge 在重启之后记录了长连接已连上。确认失败时 SHALL 把链接切回原来的目标、再重启一次、记事件并私信主用户一次。

#### Scenario: 新 bridge 起不来
- **WHEN** 切换后 90 秒内 bridge 日志里没有连上的记录
- **THEN** 切回原来的 bridge 并重启，主用户收到一条私信说明，`ticket bridge status` 显示这次失败

### Requirement: 状态和自检
`ticket bridge status` SHALL 显示：bridge 是否由 Larkyard 管理、在用的 bridge 版本和提交、当前 Larkyard 版本带的 bridge、是否待生效、Node 和 lark-cli 的版本和路径、服务文件里的入口。`ticket doctor` SHALL 增加「bridge」一项：由 Larkyard 管理且在线为通过；待生效、未迁移（仍是分开安装）为提示；服务文件指向不存在的路径或写死了 nvm 路径为问题。worker 环境里 `ticket bridge restart` MUST 被拒绝。

#### Scenario: 还没迁移的机器
- **WHEN** 机器上的 bridge 仍是 npm 全局安装
- **THEN** `ticket doctor` 的 bridge 一项提示「分开安装：可以用 ticket bridge adopt 迁移」，不算失败
