# NOTICE

## 基于 lark-channel-bridge 开发 / Built on lark-channel-bridge

Larkyard 是架在 [lark-channel-bridge](https://github.com/zarazhangrui/feishu-claude-code-bridge)（npm 包 `lark-channel-bridge`）之上的一层。

| 来自 lark-channel-bridge | Larkyard 自己的 |
|---|---|
| 飞书 / Lark 消息收发、bot 绑定与身份 | `ticketd` 派单服务与 `ticket` 命令 |
| 每个聊天的 Claude Code 会话、排队与消息合并 | 每张工单独立的 worker 会话、目录与沙箱权限 |
| 注入到每条消息顶部的 `<bridge_context>`（chatId、messageIds、senderId 等） | 状态卡、结果覆盖、发件箱重试与幂等 |
| profile 与 lark-cli 的运行环境（`LARK_CHANNEL_*`、`LARKSUITE_CLI_CONFIG_DIR`） | 工作区约定（注意力原则、派单规则、5 种收尾状态） |

Larkyard 的发版包里打包了 lark-channel-bridge（`vendor/bridge/`）：取自我们的 fork（https://github.com/3396720182-tech/lark-coding-agent-bridge），固定在 `vendor/bridge.lock.json` 记录的提交上，基于上游 0.7.1，加了会议和附件相关的修复（通用的同时向上游提了 PR）。本仓库里没有 bridge 的源码；打包时附上 bridge 的 `LICENSE` 和它全部生产依赖的许可证文本（`vendor/bridge/THIRD_PARTY_NOTICES.md`，依赖为 MIT、BSD-3-Clause、ISC、Apache-2.0）。

Larkyard release packages bundle lark-channel-bridge (`vendor/bridge/`), built from our fork at the commit pinned in `vendor/bridge.lock.json` (upstream 0.7.1 plus meeting and attachment fixes, the general ones also proposed upstream). The package includes the bridge's `LICENSE` and the license texts of all its production dependencies (`vendor/bridge/THIRD_PARTY_NOTICES.md`).

lark-channel-bridge is licensed under the MIT License:

> MIT License
>
> Copyright (c) 2026 Lark Channel Bridge contributors

感谢 lark-channel-bridge 的作者和贡献者。Thanks to the authors and contributors of lark-channel-bridge.

## 其他依赖 / Other tools used at runtime (not bundled)

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — runs the bridge session and every worker.
- lark-cli (`@larksuite/cli`, MIT) — sends replies, edits cards, imports documents. The one-line installer downloads the official build listed in `vendor/runtime.json` (SHA-256 checked) into `~/.ticketd`.
- Node.js (MIT) — runs the bundled bridge. The one-line installer downloads the official build listed in `vendor/runtime.json` (SHA-256 checked) into `~/.ticketd/runtime`.
