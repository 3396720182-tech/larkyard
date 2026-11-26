## 1. 实现

- [x] 1.1 `CONSENT_C3`、`reports_problems` / `reports_usage`；新入网默认 c3；`switch_consent` 抽出来给 accept 和自动升级共用
- [x] 1.2 `tickets.cat`、`ticket new --cat`、本机关键词兜底
- [x] 1.3 `cap_calls`：调度器按过程日志记技能脚本调用
- [x] 1.4 心跳 `usage`；旧控制台不认识时去掉
- [x] 1.5 c2 → c3 心跳前自动升级（只升一次，私信一句说明）
- [x] 1.6 工作区规则：派单带 `--cat`
- [x] 1.7 单测；`docs/spec.md` §12 调整；CHANGELOG

## 2. 上线

- [ ] 2.1 控制台先上线 add-oversight（认识 c3 和 usage）
- [ ] 2.2 发这一版 Larkyard
