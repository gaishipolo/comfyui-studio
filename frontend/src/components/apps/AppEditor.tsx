/** 应用编辑器 —— 一个应用的一屏工作台
 *
 * 三栏（按用户的布局图）：
 *   左：节点 / 素材 —— 往流程里加东西、看手上有哪些素材
 *   中：调试按钮 + 步骤堆叠 + 运行区
 *   右：子流程 / agent —— 不是手搭的那一步从这儿来
 *
 * 为什么把「运行」也放进编辑器：应用的价值是「跑起来出东西」，编排和运行
 * 本来就是一件事的两面。分成两个 Tab 的时候，改完一步要切页才能试，
 * 试完发现问题再切回来 —— 一屏之内把这条回路缩短到零。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import type {
  AgentStatus,
  Flow,
  FlowRun,
  FlowStep,
  NodeCapability,
  Template,
  TemplateField,
} from '../../types'
import {
  approveFlow,
  deleteFlow,
  exportFlow,
  generateFlowDraft,
  getAgentStatus,
  trialFlow,
  updateFlow,
  validateFlow,
} from '../../api/client'
import DynamicForm from '../DynamicForm'
import StepEditorDialog from '../StepEditorDialog'
import RunPanel from './RunPanel'
import NodeRail from './NodeRail'
import SideRail from './SideRail'
import { nodeVisual, paramSummary, reorderSteps, wouldBreakDependency } from '../flowEditor'
import { stepOutputName } from '../flowBindings'
import { stepTitle } from '../flowSource'
import { notify, reportError } from '../../notify'
import { confirmDialog } from '../dialog'
import type { ModuleProps } from '../../modules/registry'

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

/** 新步骤 id —— 删过步骤再加会让 s{n} 撞号 */
function nextStepId(steps: FlowStep[]): string {
  let n = steps.length + 1
  const taken = new Set(steps.map((s) => s.id))
  while (taken.has(`s${n}`)) n += 1
  return `s${n}`
}

export default function AppEditor({
  flow,
  flows,
  caps,
  user,
  run,
  onBack,
  onChanged,
  onSelect,
  onRun,
  onCancelRun,
  onRunStarted,
}: {
  flow: Flow
  flows: Flow[]
  caps: NodeCapability[]
  user: ModuleProps['user']
  run: FlowRun | null
  onBack: () => void
  onChanged: () => Promise<void>
  onSelect: (id: string) => void
  onRun: (params: Record<string, unknown>) => void
  onCancelRun: () => void
  onRunStarted: (run: FlowRun) => void
}) {
  const [draft, setDraft] = useState<Flow>(flow)
  const [busy, setBusy] = useState(false)
  const [issues, setIssues] = useState<{ errors: string[]; warnings: string[] } | null>(null)
  const [layers, setLayers] = useState<string[][]>([])
  const [editing, setEditing] = useState<FlowStep | null>(null)
  // 高级设置默认收起：第一次打开应用的人要看的是「有哪几步」，不是字段定义
  const [showAdvanced, setShowAdvanced] = useState(false)
  // 拖动排序：dragIdx 是被拖的那项，dropIdx 是「插到第几项之前」
  const [dragIdx, setDragIdx] = useState<number | null>(null)
  const [dropIdx, setDropIdx] = useState<number | null>(null)
  const [reviewNote, setReviewNote] = useState('')
  // ── agent ──
  const [agentStatus, setAgentStatus] = useState<AgentStatus | null>(null)
  const [agentOpenToken, setAgentOpenToken] = useState(0)
  const [requirement, setRequirement] = useState('')
  const [agentBusy, setAgentBusy] = useState(false)
  const [agentErrors, setAgentErrors] = useState<string[]>([])

  // 换了一个应用 → 草稿、校验结果、编辑中的步骤全部重来
  useEffect(() => {
    setDraft(flow)
    setIssues(null)
    setLayers([])
    setEditing(null)
  }, [flow])

  const capOf = useCallback((id: string) => caps.find((c) => c.id === id) ?? null, [caps])
  const patch = (next: Flow) => setDraft(next)

  const inputFields = draft.input_schema?.fields ?? []

  // 步骤显示名 —— 引用别的步骤时要说「生成参考图」而不是裸 id（gen）
  const stepTitleMap = useMemo(() => {
    const map: Record<string, string> = {}
    draft.steps.forEach((s, i) => {
      map[s.id] = stepTitle(s, i, capOf(s.template_id)?.name)
    })
    return map
  }, [draft.steps, capOf])

  const subflowIds = draft.steps.filter((s) => s.type === 'flow' && s.flow_id).map((s) => s.flow_id)

  /**
   * 某一步**前面**的步骤各叫什么产出（产出名 → 步骤 id）。
   *
   * 自动接上按它匹配：下游节点的字段标签跟上游的产出名同名就接上。
   * 只能接前面的 —— 后面的还没跑。
   */
  const upstreamBefore = (index: number): Record<string, string> => {
    const map: Record<string, string> = {}
    draft.steps.slice(0, index).forEach((s) => {
      if (s.type === 'flow') {
        const sub = flows.find((f) => f.id === s.flow_id)
        const names = (sub?.outputs ?? []).map((o) => o.name).filter(Boolean)
        if (names.length === 0) map['output'] = s.id
        else names.forEach((n) => { map[n] = s.id })
      } else {
        const name = stepOutputName(s)
        if (name) map[name] = s.id
      }
    })
    return map
  }

  // ── 步骤增删 ──────────────────────────────
  const appendStep = (step: Omit<FlowStep, 'id'>) => {
    patch({ ...draft, steps: [...draft.steps, { ...step, id: nextStepId(draft.steps) } as FlowStep] })
  }

  const addNode = (cap: NodeCapability) => {
    appendStep({
      title: cap.name,
      type: 'template',
      template_id: cap.id,
      flow_id: '',
      count: 1,
      params: {},
      // **默认所有字段都走自动接上** —— 加进来就能跑，用不着的字段
      // 接不上就自动不传（节点用自己的默认值）。用户只在例外时才动手。
      auto_fields: (cap.fields ?? []).map((f) => f.name),
      output_name: cap.name,
    })
    notify(`已把「${cap.name}」接到流程末尾`, 'success')
  }

  const addSubflow = (sub: Flow) => {
    appendStep({
      title: sub.name,
      type: 'flow',
      template_id: '',
      flow_id: sub.id,
      count: 1,
      params: {},
      output_name: sub.name,
    })
    notify(`已把「${sub.name}」当成一步接进来`, 'success')
  }

  const addEmptyStep = () => {
    appendStep({
      title: '',
      type: 'template',
      template_id: '',
      flow_id: '',
      count: 1,
      params: {},
    })
  }

  const removeStep = (id: string) => {
    // 删掉被引用的步骤会断链 —— 说清是谁在用它，别让人以为删干净了
    const users = draft.steps.filter((s) =>
      Object.values(s.params ?? {}).some(
        (v) => typeof v === 'string' && v.includes(`steps.${id}.outputs`),
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
    setDropIdx(e.clientY > rect.top + rect.height / 2 ? index + 1 : index)
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    if (dragIdx === null || dropIdx === null) return
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

  // ── 校验 / 保存 / 试跑 / 审批 / 导出 ──────────
  const doValidate = async () => {
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
        notify('应用还有问题，先改完再保存', 'warning')
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
      await updateFlow(draft.id, payload)
      notify('已保存', 'success')
      await onChanged()
    } catch (err) {
      reportError('保存失败', err)
    } finally {
      setBusy(false)
    }
  }

  const doTrial = async () => {
    setBusy(true)
    try {
      const started = await trialFlow(draft.id, {})
      notify('试跑已开始，产物不会进正式素材库', 'success')
      onRunStarted(started)
    } catch (err) {
      reportError('试跑失败', err)
    } finally {
      setBusy(false)
    }
  }

  const doApprove = async (approve: boolean) => {
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

  const handleExport = async () => {
    try {
      await exportFlow(draft.id, draft.name)
    } catch (err) {
      reportError('导出失败', err)
    }
  }

  const remove = async () => {
    const ok = await confirmDialog({
      title: `删除应用「${draft.name}」？`,
      message: '删掉之后编排配置一起没了，历史运行记录还在。',
      confirmText: '删除',
      cancelText: '取消',
    })
    if (!ok) return
    try {
      await deleteFlow(draft.id)
      notify('应用已删除', 'success')
      await onChanged()
      onBack()
    } catch (err) {
      reportError('删除失败（被别的应用当子流程调用时不能删）', err)
    }
  }

  // ── agent ──
  const openAgent = async () => {
    setAgentOpenToken((n) => n + 1)
    setAgentErrors([])
    try {
      setAgentStatus(await getAgentStatus())
    } catch {
      setAgentStatus(null)
    }
  }

  const doAgentGenerate = async () => {
    const text = requirement.trim()
    if (text.length < 2) {
      notify('把想做的应用说得具体一点，比如「先生成参考图，再换成边牧」', 'warning')
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

  const errorCount = issues?.errors.length ?? 0

  return (
    <div className="flex h-full min-h-0 gap-sm">
      <NodeRail caps={caps} onAddNode={addNode} />

      {/* ── 中栏：调试按钮 + 步骤 + 运行 ── */}
      <section
        className="flex min-w-0 flex-1 flex-col gap-sm rounded-lg border border-tertiary bg-paper p-sm"
        data-app-editor
      >
        {/* 工具栏分两行是刻意的：一行「这是谁」（返回 / 名称 / 状态），
            一行「能对它做什么」（调试动作）。挤成一行时中栏一窄就乱换行 */}
        <div className="panel flex flex-col gap-xs">
          <div className="flex items-center gap-xs">
            <button type="button" className="btn btn-quiet" data-app-back onClick={onBack}>
              ← 应用
            </button>
            <input
              className="field min-w-0 flex-1"
              value={draft.name}
              aria-label="应用名称"
              onChange={(e) => patch({ ...draft, name: e.target.value })}
            />
            <span className="chip shrink-0" data-tone={draft.status === 'draft' ? 'accent' : undefined}>
              {draft.status === 'draft'
                ? '待审批'
                : draft.status === 'archived'
                  ? '已归档'
                  : '已启用'}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-xs">
            <button
              type="button"
              className="btn btn-quiet"
              disabled={busy}
              onClick={() => void doValidate()}
            >
              校验
              {errorCount > 0 && <span className="ml-2xs text-error">{errorCount}</span>}
            </button>
            <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void doSave()}>
              保存
            </button>
            {draft.status !== 'archived' && (
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
            <button type="button" className="btn btn-quiet" onClick={() => void openAgent()}>
              一句话生成
            </button>
            <button type="button" className="btn btn-quiet" onClick={() => void handleExport()}>
              导出
            </button>
            <button
              type="button"
              className="btn btn-danger-quiet ml-auto"
              onClick={() => void remove()}
            >
              删除
            </button>
          </div>
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-sm overflow-y-auto pr-2xs">
          {/* ── 待审批：Agent 生成的配置落成草稿，人批了才上线 ── */}
          {draft.status === 'draft' && (
            <div className="panel border-accent">
              <div className="flex flex-wrap items-center gap-sm">
                <span className="chip" data-tone="accent">
                  待审批
                </span>
                <span className="text-sm text-ink-2">
                  这个应用还没上线，别人跑不到它。
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
            <p className="text-xs text-muted">
              <span className="text-ink-2">审批意见：</span>
              {draft.review_note}
            </p>
          )}

          {/* ── 步骤堆叠 ── */}
          <div>
            <div className="mb-xs flex items-center justify-between gap-sm">
              <h4 className="font-display text-base font-medium">
                步骤
                <span className="ml-2xs text-xs text-muted">
                  {draft.steps.length} 步 · 拖动排序 · 双击设置
                </span>
              </h4>
              <button type="button" className="btn btn-quiet" onClick={addEmptyStep}>
                加空步骤
              </button>
            </div>

            {draft.steps.length === 0 ? (
              <div className="empty">
                <p className="empty-title">还没有步骤</p>
                <p className="empty-hint">
                  从左边点一个节点接上来，或者在右边把别的应用当子流程接进来
                </p>
              </div>
            ) : (
              <ol className="flex flex-col">
                {draft.steps.map((step, index) => {
                  const cap = capOf(step.template_id)
                  const visual = nodeVisual(cap)
                  const layer = layers.findIndex((l) => l.includes(step.id))
                  const subName =
                    step.type === 'flow'
                      ? (flows.find((f) => f.id === step.flow_id)?.name ?? '未选子流程')
                      : ''
                  return (
                    <div key={step.id}>
                      {dropIdx === index && <div className="my-2xs h-0.5 rounded bg-accent" />}
                      <li
                        draggable
                        data-step-id={step.id}
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
                          {step.type === 'flow' ? '▤' : visual.icon}
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
                        <span className="flex min-w-0 flex-1 flex-col gap-1-xs">
                          <span className="block truncate text-sm font-medium">
                            {step.title?.trim() || (step.type === 'flow' ? subName : cap?.name) ||
                              '（未选节点）'}
                          </span>
                          <span className="flex items-center gap-xs">
                            {/* 产出名：下游按它同名自动接上。改一个词，下游就连上了 */}
                            <label
                              className="flex shrink-0 items-center gap-2xs text-xs text-muted"
                              onClick={(e) => e.stopPropagation()}
                              onDoubleClick={(e) => e.stopPropagation()}
                            >
                              产出
                              <input
                                className="field h-6 w-24 py-0 text-xs"
                                value={step.output_name ?? ''}
                                placeholder={step.title || '名称'}
                                aria-label={`步骤 ${index + 1} 的产出名`}
                                onChange={(e) =>
                                  patch({
                                    ...draft,
                                    steps: draft.steps.map((s) =>
                                      s.id === step.id ? { ...s, output_name: e.target.value } : s,
                                    ),
                                  })
                                }
                              />
                            </label>
                            <span className="min-w-0 truncate text-xs text-muted">
                              {step.type === 'flow'
                                ? '输入按同名自动传'
                                : cap?.name && step.title?.trim()
                                  ? `${cap.name} · ${paramSummary(
                                      step,
                                      cap,
                                      inputFields,
                                      stepTitleMap,
                                      upstreamBefore(index),
                                    )}`
                                  : paramSummary(
                                      step,
                                      cap,
                                      inputFields,
                                      stepTitleMap,
                                      upstreamBefore(index),
                                    )}
                            </span>
                          </span>
                        </span>
                        {layer >= 0 && (
                          <span className="shrink-0 text-xs text-muted">
                            第 {layer + 1} 层
                          </span>
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
          </div>

          {/* ── 高级设置：字段定义 / 产出 / 分层 / 校验结果 ── */}
          <div className="panel">
            <button
              type="button"
              className="flex w-full items-center justify-between gap-sm"
              onClick={() => setShowAdvanced(!showAdvanced)}
              aria-expanded={showAdvanced}
            >
              <span className="font-display text-base font-medium">
                输入与产出
                <span className="ml-2xs text-xs text-muted">
                  {inputFields.length} 个输入 · {draft.outputs.length} 项产出
                </span>
              </span>
              <span className="text-xs text-muted">{showAdvanced ? '▾ 收起' : '▸ 展开'}</span>
            </button>
            {showAdvanced && (
              <div className="mt-sm flex flex-col gap-md">
                <div>
                  <h5 className="font-display text-sm font-medium">输入字段</h5>
                  <p className="mt-2xs text-xs text-muted">
                    运行时用户要填的东西。在节点参数里选「流程输入 · 字段名」就能引用。
                    勾「多条」就是列表 —— 循环步骤靠它把「20 条提示词」喂给每一轮。
                  </p>
                  <InputFields
                    fields={inputFields}
                    onChange={(fields) =>
                      patch({ ...draft, input_schema: { ...draft.input_schema, fields } })
                    }
                  />
                </div>

                <div>
                  <div className="flex items-center justify-between gap-sm">
                    <h5 className="font-display text-sm font-medium">产出</h5>
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
                    <p className="mt-2xs text-sm text-muted">还没声明产出</p>
                  ) : (
                    <div className="mt-2xs flex flex-col gap-2xs">
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
                  <div>
                    <h5 className="font-display text-sm font-medium">并行分组</h5>
                    <p className="mt-2xs text-xs text-muted">
                      同一组里的步骤互不依赖，会同时开跑；后面的组等前面跑完
                    </p>
                    <ol className="mt-2xs flex flex-col gap-2xs">
                      {layers.map((layer, i) => (
                        <li key={i} className="flex items-center gap-sm text-sm">
                          <span className="chip">第 {i + 1} 层</span>
                          {layer.map((sid) => (
                            <code key={sid} className="text-xs text-muted">
                              {stepTitleMap[sid] ?? sid}
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
                  <div>
                    <h5 className="font-display text-sm font-medium">校验结果</h5>
                    {issues.errors.length > 0 && (
                      <ul className="mt-2xs flex flex-col gap-2xs">
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
              </div>
            )}
          </div>

          {/* ── 运行：填参数 → 跑 → 看每一步 ── */}
          <div className="panel">
            <div className="flex items-center justify-between gap-sm">
              <h4 className="font-display text-base font-medium">运行</h4>
              <span className="text-xs text-muted">
                {draft.status === 'active'
                  ? '跑一次就是真的出东西'
                  : '草稿也能跑（算试跑，产物不进正式素材库）'}
              </span>
            </div>
            {draft.status === 'archived' ? (
              <p className="mt-2xs text-sm text-muted">已归档的应用不能运行</p>
            ) : (
              <div className="mt-sm">
                <DynamicForm
                  key={draft.id}
                  template={flowAsTemplate(draft)}
                  uploadTarget="asset"
                  showBatch={false}
                  submitLabel="开始运行"
                  submitting={false}
                  onSubmit={(params) => onRun(params)}
                />
              </div>
            )}
            {run && (
              <div className="mt-sm">
                <RunPanel
                  run={run}
                  steps={draft.steps}
                  caps={caps}
                  flows={flows}
                  onCancel={onCancelRun}
                />
              </div>
            )}
          </div>

          {user.role === 'member' && (
            <p className="text-xs text-muted">你只能改自己创建的应用</p>
          )}
        </div>
      </section>

      <SideRail
        flows={flows}
        currentId={draft.id}
        stepsUsingFlows={subflowIds}
        onAddSubflow={addSubflow}
        agentStatus={agentStatus}
        agentOpenToken={agentOpenToken}
        agentBusy={agentBusy}
        agentErrors={agentErrors}
        requirement={requirement}
        onRequirementChange={setRequirement}
        onGenerate={() => void doAgentGenerate()}
      />

      {editing && (
        <StepEditorDialog
          step={editing}
          steps={draft.steps}
          caps={caps}
          inputFields={inputFields}
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
  const patchField = (name: string, part: Partial<TemplateField>) => {
    onChange(fields.map((f) => (f.name === name ? { ...f, ...part } : f)))
  }
  const remove = (name: string) => onChange(fields.filter((f) => f.name !== name))

  return (
    <div className="mt-2xs flex flex-col gap-2xs">
      {fields.length === 0 && <p className="text-sm text-muted">还没有输入字段</p>}
      {fields.map((f) => (
        <div key={f.name} className="flex flex-wrap items-end gap-xs">
          <label className="field-label flex-1">
            标签
            <input
              className="field"
              value={f.label}
              placeholder="例如：初始画面"
              onChange={(e) => patchField(f.name, { label: e.target.value })}
            />
          </label>
          <label className="field-label w-28">
            类型
            <select
              className="field"
              value={f.type}
              onChange={(e) =>
                patchField(f.name, { type: e.target.value as TemplateField['type'] })
              }
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
              onChange={(e) => patchField(f.name, { multiple: e.target.checked })}
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
