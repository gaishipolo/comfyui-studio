# 并发队列闸门 + 僵尸任务收割 + 账号管理 — 设计文档

> 日期：2026-10-29
> 状态：已实现
> 前置：`2026-10-29-task-cancel-design.md`（P0 #1/#4），功能缺口盘点 P0 #2 / P0 #3

## 1. 背景

P0 #1/#4（取消、超时）落地后，两块遗留问题继续：

**任务侧**
- 后台执行是**进程内 asyncio 协程** —— 进程一死（崩溃、部署、断电），任务永远停在
  `running`，前端无限「结果加载中」。没有任何启动时的收拾逻辑。
- `settings.MAX_CONCURRENT_TASKS` 是**死配置**（全库没人读它），批量提交 N 个任务
  就同时打引擎 N 个 prompt、开 N 个轮询协程。8GB 显存的 4060 上，两个视频任务
  就能把显存挤爆。

**账号侧**
- 管理员改不了自己的密码（`register` 只建号、`login` 只验证，没有改密端点），
  忘了密码只能进数据库改 bcrypt 哈希。
- 没有用户管理界面：建号、停用、重置密码都要走 API。

## 2. 目标

1. 并发闸门：同时在跑的任务数不超过 `MAX_CONCURRENT_TASKS`，超出的排队
2. 重启后收拾僵尸任务：**尽量接回去跑完**，别让 5 分钟的视频白跑
3. 管理员能改密、建号、停用、重置他人密码，且有前端界面

**非目标**：Celery 化、任务完成通知、配额 —— P1 另行处理。

## 3. 并发闸门

```python
_run_slots = asyncio.Semaphore(max(1, int(settings.MAX_CONCURRENT_TASKS)))
```

- `execute_task` 与 `resume_task` **都**要套 `_run_slots`。只套前者的话，
  重启接回的任务能绕过闸门，配置形同虚设。
- 排队发生在协程内部，不是丢弃或拒绝：任务已经落库为 `pending`，前端照常看得到。
- `max(1, ...)` 兜底：配置写 0 或负数时至少能跑 1 个，不至于全线卡死。

**去重**：`spawn_task` 用 `_running_tasks: set[str]` 登记已启动的协程，
重复调用返回 `False`。`_run_and_cleanup` 的 `finally` 保证异常退出也不泄漏登记
（泄漏后该任务永远被误判为「正在跑」，删都删不掉）。

## 4. 僵尸任务收割

### 4.1 两种处置

启动时 `TaskService.reap_stale_tasks()` 扫 `pending` / `running`：

| 任务状态 | 有 `provider_task_id` | 没有 |
|---|---|---|
| 含义 | 已提交到引擎，引擎上可能还在跑 | 连提交都没成功 |
| 处置 | `spawn_task(resume=True)` **接回去继续轮询** | 标 `error`，提示「可点击重试」 |

关键判断：**有 prompt_id 就说明 prompt 已经在引擎上了**，重新 submit 只会
浪费一份显存和时间。`resume_task` 与 `execute_task` 的唯一区别就是**不 submit**，
直接拿旧 `prompt_id` 走 `_finish_task` 收尾。

### 4.2 启动接线

`main.py` 的 startup 里 `await TaskService.reap_stale_tasks()`，然后打印：

```
[Task] 启动收拾遗留任务：接回 N 条（继续等引擎出结果），标记失败 M 条
```

三条启动日志都加了 `flush=True` —— stdout 重定向到文件时是**块缓冲**，
不刷就查不到（e2e 测试靠读日志断言 reap 跑了，第一版栽在这上面）。

### 4.3 旧函数的坑

`recover_stale_tasks`（原 390 行）把**所有**遗留任务一律标 error，与「尽量接回去
跑完」的策略矛盾。留着是陷阱 —— 后来者可能顺手调它。已删除，不留双入口。

## 5. 账号管理

### 5.1 新增端点

| 方法 | 路径 | 权限 | 用途 |
|---|---|---|---|
| POST | `/api/auth/change-password` | 登录用户 | 改自己的密码 |
| POST | `/api/auth/users/{id}/reset-password` | admin | 重置他人密码 |
| PATCH | `/api/auth/users/{id}` | admin | 改角色 / 启停 / 显示名 |

### 5.2 四道硬规则

改密：
1. 旧密码不匹配 → 400「当前密码不正确」
2. 新旧密码相同 → 400「新密码不能与当前密码相同」
3. 新密码少于 6 位 → 422（Pydantic 校验）

管理他人（自锁）：
4. 不能停用自己 → 400「不能停用自己的账号」
5. 不能降级自己 → 400「不能取消自己的管理员身份」
6. 系统必须保留至少一个启用的管理员 → 400「系统必须保留至少一个启用的管理员」

前两道（1/2/3）是账号安全（防误操作、防弱密码）；后三道（4/5/6）是**防自锁**
—— 否则管理员手一抖把自己停用或降级，整个系统就没人能管了。
改自己的显示名是允许的（不破坏权限），实测 200。

### 5.3 前端

- `ChangePasswordModal`：顶栏「改密」按钮，任何角色可用。
  本地先拦三道（长度 < 6、两次不一致、新旧相同），后端文案原样呈现。
- `UserManager`：admin-only「用户」Tab（`roles: ['admin']`）。
  表格含用户名/显示名/角色 select/状态/操作；对自己那两行**直接把危险操作置灰**
  并给 `title` 说明 —— 不置灰也能拦（后端 400），但置灰体验更对。
- `client.ts` 新增 `changePassword` / `listUsers` / `registerUser` /
  `resetUserPassword` / `updateUser` 五个 API。

## 6. 测试与验证

**静态**：`verify_queue_and_auth.py` 52 项（A 并发闸门 / B 后台登记 / C resume /
D reap / E 账号管理 / F 回归守卫），临时 SQLite 覆盖，假 provider 实现全部抽象方法。
19 套后端测试全绿。

**端到端**：`e2e_restart_recovery.py` ALL PASS ——
真提交 1344² 图 → 抓 `running` 窗口 `taskkill` → 重启 → 日志「接回 1 条」→
任务跑到 `done`，**prompt_id 从头到尾同一个**（证明没重新提交），产物落盘。

**API 线上**：认证 14 项实测（改密六态、权限五查、admin 七场景、自锁三条）全过。

## 7. 测试设计的坑（复用价值高）

- **ComfyUI prompt 缓存**：完全相同的 prompt 会命中缓存，0.6 秒返回。
  「抓运行中窗口」的测试必须给 prompt 加唯一盐（`time.time()` + `uuid4().hex[:8]`），
  否则根本抓不到窗口 —— 第一版就栽在这上面。
- **`reload` 读最终状态必须开全新会话 + `expunge`**。用 `session.rollback()` 刷新
  会把会话里**所有**实例置过期，下次同步读属性触发懒加载 IO，在异步上下文里
  直接 `MissingGreenlet` 炸掉。
- **中文 Windows 上 `subprocess.run(text=True)` 危险**：netstat/taskkill 输出是 GBK，
  按 UTF-8 解码在子线程炸 `UnicodeDecodeError` 且 `stdout` 变 `None`。
  必须字节收 + `decode("gbk", errors="replace")`。
- **`taskkill` 在 Git Bash**：`//PID` 无效、`MSYS_NO_PATHCONV=1 taskkill //PID` 也无效，
  正确是 `taskkill "/PID" 85768 "/F"`。
- **e2e 脚本 Popen 起的子进程后端活不过工具调用**，测完必须由 `run_in_background`
  重新挂起。
