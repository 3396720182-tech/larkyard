## ADDED Requirements

### Requirement: 从分开安装迁移到打包的 bridge
`ticket bridge adopt` SHALL 只在受管安装、当前版本带 `vendor/bridge`、bridge 服务文件存在且不是 Larkyard 写的、服务入口是 lark-channel-bridge 时执行，否则说明原因并退出。它 SHALL 先列出要改的文件和备份位置并要求在终端输入 `yes`（`--dry-run` 只列出）；然后备份并改写：bridge 服务（服务名、`LARK_CHANNEL_HOME`、日志路径不变，入口换成固定目录的 Node 和 `bridge/current`）、`~/.lark-channel/secrets-getter`、每日重启任务（换成 `ticket bridge restart`，时刻不变）、ticketd 服务（PATH 换成固定目录）；最后重新加载 bridge 服务并做重启后的在线确认。`~/.lark-channel` 里除 `secrets-getter` 以外的文件 MUST NOT 被读写。worker 环境里 MUST 拒绝。

#### Scenario: 本机迁移
- **WHEN** 维护者在终端里执行 `ticket bridge adopt` 并输入 `yes`
- **THEN** bridge 重启一次后由打包的 bridge 运行，profile、会话、凭据和日志位置都不变，`lark-channel-bridge status` 仍能看到同一个服务

#### Scenario: 迁移后起不来
- **WHEN** 迁移后的 bridge 90 秒内没有连上
- **THEN** 自动按 `--undo` 恢复所有改动并重新加载，终端说明失败原因

### Requirement: 原样退回
`ticket bridge adopt --undo` SHALL 把备份的文件原样拷回、重新加载对应服务、删除迁移记录。没有迁移记录或备份文件缺失时 MUST 不改任何文件并说明。

#### Scenario: 想退回 npm 安装
- **WHEN** 迁移后执行 `ticket bridge adopt --undo`
- **THEN** 服务文件、`secrets-getter`、每日重启任务、ticketd 服务文件和迁移前逐字节相同，bridge 由原来的 npm 安装运行
