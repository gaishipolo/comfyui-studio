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
import type { Flow, FlowRun, FlowStep, NodeCapability, Template, TemplateField } from '../types'
import {
  createFlow,
  deleteFlow,
  getFlowRun,
  listFlows,
  listNodeCapabilities,
  runFlow,
  updateFlow,
  validateFlow,
} from '../api/client'
import DynamicForm from './DynamicForm'
import StepEditorDialog from './StepEditorDialog'
import { nodeVisual, paramSummary, reorderSteps, wouldBreakDependency } from './flowEditor'
import { notify, reportError } from '../notify'
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
        />
      ) : (
        <EditView
          flows={flows}
          caps={caps}
          selected={selected}
          user={user}
          onSelect={setSelectedId}
          onChanged={load}
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
}: {
  flows: Flow[]
  caps: NodeCapability[]
  selected: Flow | null
  run: FlowRun | null
  loading: boolean
  onSelect: (id: string) => void
  onRun: (params: Record<string, unknown>) => void
}) {
  const nodeName = (templateId: string) =>
    caps.find((c) => c.id === templateId)?.name ?? '未知节点'
  if (loading && !flows.length) return <p className="text-sm text-muted">加载中…</p>
  if (!flows.length) {
    return (
      <div className="empty">
        <p className="empty-title">还没有流程</p>
        <p className="empty-hint">切到「编排」页新建一条，把节点串起来</p>
      </div>
    )
  }
  return (
    <div className="flex min-h-0 flex-1 gap-md">
      <aside className="w-64 shrink-0 overflow-y-auto">
        <ul className="flex flex-col gap-2xs">
          {flows.map((f) => (
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
                <div className="text-xs text-muted">
                  {f.steps.length} 步 · {f.status === 'active' ? '已启用' : f.status}
                </div>
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
                    <code className="text-xs text-muted">{s.id}</code>
                    <span>{nodeName(s.template_id)}</span>
                    <span className="text-xs text-muted">{STEP_TYPE_LABEL[s.type] ?? s.type}</span>
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
                  <span className="chip">{RUN_STATUS_LABEL[run.status] ?? run.status}</span>
                </div>
                {run.error_msg && (
                  <p className="mt-2xs text-sm text-error">{run.error_msg}</p>
                )}
                <ol className="mt-sm flex flex-col gap-2xs">
                  {run.steps.map((s, i) => (
                    <li key={s.step_id} className="flex items-center gap-sm text-sm">
                      <span className="chip">{i + 1}</span>
                      {typeof s.layer === 'number' && (
                        <span className="text-xs text-muted">第 {s.layer + 1} 层</span>
                      )}
                      <code className="text-xs text-muted">{s.step_id}</code>
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
                  ))}
                  {run.status === 'running' && run.steps.length === 0 && (
                    <li className="text-sm text-muted">正在执行第一步…</li>
                  )}
                </ol>
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
}: {
  flows: Flow[]
  caps: NodeCapability[]
  selected: Flow | null
  user: ModuleProps['user']
  onSelect: (id: string) => void
  onChanged: () => Promise<void>
}) {
  const [draft, setDraft] = useState<Flow | null>(selected)
  const [busy, setBusy] = useState(false)
  const [issues, setIssues] = useState<{ errors: string[]; warnings: string[] } | null>(null)
  const [layers, setLayers] = useState<string[][]>([])
  const [editing, setEditing] = useState<FlowStep | null>(null)
  const [showInputs, setShowInputs] = useState(false)
  // 拖动排序：dragIdx 是被拖的那项，dropIdx 是「插到第几项之前」
  const [dragIdx, setDragIdx] = useState<number | null>(null)
  const [dropIdx, setDropIdx] = useState<number | null>(null)

  useEffect(() => {
    setDraft(selected)
    setIssues(null)
    setLayers([])
    setEditing(null)
  }, [selected])

  const capOf = (id: string) => caps.find((c) => c.id === id) ?? null
  const patch = (next: Flow) => setDraft(next)

  const addStep = () => {
    if (!draft) return
    const n = draft.steps.length + 1
    patch({
      ...draft,
      steps: [
        ...draft.steps,
        {
          id: `s${n}`,
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
      if (draft.id) {
        await updateFlow(draft.id, draft)
      } else {
        await createFlow(draft)
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

  return (
    <div className="flex min-h-0 flex-1 gap-md">
      <aside className="w-56 shrink-0 overflow-y-auto">
        <button type="button" className="btn btn-quiet mb-sm w-full" onClick={() => void createNew()}>
          新建流程
        </button>
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
                <div className="text-xs text-muted">{f.steps.length} 步</div>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col overflow-y-auto">
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
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm font-medium">
                            {cap?.name ?? '（未选节点）'}
                          </span>
                          <span className="block truncate text-xs text-muted">
                            {paramSummary(step, cap)}
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
                          aria-label={`删除步骤 ${step.id}`}
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
                  运行时要用户填的东西。节点里用 {'{{ input.字段名 }}'} 引用
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
    onChange([
      ...fields,
      {
        name: `input${fields.length + 1}`,
        label: `输入 ${fields.length + 1}`,
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
          <label className="field-label">
            字段名
            <input
              className="field"
              value={f.name}
              onChange={(e) => patch(f.name, { name: e.target.value })}
            />
          </label>
          <label className="field-label flex-1">
            标签
            <input
              className="field"
              value={f.label}
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
          <button
            type="button"
            className="btn btn-danger-quiet"
            onClick={() => remove(f.name)}
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
