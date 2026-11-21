## 背景

`install.sh` → `bootstrap` 在没有 `~/.ticketd/app/current` 的机器上会解包、切 `current`、写受管状态，但不碰服务（`bootstrap.py` 只管文件）。之后把服务改成经启动器运行的只有两处：`ticket setup` 第 ③ 步（`setup.step_services` → `update.capture_service` → `scripts/install.sh` 受管模式），以及 `ticket update adopt`（从克隆转）。`ticket bridge adopt` 第 4 步只重装已经经启动器运行的 ticketd 服务（还在跑克隆就拒绝）。这三条路径都按 `TICKETD_LABEL`（默认 `dev.larkyard.ticketd`）找服务，都不知道旧代码里写死了什么。

## 决定

**一个新命令，不改现有三条路径的行为。** `ticket migrate` 编排已有的积木：`scripts/install.sh` 的受管模式、`update._rewrite`（主会话约定）、`bridge.adopt`（整段复用，包括它自己的备份和自动退回）。`setup` 只加一道拦截。

**按启动命令认服务，不按服务名。** 早期版本的服务名写死在模板里，用户可能不记得；按 `bin/ticketd` / `launcher.py` + 数据目录认最稳。数据目录不同的服务不算（另一套安装）。

**用旧代码自己算配置。** 早期版本只读环境变量、默认值写在代码里（甚至不是字面量，比如 `Path.home() / …`），正则和 AST 都不可靠。用旧代码的 `config` 模块、只给服务里的环境去算，得到的就是正在跑的守护进程看到的值。只执行 `config` 模块（标准库 `os`、`pathlib`），30 秒超时，`python3 -I`。

**只写不同的值。** 旧代码本来就读 `config.env`（从 clone 装的 Larkyard）时什么都不写；冲突时以正在跑的为准（「保留现有」），计划里写出原值，退回时按键改回。

**服务名统一成默认名。** 之后的 `ticket setup`、`ticket bridge adopt`、`ticket update` 都按默认名找服务；保留自定义名就得永远带 `TICKETD_LABEL`，忘一次就装出第二个服务。旧服务文件移进备份目录，退回时放回原处。

**先停旧的、等锁、再拷库。** `launchctl bootout` 返回时进程不一定退出了（停机流程最长 45 秒）；拿 `daemon.lock`（和守护进程同一把 `flock`）确认，拿到马上放。库快照用 SQLite 备份接口，`mode=ro` 打开源库。

**ticketd 失败全退，bridge 失败只退 bridge。** ticketd 这段没成功，机器上不能留半个受管安装；bridge 这段失败时 ticketd 已经升级好、照常可用（和「先 `update adopt`、过几天再 `bridge adopt`」是同一个中间状态），全退反而多打断一次。

**`install.sh` 里先检查、再确认、再安装。** 检查用包里的代码（`python3 -m ticketd.migrate --check`），只解出了 `ticketd/` 也够用；检查不过就什么都不装。更新源改由 `migrate` 在备份之后写（`bootstrap --no-update-url`），退回时能撤掉。确认在 `install.sh` 里读 `/dev/tty`，之后 `ticket migrate --yes` 不再问。

**`--dry-run` 不打开数据库读写。** `cli.main` 平时第一步就 `db.connect()`（建表、加列）；`migrate` 在这之前处理。

## 没做的

- 受管安装、版本更低的机器不在这里切换：交给守护进程按试用期流程（和 `bootstrap` 一致）。计划里说明怎么做。
- 不删旧代码目录、不卸载旧的 bridge 安装、不重写主会话约定的内容（只改 `ticket` 路径）：手写的约定可能有私人内容，`ticket doctor` 会提示和模板不一致，由人决定是否 `setup-workspace.sh --force`。
- 退回不恢复数据库：迁移只增不删，旧代码照常读写（模拟里用早期版本实测过）；快照留在备份目录。
- Node 和 lark-cli 仍由 `bootstrap` 按 `vendor/runtime.json` 下载，没有离线包。
