## 1. 约定

- [x] 1.1 `prompts/worker.md`：summary / ask 写法（各 ≤2 句、不含技术内容、细节进结果.md）和正反例；沙箱外执行脚本开头写一句说明
- [x] 1.2 `workspace/CLAUDE.md.tmpl`：主会话回复只写结论和下一步

## 2. 卡片模板（`ticketd/templates.py`）

- [x] 2.1 `technical()`、`brief()`：判断技术内容，给出正文显示的短版
- [x] 2.2 `result_elements`：补充细节和「需要你」超限时收进「详情」，有稿子时提示看文档
- [x] 2.3 `run_elements`：一句「要做什么」+ 默认收起的完整脚本（`element_id=run_script`）；稿子按钮、徽标不进脚本面板
- [x] 2.4 `ping_message` 只带问题第一句；默认提问措辞改为「这段脚本」

## 3. 验证

- [x] 3.1 单测 `tests/test_card_concise.py`（spec 每个场景），既有用例同步
- [x] 3.2 `scripts/validate_cards.py` 加长 / 技术内容变体，cardkit 校验通过
- [x] 3.3 `docs/spec.md` §5.3、§5.5 写「调整」，`CHANGELOG.md`
