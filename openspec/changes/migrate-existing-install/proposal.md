## Why

一行安装（`install.sh`）和 `ticket setup` 是给新机器设计的。已经在跑的旧安装——早期的私有版本（自定义服务名、主用户和工单根目录写死在代码里、不读 `config.env`）和从 clone 装的 Larkyard——没有一条安全的升级路径：

- `install.sh` 在这类机器上会解包新版本、切 `current`，但不动正在跑的服务；
- 之后的 `ticket setup` 按默认服务名找 ticketd 服务：服务名不同的机器会**多装出第二个服务**，两个服务抢同一个数据库锁；写死在旧代码里的主用户没有搬进 `config.env`，新服务拒绝所有派单；工单根目录回到默认的 `~/larkyard-tickets`；
- 服务名相同的机器会被 `setup` 静默改成经启动器运行，但主会话约定里的 `ticket` 路径还指着旧克隆，也没有退回的办法；
- `ticket update adopt` 只认带版本号、工作树干净的 Larkyard 克隆，早期私有版本用不了；`ticket bridge adopt` 要求 ticketd 已经是受管安装。

结果是每台旧机器都要手工排十来步，顺序错了就会两个服务抢锁或者主用户丢失。

## What Changes

- **`ticket migrate [--dry-run | --undo]`**：一条命令把已有的就地安装转成受管安装 + 打包的 bridge。
  - 认出跑 ticketd 的服务（按启动命令里的 `bin/ticketd` / `launcher.py` 和数据目录认，不看服务名），同一个数据目录有两个服务就拒绝；
  - 用旧代码自己的 `config` 模块、按服务的环境算出它**实际在用的**主用户和工单根目录，和新版本会用的值不同才写进 `config.env`；服务环境里其他 `TICKETD_*` 也搬进去；
  - 停旧服务（等数据库锁放开）→ 拷数据库快照 → 写 `config.env` → 服务统一成 `dev.larkyard.ticketd`、经启动器运行（环境沿用原来的）→ 主会话约定里的 `ticket` 路径改到 `current` → 等新守护进程的心跳；任何一步失败都按备份自动退回；
  - bridge 是分开安装的（Homebrew / npm / 其他）就接着做 `ticket bridge adopt`；这一步失败只退回 bridge，ticketd 保持升级好的状态；
  - `--dry-run` 只读（不打开数据库读写、不写 `~/.ticketd`），在哪儿都能跑；动手和 `--undo` 在 worker 里、在 bridge 驱动的会话里（`LARK_CHANNEL=1`）拒绝。
- **`install.sh --upgrade-existing [--dry-run]`**：核对签名和包之后，先用包里的代码只读检查现有安装（`python3 -m ticketd.migrate --check`），有挡路的就停、什么都不装；`--dry-run` 到此为止；否则在终端里确认 `yes` → 装受管安装（不写更新源）→ `ticket migrate`（由它备份并写更新源）。
- **`ticket setup`**：发现直接跑旧代码的 ticketd 服务（任何服务名）时拒绝，提示用 `ticket migrate`，不再装出第二个服务。

## Capabilities

### New Capabilities
- `migrate-existing`: 认出现有安装、对照旧代码的实际配置、只读检查、升级、自动退回、`--undo`、`install.sh --upgrade-existing` 的衔接、`setup` 的拦截。

### Modified Capabilities
（无。现有行为以 `docs/spec.md` 为准；落地时新增 §14，并在 §13 的 `ticket setup` 处注明拦截。）

## Impact

- 代码：新增 `ticketd/migrate.py`、`tests/test_migrate.py`；`ticketd/cli.py` 加 `ticket migrate`（在打开数据库之前处理）；`install.sh` 加 `--upgrade-existing` / `--dry-run`；`ticketd/setup.py` 加拦截。
- 不改数据库结构、不改 bridge 的 profile / 会话 / 凭据；旧代码目录和旧的 bridge 安装都不删（退回要用）。
- 发版：要发 0.3.1（`install.sh` 和包里的代码都变了）。
