## 1. 客户端（T0107）

- [x] 1.1 `ticketd/userauth.py`：SCOPES、状态检查（D2）、失败归类和中文说明（D6）、发链接 / 等授权 / 补发（D4、D5）、守护进程里的后台线程
- [x] 1.2 `publish.py` / `outbox.py`：授权类失败记 `auth_backfill`、置 `need_link`、指标记 `doc:auth` / `video:auth`、`doc_failures.reason` 用中文
- [x] 1.3 `cli.py`：自检项 `user_auth`，终端里现查
- [x] 1.4 `setup.py`：授权带 SCOPES
- [x] 1.5 `diag.py`：新一节
- [x] 1.6 `daemon.py`：主循环接上
- [x] 1.7 假 lark-cli 支持 `auth status / check / login`；单测 `tests/test_userauth.py`
- [x] 1.8 `docs/spec.md`、CHANGELOG、README 安装步骤 ⑤

## 2. 控制台（larkyard-console `show-user-auth`）

- [x] 2.1 首页「需要处理」单列「还没授权飞书用户身份」；人员 / 设备页的自检显示中文名
- [x] 2.2 心跳入库：新出现的不通过项各记一条（原来只在整体从通过变不通过时记）

## 3. 上线

- [ ] 3.1 推分支、开 PR（两个仓库）
- [ ] 3.2 控制台部署、客户端发版
- [ ] 3.3 一位没授权的同事升级后收到链接、点开同意、补发的文档能打开；控制台上这个人从「还没授权」里消失
