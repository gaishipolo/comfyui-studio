# 任务取消 + 超时中断 — 设计文档

> 日期：2026-10-29
> 状态：已实现
> 前置：功能缺口盘点（P0 #1 任务不可取消 / P0 #4 超时写死 600 秒）

## 1. 背景

视频任务要跑几分钟（LTX 121 帧实测 310 秒），此前**没有任何中断手段**：

- 全库 grep 不到 `cancel` / `interrupt`，跑错了只能等它跑完
- `_poll_result(max_wait=600)` 超时写死 10 分钟，长视频很容易破
- 超时后只是本地放弃轮询，**不通知引擎** —— prompt 继续占显存，
  跑完的产物还没人收（孤儿文件）

## 2. 目标

1. 未终态（pending / running）任务可取消，引擎侧一并停手
2. 超时可配置，且超时时顺手中断引擎
3. 已取消的任务可以重试（用户反悔的自然出口）

**非目标**：并发队列闸门、Celery 化、任务完成通知 —— 其中并发队列闸门与僵尸任务
收割已于同日落地，见 `2026-10-29-task-queue-and-auth-design.md`；Celery 化、
完成通知仍属 P1 另行处理。

## 3. 状态模型

```
pending → running → done | error | cancelled
```

- `cancelled` 是**独立终态**，与 `done` / `error` 并列，不复用 `error`。
  复用会让运营分不清「取消」和「失败」，失败率统计也会失真。
- `Task.status` 是 `String(16)`，加一个枚举值**不需要迁移表结构**。
- 终态不可再取消（返回 409），但 `cancelled` **可以重试**。

## 4. 取消的实现：协作式，不是硬砍

不使用 `asyncio.Task.cancel()`。硬取消那一刀随时可能落在 `db.commit()` 或
httpx 请求半途上，把数据库会话留在脏状态。

改用**协作式**：`task_service._cancel_requested: set[str]` 登记，
执行协程在**轮询间隙**检查并在安全点退出。代价是最多晚
`TASK_POLL_INTERVAL`（2 秒）才停，换来的是不会撕裂正在写的事务。

取消登记必须在 `retry` 时清掉（`clear_cancel_request`），否则重试后的协程
一进轮询就看到「已取消」直接退出，任务永远停在 pending —— 这个坑比取消本身更隐蔽。

### 取消竞态

用户可能在 `provider.submit()` 还没返回时就点取消 —— 那会儿 `provider_task_id`
还是空的，`cancel_task` 通知不了引擎。因此 `execute_task` 在 submit 之后补一刀：

```python
prompt_id = await provider.submit(workflow, task.params)
task.provider_task_id = prompt_id
await db.commit()

if _cancel_requested_for(task.id):      # 取消落在 submit 窗口里
    await provider.cancel(prompt_id)    # 补刀，否则 prompt 照跑不误
    return
```

## 5. 引擎侧取消：首选 + 降级

`GenerationProvider.cancel(provider_task_id) -> bool`，默认 `raise NotImplementedError`
（老引擎不受影响，调用方兜住即可）。

ComfyUI 实现走两条腿：

**首选 `POST /api/jobs/cancel`**（较新版 ComfyUI，实测 0.40.0）：

```json
{"job_ids": ["<prompt_id>"]}   →   200 {"cancelled": true|false}
```

一个请求同时管住 pending 和 running，带 UUID 校验（非法 id 返回 400）。
`{"cancelled": false}` 表示任务早已跑完或不存在 —— **是正常结果，不是错误**。

**降级**（老版本没有 `/api/jobs`，404/405）：

| 情况 | 动作 |
|---|---|
| 在 `queue_pending` | `POST /queue {"delete": [prompt_id]}` 只撤这一条 |
| 在 `queue_running` | `POST /interrupt {"prompt_id": prompt_id}` 定向中断 |
| 都不在 | 返回 False（早已跑完） |

两个坑：

- **绝不能用 `{"clear": true}`** —— 那会清空整个队列，把别人排队的任务一起干掉。
- **老版本的 `/interrupt` 不看 body、是全局中断**。所以必须先用 `GET /queue`
  确认「跑着的确实是我们这条」再发，否则可能误杀别的任务。
  新版本支持 `{"prompt_id": ...}` 定向，实测 0.40.0 已具备。

## 6. 超时

`settings.TASK_TIMEOUT_SECONDS`（默认 1800 秒）取代写死的 600。

超时时**必须**先 `provider.cancel(prompt_id)` 再返回错误 —— 否则 prompt 继续
占显存、产物成孤儿。引擎不支持取消时也不能让超时收尾炸掉。

## 7. API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks/{id}/cancel` | 取消。仅本人或管理员；终态返回 409 |

`POST /api/tasks/{id}/retry` 的准入从 `status == "error"` 放宽到 `error` / `cancelled`。

## 8. 前端

- `Task.status` 类型加 `'cancelled'`，状态徽标「已取消」
- 未终态显示「取消」按钮（带确认）；终态显示「重试」（含已取消）/「删除」
- 勾选批量删除只对**终态**开放 —— 未终态本来后端就删不掉
  （`delete_task` 会挡 `_running_tasks`），前端不该假装能勾

## 9. 测试

`backend/tests/verify_cancel.py`，46 项全绿：

- 引擎取消三条路径 + 请求体形状 + 「只撤自己那条」+ 定向 interrupt
- 降级边界：队列为空、查队列失败、畸形队列项、早已跑完
- 协作式取消：登记即退、不误调引擎、`clear_cancel_request` 生效
- 超时：状态、向引擎发 cancel、文案、引擎不支持取消时仍能收尾
- 状态机：三个终态不可取消、running 可取消、无 provider_task_id 不误调引擎
- 回归守卫：超时不得再写死 600、超时分支必须调 cancel、
  `execute_task` 必须处理 submit 竞态且不覆盖 cancelled

## 10. 踩坑

- **假 HTTP 响应对象要忠实**。第一版自造的 `FakeResp` 超时抛 `RuntimeError`，
  而生产代码 `except httpx.HTTPError` 接不住 —— 测试会假绿。
  改用真的 `httpx.Response(...)`，让 `raise_for_status()` / `json()` 行为与线上一致。
- `resp.json()` 在 200 但非 JSON 时会抛 `ValueError`，调用处要兜。
