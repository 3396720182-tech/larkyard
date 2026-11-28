# Larkyard 开发约定

这是 Larkyard 仓库本身的开发说明（给在本仓库里改代码的 Claude Code 看）。
放进 bridge 工作区的约定在 `workspace/CLAUDE.md.tmpl`，不是这个文件。

## 结构

- `ticketd/`：派单服务（Python 3.9 标准库，零依赖）。`config.py` 是所有配置的唯一来源。
- `bin/ticket`、`bin/ticketd`：入口脚本，默认用 `/usr/bin/python3`（`TICKETD_PYTHON` 可改）。
- `launchd/`、`systemd/`：macOS / Linux 的服务模板，由 `scripts/install.sh` 渲染。
- 平台差异只放在 `ticketd/host.py`、`ticketd/procs.py`、`ticketd/perms.py`、`ticketd/bridge.py`（bridge 服务和重启窗口）和 `scripts/install.sh` / `uninstall.sh`（spec §3.1、§13）。改这些地方时两个平台的分支都要有单测：Python 里 mock `host.system()`，脚本用 `tests/test_install.py` 的假 `uname` / `launchctl` / `systemctl`。macOS 的行为不能顺带改掉。
- `prompts/worker.md`：worker 的系统提示；`ticketd/worker.py` 里的 `ASSIGNMENT` 是每轮的任务卡。
- `workspace/`：bridge 工作区的 `CLAUDE.md` 模板和个人覆盖示例。
- `docs/spec.md`：设计规格。行为以它为准；改了行为要在对应章节写「调整」，说明改了什么、为什么。
- `tests/`：`unittest`，用 `tests/fakes/` 里的假 claude 和假 lark-cli，不碰飞书。
- `server/telemetry/`：匿名统计的后端（Cloudflare Worker + D1，JavaScript），`npm test` 用 Node 自带的 sqlite 跑，不碰 Cloudflare。字段白名单 `ticketd/telemetry_schema.json` 由客户端和后端共用；改字段按 `docs/telemetry.md` 末尾的清单来。

## 改动流程

1. 先读 `docs/spec.md` 对应章节，再读代码。
2. 小步改，测试跟着改：`/usr/bin/python3 -m unittest discover -s tests`。6 个查看真实进程的孤儿清理用例（macOS 用 `ps`，Linux 读 `/proc`）在禁止这样做的沙箱里会失败，属环境限制，不要为此改代码。CI（`.github/workflows/tests.yml`）在 ubuntu-latest 和 macos-latest 上各跑一遍全部单测。
3. 改了卡片结构，跑 `/usr/bin/python3 scripts/validate_cards.py`（会调飞书 cardkit 校验，不发消息；离线时用 `--dump`）。
   改了调度、沙箱、沙箱外执行或卡住处置，或者升级了 Claude Code，跑 `/usr/bin/python3 scripts/selftest.py`（真实 claude + 真实沙箱，不碰飞书，几分钟、几美分）。
4. 新增配置项：加在 `ticketd/config.py`（经 `_env` 读取，自动支持 `config.env`），同步 `config.example.env` 和 README 的配置表。
5. 用户可见的变化记到 `CHANGELOG.md`。

## bridge 源码（`bridge/`）

发版包里的 bridge 由本仓库 `bridge/` 下的源码构建（TypeScript，pnpm；T0112 起，spec §13「源码并入仓库」）。原来的 fork 和上游都不再跟进。
- bridge 的改动直接改 `bridge/`，测试先行：`cd bridge && pnpm install && pnpm test && pnpm typecheck`（CI 的 `bridge` 任务在 ubuntu 和 macOS 上跑）。沙箱里 `tests/integration/ui/server.test.ts` 的 12 个用例要在本机监听端口，会失败，属环境限制；在 bridge 驱动的会话里跑时先清掉 `LARK_CHANNEL*`、`LARKSUITE_CLI_CONFIG_DIR`，否则 2 个用例会读到本机的 profile。
- 不改编译产物；发版时 `scripts/build_bridge.py --ref <tag> --out <目录>` 从这个 tag 已提交的 `bridge/` 构建，`release.py release --bridge <目录>` 核对它的 `commit` 就是 `<tag>:bridge` 的树对象号。`bridge/` 源码本身不进发版包。
- ticketd 读 bridge 日志里的几类事件判断它在线和空闲（`ticketd/bridge.py` 的 `ONLINE_EVENTS`、`STARTED_EVENT`、`ACTIVITY_PHASES`；并发池的 `pool acquired/released` 带 `active`）。改 bridge 的日志时别改掉这些。
- `bridge/LICENSE`（上游 MIT 版权声明）和 `NOTICE.md` 要留着。
- 上游以后的修复不会自动过来；需要时手工挑着搬，提交说明里写清来源。

## 去敏（每次提交前检查）

仓库公开，不得出现：密钥、令牌、真实的 open_id / chat_id / message_id、本机用户名路径、公司或客户项目信息、工单历史、数据库和日志。
测试和示例用 `ou_owner`、`oc_1`、`om_1` 这类假值。提交和推送前由钩子自动检查（每个克隆装一次）：

    /usr/bin/python3 scripts/check_private.py --install-hooks   # pre-commit 查暂存的新增行和提交身份，pre-push 查要推的每个提交
    /usr/bin/python3 scripts/check_private.py                   # 手动查整个工作树

只有你自己知道的串（用户名、公司名、真实 open_id）写进 `~/.ticketd/private-patterns`（一行一个正则，不进仓库）。
检查只打印位置不回显内容；确属误报的行写上 `check-private: ignore`。`tests/test_check_private.py` 会在 CI 里查整个仓库。
