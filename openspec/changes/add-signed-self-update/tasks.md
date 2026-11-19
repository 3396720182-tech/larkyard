## 1. 规格和配置

- [x] 1.1 `docs/spec.md` 新增「自动更新」一节（引用本变更的两份 spec），§10 写「调整」
- [x] 1.2 `ticketd/config.py` 加 `TICKETD_UPDATE_URL`、`TICKETD_UPDATE_CHANNEL`、`TICKETD_UPDATE_EVERY`（下限 600）、`TICKETD_UPDATE_PROBATION`、`TICKETD_UPDATE_STALE_DAYS`；同步 `config.example.env` 和 README 配置表
- [x] 1.3 定义清单和暂停标记的 JSON 格式（字段、确定性序列化），写进 `docs/spec.md`

## 2. 发版工具（`scripts/release.py`）

- [x] 2.1 从 tag 打包：只含被跟踪文件、检查工作树干净和版本号一致、同一 tag 两次打包 SHA-256 相同（固定 mtime / 属主 / 排序）
- [x] 2.2 一次发版签齐 canary / early / stable 三份清单（`ssh-keygen -Y sign -n larkyard-release`）；拒绝非 Ed25519 密钥
- [x] 2.2a 本机递增序号 `seq`：计数器文件 + 签名日志，回拨时拒绝签名
- [x] 2.3 回滚清单（`expires_at` 默认 72 小时、最长 7 天）、按 7 天窗口预签 12 周暂停标记、用离线备份私钥签的换钥清单
- [x] 2.4 检查 `db.py` 相对上一个 tag 是否有破坏性迁移，出现则警告
- [x] 2.5 单测：上面每种情况各一条，用临时 git 仓库和临时密钥

## 3. 客户端验签和检查（`ticketd/update.py`）

- [x] 3.0 `ticket update trust --main/--backup --fingerprint/--backup-fingerprint`：指纹不一致拒绝写入
- [x] 3.1 启用条件：URL、主公钥和备份公钥、受管安装、`ssh-keygen -Y verify` 可用；未启用时不发请求
- [x] 3.2 拉清单、暂停标记、换钥清单（`file://` / `https://`，超时、0～间隔 1/6 抖动、失败只记事件）
- [x] 3.3 验签 → 产品名和通道 → 序号大于 `update.max_seq` → 降级保护（回滚清单查有效期）和最低起点 → 下载包 → 大小和 SHA-256；每种失败记对应事件
- [x] 3.4 换钥：只接受备份公钥验过的 `rekey` 清单，替换主公钥；主公钥签的换钥清单忽略并记事件
- [x] 3.4a 暂停标记只在 `not_before`～`not_after` 内生效
- [x] 3.5 worker 环境里拒绝 `hold / resume / rollback / adopt` 和修改信任公钥
- [x] 3.6 单测：spec 里「验签和校验不过就拒装」（含重放旧清单、canary 清单放到 stable）、「降级保护」（含过期回滚）、「信任公钥必须按带外指纹核对」「换钥」「全局暂停」各场景

## 4. 安装、切换、自检、回滚

- [x] 4.1 `ticket update adopt` / `adopt --undo`：建版本目录和 `current` 链接、安装启动器、重新生成服务模板，并改写 bridge 工作区 `CLAUDE.md` 的 `ticket` 路径（改前备份，`--undo` 改回）；不执行时 plist / unit 逐字节不变（单测比对）
- [x] 4.2 空闲判定和「安装中」标记，安装期间调度器不启动新 worker
- [x] 4.3 解包到版本目录（拒绝绝对路径和 `..`）、原子切换 `current`、保留最近 3 个版本
- [x] 4.4 重启服务：~~`host.py` 旁加 macOS（`launchctl kickstart -k`）和 Linux（`systemctl --user restart`）分支~~ **调整**（见 design.md D7）：守护进程切换后自己退出（退出码 75），由 launchd `KeepAlive` / systemd `Restart=always` 拉起，不需要平台分支；两个平台的服务文件单测见 `tests/test_install.py`
- [x] 4.5 版本目录外的启动器 `~/.ticketd/app/launcher.py`：只用标准库、不导入 `ticketd`，读 `update-state.json`，过期或期限内第 3 次启动就切回 `from`；全部分支单测，含「新版本 `bin/ticketd` 有语法错误」
- [x] 4.5a 启动器自身两步更新：新版本确认后才原子替换
- [x] 4.6 自检通过记「已确认」；失败自动回滚、私信主用户、拉黑该版本

## 5. 命令和可见性

- [x] 5.1 `ticket update status | check | hold [--days N] | resume | rollback`
- [x] 5.2 `ticket doctor` 增加更新一项（未启用 / 正常 / 清单过期 / 验签工具不可用）
- [x] 5.3 `events` 记更新事件（检查失败、验签失败、校验失败、通道不符、拒绝降级、已安装、已确认、已回滚）
- [x] 5.4 `CHANGELOG.md`、README（启用方法、手工回退方法、私钥丢失怎么办）

## 6. 在两台机器上验收（不依赖控制台）

真机验收还没做（不在用户线上环境执行 adopt 和真实更新）。下面每项后面的「替身」写的是已在临时 HOME + `file://` 替身更新源 + 模仿 launchd / systemd 拉起的监督器里跑过的对应用例（`TICKETD_E2E_UPDATE=1`，`tests/test_update_e2e.py`），真机上按原文再做一遍才勾。

- [ ] 6.1 本地目录做更新源，canary 通道正常升级一次，空闲后 1 小时内装上 （替身：`test_upgrade_then_confirm`）
- [ ] 6.2 拒装：错误签名、SHA-256 不一致、普通清单指向旧版本、过期回滚清单、重放旧序号清单、canary 清单放到 stable，各自记录对应事件 （替身：`tests/test_update.py` 的 CheckTest 各条，单测层面）
- [ ] 6.3 有工单在跑、有沙箱外脚本在跑时不安装，结束后再装，工单不丢 （替身：`test_waits_for_running_ticket_then_installs` 覆盖「有工单在跑」；沙箱外脚本只有单测 `IdleTest`）
- [ ] 6.4 发一个启动就崩的版本、一个 `bin/ticketd` 有语法错误的版本，都在 10 分钟内自动切回并收到私信 （替身：`test_crash_on_start_rolls_back_and_dms`、`test_syntax_error_entry_rolls_back`、另加 `test_hung_new_version_rolled_back_by_watchdog`）
- [ ] 6.4a 用备份私钥换钥一次，之后旧主私钥签的清单被拒 （替身：`tests/test_update.py` RekeyTest，单测层面）
- [ ] 6.5 签名暂停标记生效；过期的暂停标记被忽略 （替身：`tests/test_update.py` PauseTest，单测层面）
- [ ] 6.6 未配置更新源的安装：抓包或日志确认没有更新请求，匿名统计行为不变 （替身：`EnableTest.test_default_install_is_disabled_and_sends_nothing`，mock 掉网络层确认没有调用）
- [ ] 6.7 全部单测（ubuntu + macos CI）通过；改了调度和重启，跑 `scripts/selftest.py` （本机 macOS 全部单测已跑；CI 和 `scripts/selftest.py` 待推送后 / 沙箱外）
