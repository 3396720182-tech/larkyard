> spec 已写（2026-11-20）；1～7 节已实现并验证（临时 HOME 全流程 + 单测 + 三路对抗审查），真实租户上的扫码建应用、bot 身份查所有者、真实 launchd 下的迁移还没实测。

## 1. fork 源码

- [x] 1.1 fork 新分支 `larkyard/vendor`：会议三个修复之上加「会中 @ 任意位置、忽略自己的弹幕、轮询发现散会」和本机补丁移植（会中授权、会中提醒、会议号提取、附件批处理、终态折叠、流式宽限、模型选项），每项有单测
- [x] 1.2 打包产物冒烟：原先针对编译产物写的冒烟脚本全部通过；新旧打包产物对同一组输入逐项输出一致
- [ ] 1.3 给上游的 PR 分支和正文（分支和正文已备好；推送、开 PR 走申请，未执行）

## 2. 构建和打包

- [x] 2.1 `vendor/bridge.lock.json`、`vendor/runtime.json`
- [x] 2.2 `scripts/build_bridge.py`：检出锁定提交、构建、扁平生产依赖、许可证声明、`BRIDGE.json`（`tree_sha256`）、拒绝链接
- [x] 2.3 `release.py release --bridge`：核对锁文件和 `tree_sha256`、加进包、可复现；`publish-installer`
- [x] 2.4 单测：错的 bridge 被拒、两次打包字节相同、老客户端的 `update.extract` 能装带 bridge 的包

## 3. 运行时和 bridge 管理（`ticketd/bridge.py`）

- [x] 3.1 `ensure_runtime()`：Node、lark-cli 下载核对、只解需要的文件、原子换链接、镜像
- [x] 3.2 bridge 服务模板（launchd / systemd）、每日重启任务模板、渲染和加载
- [x] 3.3 `bridge/current` 链接、`state.json`、`restart [--if-stale]`、在线确认和切回、私信
- [x] 3.4 `update.prune` 保留 bridge 在用的版本；新版本确认后记 `bridge.pending`
- [x] 3.5 `ticket bridge status`、`ticket doctor` 的 bridge 一项、worker 拒绝
- [x] 3.6 `scripts/install.sh` 优先用固定目录里的 Node 和 lark-cli（不存在时输出不变）
- [x] 3.7 单测（两个平台的分支都要有）

## 4. 一行安装和向导

- [x] 4.1 `install.sh`：参数、环境检查、内置指纹、取公钥核对、验签、下载核对、交给 `ticketd.bootstrap`、`/dev/tty`、`--enroll`
- [x] 4.2 `ticketd/bootstrap.py`：再验一遍、解包、启动器、`current`、信任公钥、接受清单、`config.env`、运行时、可重复执行、不降级
- [x] 4.3 `ticket setup`：应用、工作区、服务、认主人（应用 owner / 6 位码）、用户授权、自检；非终端时的行为
- [x] 4.4 单测；临时 HOME 里从零跑一遍一行安装（本地更新源、测试钥匙、假 claude / lark-cli / launchctl / bridge）

## 5. 迁移

- [x] 5.1 `ticket bridge adopt`：前提检查、列出改动和 `yes`、`--dry-run`、备份、改写、重新加载、在线确认、失败自动退回
- [x] 5.2 `--undo` 逐字节恢复
- [x] 5.3 单测（假服务管理器）
- [x] 5.4 本机迁移清单交给主会话（本变更不在维护者本机执行；本机迁移本身未做）

## 6. 文档

- [x] 6.1 README（中英文）首页安装改成一条命令，删掉单独装 bridge 的步骤；保留「从 clone 安装」作为开发者路径
- [x] 6.2 `docs/spec.md` 新增「bridge 内化与一行安装」一节，调整 §3、§11
- [x] 6.3 CHANGELOG、CLAUDE.md 上游边界、`workspace/` 模板、`config.example.env`、NOTICE

## 7. 审查

- [x] 7.1 全部单测
- [x] 7.2 2～3 个范围小的独立对抗审查，修完阻断项
