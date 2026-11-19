## 1. 规格和配置

- [ ] 1.1 `docs/spec.md` 新增「自动更新」一节（引用本变更的两份 spec），§10 写「调整」
- [ ] 1.2 `ticketd/config.py` 加 `TICKETD_UPDATE_URL`、`TICKETD_UPDATE_CHANNEL`、`TICKETD_UPDATE_EVERY`（下限 600）、`TICKETD_UPDATE_PROBATION`、`TICKETD_UPDATE_STALE_DAYS`；同步 `config.example.env` 和 README 配置表
- [ ] 1.3 定义清单和暂停标记的 JSON 格式（字段、确定性序列化），写进 `docs/spec.md`

## 2. 发版工具（`scripts/release.py`）

- [ ] 2.1 从 tag 打包：只含被跟踪文件、检查工作树干净和版本号一致、同一 tag 两次打包 SHA-256 相同（固定 mtime / 属主 / 排序）
- [ ] 2.2 生成清单并用 `ssh-keygen -Y sign -n larkyard-release` 签名；拒绝非 Ed25519 密钥
- [ ] 2.3 生成回滚清单、暂停标记（带有效期）、带 `next_public_key` 的换钥清单
- [ ] 2.4 检查 `db.py` 相对上一个 tag 是否有破坏性迁移，出现则警告
- [ ] 2.5 单测：上面每种情况各一条，用临时 git 仓库和临时密钥

## 3. 客户端验签和检查（`ticketd/update.py`）

- [ ] 3.1 启用条件：URL、`allowed_signers`、受管安装、`ssh-keygen -Y verify` 可用；未启用时不发请求
- [ ] 3.2 拉清单、暂停标记（`file://` / `https://`，超时、抖动、失败只记事件）
- [ ] 3.3 验签 → 产品名和通道 → 降级保护和最低起点 → 下载包 → 大小和 SHA-256；每种失败记对应事件
- [ ] 3.4 换钥：自检通过后用 `next_public_key` 替换 `allowed_signers`
- [ ] 3.5 worker 环境里拒绝 `hold / resume / rollback / adopt` 和修改信任公钥
- [ ] 3.6 单测：spec 里「验签和校验不过就拒装」「降级保护」「信任的公钥」各场景

## 4. 安装、切换、自检、回滚

- [ ] 4.1 `ticket update adopt` / `adopt --undo`：建版本目录和 `current` 链接，重新生成服务模板；不执行时 plist / unit 逐字节不变（单测比对）
- [ ] 4.2 空闲判定和「安装中」标记，安装期间调度器不启动新 worker
- [ ] 4.3 解包到版本目录（拒绝绝对路径和 `..`）、原子切换 `current`、保留最近 3 个版本
- [ ] 4.4 重启服务：`host.py` 旁加 macOS（`launchctl kickstart -k`）和 Linux（`systemctl --user restart`）分支，两个平台 mock 单测
- [ ] 4.5 启动器看门狗：`update.pending` 过期或期限内第 3 次启动就切回 `from`；全部分支单测
- [ ] 4.6 自检通过记「已确认」；失败自动回滚、私信主用户、拉黑该版本

## 5. 命令和可见性

- [ ] 5.1 `ticket update status | check | hold [--days N] | resume | rollback`
- [ ] 5.2 `ticket doctor` 增加更新一项（未启用 / 正常 / 清单过期 / 验签工具不可用）
- [ ] 5.3 `events` 记更新事件（检查失败、验签失败、校验失败、通道不符、拒绝降级、已安装、已确认、已回滚）
- [ ] 5.4 `CHANGELOG.md`、README（启用方法、手工回退方法、私钥丢失怎么办）

## 6. 在两台机器上验收（不依赖控制台）

- [ ] 6.1 本地目录做更新源，canary 通道正常升级一次，空闲后 1 小时内装上
- [ ] 6.2 三种拒装：错误签名、SHA-256 不一致、无回滚标记的旧版本，各自记录对应事件
- [ ] 6.3 有工单在跑、有沙箱外脚本在跑时不安装，结束后再装，工单不丢
- [ ] 6.4 发一个启动就崩的版本，10 分钟内自动切回并收到私信
- [ ] 6.5 签名暂停标记生效；过期的暂停标记被忽略
- [ ] 6.6 未配置更新源的安装：抓包或日志确认没有更新请求，匿名统计行为不变
- [ ] 6.7 全部单测（ubuntu + macos CI）通过；改了调度和重启，跑 `scripts/selftest.py`
