## 1. 客户端（T0073）

- [x] 1.1 清单 c2（保留 c1），新入网用 c2，`ticket console accept [c1|c2]`
- [x] 1.2 心跳 `problems`：映射、20 条上限、`more`、游标（200 且 ok 才前移）、show 不前移
- [x] 1.3 `ticket console status` / `show` 对 c1 设备的提示
- [x] 1.4 `ticket diag` / `ticket diag send`、打码、48KB 上限、只发 diag 目录里的文件
- [x] 1.5 主会话约定（`workspace/CLAUDE.md.tmpl`）
- [x] 1.6 `docs/spec.md` §12、CHANGELOG
- [x] 1.7 单测（`tests/test_problem_reports.py`、`tests/test_console.py`）

## 2. 上线（和控制台一起）

- [ ] 2.1 控制台先上线（认得 c2、`problems`、两个新接口）
- [ ] 2.2 客户端发版；在一台已入网的机器上 `ticket console accept`，造一次失败，控制台看到记录
- [ ] 2.3 `ticket diag T…` → `ticket diag send` → 控制台收到、管理员收到私信
