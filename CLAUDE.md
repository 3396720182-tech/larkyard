# Larkyard 开发约定

这是 Larkyard 仓库本身的开发说明（给在本仓库里改代码的 Claude Code 看）。
放进 bridge 工作区的约定在 `workspace/CLAUDE.md.tmpl`，不是这个文件。

## 结构

- `ticketd/`：派单服务（Python 3.9 标准库，零依赖）。`config.py` 是所有配置的唯一来源。
- `bin/ticket`、`bin/ticketd`：入口脚本，固定用 `/usr/bin/python3`。
- `prompts/worker.md`：worker 的系统提示；`ticketd/worker.py` 里的 `ASSIGNMENT` 是每轮的任务卡。
- `workspace/`：bridge 工作区的 `CLAUDE.md` 模板和个人覆盖示例。
- `docs/spec.md`：设计规格。行为以它为准；改了行为要在对应章节写「调整」，说明改了什么、为什么。
- `tests/`：`unittest`，用 `tests/fakes/` 里的假 claude 和假 lark-cli，不碰飞书。

## 改动流程

1. 先读 `docs/spec.md` 对应章节，再读代码。
2. 小步改，测试跟着改：`/usr/bin/python3 -m unittest discover -s tests`。5 个依赖 `ps` 的孤儿清理用例在禁止 `ps` 的沙箱里会失败，属环境限制，不要为此改代码。
3. 改了卡片结构，跑 `/usr/bin/python3 scripts/validate_cards.py`（会调飞书 cardkit 校验，不发消息；离线时用 `--dump`）。
4. 新增配置项：加在 `ticketd/config.py`（经 `_env` 读取，自动支持 `config.env`），同步 `config.example.env` 和 README 的配置表。
5. 用户可见的变化记到 `CHANGELOG.md`。

## 上游边界

Larkyard 基于 lark-channel-bridge 开发，但不 fork、不修改它。需要 bridge 改动的地方，在 README「已知限制」或 issue 里记下来，不要在本仓库里补丁上游代码。

## 去敏（每次提交前检查）

仓库公开，不得出现：密钥、令牌、真实的 open_id / chat_id / message_id、本机用户名路径、公司或客户项目信息、工单历史、数据库和日志。
测试和示例用 `ou_owner`、`oc_1`、`om_1` 这类假值。提交前可以跑：

    git grep -n -E "ou_[0-9a-f]{20,}|oc_[0-9a-f]{20,}|om_x1|/Users/[a-z]{2,}" -- . ':!CLAUDE.md'
