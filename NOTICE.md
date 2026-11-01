# NOTICE

## 基于 lark-channel-bridge 开发 / Built on lark-channel-bridge

Larkyard 是架在 [lark-channel-bridge](https://github.com/zarazhangrui/feishu-claude-code-bridge)（npm 包 `lark-channel-bridge`）之上的一层，在它的 0.7.0 版本上开发和验证。

| 来自 lark-channel-bridge | Larkyard 自己的 |
|---|---|
| 飞书 / Lark 消息收发、bot 绑定与身份 | `ticketd` 派单服务与 `ticket` 命令 |
| 每个聊天的 Claude Code 会话、排队与消息合并 | 每张工单独立的 worker 会话、目录与沙箱权限 |
| 注入到每条消息顶部的 `<bridge_context>`（chatId、messageIds、senderId 等） | 状态卡、结果覆盖、发件箱重试与幂等 |
| profile 与 lark-cli 的运行环境（`LARK_CHANNEL_*`、`LARKSUITE_CLI_CONFIG_DIR`） | 工作区约定（注意力原则、派单规则、5 种收尾状态） |

Larkyard 不包含、不 fork、也不修改 lark-channel-bridge 的代码；它作为独立依赖安装，Larkyard 只读取它注入的上下文、复用它的 lark-cli 环境。

lark-channel-bridge is licensed under the MIT License:

> MIT License
>
> Copyright (c) 2026 Lark Channel Bridge contributors

感谢 lark-channel-bridge 的作者和贡献者。Thanks to the authors and contributors of lark-channel-bridge.

## 其他依赖 / Other tools used at runtime (not bundled)

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — runs the bridge session and every worker.
- lark-cli (`@larksuite/cli`) — sends replies, edits cards, imports documents.
