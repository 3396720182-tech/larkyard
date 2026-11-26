## 1. 实现

- [x] 1.1 `CONSENT_C3`、`reports_problems` / `reports_usage`；新入网默认 c3；`switch_consent` 抽出来给 accept 和私信同意共用
- [x] 1.2 `tickets.cat`、`ticket new --cat`、本机关键词兜底
- [x] 1.3 `cap_calls`：调度器按过程日志记技能脚本调用
- [x] 1.4 心跳 `usage`；旧控制台不认识时去掉
- [x] 1.5 私信新清单、`ticket console agree`、守护进程核对
- [x] 1.6 工作区规则：派单带 `--cat`；用户回复「同意」时执行 agree
- [x] 1.7 单测；`docs/spec.md` §12 调整；CHANGELOG

## 2. 上线

- [ ] 2.1 控制台先上线 add-oversight（认识 c3 和 usage）
- [ ] 2.2 发这一版 Larkyard
