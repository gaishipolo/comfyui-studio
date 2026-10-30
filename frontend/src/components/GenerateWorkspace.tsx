/** 生成工作台 — 场景选择 + 动态表单 + 本会话任务面板（提交后的反馈就落在这里） */
import { useState, useRef, useEffect, useCallback } from 'react'
import type { GenerateDraft, Template, Task, Asset, User } from '../types'
import {
  submitTask, listTasks, getTaskAssets, getAssetUrl, openAssetInNewTab,
} from '../api/client'
import FriendlyError from './FriendlyError'
import TemplateGallery from './TemplateGallery'
import DynamicForm from './DynamicForm'
import AuthMedia from './AuthMedia'

interface Props {
  templates: Template[]
  user: User
  onTemplatesChange: () => void
  onTaskSubmitted?: () => void
  /** 「用这些参数再生成」草稿（任务页发起），进来就预填表单 */
  draft?: GenerateDraft | null
  onDraftConsumed?: () => void
  /** 会话任务上也能「再生成」（自己带自己，草稿仍走 App 统一转） */
  onReuseTask?: (task: Task) => void
}

const SESSION_KEY = 'session_task_ids'

/** 本会话提交过的任务 id（刷新页面还在，换浏览器/换会话就没有） */
function readSessionIds(): string[] {
  try {
    const raw = JSON.parse(sessionStorage.getItem(SESSION_KEY) || '[]')
    return Array.isArray(raw) ? raw.filter((x): x is string => typeof x === 'string') : []
  } catch {
    return []
  }
}

function writeSessionIds(ids: string[]) {
  // 只留最近 50 条，别让 sessionStorage 无限制长下去
  sessionStorage.setItem(SESSION_KEY, JSON.stringify(ids.slice(0, 50)))
}

const statusConfig: Record<string, { label: string; tone: string }> = {
  pending: { label: '排队中', tone: 'neutral' },
  running: { label: '生成中', tone: 'accent' },
  done: { label: '已完成', tone: 'success' },
  error: { label: '失败', tone: 'error' },
  cancelled: { label: '已取消', tone: 'neutral' },
}

export default function GenerateWorkspace({
  templates, onTaskSubmitted, draft, onDraftConsumed, onReuseTask,
}: Props) {
  const [selected, setSelected] = useState<Template | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [notice, setNotice] = useState('')
  /** 草稿预填值 + 换草稿时递增 formKey 强制 DynamicForm 重挂载（预填才生效） */
  const [initialValues, setInitialValues] = useState<Record<string, unknown> | undefined>()
  const [formKey, setFormKey] = useState(0)

  // 消费「再生成」草稿：选中场景 + 预填参数，用户改一改再提交
  useEffect(() => {
    if (!draft) return
    const tpl = templates.find((t) => t.id === draft.template_id)
    if (tpl) {
      setSelected(tpl)
      setInitialValues(draft.params)
      setFormKey((k) => k + 1)
      setNotice('已带入原任务参数，改一改再提交即可')
      // 从任务页跳过来时滚动位置还停在原处，带回页面顶部看表单
      window.scrollTo({ top: 0, behavior: 'smooth' })
    } else {
      setNotice('原任务的生成场景已不存在，无法带入参数')
    }
    onDraftConsumed?.()
  }, [draft, templates, onDraftConsumed])
  const [taskIds, setTaskIds] = useState<string[]>(() => readSessionIds())
  const [sessionTasks, setSessionTasks] = useState<Task[]>([])
  const [assetsMap, setAssetsMap] = useState<Record<string, Asset[]>>({})
  /** 素材拉取状态：无记录=还没拉、loading=拉取中、ready=拉到了（可能为空）、error=失败 */
  const [assetsState, setAssetsState] = useState<
    Record<string, 'loading' | 'ready' | 'error'>
  >({})
  const [panelOpen, setPanelOpen] = useState(() => readSessionIds().length > 0)
  const panelRef = useRef<HTMLDivElement>(null)

  const refreshSessionTasks = useCallback(async () => {
    const ids = readSessionIds()
    if (ids.length === 0) {
      setSessionTasks([])
      return
    }
    try {
      const all = await listTasks()
      const byId = new Map(all.map((t) => [t.id, t]))
      // 按会话内的提交顺序排（新的在前），已删掉的任务自然缺席
      const tasks = ids.map((id) => byId.get(id)).filter((t): t is Task => Boolean(t))
      setSessionTasks(tasks)
    } catch {
      // 静默失败，下个轮询周期再试
    }
  }, [])

  const hasActive = sessionTasks.some(
    (t) => t.status === 'running' || t.status === 'pending',
  )

  useEffect(() => {
    refreshSessionTasks()
    if (!hasActive) return
    const timer = setInterval(refreshSessionTasks, 3000)
    return () => clearInterval(timer)
  }, [hasActive, taskIds, refreshSessionTasks])

  /** 拉一次任务素材。失败留在 error 态，界面上给重试入口 */
  const loadAssets = useCallback((taskId: string) => {
    setAssetsState((prev) => ({ ...prev, [taskId]: 'loading' }))
    getTaskAssets(taskId)
      .then((assets) => {
        setAssetsMap((prev) => ({ ...prev, [taskId]: assets }))
        setAssetsState((prev) => ({ ...prev, [taskId]: 'ready' }))
      })
      .catch(() => setAssetsState((prev) => ({ ...prev, [taskId]: 'error' })))
  }, [])

  // 任务一完成就把它的结果缩略图拉回来（只拉一次，避免轮询反复打同一个接口）
  useEffect(() => {
    for (const task of sessionTasks) {
      if (task.status === 'done' && !assetsState[task.id]) {
        loadAssets(task.id)
      }
    }
  }, [sessionTasks, assetsState, loadAssets])

  const handleSubmit = async (params: Record<string, unknown>, batchCount: number) => {
    if (!selected) return
    setSubmitting(true)
    setNotice('')
    try {
      const created = await submitTask(selected.id, params, batchCount)
      const ids = created.map((t) => t.id)
      const merged = [...ids, ...readSessionIds().filter((id) => !ids.includes(id))]
      writeSessionIds(merged)
      setTaskIds(merged)
      setPanelOpen(true)
      setNotice(`已提交 ${ids.length} 个任务，进度见下方「本会话任务」`)
      onTaskSubmitted?.()
      // 提交后把视线带到面板：滚过去，最新任务就在最上面
      requestAnimationFrame(() =>
        panelRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }),
      )
    } catch (err) {
      alert(`提交失败: ${err instanceof Error ? err.message : err}`)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div>
      <div className="grid grid-cols-1 gap-xl lg:grid-cols-2">
        {/* 左侧：场景选择 */}
        <div>
          <TemplateGallery
            templates={templates}
            selected={selected}
            onSelect={setSelected}
          />
          {templates.length > 0 && !selected && (
            <p className="mt-md border border-rule bg-paper-2 px-sm py-xs text-sm text-ink-2">
              选择一个生成场景，右侧会自动出现对应的输入表单
            </p>
          )}
        </div>

        {/* 右侧：动态表单 */}
        <div>
          {selected ? (
            <div className="border border-rule bg-paper-2 p-lg">
              <h2 className="font-display text-section">{selected.name}</h2>
              <p className="mt-xs text-sm text-muted">{selected.description}</p>
              <DynamicForm
                key={`${selected.id}:${formKey}`}
                template={selected}
                onSubmit={handleSubmit}
                submitting={submitting}
                initialValues={initialValues}
              />
            </div>
          ) : (
            <div className="flex h-full items-center border border-rule bg-paper p-lg">
              <p className="text-base text-muted">请先选择生成场景</p>
            </div>
          )}
        </div>
      </div>

      {/* 本会话任务：进度与结果就在表单下方，不用跳去任务页 */}
      <section ref={panelRef} className="mt-xl border border-rule bg-paper-2">
        <div className="flex flex-wrap items-center justify-between gap-md px-md py-sm">
          <button
            type="button"
            className="btn-quiet"
            aria-expanded={panelOpen}
            onClick={() => setPanelOpen(!panelOpen)}
          >
            {panelOpen ? '▾' : '▸'} 本会话任务
            {sessionTasks.length > 0 && (
              <span className="chip" data-tone="accent">{sessionTasks.length}</span>
            )}
          </button>
          <span role="status" className="text-sm text-muted">
            {notice ||
              (hasActive
                ? '生成中，进度每 3 秒刷新'
                : sessionTasks.length > 0
                  ? '点缩略图看大图'
                  : '提交后可在这里看进度和结果')}
          </span>
        </div>

        {panelOpen && (
          <div className="rule-t px-md py-sm">
            {sessionTasks.length === 0 ? (
              <p className="py-lg text-center text-sm text-muted">
                本会话还没有任务，选个场景提交一次生成试试
              </p>
            ) : (
              <ul>
                {sessionTasks.map((task) => {
                  const assets = assetsMap[task.id]
                  const assetState = assetsState[task.id]
                  const sc = statusConfig[task.status] ?? statusConfig.pending
                  return (
                    <li key={task.id} className="rule-b py-sm last:border-b-0">
                      <div className="flex flex-wrap items-center gap-sm">
                        <span className="chip" data-tone={sc.tone}>{sc.label}</span>
                        <span className="min-w-0 flex-1 truncate text-sm text-ink">
                          {String(task.params.prompt ?? task.params.description ?? '生成任务').slice(0, 60)}
                        </span>
                        <span className="text-sm text-muted">
                          {new Date(task.created_at).toLocaleTimeString('zh-CN')}
                        </span>
                        <span className="num text-sm text-ink-2">{task.progress}%</span>
                        {/* 「改一改再跑」：带参回表单，比从头填一遍省事 */}
                        {onReuseTask && task.status !== 'pending' && task.status !== 'running' && (
                          <button
                            type="button"
                            className="btn-quiet"
                            title="把这次的参数带回表单，改一改再提交"
                            onClick={() => onReuseTask(task)}
                          >
                            再生成
                          </button>
                        )}
                      </div>

                      {/* 进度：一条细线，不用色块堆视觉重量 */}
                      <div
                        className="mt-2xs h-1 w-full bg-paper-3"
                        role="progressbar"
                        aria-label="生成进度"
                        aria-valuenow={Math.round(task.progress)}
                        aria-valuemin={0}
                        aria-valuemax={100}
                      >
                        <div
                          className="h-full bg-ink"
                          style={{ width: `${task.progress}%` }}
                        />
                      </div>

                      {task.error_msg && <FriendlyError message={task.error_msg} />}

                      {!!assets?.length && (
                        <div className="mt-xs grid grid-cols-4 gap-xs sm:grid-cols-6">
                          {assets.map((asset) => (
                            <button
                              key={asset.id}
                              type="button"
                              title="点开看大图"
                              onClick={() =>
                                openAssetInNewTab(getAssetUrl(task.id, asset.file_name)).catch(
                                  () => {},
                                )
                              }
                              className="block w-full"
                            >
                              <div className="aspect-square overflow-hidden border border-rule bg-paper">
                                <AuthMedia
                                  src={getAssetUrl(task.id, asset.file_name)}
                                  alt={asset.file_name}
                                  mime={asset.mime_type}
                                  className="h-full w-full object-cover"
                                />
                              </div>
                            </button>
                          ))}
                        </div>
                      )}

                      {/* 结果区的空态/失效态 —— 别让「没有」表现成「还在加载」 */}
                      {task.status === 'done' &&
                        (!assetState || assetState === 'loading' ? (
                          <p className="mt-xs text-sm text-muted">结果加载中…</p>
                        ) : assetState === 'error' ? (
                          <p className="mt-xs text-sm text-muted">
                            素材加载失败{' '}
                            <button
                              type="button"
                              className="btn-quiet"
                              onClick={() => loadAssets(task.id)}
                            >
                              重试
                            </button>
                          </p>
                        ) : assetState === 'ready' && !assets?.length ? (
                          <p className="mt-xs text-sm text-muted">
                            {task.result?.outputs?.length
                              ? `产出文件已被删除（原有 ${task.result.outputs.length} 个）`
                              : '该任务没有产出文件'}
                          </p>
                        ) : null)}
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
        )}
      </section>
    </div>
  )
}
