# P2-1 配额用量统计设计（2026-10-29）

用户逐项拍板的需求：**只统计不拦截**、看**任务数 / 存储占用 / 生成耗时**、**仅 admin** 可见、**可切时间段**。UI 测试由用户负责，本轮交付到构建 + API 层验证为止。

## 口径（先定死再写代码）

| 维度 | 口径 |
|---|---|
| 任务数 | 总数 + 按状态分列（成功 / 失败 / 取消） |
| 存储占用 | 素材 `file_size` 累加（按素材归属任务的 `created_by` 记到人头） |
| 平均耗时 | 只算 `status == "done"` 且 `started_at` / `finished_at` 都非空的；**没有完成任务返回 `null` 而不是 0**（0 会被误读成「秒完成」） |
| 合计行耗时 | 全体完成任务的 `sum(时长) / 个数` 重算，**不是各行平均值的平均**（后者被小样本行拉偏） |
| 拦截 | **不做**。统计纯展示，超不超额都能正常提交，避免误伤业务 |

**权限**：整个端点 403 收口（`is_admin(user)`），不区分「看自己还是看全部」——非 admin 一律 `403 仅管理员可查看用量统计`。

**时间语义**：与素材筛选完全一致——纯日期的 `date_to` 含当天全天。为此把解析逻辑抽成唯一实现 `app/timeutils.py:parse_day(s, end=False)`，`assets.py` 与 `stats.py` 共用，不留第二份副本（语义漂移是这类「同款小函数」最常见的债）。

## 后端

**`GET /api/stats/usage?date_from=&date_to=`**（`app/api/stats.py`，挂载在 `main.py`）

- 时间过滤同时作用于 `Task.created_at` 和 `Asset.created_at` 两套条件（素材时间与任务时间分开算，谁落在区间算谁）。
- 两条查询：任务行 `(created_by, status, started_at, finished_at)`；素材行 `(created_by, file_size)` join `Task` 取归属（素材表本身没有归属字段）。
- 聚合 `bucket(uid)`：`task_total / task_done / task_error / task_cancelled / durations[] / storage_bytes / asset_count`。
- 无主任务（`created_by` 为空）归到 `username="无主数据"`，不静默丢数据。
- 响应 `UsageResponse`：`date_from` / `date_to` 回显 + `by_user[]` 每人一行 + `totals` 合计行。

## 前端

- **`components/modules/UsagePanel.tsx`** + `modules/index.ts` 注册 `usage` Tab（`roles: ['admin']`，`order: 6`）。
- 表格列：用户 / 任务数 / 成功 / 失败 / 取消 / 素材数 / 存储占用 / 平均耗时；**合计行直接展示后端算好的 `totals`**，不做前端二次相加（口径归一处）。
- 时间切换：两个 `<input type="date">` + 快捷按钮「本月」「上月」「全部」。
- 空态「该时间段内没有任务记录」；非 admin 兜底提示「仅管理员可查看用量统计」。
- 格式化：`formatSize`（KB/MB/GB）、`formatDuration`（「3 分 20 秒」/「45 秒」/ `null` → `—`）。

## 验证

**`backend/tests/verify_usage_stats.py`**（25 项 + 清理）：
1. 权限：admin 200 / member 403。
2. 无区间增量核对：admin +5 任务、+3 成功、+1 失败、+1 取消、+15000B；member +2 任务、+13000B；member 平均耗时精确 120s（[60,180]）；合计 = 各行之和。
3. 时间区间：10-01 区间只 +1、今天区间 +4、未来起点 `by_user` 为空。
4. 口径守卫：只有失败任务的用户 `avg_duration_seconds` 为 `null`（不瞎填 0）。
5. 回归守卫：端点只给 admin / 耗时只算 done / 两处时间解析都走共享 `parse_day`。

**`verify_frontend_contract.py`** 补 `UsageByUser`（11 字段）/ `UsageResponse`（4 字段）字段表，拉真实响应逐字段比对（`avg_duration_seconds` 归 `req`——它是「存在但可为 null」，不是缺字段）。

**全量回归 23 套全绿**（原 21 + `verify_from_asset_upload` + `verify_usage_stats`）。

### 测试教训（写绝对断言踩的坑）

- **库里不干净是常态**：admin 名下有历史真实任务，「任务数 == 5」这类绝对断言必假红。解法是**基线快照 + 增量断言**——播种前 `snapshot()` 取基线，播种后比 `delta()`，只断言「+N」。
- **基线必须与断言同口径**：按时间区间断言时，基线和 after 快照要用**同一个查询参数**取（`Q_OLD` / `Q_TODAY`）；拿「无区间快照」跟「带区间基线」做 delta 是错的，过滤条件不同根本不可比。而且基线要取在**播种之前**——播种后再取一次就等于把测试数据也算进基线，增量恒为 0。
- **平均耗时不可加**：delta 套不上，只能用「名下只有测试数据的新建账号」做精确断言（本次用临时 member 断 120s）。
- 日期参数不能硬编码测试跑的那天，`TODAY` 动态算（P1 同款教训）。

## 非目标（本期不做）

- 超额拦截 / 配额上限（产品拍板「只统计不拦截」，要做另立需求）。
- 时间趋势图（本次只选了三个维度，趋势图是独立工作量）。
- 普通用户看自己的用量（可见范围拍板「仅 admin」）。

## 收尾杂项

- 测试残留已清理：临时任务 8 个、模板 1 个、`deltest-usage*` 用户 2 个，库内只剩 `admin` / `member01` / `xx`。
