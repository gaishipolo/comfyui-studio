/** 流程编排 —— 运行台 + 编排器
 *
 * 系统的两层配置：节点（模板）是第一层，流程是第二层。两层都是数据，
 * 这也是将来 Agent 能参与编排的前提（Agent 只能写数据）。
 *
 * 这一版是「薄」的：
 * - 运行台是完整的：选流程 → 填流程输入 → 运行 → 看每一步进度
 * - 编排器刻意做薄：顺序列表 + 参数表达式，不做拖拽画布。
 *   流程的价值在「链得起来」，不在画布好不好看 —— 等真实用例跑出来，
 *   知道大家到底怎么串，再决定要不要上画布（同一份数据换个视图而已）
 *
 * 参数取值用表达式：{{ input.xxx }} 取流程输入，{{ steps.s1.outputs }} 取上游产物。
 * 不用「同名自动取」是因为两个节点可能都有同名字段，同名自动取会串味。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  AgentStatus,
  Flow,
  FlowRun,
  FlowStep,
  NodeCapability,
  Template,
  TemplateField,
} from '../types'
import {
  approveFlow,
  cancelFlowRun,
  createFlow,
  deleteFlow,
  exportFlow,
  generateFlowDraft,
  getAgentStatus,
  getFlowRun,
  importFlow,
  listFlows,
  listNodeCapabilities,
  runFlow,
  trialFlow,
  updateFlow,
  validateFlow,
} from '../api/client'
import DynamicForm from './DynamicForm'
import StepEditorDialog from './StepEditorDialog'
import { nodeVisual, paramSummary, reorderSteps, wouldBreakDependency } from './flowEditor'
import { stepTitle } from './flowSource'
import { notify, reportError } from '../notify'
import { alertDialog, confirmDialog } from './dialog'
import type { ModuleProps } from '../modules/registry'

type Tab = 'run' | 'edit'

/** 流程没有单一引擎，但 DynamicForm 只用到 template.engine 做上传 ——
 *  流程模式上传目标固定是素材库，所以留空即可 */
function flowAsTemplate(flow: Flow): Template {
  return {
    id: flow.id,
    name: flow.name,
    description: flow.description,
    category: flow.category,
    icon: flow.icon,
    input_schema: { fields: flow.input_schema?.fields ?? [] },
    engine: '',
    capability: '',
    workflow_file: '',
    param_mapping: {},
    output_config: {},
    is_active: true,
    sort_order: 0,
    created_at: flow.created_at,
    updated_at: flow.updated_at,
  }
}

const STEP_TYPE_LABEL: Record<string, string> = {
  template: '节点',
  flow: '子流程',
  repeat: '循环',
}

const RUN_STATUS_LABEL: Record<string, string> = {
  running: '运行中',
  done: '已完成',
  error: '失败',
  cancelled: '已取消',
}

const TASK_STATUS_LABEL: Record<string, string> = {
  pending: '排队中',
  running: '生成中',
  done: '已完成',
  error: '失败',
  cancelled: '已取消',
}

export default function FlowWorkspace({ user }: ModuleProps) {
  const [tab, setTab] = useState<Tab>('run')
  const [flows, setFlows] = useState<Flow[]>([])
  const [caps, setCaps] = useState<NodeCapability[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [run, setRun] = useState<FlowRun | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const pollRef = useRef<number | null>(null)

  const selected = useMemo(
    () => flows.find((f) => f.id === selectedId) ?? null,
    [flows, selectedId],
  )

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [fs, cs] = await Promise.all([listFlows(), listNodeCapabilities()])
      setFlows(fs)
      setCaps(cs)
      if (fs.length && !fs.some((f) => f.id === selectedId)) {
        setSelectedId(fs[0].id)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [selectedId])

  useEffect(() => {
    void load()
  }, [load])

  // 运行中的流程：轮询进度直到终态
  useEffect(() => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current)
      pollRef.current = null
    }
    if (!run || run.status !== 'running') return
    pollRef.current = window.setInterval(() => {
      void (async () => {
        try {
          const next = await getFlowRun(run.id)
          setRun(next)
          if (next.status !== 'running' && pollRef.current !== null) {
            window.clearInterval(pollRef.current)
            pollRef.current = null
          }
        } catch {
          /* 轮询失败不打断运行 —— 下一次再试 */
        }
      })()
    }, 1500)
    return () => {
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [run])

  const handleRun = async (params: Record<string, unknown>) => {
    if (!selected) return
    try {
      const started = await runFlow(selected.id, params)
      setRun(started)
      notify(`流程「${selected.name}」已开始`, 'success')
    } catch (err) {
      reportError('流程启动失败', err)
    }
  }

  const handleCancel = async () => {
    if (!run) return
    try {
      const next = await cancelFlowRun(run.id)
      setRun(next)
      notify('已取消这次运行', 'info')
    } catch (err) {
      reportError('取消失败', err)
    }
  }

  return (
    <div className="flex h-full flex-col gap-md">
      <div className="flex flex-wrap items-center justify-between gap-sm">
        <div>
          <h2 className="font-display text-lg font-semibold">流程</h2>
          <p className="text-xs text-muted">
            把节点串成一条链：上一步的产物自动喂给下一步，不用下载再上传
          </p>
        </div>
        <div className="flex items-center gap-xs">
          <div className="flex rounded-lg border border-tertiary p-2xs">
            {(['run', 'edit'] as Tab[]).map((t) => (
              <button
                key={t}
                type="button"
                className={`rounded px-sm py-2xs text-sm ${tab === t ? 'bg-secondary font-medium' : 'text-muted'}`}
                onClick={() => setTab(t)}
              >
                {t === 'run' ? '运行' : '编排'}
              </button>
            ))}
          </div>
          <button type="button" className="btn btn-quiet" onClick={() => void load()}>
            刷新
          </button>
        </div>
      </div>

      {error && <p className="text-sm text-error">{error}</p>}

      {tab === 'run' ? (
        <RunView
          flows={flows}
          caps={caps}
          selected={selected}
          run={run}
          loading={loading}
          onSelect={setSelectedId}
          onRun={handleRun}
          onCancel={handleCancel}
        />
      ) : (
        <EditView
          flows={flows}
          caps={caps}
          selected={selected}
          user={user}
          onSelect={setSelectedId}
          onChanged={load}
          onRunStarted={(r) => {
            // 试跑开始 → 切到运行台盯着它跑
            setRun(r)
            setTab('run')
          }}
        />
      )}
    </div>
  )
}

// ────────────────────────────── 运行台 ──────────────────────────────

function RunView({
  flows,
  caps,
  selected,
  run,
  loading,
  onSelect,
  onRun,
  onCancel,
}: {
  flows: Flow[]
  caps: NodeCapability[]
  selected: Flow | null
  run: FlowRun | null
  loading: boolean
  onSelect: (id: string) => void
  onRun: (params: Record<string, unknown>) => void
  onCancel: () => void
}) {
  const nodeName = (templateId: string) =>
    caps.find((c) => c.id === templateId)?.name ?? '未知节点'
  // 进度只在这里算一次。后端已经 clamp 过，前端再兜一层：
  // 一条脏数据不该让进度条冲出轨道、也不该显示成 "150%"。
  const pct = Math.max(0, Math.min(100, run?.progress ?? 0))
  // 运行台只列**能跑的**流程。
  // 草稿还没审批上线，运营点了也跑不了 —— 列出来只会让人以为坏了。
  // 草稿在「编排」页看。
  const runnable = flows.filter((f) => f.status === 'active')
  if (loading && !runnable.length) return <p className="text-sm text-muted">加载中…</p>
  if (!runnable.length) {
    return (
      <div className="empty">
        <p className="empty-title">还没有可运行的流程</p>
        <p className="empty-hint">切到「编排」页新建一条，把节点串起来</p>
      </div>
    )
  }
  return (
    <div className="flex min-h-0 flex-1 gap-md">
      <aside className="w-64 shrink-0 overflow-y-auto">
        <ul className="flex flex-col gap-2xs">
          {runnable.map((f) => (
            <li key={f.id}>
              <button
                type="button"
                className={`w-full rounded-lg border p-sm text-left ${
                  selected?.id === f.id
                    ? 'border-accent bg-secondary'
                    : 'border-tertiary hover:bg-secondary'
                }`}
                onClick={() => onSelect(f.id)}
              >
                <div className="font-medium">{f.name}</div>
                <div className="text-xs text-muted">{f.steps.length} 步</div>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section className="min-w-0 flex-1 overflow-y-auto">
        {selected ? (
          <>
            <div className="panel mb-md">
              <h3 className="font-display text-base font-medium">{selected.name}</h3>
              {selected.description && (
                <p className="mt-2xs text-sm text-muted">{selected.description}</p>
              )}
              <ol className="mt-sm flex flex-col gap-2xs">
                {selected.steps.map((s, i) => (
                  <li key={s.id} className="flex items-center gap-sm text-sm">
                    <span className="chip">{i + 1}</span>
                    <span>{nodeName(s.template_id)}</span>
                    {s.title && <span className="text-muted">{s.title}</span>}
                    <span className="text-xs text-muted">
                      {s.type === 'repeat'
                        ? `循环 ${s.count || 'N'} 次`
                        : s.type === 'flow'
                          ? `子流程 · ${flows.find((f) => f.id === s.flow_id)?.name ?? '未选'}`
                          : STEP_TYPE_LABEL[s.type] ?? s.type}
                    </span>
                  </li>
                ))}
              </ol>
            </div>

            <DynamicForm
              key={selected.id}
              template={flowAsTemplate(selected)}
              uploadTarget="asset"
              showBatch={false}
              submitLabel="开始运行"
              submitting={false}
              onSubmit={(params) => onRun(params)}
            />

            {run && (
              <div className="panel mt-md">
                <div className="flex items-center justify-between gap-sm">
                  <h4 className="font-display text-base font-medium">
                    运行 {run.flow_name}
                  </h4>
                  <div className="flex items-center gap-xs">
                    <span
                      role="progressbar"
                      data-run-progress
                      aria-label="流程进度"
                      aria-valuenow={Math.round(pct)}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      className="h-1.5 w-16 overflow-hidden rounded-pill bg-paper-3"
                    >
                      <span
                        className={`block h-full rounded-pill ${
                          run.status === 'error'
                            ? 'bg-error'
                            : run.status === 'cancelled'
                              ? 'bg-muted'
                              : 'bg-accent'
                        }`}
                        style={{ width: `${pct}%` }}
                      />
                    </span>
                    <span data-run-percent className="w-9 text-right text-sm text-ink-2">
                      {Math.round(pct)}%
                    </span>
                    <span data-run-status className="chip">
                      {RUN_STATUS_LABEL[run.status] ?? run.status}
                    </span>
                    {run.status === 'running' && (
                      <button
                        type="button"
                        className="btn btn-quiet"
                        data-run-cancel
                        onClick={onCancel}
                      >
                        取消
                      </button>
                    )}
                  </div>
                </div>
                {run.error_msg && (
                  <p className="mt-2xs text-sm text-error">{run.error_msg}</p>
                )}
                <ol data-run-steps className="mt-sm flex flex-col gap-2xs">
                  {run.steps.map((s, i) => {
                    // 循环步骤一次运行会有 N 行（同一 step_id）——
                    // key 只用 step_id 会重复，React 会复用错行。
                    // 嵌套子流程的 task_id 为空，也不能拿来当 key。
                    const stepDef = selected.steps.find((st) => st.id === s.step_id)
                    return (
                      <li
                        key={`${s.step_id}-${s.iteration ?? 0}-${i}`}
                        className="flex items-center gap-sm text-sm"
                      >
                        <span className="chip">{i + 1}</span>
                        {typeof s.layer === 'number' && (
                          <span className="text-xs text-muted">第 {s.layer + 1} 层</span>
                        )}
                        <span>
                          {stepDef?.title ||
                            (stepDef?.type === 'flow'
                              ? (flows.find((f) => f.id === stepDef.flow_id)?.name ??
                                '子流程')
                              : nodeName(stepDef?.template_id ?? ''))}
                        </span>
                        {stepDef?.type === 'repeat' && typeof s.iteration === 'number' && (
                          <span className="text-xs text-muted">第 {s.iteration + 1} 次</span>
                        )}
                        <span
                          className={
                            s.status === 'done'
                              ? 'text-muted'
                              : s.status === 'error'
                                ? 'text-error'
                                : 'text-ink-2'
                          }
                        >
                          {TASK_STATUS_LABEL[s.status] ?? s.status}
                        </span>
                        {s.outputs && s.outputs.length > 0 && (
                          <span className="text-xs text-muted">
                            {s.outputs.length} 个产物
                          </span>
                        )}
                      </li>
                    )
                  })}
                  {run.status === 'running' && run.steps.length === 0 && (
                    <li className="text-sm text-muted">正在执行第一步…</li>
                  )}
                </ol>

                {run.status === 'done' && run.outputs?.length > 0 && (
                  <>
                    <h5 className="mt-md font-display text-sm font-medium">流程产出</h5>
                    <div className="mt-2xs flex flex-col gap-2xs">
                      {run.outputs.map((o) => (
                        <div key={o.name} className="flex items-center gap-sm text-sm">
                          <span className="chip">{o.name}</span>
                          <span className="text-xs text-muted">
                            {o.outputs.length} 个产物
                          </span>
                        </div>
                      ))}
                    </div>
                  </>
                )}
              </div>
            )}
          </>
        ) : (
          <p className="text-sm text-muted">选一个流程</p>
        )}
      </section>
    </div>
  )
}

// ────────────────────────────── 编排器 ──────────────────────────────

function EditView({
  flows,
  caps,
  selected,
  user,
  onSelect,
  onChanged,
  onRunStarted,
}: {
  flows: Flow[]
  caps: NodeCapability[]
  selected: Flow | null
  user: ModuleProps['user']
  onSelect: (id: string) => void
  onChanged: () => Promise<void>
  /** 试跑开始时通知外层 —— 让运行台切到这次运行 */
  onRunStarted?: (run: FlowRun) => void
}) {
  const [draft, setDraft] = useState<Flow | null>(selected)
  const [busy, setBusy] = useState(false)
  const [issues, setIssues] = useState<{ errors: string[]; warnings: string[] } | null>(null)
  const [layers, setLayers] = useState<string[][]>([])
  const [editing, setEditing] = useState<FlowStep | null>(null)
  // 默认展开：流程输入是内容同事编排时最需要看的东西（运行时运营要填什么）
  const [showInputs, setShowInputs] = useState(true)
  // 拖动排序：dragIdx 是被拖的那项，dropIdx 是「插到第几项之前」
  const [dragIdx, setDragIdx] = useState<number | null>(null)
  const [dropIdx, setDropIdx] = useState<number | null>(null)
  // 审批意见 —— 草稿批/驳时填
  const [reviewNote, setReviewNote] = useState('')
  // ── 一句话生成 ──
  // 模型只写配置：生成完落到 draft，人看效果再审批。
  // 这些状态都放在编排器里（而不是弹窗组件），因为生成后要选中新草稿。
  const [agentOpen, setAgentOpen] = useState(false)
  const [agentStatus, setAgentStatus] = useState<AgentStatus | null>(null)
  const [requirement, setRequirement] = useState('')
  const [agentBusy, setAgentBusy] = useState(false)
  const [agentErrors, setAgentErrors] = useState<string[]>([])

  useEffect(() => {
    setDraft(selected)
    setIssues(null)
    setLayers([])
    setEditing(null)
  }, [selected])

  const capOf = useCallback(
    (id: string) => caps.find((c) => c.id === id) ?? null,
    [caps],
  )
  const patch = (next: Flow) => setDraft(next)

  // ── 导出 / 导入 ──────────────────────────
  /** 导出流程为 JSON 文件（带上引用的节点与子流程快照） */
  const handleExport = async (flow: Flow) => {
    try {
      await exportFlow(flow.id, flow.name)
    } catch (err) {
      reportError('导出失败', err)
    }
  }

  /** 导入流程导出文件。只在真的重名时才问覆盖 —— 没冲突就不该拦一道 */
  const handleImport = async (file: File) => {
    let payload: unknown
    try {
      payload = JSON.parse(await file.text())
    } catch {
      notify('这个文件不是合法的 JSON', 'error')
      return
    }
    const fmt = (payload as { format?: string })?.format
    if (fmt !== 'comfyui-workbench/flow') {
      notify(`不是本系统导出的流程文件${fmt ? `（format=${fmt}）` : ''}`, 'error')
      return
    }
    const name =
      (payload as { flow?: { name?: string } })?.flow?.name || file.name.replace(/\.json$/i, '')
    const exists = flows.some((f) => f.name === name)
    let overwrite = false
    if (exists) {
      overwrite = await confirmDialog({
        title: `已存在同名流程「${name}」`,
        message: '覆盖会保留原 id，历史运行记录不受影响。',
        confirmText: '覆盖',
        cancelText: '取消',
      })
      if (!overwrite) return
    }
    try {
      const res = await importFlow(payload, overwrite)
      await onChanged()
      onSelect(res.id)
      if (res.warnings.length) {
        await alertDialog({
          title: `已导入「${res.name}」`,
          message: res.warnings.map((w) => `· ${w}`).join('\n'),
          tone: 'warning',
        })
      } else {
        notify(`已导入「${res.name}」`, 'success')
      }
    } catch (err) {
      reportError('导入失败', err)
    }
  }

  // 流程输入字段 —— 参数摘要里要说「流程输入 · 初始画面」而不是字段名
  const inputFields = draft?.input_schema?.fields ?? []

  // 步骤显示名 —— 引用别的步骤时要说「生成参考图」而不是裸 id（gen）
  const stepTitleMap = useMemo(() => {
    const map: Record<string, string> = {}
    draft?.steps.forEach((s, i) => {
      map[s.id] = stepTitle(s, i, capOf(s.template_id)?.name)
    })
    return map
  }, [draft?.steps, capOf])

  const addStep = () => {
    if (!draft) return
    // id 是内部标识，不能和已有的重复 —— 删过步骤再加会让 s{n} 撞号
    let n = draft.steps.length + 1
    const taken = new Set(draft.steps.map((s) => s.id))
    while (taken.has(`s${n}`)) n += 1
    patch({
      ...draft,
      steps: [
        ...draft.steps,
        {
          id: `s${n}`,
          title: '',
          type: 'template',
          template_id: '',
          flow_id: '',
          count: 1,
          params: {},
        },
      ],
    })
  }

  const removeStep = (id: string) => {
    if (!draft) return
    // 删掉被引用的步骤会断链 —— 说清是谁在用它，别让人以为删干净了
    const users = draft.steps.filter((s) =>
      Object.values(s.params ?? {}).some((v) =>
        typeof v === 'string' && v.includes(`steps.${id}.outputs`),
      ),
    )
    if (users.length > 0) {
      const idx = draft.steps.findIndex((s) => s.id === id)
      const names = users
        .map((u) =>
          stepTitle(u, draft.steps.findIndex((s) => s.id === u.id), capOf(u.template_id)?.name),
        )
        .join('、')
      notify(
        `「${stepTitle(draft.steps[idx], idx, capOf(draft.steps[idx]?.template_id)?.name)}」的产物正被${names}使用，删了那些步骤就拿不到产物了`,
        'warning',
      )
    }
    patch({ ...draft, steps: draft.steps.filter((s) => s.id !== id) })
  }

  // ── 拖动排序 ──
  const handleDragOver = (e: React.DragEvent, index: number) => {
    e.preventDefault()
    const rect = e.currentTarget.getBoundingClientRect()
    // 落在上半还是下半：决定插到这一项之前还是之后
    setDropIdx(e.clientY > rect.top + rect.height / 2 ? index + 1 : index)
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    if (!draft || dragIdx === null || dropIdx === null) return
    const toIdx = dropIdx > dragIdx ? dropIdx - 1 : dropIdx
    if (!wouldBreakDependency(draft.steps, dragIdx, toIdx)) {
      patch({ ...draft, steps: reorderSteps(draft.steps, dragIdx, toIdx) })
    } else {
      notify('这个位置会打断依赖：被引用的上游必须排在前面', 'warning')
    }
    setDragIdx(null)
    setDropIdx(null)
  }

  const endDrag = () => {
    setDragIdx(null)
    setDropIdx(null)
  }

  const doValidate = async () => {
    if (!draft) return
    setBusy(true)
    try {
      const result = await validateFlow({
        steps: draft.steps,
        input_schema: draft.input_schema,
        outputs: draft.outputs,
      })
      setIssues({ errors: result.errors, warnings: result.warnings })
      setLayers(result.layers)
    } catch (err) {
      reportError('校验失败', err)
    } finally {
      setBusy(false)
    }
  }

  const doSave = async () => {
    if (!draft) return
    setBusy(true)
    try {
      const result = await validateFlow({
        steps: draft.steps,
        input_schema: draft.input_schema,
        outputs: draft.outputs,
      })
      setIssues({ errors: result.errors, warnings: result.warnings })
      setLayers(result.layers)
      if (!result.ok) {
        notify('流程还有问题，先改完再保存', 'warning')
        return
      }
      // 只提交可编辑字段：status / id / version / 审批留痕一律不从这里走。
      // 草稿上线只能走审批接口 —— 顺手带个 status 就等于绕开审批。
      const payload = {
        name: draft.name,
        description: draft.description,
        category: draft.category,
        icon: draft.icon,
        input_schema: draft.input_schema,
        outputs: draft.outputs,
        steps: draft.steps,
      }
      if (draft.id) {
        await updateFlow(draft.id, payload)
      } else {
        await createFlow(payload)
      }
      notify('流程已保存', 'success')
      await onChanged()
    } catch (err) {
      reportError('保存失败', err)
    } finally {
      setBusy(false)
    }
  }

  const createNew = async () => {
    setBusy(true)
    try {
      const created = await createFlow({
        name: '新流程',
        description: '',
        input_schema: { fields: [] },
        outputs: [],
        steps: [],
        status: 'active',
      })
      await onChanged()
      onSelect(created.id)
    } catch (err) {
      reportError('新建失败', err)
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    if (!draft?.id) return
    try {
      await deleteFlow(draft.id)
      notify('流程已删除', 'success')
      setDraft(null)
      await onChanged()
    } catch (err) {
      reportError('删除失败', err)
    }
  }

  /**
   * 审批草稿 —— 批准上线或驳回。
   *
   * 驳回不是删掉，是归档（status=archived）：留个档，
   * 「当时为什么没批」也记下来了。
   */
  const doApprove = async (approve: boolean) => {
    if (!draft?.id) return
    setBusy(true)
    try {
      const result = await approveFlow(draft.id, approve, reviewNote)
      if (!result.ok) {
        notify(result.message || '审批失败', 'warning')
        return
      }
      notify(approve ? '已批准上线' : '已驳回', 'success')
      setReviewNote('')
      await onChanged()
    } catch (err) {
      reportError('审批失败', err)
    } finally {
      setBusy(false)
    }
  }

  /**
   * 试跑 —— 真的出图，但产物不进正式素材库。
   *
   * 审批前得先看看效果对不对。试跑的图默认从素材库排除
   * （素材库里有「试跑产物」筛选能翻出来）。
   */
  const doTrial = async () => {
    if (!draft?.id) return
    setBusy(true)
    try {
      const run = await trialFlow(draft.id, {})
      notify('试跑已开始，产物不会进正式素材库', 'success')
      onRunStarted?.(run)
    } catch (err) {
      reportError('试跑失败', err)
    } finally {
      setBusy(false)
    }
  }

  /**
   * 打开「一句话生成」面板，顺便问一次通道状态。
   *
   * 每次打开都问：部署方可能刚配好模型，不该让人刷新整页才生效。
   * 状态拿不到也不拦人 —— 真点了生成，后端会给明确的失败原因。
   */
  const openAgent = async () => {
    setAgentOpen(true)
    setAgentErrors([])
    try {
      setAgentStatus(await getAgentStatus())
    } catch {
      setAgentStatus(null)
    }
  }

  /**
   * 一句话 → 草稿。
   *
   * 生成成功不代表上线：新草稿会被选中，接着该试跑、看效果、审批。
   * 生成失败不落库，错误直接列在面板里 —— 让人改的是需求，不是去猜哪里错了。
   */
  const doAgentGenerate = async () => {
    const text = requirement.trim()
    if (text.length < 2) {
      notify('把想做的流程说得具体一点，比如「先生成参考图，再换成边牧」', 'warning')
      return
    }
    setAgentBusy(true)
    setAgentErrors([])
    try {
      const result = await generateFlowDraft({ requirement: text })
      if (!result.ok) {
        setAgentErrors(result.errors.length ? result.errors : [result.message || '生成失败'])
        if (result.message) notify(result.message, 'warning')
        return
      }
      await onChanged()
      onSelect(result.flow_id)
      setRequirement('')
      setAgentOpen(false)
      notify(
        result.warnings.length
          ? `已生成草稿「${result.name}」，另有 ${result.warnings.length} 条校验提醒`
          : result.message || `已生成草稿「${result.name}」`,
        'success',
      )
    } catch (err) {
      reportError('生成失败', err)
    } finally {
      setAgentBusy(false)
    }
  }

  return (
    <div className="flex min-h-0 flex-1 gap-md">
      <aside className="w-56 shrink-0 overflow-y-auto">
        <button type="button" className="btn btn-quiet mb-sm w-full" onClick={() => void openAgent()}>
          一句话生成（Agent）
        </button>
        <button type="button" className="btn btn-quiet mb-sm w-full" onClick={() => void createNew()}>
          新建流程
        </button>
        {/* 导入只能 admin（会往系统里加流程，可能连带加节点） */}
        {user.role === 'admin' && (
          <label className="btn btn-quiet mb-sm w-full cursor-pointer text-center">
            导入流程
            <input
              type="file"
              accept=".json,application/json"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0]
                e.target.value = '' // 允许重复导入同一个文件
                if (file) void handleImport(file)
              }}
            />
          </label>
        )}
        <ul className="flex flex-col gap-2xs">
          {flows.map((f) => (
            <li key={f.id}>
              <button
                type="button"
                className={`w-full rounded-lg border p-sm text-left ${
                  draft?.id === f.id
                    ? 'border-accent bg-secondary'
                    : 'border-tertiary hover:bg-secondary'
                }`}
                onClick={() => onSelect(f.id)}
              >
                <div className="font-medium">{f.name}</div>
                <div className="text-xs text-muted">
                  {f.steps.length} 步 ·{' '}
                  {f.status === 'active'
                    ? '已启用'
                    : f.status === 'draft'
                      ? '待审批'
                      : '已归档'}
                </div>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col overflow-y-auto">
        {/* ── 一句话生成：模型写配置，人负责判断它有没有用 ── */}
        {agentOpen && (
          <div className="panel mb-md border-accent">
            <div className="flex flex-wrap items-start justify-between gap-sm">
              <div>
                <h4 className="font-display text-base font-medium">用一句话生成流程</h4>
                <p className="mt-2xs text-xs text-muted">
                  模型只负责写配置。生成后是草稿：先试跑看效果，再由人审批上线。
                </p>
              </div>
              <button type="button" className="btn btn-quiet" onClick={() => setAgentOpen(false)}>
                收起
              </button>
            </div>

            {agentStatus && !agentStatus.configured ? (
              <p className="mt-sm rounded-md border border-warning-line bg-warning-soft px-sm py-xs text-sm text-warning">
                后端还没配置编排模型（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL），暂时用不了。
                可以先手工编排，配置好之后这里会自动可用。
              </p>
            ) : (
              <>
                <label className="field-label mt-sm block">
                  想做什么
                  <textarea
                    className="field mt-2xs"
                    rows={3}
                    value={requirement}
                    disabled={agentBusy}
                    placeholder="例：先用 Flux2 生成一张拉布拉多拉拖拉机的图，再用图生图把拉布拉多换成边牧，最后放大 2 倍"
                    onChange={(e) => setRequirement(e.target.value)}
                  />
                </label>
                <div className="mt-sm flex flex-wrap items-center gap-sm">
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={agentBusy}
                    onClick={() => void doAgentGenerate()}
                  >
                    {agentBusy ? '正在生成…' : '生成草稿'}
                  </button>
                  <span className="text-xs text-muted">
                    {agentBusy
                      ? '模型在想，通常 10~40 秒'
                      : agentStatus?.model
                        ? `模型：${agentStatus.model}`
                        : '生成后可直接试跑，产物不进正式素材库'}
                  </span>
                </div>
              </>
            )}

            {agentErrors.length > 0 && (
              <ul className="mt-sm flex flex-col gap-2xs text-sm text-error">
                {agentErrors.map((msg, i) => (
                  <li key={`${i}-${msg}`}>· {msg}</li>
                ))}
              </ul>
            )}
          </div>
        )}

        {!draft ? (
          <div className="empty">
            <p className="empty-title">选一个流程，或者新建一条</p>
            <p className="empty-hint">
              把节点串起来：拖动调整顺序，双击节点设置它的输入
            </p>
          </div>
        ) : (
          <>
            <div className="panel mb-md">
              <div className="flex flex-wrap items-end gap-sm">
                <label className="field-label w-56">
                  流程名称
                  <input
                    className="field"
                    value={draft.name}
                    onChange={(e) => patch({ ...draft, name: e.target.value })}
                  />
                </label>
                <label className="field-label flex-1">
                  说明
                  <input
                    className="field"
                    value={draft.description}
                    onChange={(e) => patch({ ...draft, description: e.target.value })}
                  />
                </label>
              </div>
            </div>

            {/* ── 草稿审批区：Agent 生成的流程落成 draft，人批了才上线 ── */}
            {draft.status === 'draft' && (
              <div className="panel mb-md border-accent">
                <div className="flex flex-wrap items-center gap-sm">
                  <span className="chip" data-tone="accent">
                    待审批
                  </span>
                  <span className="text-sm text-ink-2">
                    这条流程还没上线，运营在运行台跑不到它。
                  </span>
                </div>
                {draft.agent_note && (
                  <p className="mt-2xs text-sm text-muted">
                    <span className="text-ink-2">Agent 说明：</span>
                    {draft.agent_note}
                  </p>
                )}
                <div className="mt-sm flex flex-wrap items-end gap-sm">
                  <label className="field-label flex-1">
                    审批意见
                    <input
                      className="field"
                      value={reviewNote}
                      placeholder="例如：第 2 步该用图生图，不是文生图"
                      onChange={(e) => setReviewNote(e.target.value)}
                    />
                  </label>
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={busy}
                    onClick={() => void doApprove(true)}
                  >
                    批准上线
                  </button>
                  <button
                    type="button"
                    className="btn btn-danger-quiet"
                    disabled={busy}
                    onClick={() => void doApprove(false)}
                  >
                    驳回
                  </button>
                </div>
              </div>
            )}
            {draft.status !== 'draft' && draft.review_note && (
              <p className="mb-md text-xs text-muted">
                <span className="text-ink-2">审批意见：</span>
                {draft.review_note}
              </p>
            )}

            {/* ── 节点列表：可拖动排序，双击开设置 ── */}
            <div className="mb-md flex items-center justify-between gap-sm">
              <h4 className="font-display text-base font-medium">
                节点
                <span className="ml-2xs text-xs text-muted">
                  拖动排序 · 双击设置输入
                </span>
              </h4>
              <button type="button" className="btn btn-quiet" onClick={addStep}>
                添加节点
              </button>
            </div>

            {draft.steps.length === 0 ? (
              <div className="empty">
                <p className="empty-title">还没有节点</p>
                <p className="empty-hint">点「添加节点」，然后双击它选一个节点</p>
              </div>
            ) : (
              <ol className="flex flex-col">
                {draft.steps.map((step, index) => {
                  const cap = capOf(step.template_id)
                  const visual = nodeVisual(cap)
                  const layer = layers.findIndex((l) => l.includes(step.id))
                  return (
                    <div key={step.id}>
                      {dropIdx === index && <div className="my-2xs h-0.5 rounded bg-accent" />}
                      <li
                        draggable
                        onDragStart={() => setDragIdx(index)}
                        onDragOver={(e) => handleDragOver(e, index)}
                        onDrop={handleDrop}
                        onDragEnd={endDrag}
                        onDoubleClick={() => setEditing(step)}
                        className={`group flex cursor-pointer items-center gap-sm rounded-lg border px-sm py-sm ${
                          dragIdx === index
                            ? 'border-accent bg-secondary opacity-60'
                            : 'border-tertiary hover:border-rule-strong hover:bg-secondary'
                        }`}
                      >
                        <span
                          aria-hidden="true"
                          className="cursor-grab text-muted active:cursor-grabbing"
                          title="拖动排序"
                        >
                          ⠿
                        </span>
                        <span className="chip">{index + 1}</span>
                        <span aria-hidden="true" className="text-ink-2">
                          {visual.icon}
                        </span>
                        {step.type === 'repeat' && (
                          <span
                            className="chip shrink-0"
                            data-tone="accent"
                            title={`跑 ${step.count || '列表长度'} 次`}
                          >
                            循环 {step.count || 'N'} 次
                          </span>
                        )}
                        {step.type === 'flow' && (
                          <span className="chip shrink-0" data-tone="accent">
                            子流程
                          </span>
                        )}
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm font-medium">
                            {step.title?.trim() || cap?.name || '（未选节点）'}
                          </span>
                          <span className="block truncate text-xs text-muted">
                            {cap?.name && step.title?.trim()
                              ? `${cap.name} · ${paramSummary(step, cap, inputFields, stepTitleMap)}`
                              : paramSummary(step, cap, inputFields, stepTitleMap)}
                          </span>
                        </span>
                        {layer >= 0 && (
                          <span className="text-xs text-muted shrink-0">第 {layer + 1} 层</span>
                        )}
                        <button
                          type="button"
                          className="btn btn-danger-quiet shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
                          onClick={(e) => {
                            e.stopPropagation()
                            removeStep(step.id)
                          }}
                          aria-label={`删除步骤 ${step.title?.trim() || step.id}`}
                        >
                          删除
                        </button>
                      </li>
                    </div>
                  )
                })}
                {dropIdx === draft.steps.length && (
                  <div className="my-2xs h-0.5 rounded bg-accent" />
                )}
              </ol>
            )}

            {/* ── 流程输入（流程级，收起来） ── */}
            <div className="panel mt-md mb-md">
              <button
                type="button"
                className="flex w-full items-center justify-between gap-sm"
                onClick={() => setShowInputs(!showInputs)}
                aria-expanded={showInputs}
              >
                <span className="font-display text-base font-medium">
                  流程输入
                  <span className="ml-2xs text-xs text-muted">
                    {(draft.input_schema?.fields ?? []).length} 个字段
                  </span>
                </span>
                <span className="text-xs text-muted">{showInputs ? '▾ 收起' : '▸ 展开'}</span>
              </button>
              {showInputs && (
                <p className="mt-2xs text-xs text-muted">
                  运行时要用户填的东西。在节点参数里选「流程输入 · 字段名」就能引用。
                  勾「多条」就是列表 —— 循环节点靠它把「20 条提示词」喂给每一轮。
                </p>
              )}
              {showInputs && (
                <InputFields
                  fields={draft.input_schema?.fields ?? []}
                  onChange={(fields) =>
                    patch({ ...draft, input_schema: { ...draft.input_schema, fields } })
                  }
                />
              )}
            </div>

            {/* ── 流程产出：跑完交付什么 ── */}
            <div className="panel mt-md mb-md">
              <div className="flex items-center justify-between gap-sm">
                <h4 className="font-display text-base font-medium">
                  流程产出
                  <span className="ml-2xs text-xs text-muted">
                    {draft.outputs.length} 项
                  </span>
                </h4>
                <button
                  type="button"
                  className="btn btn-quiet"
                  onClick={() =>
                    patch({
                      ...draft,
                      outputs: [
                        ...draft.outputs,
                        {
                          name: '',
                          from_step: draft.steps[draft.steps.length - 1]?.id ?? '',
                          pick: 'all',
                        },
                      ],
                    })
                  }
                >
                  添加产出
                </button>
              </div>
              <p className="mt-2xs text-xs text-muted">
                跑完之后交付什么。留空则默认交付最后一步的产物。
              </p>
              {draft.outputs.length === 0 ? (
                <p className="mt-sm text-sm text-muted">还没声明产出</p>
              ) : (
                <div className="mt-sm flex flex-col gap-2xs">
                  {draft.outputs.map((out, i) => (
                    <div key={i} className="flex flex-wrap items-end gap-xs">
                      <label className="field-label flex-1">
                        名称
                        <input
                          className="field"
                          value={out.name}
                          placeholder="例如：成片"
                          onChange={(e) =>
                            patch({
                              ...draft,
                              outputs: draft.outputs.map((o, j) =>
                                j === i ? { ...o, name: e.target.value } : o,
                              ),
                            })
                          }
                        />
                      </label>
                      <label className="field-label flex-1">
                        来自
                        <select
                          className="field"
                          value={out.from_step}
                          onChange={(e) =>
                            patch({
                              ...draft,
                              outputs: draft.outputs.map((o, j) =>
                                j === i ? { ...o, from_step: e.target.value } : o,
                              ),
                            })
                          }
                        >
                          {draft.steps.map((s, si) => (
                            <option key={s.id} value={s.id}>
                              {stepTitle(s, si)}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="field-label w-32">
                        取哪部分
                        <select
                          className="field"
                          value={out.pick}
                          onChange={(e) =>
                            patch({
                              ...draft,
                              outputs: draft.outputs.map((o, j) =>
                                j === i ? { ...o, pick: e.target.value } : o,
                              ),
                            })
                          }
                        >
                          <option value="all">全部</option>
                          <option value="first">第 1 张</option>
                          <option value="last">最后一张</option>
                        </select>
                      </label>
                      <button
                        type="button"
                        className="btn btn-danger-quiet"
                        onClick={() =>
                          patch({
                            ...draft,
                            outputs: draft.outputs.filter((_, j) => j !== i),
                          })
                        }
                      >
                        删除
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {layers.length > 0 && (
              <div className="panel mb-md">
                <h4 className="font-display text-base font-medium">并行分组</h4>
                <p className="mt-2xs text-xs text-muted">
                  同一组里的节点互不依赖，会同时开跑；后面的组等前面跑完
                </p>
                <ol className="mt-sm flex flex-col gap-2xs">
                  {layers.map((layer, i) => (
                    <li key={i} className="flex items-center gap-sm text-sm">
                      <span className="chip">第 {i + 1} 层</span>
                      {layer.map((sid) => (
                        <code key={sid} className="text-xs text-muted">
                          {sid}
                        </code>
                      ))}
                      {layer.length > 1 && (
                        <span className="text-xs text-accent">{layer.length} 步并行</span>
                      )}
                    </li>
                  ))}
                </ol>
              </div>
            )}

            {issues && (issues.errors.length > 0 || issues.warnings.length > 0) && (
              <div className="panel mb-md">
                {issues.errors.length > 0 && (
                  <ul className="flex flex-col gap-2xs">
                    {issues.errors.map((e, i) => (
                      <li key={i} className="text-sm text-error">
                        {e}
                      </li>
                    ))}
                  </ul>
                )}
                {issues.warnings.length > 0 && (
                  <ul className="mt-2xs flex flex-col gap-2xs">
                    {issues.warnings.map((w, i) => (
                      <li key={i} className="text-sm text-warning">
                        {w}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}

            <div className="mt-auto flex items-center gap-sm">
              <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void doSave()}>
                保存
              </button>
              <button type="button" className="btn btn-quiet" disabled={busy} onClick={() => void doValidate()}>
                只校验
              </button>
              {draft.id && draft.status !== 'archived' && (
                <button
                  type="button"
                  className="btn btn-quiet"
                  disabled={busy}
                  onClick={() => void doTrial()}
                  title="真的跑一遍，出图但产物不进正式素材库"
                >
                  试跑
                </button>
              )}
              {draft.id && (
                <button
                  type="button"
                  className="btn btn-quiet"
                  onClick={() => void handleExport(draft)}
                >
                  导出
                </button>
              )}
              {draft.id && (
                <button type="button" className="btn btn-danger-quiet" onClick={() => void remove()}>
                  删除流程
                </button>
              )}
              {user.role === 'member' && (
                <span className="text-xs text-muted">你只能改自己创建的流程</span>
              )}
            </div>
          </>
        )}
      </section>

      {editing && draft && (
        <StepEditorDialog
          step={editing}
          steps={draft.steps}
          caps={caps}
          inputFields={draft.input_schema?.fields ?? []}
          flows={flows.filter((f) => f.id !== draft.id)}
          onClose={() => setEditing(null)}
          onDelete={() => {
            removeStep(editing.id)
            setEditing(null)
          }}
          onSave={(next) => {
            patch({
              ...draft,
              steps: draft.steps.map((s) => (s.id === editing.id ? next : s)),
            })
            setEditing(null)
          }}
        />
      )}
    </div>
  )
}

/** 流程输入字段的编辑 */
function InputFields({
  fields,
  onChange,
}: {
  fields: TemplateField[]
  onChange: (fields: TemplateField[]) => void
}) {
  const add = () => {
    // 字段名是内部标识（引用靠它），由系统生成 —— 用户只管「标签」，
    // 标签才是他在节点参数里看到的东西。
    let n = fields.length + 1
    const taken = new Set(fields.map((f) => f.name))
    while (taken.has(`input${n}`)) n += 1
    onChange([
      ...fields,
      {
        name: `input${n}`,
        label: `输入 ${n}`,
        type: 'textarea',
        required: true,
      } as TemplateField,
    ])
  }
  const patch = (name: string, part: Partial<TemplateField>) => {
    onChange(fields.map((f) => (f.name === name ? { ...f, ...part } : f)))
  }
  const remove = (name: string) => onChange(fields.filter((f) => f.name !== name))

  return (
    <div className="mt-sm flex flex-col gap-2xs">
      {fields.length === 0 && <p className="text-sm text-muted">还没有输入字段</p>}
      {fields.map((f) => (
        <div key={f.name} className="flex flex-wrap items-end gap-xs">
          <label className="field-label flex-1">
            标签
            <input
              className="field"
              value={f.label}
              placeholder="例如：初始画面"
              onChange={(e) => patch(f.name, { label: e.target.value })}
            />
          </label>
          <label className="field-label w-28">
            类型
            <select
              className="field"
              value={f.type}
              onChange={(e) => patch(f.name, { type: e.target.value as TemplateField['type'] })}
            >
              <option value="textarea">文本</option>
              <option value="text">单行</option>
              <option value="number">数字</option>
              <option value="select">下拉</option>
              <option value="file_upload">文件</option>
            </select>
          </label>
          <label className="check-row h-9">
            <input
              type="checkbox"
              checked={!!f.multiple}
              onChange={(e) => patch(f.name, { multiple: e.target.checked })}
            />
            多条
          </label>
          <button
            type="button"
            className="btn btn-danger-quiet"
            onClick={() => remove(f.name)}
            aria-label={`删除输入字段 ${f.label || f.name}`}
          >
            删除
          </button>
        </div>
      ))}
      <button type="button" className="btn btn-quiet self-start" onClick={add}>
        添加输入字段
      </button>
    </div>
  )
}
