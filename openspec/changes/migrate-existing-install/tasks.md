## 1. 规格

- [x] 1.1 proposal / design / specs（migrate-existing）

## 2. 实现

- [x] 2.1 `ticketd/migrate.py`：认服务、对照旧代码配置、只读计划、动手（备份、停服务等锁、库快照、配置、服务、主会话约定、等心跳、失败自动退回）、bridge 衔接、`--undo`
- [x] 2.2 `ticket migrate [--dry-run | --undo]`，在打开数据库之前处理
- [x] 2.3 `install.sh --upgrade-existing [--dry-run]`：检查 → 确认 → `bootstrap --upgrade-existing`（推迟数据库和配置） → `ticket migrate --yes --update-url`
- [x] 2.4 `ticket setup` 发现直接跑旧代码的 ticketd 服务时拒绝

## 3. 测试

- [x] 3.1 单测：早期布局（自定义服务名、写死的主用户和工单根目录、npm 的 bridge）和 clone 布局（默认服务名、读 config.env、Homebrew 的 bridge），macOS / Linux 两个分支；退回逐字节；起不来、旧进程不退出时自动退回；bridge 失败只退 bridge；挡路的情况；worker 和 `LARK_CHANNEL=1`；`--dry-run` 不建库；`setup` 拦截；`config.env` 只改一个键；计划打出旧代码目录的版本、`git log -1`、`git status -s`
- [x] 3.2 临时 HOME 全流程（维护者的工单目录里的模拟脚本，不进仓库）：早期私有版和 clone 版各一遍，真实的守护进程、真实的签名包和 `install.sh`，launchd 是替身

## 4. 文档

- [x] 4.1 `docs/spec.md` §14、README「从已有安装升级」、CHANGELOG

## 5. 审查

- [x] 5.1 全部单测
- [x] 5.2 小范围独立对抗审查，修完阻断项

## 4. 离线升级包（0.3.2）

- [x] 4.1 `install.sh --from-dir`：任意路径（空格、中文、`# % ?`）转成 `file://` 地址；变量统一 `${VAR}`
- [x] 4.2 `scripts/upgrade-offline.sh`（升级包入口，不拒绝任何路径）和 `release.py bundle`（打包前验签、核对摘要和指纹）
- [x] 4.3 测试：刁钻路径下的 `--dry-run`、`--no-setup` 安装、无参数转发、`bundle` 的布局和拒绝
