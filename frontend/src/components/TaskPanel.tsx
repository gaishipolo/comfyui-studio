/** 任务面板 — 生成队列：一张细线台账，展开后是这次生成的结果
 *
 * 用户隔离后（2026-10-29）普通用户只看得到自己提交的任务；
 * admin 看全部，多一列「创建人」便于分清谁的活。 */
import { Fragment, useState, useEffect, useCallback } from 'react'
import type { Task, Asset, User } from '../types'
import { listTasks, getTaskAssets, retryTask, cancelTask, getAssetUrl, getAssetThumbUrl, openAssetInNewTab, deleteTask } from '../api/client'
import FriendlyError from './FriendlyError'
import AuthMedia from './AuthMedia'

const statusConfig: Record<string, { label: string; tone: string }> = {
  pending: { label: '排队中', tone: 'neutral' },
  running: { label: '生成中', tone: 'accent' },
  done: { label: '已完成', tone: 'success' },
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
  const [assetsMap, setAssetsMap] = useState<Record<string, Asset[]>>({})
  /** 素材拉取状态：无记录=还没拉、loading=拉取中、ready=拉到了（可能为空）、error=失败 */
  const [assetsState, setAssetsState] = useState<
    Record<string, 'loading' | 'ready' | 'error'>
  >({})
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const refresh = useCallback(async () => {
    try {
      const data = await listTasks()
      setTasks(data)
      // 轮询期间任务可能已被别处删掉 —— 失效 id 从选中集剔除，别让「删除 (N)」虚高
      setSelected((prev) => {
        const alive = new Set(data.map((t) => t.id))
        const next = new Set([...prev].filter((id) => alive.has(id)))
        return next.size === prev.size ? prev : next
      })
    } catch {
      // 静默失败
    }
  }, [])

  // 初始加载
  useEffect(() => {
    refresh()
  }, [refresh])

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
      alert(`重试失败: ${err instanceof Error ? err.message : err}`)
    }
  }

  /** 取消任务（排队中或执行中）。会一并让引擎停手，不再占显存 */
  const handleCancel = async (taskId: string) => {
    if (!window.confirm('确定取消这条任务？\n\n引擎侧会立刻中断，已跑的进度不保留。')) return
    try {
      await cancelTask(taskId)
      refresh()
    } catch (err) {
      alert(`取消失败: ${err instanceof Error ? err.message : err}`)
    }
  }

  /** 删除任务及其素材（记录 + 磁盘文件）。不可逆 */
  const handleDelete = async (task: Task) => {
    const n = assetsMap[task.id]?.length
    const detail = n ? `，并连带删除它的 ${n} 个素材` : ''
    if (
      !confirm(
        `确定删除这条任务？\n\n此操作不可撤销${detail}，磁盘上的文件也会一并删除。`,
      )
    )
      return
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
        alert(
          `任务与素材记录已删除，但有 ${res.file_errors.length} 个磁盘文件未能清理：\n${res.file_errors.join('\n')}`,
        )
      }
    } catch (err) {
      alert(`删除失败: ${err instanceof Error ? err.message : err}`)
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

  const toggleSelectAll = () => {
    setSelected(allSelected ? new Set() : new Set(selectableIds))
  }

  /** 批量删除已选任务。逐条调单删接口，跳过执行中的，逐条如实汇总 */
  const handleBatchDelete = async () => {
    const ids = selectableIds.filter((id) => selected.has(id))
    if (ids.length === 0) return
    if (
      !confirm(
        `确定删除选中的 ${ids.length} 条任务？\n\n此操作不可撤销，每条任务连带删除它的素材，磁盘上的文件也会一并删除。`,
      )
    )
      return
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
        // 执行中被拒 ≠ 删不动：前者是设计使然，单独归类，别混进"失败"
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
    // 三种结果分开报，别混成一句"删除完成"
    const lines: string[] = []
    if (skipped.length) {
      lines.push(`有 ${skipped.length} 条正在执行中，已跳过：\n${skipped.join('\n')}`)
    }
    if (failed.length) {
      lines.push(
        `有 ${failed.length} 条删除失败（多为非本人提交，需管理员操作）：\n${failed.join('\n')}`,
      )
    }
    if (fileErrors.length) {
      lines.push(
        `记录已删除，但有 ${fileErrors.length} 个磁盘文件未能清理：\n${fileErrors.join('\n')}`,
      )
    }
    if (lines.length) alert(lines.join('\n\n'))
  }

  return (
    <section>
      <div className="flex items-center justify-between gap-md">
        <h2 className="font-display text-section">生成任务</h2>
        <div className="flex items-center gap-xs">
          {selectedCount > 0 && (
            <>
              <button onClick={handleBatchDelete} className="btn btn-danger">
                删除 ({selectedCount})
              </button>
              <button onClick={() => setSelected(new Set())} className="btn-quiet">
                清除选择
              </button>
            </>
          )}
          <button onClick={refresh} className="btn-quiet">
            刷新
          </button>
        </div>
      </div>

      {tasks.length === 0 ? (
        <p className="mt-md border-t border-rule py-xl text-base text-muted">
          暂无任务，选择场景并提交生成
        </p>
      ) : (
        <div className="mt-md overflow-x-auto">
          <table className="table">
            <caption className="visually-hidden">生成任务</caption>
            <thead>
              <tr>
                <th scope="col">
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
                <th scope="col">状态</th>
                <th scope="col">任务</th>
                {showCreator && <th scope="col">创建人</th>}
                <th scope="col">创建时间</th>
                <th scope="col" className="num">进度</th>
                <th scope="col">操作</th>
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
                      className="cursor-pointer"
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
                      <td>
                        <button
                          type="button"
                          aria-expanded={expanded}
                          onClick={(e) => {
                            e.stopPropagation()
                            toggleExpand(task.id)
                          }}
                          className="text-left"
                        >
                          {String(
                            task.params.prompt ?? task.params.description ?? '生成任务',
                          ).slice(0, 50)}
                        </button>
                      </td>
                      {showCreator && (
                        <td className="text-sm text-muted">
                          {task.creator_name || task.created_by || '—'}
                        </td>
                      )}
                      <td className="text-sm text-muted">
                        {new Date(task.created_at).toLocaleString('zh-CN')}
                      </td>
                      <td className="num">{task.progress}%</td>
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
                                className="btn-quiet"
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
                              <h3 className="mt-md text-sm text-ink-2">
                                生成结果（{assetsMap[task.id].length} 个）
                              </h3>
                              <div className="mt-xs grid grid-cols-2 gap-sm sm:grid-cols-4">
                                {assetsMap[task.id].map((asset) => (
                                  <button
                                    key={asset.id}
                                    type="button"
                                    onClick={() => {
                                      openAssetInNewTab(
                                        getAssetUrl(task.id, asset.file_name),
                                      ).catch(() => {})
                                    }}
                                    className="block w-full text-left"
                                  >
                                    <div className="aspect-square overflow-hidden border border-rule bg-paper">
                                      <AuthMedia
                                        src={getAssetUrl(task.id, asset.file_name)}
                                        posterSrc={getAssetThumbUrl(task.id, asset.file_name)}
                                        alt={asset.file_name}
                                        mime={asset.mime_type}
                                        className="h-full w-full object-cover"
                                      />
                                    </div>
                                    <div className="mt-2xs flex items-baseline justify-between gap-xs">
                                      <span className="truncate text-xs text-muted">
                                        {asset.file_name}
                                      </span>
                                      <span className="num text-xs text-ink-2">
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
                              <p className="mt-md text-sm text-muted">
                                素材加载失败{' '}
                                <button
                                  type="button"
                                  className="btn-quiet"
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
        </div>
      )}
    </section>
  )
}
