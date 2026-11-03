/** 任务面板 — 生成队列：一张细线台账，展开后是这次生成的结果
 *
 * 用户隔离后（2026-10-29）普通用户只看得到自己提交的任务；
 * admin 看全部，多一列「创建人」便于分清谁的活。 */
import { Fragment, useState, useEffect, useCallback, useRef } from 'react'
import type { Task, Asset, User } from '../types'
import { listTasks, getTaskAssets, retryTask, cancelTask, getAssetUrl, getAssetThumbUrl, openAssetInNewTab, deleteTask, getAllTemplates } from '../api/client'
import FriendlyError from './FriendlyError'
import AuthMedia from './AuthMedia'
import { taskListViewState } from './listViewState'
import { alertDialog, confirmDialog } from './dialog'
import { reportError } from '../notify'
import { formatDateTime } from '../time'

const statusConfig: Record<string, { label: string; tone: string }> = {
  pending: { label: '排队中', tone: 'neutral' },
  running: { label: '生成中', tone: 'accent' },
  // 完成态用中性色：它是**最不需要被注意**的状态，而一屏里它出现最多。
  // 彩色留给「要你处理」的（失败、超额、离线）和「正在进行」的 —— 见下面 error/accent
  done: { label: '已完成', tone: 'neutral' },
  error: { label: '失败', tone: 'error' },
  cancelled: { label: '已取消', tone: 'neutral' },
}

/** 终态：这类任务不会再变，可以删、可以重试 */
const isTerminal = (status: string) =>
  status === 'done' || status === 'error' || status === 'cancelled'

const formatSize = (bytes: number) =>
  bytes > 1024 * 1024
    ? `${(bytes / 1024 / 1024).toFixed(1)}MB`
    : `${(bytes / 1024).toFixed(0)}KB`

export default function TaskPanel({ user, onReuseTask }: { user: User; onReuseTask?: (task: Task) => void }) {
  /** admin 看全员任务，多一列创建人；普通用户只看自己的，没必要显示 */
  const showCreator = user.role === 'admin'
  const [tasks, setTasks] = useState<Task[]>([])
  const [loadFailed, setLoadFailed] = useState(false)
  /** 模板 id → 名称。任务只存 template_id，列表要显示「用的哪个模板」得自己查一次 */
  const [tplNames, setTplNames] = useState<Record<string, string>>({})
  const [assetsMap, setAssetsMap] = useState<Record<string, Asset[]>>({})
  /** 素材拉取状态：无记录=还没拉、loading=拉取中、ready=拉到了（可能为空）、error=失败 */
  const [assetsState, setAssetsState] = useState<
    Record<string, 'loading' | 'ready' | 'error'>
  >({})
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [batchDeleting, setBatchDeleting] = useState(false)
  /** 异步确认框期间的同步重入守卫（state 要等下一次渲染才生效） */
  const batchDeleteBusy = useRef(false)

  /** 后端默认只回 50 条（上限 200）。不显式传 limit 就等于静默截断：
   *  超过 50 条后旧任务从页面上消失，用户以为被删了。 */
  const PAGE_SIZE = 50
  const MAX_LIMIT = 200
  const [pageLimit, setPageLimit] = useState(PAGE_SIZE)
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const data = await listTasks(undefined, { limit: pageLimit })
      setTasks(data)
      setLoadFailed(false)
      setHasMore(data.length >= pageLimit && pageLimit < MAX_LIMIT)
      // 轮询期间任务可能已被别处删掉 —— 失效 id 从选中集剔除，别让「删除 (N)」虚高
      setSelected((prev) => {
        const alive = new Set(data.map((t) => t.id))
        const next = new Set([...prev].filter((id) => alive.has(id)))
        return next.size === prev.size ? prev : next
      })
    } catch {
      setLoadFailed(true)
    }
  }, [pageLimit])

  // 初始加载
  useEffect(() => {
    refresh()
  }, [refresh])

  /** 扩大窗口再拉一次。用「加大 limit 重取」而不是 append：
   *  任务会在轮询中变状态，append 会留下过期的旧副本。 */
  const loadMore = async () => {
    if (loadingMore || !hasMore) return
    const nextLimit = Math.min(pageLimit + PAGE_SIZE, MAX_LIMIT)
    setLoadingMore(true)
    try {
      const data = await listTasks(undefined, { limit: nextLimit })
      setTasks(data)
      setPageLimit(nextLimit)
      setHasMore(data.length >= nextLimit && nextLimit < MAX_LIMIT)
      setLoadFailed(false)
    } catch (err) {
      reportError('加载更多任务失败', err)
    } finally {
      setLoadingMore(false)
    }
  }

  // 拉一次模板名（含停用，任务可能指向已停用的模板），建 id→name 映射
  useEffect(() => {
    let alive = true
    getAllTemplates()
      .then((list) => {
        if (!alive) return
        const map: Record<string, string> = {}
        for (const t of list) map[t.id] = t.name
        setTplNames(map)
      })
      .catch(() => {
        /* 失败不影响任务列表本身 */
      })
    return () => {
      alive = false
    }
  }, [])

  // 自动轮询（有 running/pending 任务时每 3 秒刷新）
  const hasActive = tasks.some((t) => t.status === 'running' || t.status === 'pending')
  useEffect(() => {
    if (!hasActive) return
    const timer = setInterval(refresh, 3000)
    return () => clearInterval(timer)
  }, [hasActive, refresh])

  /** 拉一次任务素材。失败留在 error 态，界面上给重试入口 */
  const loadAssets = useCallback(async (taskId: string) => {
    setAssetsState((prev) => ({ ...prev, [taskId]: 'loading' }))
    try {
      const assets = await getTaskAssets(taskId)
      setAssetsMap((prev) => ({ ...prev, [taskId]: assets }))
      setAssetsState((prev) => ({ ...prev, [taskId]: 'ready' }))
    } catch {
      setAssetsState((prev) => ({ ...prev, [taskId]: 'error' }))
    }
  }, [])

  const toggleExpand = (taskId: string) => {
    if (expandedId === taskId) {
      setExpandedId(null)
      return
    }
    setExpandedId(taskId)
    // 每次展开都拉取最新素材（任务重试后缓存会过期）
    loadAssets(taskId)
  }

  const handleRetry = async (taskId: string) => {
    try {
      await retryTask(taskId)
      // 旧素材已清理，丢弃缓存
      setAssetsMap((prev) => {
        const next = { ...prev }
        delete next[taskId]
        return next
      })
      refresh()
    } catch (err) {
      reportError('重试失败', err)
    }
  }

  /** 取消任务（排队中或执行中）。会一并让引擎停手，不再占显存 */
  const handleCancel = async (taskId: string) => {
    const ok = await confirmDialog({
      title: '取消这条任务？',
      message: '引擎侧会立刻中断，已跑的进度不保留。',
      tone: 'danger',
      confirmText: '取消任务',
      cancelText: '继续跑',
    })
    if (!ok) return
    try {
      await cancelTask(taskId)
      refresh()
    } catch (err) {
      reportError('取消失败', err)
    }
  }

  /** 删除任务及其素材（记录 + 磁盘文件）。不可逆 */
  const handleDelete = async (task: Task) => {
    const n = assetsMap[task.id]?.length
    const ok = await confirmDialog({
      title: '删除这条任务？',
      message: n
        ? `此操作不可撤销，会连带删除它的 ${n} 个素材，磁盘上的文件也会一并删除。`
        : '此操作不可撤销，磁盘上的文件也会一并删除。',
      tone: 'danger',
      confirmText: '删除',
    })
    if (!ok) return
    try {
      const res = await deleteTask(task.id)
      setAssetsMap((prev) => {
        const next = { ...prev }
        delete next[task.id]
        return next
      })
      setAssetsState((prev) => {
        const next = { ...prev }
        delete next[task.id]
        return next
      })
      if (expandedId === task.id) setExpandedId(null)
      refresh()
      // 记录都删掉了，但磁盘文件可能有残留 —— 如实告知，别让用户以为空间已释放
      if (res.file_errors?.length) {
        await alertDialog({
          title: `有 ${res.file_errors.length} 个磁盘文件未能清理`,
          message: `任务与素材记录已删除，不影响使用。\n\n${res.file_errors.join('\n')}`,
          tone: 'error',
        })
      }
    } catch (err) {
      reportError('删除失败', err)
    }
  }

  /** 勾选切换。running 行不可选（删不掉，勾了也是白勾） */
  const toggleSelect = (task: Task) => {
    if (!isTerminal(task.status)) return
    setSelected((prev) => {
      const next = new Set(prev)
      next.has(task.id) ? next.delete(task.id) : next.add(task.id)
      return next
    })
  }

  /** 可批量删除的任务 id（未终态的删不掉，勾了也是白勾） */
  const selectableIds = tasks.filter((t) => isTerminal(t.status)).map((t) => t.id)
  const selectedCount = selectableIds.filter((id) => selected.has(id)).length
  const allSelected = selectableIds.length > 0 && selectedCount === selectableIds.length
  const listState = taskListViewState({ failed: loadFailed, count: tasks.length })

  const toggleSelectAll = () => {
    setSelected(allSelected ? new Set() : new Set(selectableIds))
  }

  /** 批量删除已选任务。逐条调单删接口，跳过执行中的，逐条如实汇总 */
  const handleBatchDelete = async () => {
    // 确认框是异步的，弹着的时候按钮还没禁用 —— 没有这道同步守卫，
    // 连点两次会排两个确认框，用户各确认一次就把同一批删了两遍。
    if (batchDeleteBusy.current) return
    batchDeleteBusy.current = true
    try {
      const ids = selectableIds.filter((id) => selected.has(id))
      if (ids.length === 0) return
      const ok = await confirmDialog({
        title: `删除选中的 ${ids.length} 条任务？`,
        message: '此操作不可撤销，每条任务连带删除它的素材，磁盘上的文件也会一并删除。',
        tone: 'danger',
        confirmText: `删除 ${ids.length} 条`,
      })
      if (!ok) return
      setBatchDeleting(true)
      const deletedIds: string[] = []
      const failed: string[] = []
      const skipped: string[] = []
      const fileErrors: string[] = []
      for (const id of ids) {
        try {
          const res = await deleteTask(id)
          deletedIds.push(id)
          if (res.file_errors?.length) {
            fileErrors.push(...res.file_errors.map((e) => `${id.slice(0, 8)}: ${e}`))
          }
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err)
          if (msg.includes('执行中')) skipped.push(`${id.slice(0, 8)}: ${msg}`)
          else failed.push(`${id.slice(0, 8)}: ${msg}`)
        }
      }
      setAssetsMap((prev) => {
        const next = { ...prev }
        for (const id of deletedIds) delete next[id]
        return next
      })
      if (expandedId && deletedIds.includes(expandedId)) setExpandedId(null)
      setSelected(new Set())
      refresh()
      const lines: string[] = []
      if (skipped.length) lines.push(`有 ${skipped.length} 条正在执行中，已跳过：\n${skipped.join('\n')}`)
      if (failed.length) lines.push(`有 ${failed.length} 条删除失败（多为非本人提交，需管理员操作）：\n${failed.join('\n')}`)
      if (fileErrors.length) lines.push(`记录已删除，但有 ${fileErrors.length} 个磁盘文件未能清理：\n${fileErrors.join('\n')}`)
      if (lines.length) {
        await alertDialog({
          title: '部分任务未能完全删除',
          message: lines.join('\n\n'),
          tone: 'error',
        })
      }
    } finally {
      batchDeleteBusy.current = false
      setBatchDeleting(false)
    }
  }

  return (
    <section>
      <div className="page-head">
        <div>
          <h2 className="page-title">生成任务</h2>
          <p className="page-sub">
            {listState === 'empty'
              ? '还没有提交过任务'
              : `共 ${tasks.length} 条${hasActive ? ' · 有任务正在跑' : ''}`}
          </p>
        </div>
        <button onClick={refresh} className="btn">
          刷新
        </button>
      </div>

      {/* 批量操作独立成条：挤在标题行会让「刷新」随选中数量左右横跳 */}
      {selectedCount > 0 && (
        <div className="bulk-bar">
          <span className="bulk-bar-count">已选 {selectedCount} 条</span>
          <button onClick={handleBatchDelete} className="btn-danger" disabled={batchDeleting}>
            {batchDeleting ? '删除中…' : `删除 (${selectedCount})`}
          </button>
          <button onClick={() => setSelected(new Set())} className="btn-quiet">
            清除选择
          </button>
        </div>
      )}

      {listState === 'error' ? (
        <div className="flex flex-col items-start gap-xs rounded-md border border-error-line bg-error-soft px-md py-md text-error">
          <p className="font-medium">任务加载失败</p>
          <p className="text-sm">请检查网络或登录状态。</p>
          <button onClick={refresh} className="btn mt-xs">
            重试
          </button>
        </div>
      ) : listState === 'empty' ? (
        <div className="empty">
          <span className="empty-icon" aria-hidden="true">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
              <path
                d="M9 5h6M9 5a2 2 0 0 0-2 2v1M9 5a2 2 0 0 1 2-2M15 5a2 2 0 0 1 2 2v1M4 8h16v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2zM9 12h6M9 16h4"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <p className="empty-title">暂无任务</p>
          <p className="empty-hint">选择场景并提交生成，任务会出现在这里</p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-md border border-rule bg-paper shadow-xs">
          <div className="overflow-x-auto">
          {/* table-dense：台账类表格，行高比表单表格再收一档 —— 50 条一屏能少滚一段 */}
          <table className="table table-dense">
            <caption className="visually-hidden">生成任务</caption>
            <thead>
              <tr>
                <th scope="col" className="w-[3rem]">
                  <label className="check-row">
                    <input
                      type="checkbox"
                      aria-label="全选任务"
                      checked={allSelected}
                      ref={(el) => {
                        if (el) el.indeterminate = selectedCount > 0 && !allSelected
                      }}
                      onChange={toggleSelectAll}
                    />
                  </label>
                </th>
                <th scope="col" className="w-[7rem]">状态</th>
                <th scope="col">任务</th>
                {showCreator && <th scope="col" className="w-[7rem]">创建人</th>}
                <th scope="col" className="w-[10.5rem]">创建时间</th>
                <th scope="col" className="num w-[8.5rem]">进度</th>
                <th scope="col" className="w-[7rem]">操作</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => {
                const sc = statusConfig[task.status] ?? statusConfig.pending
                const expanded = expandedId === task.id
                return (
                  <Fragment key={task.id}>
                    <tr
                      onClick={() => toggleExpand(task.id)}
                      className={`cursor-pointer ${expanded ? 'bg-paper-2' : ''}`}
                    >
                      <td onClick={(e) => e.stopPropagation()}>
                        <label className="check-row">
                          <input
                            type="checkbox"
                            aria-label={`选择任务 ${task.id.slice(0, 8)}`}
                            checked={selected.has(task.id)}
                            disabled={!isTerminal(task.status)}
                            title={
                              isTerminal(task.status)
                                ? '选择后可批量删除'
                                : '任务未结束，完成后才能删除'
                            }
                            onChange={() => toggleSelect(task)}
                          />
                        </label>
                      </td>
                      <td>
                        <span className="chip" data-tone={sc.tone}>
                          {sc.label}
                        </span>
                      </td>
                      <td className="min-w-[14rem]">
                        <button
                          type="button"
                          aria-expanded={expanded}
                          onClick={(e) => {
                            e.stopPropagation()
                            toggleExpand(task.id)
                          }}
                          className="line-clamp-2 text-left font-medium text-ink transition-colors hover:text-accent"
                        >
                          {String(
                            task.params.prompt ?? task.params.description ?? '生成任务',
                          ).slice(0, 50)}
                        </button>
                        {tplNames[task.template_id] && (
                          <div className="mt-2xs truncate text-xs text-muted">
                            {tplNames[task.template_id]}
                          </div>
                        )}
                      </td>
                      {showCreator && (
                        <td className="whitespace-nowrap text-sm text-muted">
                          {task.creator_name || task.created_by || '—'}
                        </td>
                      )}
                      <td className="num whitespace-nowrap text-sm text-muted">
                        {formatDateTime(task.created_at)}
                      </td>
                      <td className="num">
                        {task.status === 'done' ? (
                          // 完成态不再画进度条：状态「已完成」+ 满格条 + 100% 是三重复述。
                          // 50 行全是满条时，真正在跑的那几条就淹没在绿色里了
                          <span className="text-sm text-muted" title="已完成">
                            —
                          </span>
                        ) : (
                          <div className="flex items-center justify-end gap-xs">
                            <span
                              aria-hidden="true"
                              className="h-1.5 w-14 overflow-hidden rounded-pill bg-paper-3"
                            >
                              <span
                                className={`block h-full rounded-pill ${
                                  task.status === 'error'
                                    ? 'bg-error'
                                    : task.status === 'cancelled'
                                      ? 'bg-muted'
                                      : 'bg-accent'
                                }`}
                                style={{ width: `${Math.max(0, Math.min(100, task.progress))}%` }}
                              />
                            </span>
                            <span className="w-9 text-right text-sm text-ink-2">
                              {task.progress}%
                            </span>
                          </div>
                        )}
                      </td>
                      <td>
                        <div className="flex items-center gap-2xs">
                          {(task.status === 'pending' || task.status === 'running') && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                handleCancel(task.id)
                              }}
                              title="取消任务，引擎侧会一并中断"
                              className="btn-quiet"
                            >
                              取消
                            </button>
                          )}
                          {(task.status === 'error' || task.status === 'cancelled') && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                handleRetry(task.id)
                              }}
                              className="btn-quiet"
                            >
                              重试
                            </button>
                          )}
                          {(task.status === 'done' ||
                            task.status === 'error' ||
                            task.status === 'cancelled') && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                handleDelete(task)
                              }}
                              title="删除任务及其素材"
                              className="btn-danger-quiet"
                            >
                              删除
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>

                    {/* 展开详情 */}
                    {expanded && (
                      <tr>
                        <td colSpan={showCreator ? 7 : 6} className="bg-paper-2">
                          {task.error_msg && <FriendlyError message={task.error_msg} />}

                          {/* 「改一改再跑」：带参回生成页表单（设计文档承诺的复制参数重新生成） */}
                          {onReuseTask && (
                            <div className="mt-sm">
                                <button
                                  type="button"
                                  className="btn"
                                  title="把这次的参数带回生成页，改一改再提交"
                                  onClick={() => onReuseTask(task)}
                                >
                                  用这些参数再生成
                                </button>
                            </div>
                          )}

                          {/* 生成结果 */}
                          {assetsMap[task.id]?.length > 0 && (
                            <div>
                                <h3 className="mt-md text-sm font-medium text-ink-2">
                                  生成结果（{assetsMap[task.id].length} 个）
                                </h3>
                              <div className="mt-xs grid grid-cols-2 gap-sm sm:grid-cols-4">
                                {assetsMap[task.id].map((asset) => (
                                  <button
                                    key={asset.id}
                                    type="button"
                                    onClick={() => {
                                      openAssetInNewTab(
                                        getAssetUrl(asset.id),
                                      ).catch((err) => {
                                        reportError('打开素材失败', err)
                                      })
                                    }}
                                    className="card card-hover block w-full text-left"
                                  >
                                    <div className="aspect-square overflow-hidden bg-paper-2">
                                      <AuthMedia
                                        src={getAssetUrl(asset.id)}
                                        posterSrc={getAssetThumbUrl(asset.id)}
                                        alt={asset.file_name}
                                        mime={asset.mime_type}
                                        className="h-full w-full object-cover"
                                      />
                                    </div>
                                    <div className="flex items-baseline justify-between gap-xs px-xs py-2xs">
                                      <span className="truncate text-xs text-muted">
                                        {asset.file_name}
                                      </span>
                                      <span className="num shrink-0 text-xs text-ink-2">
                                        {formatSize(asset.file_size)}
                                      </span>
                                    </div>
                                  </button>
                                ))}
                              </div>
                            </div>
                          )}

                          {/* 结果区的空态/失效态 —— 别让「没有」表现成「还在加载」 */}
                          {task.status === 'done' &&
                            !assetsMap[task.id]?.length &&
                            (assetsState[task.id] === 'ready' ? (
                              <p className="mt-md text-sm text-muted">
                                {task.result?.outputs?.length
                                  ? `产出文件已被删除（原有 ${task.result.outputs.length} 个）`
                                  : '该任务没有产出文件'}
                              </p>
                              ) : assetsState[task.id] === 'error' ? (
                                <p className="mt-md flex items-center gap-xs text-sm text-muted">
                                  素材加载失败
                                  <button
                                    type="button"
                                    className="btn"
                                    onClick={() => loadAssets(task.id)}
                                  >
                                    重试
                                  </button>
                                </p>
                            ) : (
                              <p className="mt-md text-sm text-muted">结果加载中…</p>
                            ))}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
          {(hasMore || pageLimit > PAGE_SIZE) && (
            <div className="border-t border-rule px-md py-sm text-center">
              {hasMore ? (
                <button
                  type="button"
                  className="btn"
                  onClick={loadMore}
                  disabled={loadingMore}
                >
                  {loadingMore ? '加载中…' : '加载更早的任务'}
                </button>
              ) : (
                <span className="text-xs text-muted">
                  已显示最近 {tasks.length} 条任务
                </span>
              )}
            </div>
          )}
          </div>
        </div>
      )}
    </section>
  )
}
