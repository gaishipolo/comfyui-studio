# 归档：一次性 UI 手工脚本

这些脚本是 2026-10 开发期的**一次性手工冒烟**，不是回归测试：

- `frontend/verify_f*.sh` → 归档自 `frontend/`（硬编码了某台机器的盘符路径，依赖
  `npx agent-browser` 临时下载浏览器驱动）。
- `tests/ui_verify*.sh` → 归档自 `tests/`（同样硬编码绝对路径）。
- `.ui-snap.yaml` → 归档自 `tests/`：一份 Playwright 可访问性快照，**把当时的
  测试残留模板名**（`验证用-B5-generic-*`、`删除能力-*` 等 14 个）固化成了期望状态。
  那些模板早已被清理，快照本身也早已陈旧。

保留它们只为留痕（历史审查报告 `docs/reviews/2026-10-27-code-review.md` 引用过
`tests/ui_verify_pw.sh` 的结论）。**不要把它们当回归入口跑** —— 当前回归入口是：

- 后端单元测试：`backend/tests/test_*.py`
- 后端离线 verify：`backend/tests/verify_*.py`
- 前端状态测试：`frontend/` 下的 `npm run test:state`
- 前端浏览器套件：`frontend/` 下的 `npm run test:ui`

原因与建议见 `docs/code-review-2026-11-01.md` 的 M43。
