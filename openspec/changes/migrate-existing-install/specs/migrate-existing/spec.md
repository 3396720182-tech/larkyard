## ADDED Requirements

### Requirement: 认出现有安装
`ticket migrate` SHALL 在服务目录（macOS 的 `~/Library/LaunchAgents`、Linux 的 systemd 用户单元目录）里按启动命令认出跑 ticketd 的服务：命令里有 `<目录>/bin/ticketd`（就地运行）或 `launcher.py`（受管安装），且服务环境里的 `TICKETD_HOME`（没有就是默认的 `~/.ticketd`）就是本机的数据目录；服务名不限。找到多于一个时 MUST 拒绝并列出它们。找不到时 MUST 拒绝并提示新机器用一行安装。

#### Scenario: 早期私有版本的服务名
- **WHEN** 机器上只有一个 `com.example.ticketd` 服务，启动命令是 `~/Projects/ticketd/bin/ticketd`
- **THEN** 计划里认出它和旧代码目录，新服务名是 `dev.larkyard.ticketd`

#### Scenario: 两个服务
- **WHEN** 同一个数据目录有两个跑 ticketd 的服务
- **THEN** 拒绝，列出两个服务文件，什么都不改

### Requirement: 搬配置
对就地运行的旧代码，`ticket migrate` SHALL 用旧代码自己的 `config` 模块、只用服务里的环境（加 `HOME` 和系统 PATH），算出 `owner()` 和 `workroot()` 的值；和新版本按 `config.env` 会用的值不同时写进 `config.env`（主用户为空时不写）。服务环境里除 `TICKETD_HOME`、`LARK_CHANNEL_HOME`、`LARK_CHANNEL_PROFILE`、`TICKETD_PYTHON` 以外、`config.env` 里没有的 `TICKETD_*` SHALL 也写进去。`config.env` 里已有不同的值时 SHALL 以旧代码实际在用的为准，并在计划里写出原值。计划里的 open_id MUST 只显示末 6 位。

#### Scenario: 主用户写死在代码里
- **WHEN** 旧代码 `owner()` 返回代码里写死的 open_id，`config.env` 里没有 `TICKETD_OWNER`
- **THEN** 迁移后 `config.env` 里是这个 open_id，新守护进程照常接单

#### Scenario: 从 clone 装的 Larkyard
- **WHEN** 旧代码本来就读 `config.env`
- **THEN** 主用户和工单根目录不重复写

### Requirement: 只读检查
`ticket migrate --dry-run` 和 `python3 -m ticketd.migrate --check` SHALL 只读：不建表、不迁移数据库（只用 `mode=ro` 打开查 running 的工单），不写 `~/.ticketd` 和服务文件，不停服务。有挡路的情况（多个服务、正在运行的工单或读不了数据库、受管安装版本比要装的旧、服务跑克隆但已有更旧的 `current`、定不出主用户、已经迁移过且没退回、新服务名的文件已被别的服务占用）SHALL 列出并以非零退出。

#### Scenario: 先看再做
- **WHEN** 用户执行 `install.sh --upgrade-existing --dry-run`
- **THEN** 打印计划、退出码 0，HOME 下没有任何文件变化，旧服务照常在跑

### Requirement: 升级
`ticket migrate` SHALL 在终端里确认 `yes` 后（`install.sh` 已确认时用 `--yes`）按顺序：备份旧服务文件、`config.env` 和主会话约定到 `~/.ticketd/migrate/backup/<时间>/` 并先记下迁移记录 → 停旧服务并等 `daemon.lock` 放开（超时就失败）→ 用 SQLite 备份接口拷数据库快照 → 写配置 → 服务名不同时移走旧服务文件 → 用当前版本的 `scripts/install.sh` 以受管模式装 `dev.larkyard.ticketd`（环境沿用旧服务的）→ 受管状态里记下服务名和环境 → 主会话约定里的 `<旧代码目录>/bin/ticket` 改成 `~/.ticketd/app/current/bin/ticket` → 等新守护进程的心跳。这些步骤里任何一步失败 SHALL 按记录自动退回已经做过的步骤并说明原因。之后 bridge 服务是分开安装的就做 `ticket bridge adopt` 的全部步骤；这一步失败 SHALL 只退回 bridge，并说明 ticketd 已经升级好。worker 环境和 `LARK_CHANNEL=1` 时 MUST 拒绝。

#### Scenario: 新守护进程起不来
- **WHEN** 装好新服务后 90 秒内没有心跳
- **THEN** 新服务被卸掉、旧服务文件和配置、主会话约定恢复原样，旧服务重新加载

#### Scenario: 在主会话里执行
- **WHEN** 环境里有 `LARK_CHANNEL=1`
- **THEN** 拒绝，提示在自己的终端里执行

### Requirement: 退回
`ticket migrate --undo` SHALL 先退回 bridge 迁移（如果做过），再卸掉新服务、恢复旧服务文件并重新加载、把写过的配置键改回原值或删掉、改回主会话约定；这次安装才建的 `current` 和启动器 SHALL 撤掉（版本目录保留）。数据库不改回（只增不删，旧代码照常读写），说明快照位置。没有迁移记录、备份缺失、有正在运行的工单、或原来的代码目录不在了时 MUST 不改任何文件。

#### Scenario: 退回早期版本
- **WHEN** 迁移后执行 `ticket migrate --undo`
- **THEN** 旧服务文件、bridge 服务、重启任务、`secrets-getter`、`config.env`、主会话约定和迁移前逐字节相同，旧代码在迁移过的数据库上照常运行

### Requirement: 一行安装的升级模式
`install.sh --upgrade-existing` SHALL 在核对公钥、签名和包之后、写 `~/.ticketd` 之前用包里的代码执行只读检查，不通过就停；`--dry-run` 到此为止。否则 SHALL 从 `/dev/tty` 读 `yes`，然后按一行安装装受管安装，但 MUST NOT 写 `config.env`、MUST NOT 打开数据库（推迟到 `ticket migrate` 停掉旧守护进程、拷完快照之后）；再执行 `ticket migrate --yes --update-url <地址>`（由它在备份之后写更新源；发布地址是本地目录时用内置的正式地址或不写）。迁移没开始就失败、或自动退回时，这次装上的 `current` SHALL 撤掉。`--dry-run` 不和 `--upgrade-existing` 一起用、`--upgrade-existing` 和 `--enroll` 一起用时 MUST 拒绝；`LARK_CHANNEL=1` 时（非 `--dry-run`）MUST 在核对之前拒绝。

### Requirement: setup 不装第二个服务
`ticket setup` 在扫码建应用、写工作区和任何服务之前发现直接跑旧代码的 ticketd 服务（任何服务名）时 MUST 拒绝，提示用 `ticket migrate`。
