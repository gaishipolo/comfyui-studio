/** 节点参数弹窗 —— 双击节点卡片打开
 *
 * 主区（节点列表）只显示「谁 + 吃什么」，细节收在这里。理由跟 LibTV 那类
 * 画布工具一样：流程步骤一多，摊开的参数行会把整页塞满，扫不动了。
 *
 * **参数怎么取值不让人手打表达式。** 原本写 `{{ input.prompt_a }}`，
 * 那是数据结构的内部表示 —— 内容同事不该知道流程内部叫什么。
 * 这里改成三选一：固定值 / 流程输入 / 上一步产物，各自有对应的控件。
 * 底层仍存表达式字符串（后端、Agent 接口、数据模型一行没动）——
 * 换的是界面，不是数据。
 *
 * 「这一步吃什么」是参数；「产出什么」由节点模板决定，这里只显示
 * 「谁在吃它的产物」，让人知道这条线接到哪里去了。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FlowStep, NodeCapability, TemplateField } from '../types'
import { useModalKeyboard } from './useModalKeyboard'
import { nodeVisual, paramSummary, stepDependencies } from './flowEditor'
import {
  buildSourceOptions,
  formatParamSource,
  optionPick,
  optionToSource,
  parseParamSource,
  sourceToOption,
  stepTitle,
  upstreamOptionValue,
} from './flowSource'

interface Props {
  step: FlowStep
  steps: FlowStep[]
  caps: NodeCapability[]
  inputFields: TemplateField[]
  onSave: (step: FlowStep) => void
  onDelete: () => void
  onClose: () => void
}

export default function StepEditorDialog({
  step,
  steps,
  caps,
  inputFields,
  onSave,
  onDelete,
  onClose,
}: Props) {
  const [draft, setDraft] = useState<FlowStep>(step)
  const dialogRef = useRef<HTMLDivElement>(null)
  useModalKeyboard(true, dialogRef, onClose)

  useEffect(() => {
    setDraft(step)
  }, [step])

  const cap = useMemo(
    () => caps.find((c) => c.id === draft.template_id) ?? null,
    [caps, draft.template_id],
  )
  const visual = nodeVisual(cap)

  // 步骤在流程里的位置 —— 上游只能取它前面的
  const selfIndex = steps.findIndex((s) => s.id === draft.id)
  const upstreams = useMemo(
    () => steps.filter((s, i) => s.id !== draft.id && i < selfIndex),
    [steps, draft.id, selfIndex],
  )
  const nodeNameOf = useCallback(
    (templateId: string) => caps.find((c) => c.id === templateId)?.name ?? '',
    [caps],
  )
  const stepTitles = useMemo(() => {
    const map: Record<string, string> = {}
    steps.forEach((s, i) => {
      map[s.id] = stepTitle(s, i, nodeNameOf(s.template_id))
    })
    return map
  }, [steps, nodeNameOf])

  // 谁在吃这一步的产物
  const consumers = steps.filter((s) => stepDependencies(steps, s.id).includes(draft.id))

  const patch = (part: Partial<FlowStep>) => setDraft((d) => ({ ...d, ...part }))

  const setParam = (name: string, value: string) =>
    patch({ params: { ...draft.params, [name]: value } })

  /**
   * 参数取值编辑器。
   *
   * 三种来源各自一个控件：
   * - 固定值 → 按字段类型给对应的输入控件（下拉就别让人手打「2k」）
   * - 流程输入 → 列出流程输入字段，选一个
   * - 上一步产物 → 列出能作为上游的步骤，选一个（可选取第几张）
   *
   * 用户看不到 {{ }}，但底层存的还是表达式。
   */
  const ParamControl = ({
    field,
    raw,
  }: {
    field: TemplateField
    raw: string
  }) => {
    const parsed = parseParamSource(raw)
    // 固定值就是 raw 本身；自定义表达式的原文也是 raw。
    // 不另开 state 存 —— 那就要在外部改值时同步，反而容易不同步。
    const literal = parsed.source.kind === 'literal' ? parsed.source.value : ''
    const customText = parsed.isCustom ? raw : ''

    // 只有文件类字段能接上游产物 —— 文本字段喂产物列表没有意义，
    // 给它列「上一步」是误导（后端会直接报错）。
    const canTakeOutput = field.type === 'file_upload' || field.type === 'image_upload'
    const options = buildSourceOptions(
      parsed,
      inputFields,
      canTakeOutput ? upstreams : [],
      upstreams.map((u) => stepTitles[u.id] ?? ''),
    )
    // 上游选项要带 pick —— 同一个上游可能取全部 / 第 1 张 / 最后一张
    const withPick = options.flatMap((o) => {
      if (o.kind !== 'upstream') return [o]
      const stepId = o.value.slice('upstream:'.length)
      return [
        { ...o, value: upstreamOptionValue(stepId, 'all'), label: `${o.label}（全部）` },
        { ...o, value: upstreamOptionValue(stepId, 'first'), label: `${o.label}（第 1 张）` },
        { ...o, value: upstreamOptionValue(stepId, 'last'), label: `${o.label}（最后一张）` },
      ]
    })

    const current = sourceToOption(parsed.source, parsed.isCustom)
    const inputFieldName =
      parsed.source.kind === 'input' ? parsed.source.field : ''

    const applyOption = (optionValue: string) => {
      const p = optionPick(optionValue)
      const src = optionToSource(optionValue, literal)
      if (src === 'custom') {
        // 切回自定义：原文不动
        return
      }
      if (src.kind === 'upstream') {
        setParam(field.name, formatParamSource({ ...src, pick: p }))
        return
      }
      if (src.kind === 'input') {
        setParam(field.name, formatParamSource(src))
        return
      }
      // 固定值：保留已输入的字面量
      setParam(field.name, literal)
    }

    return (
      <div className="flex flex-col gap-2xs">
        <div className="flex flex-wrap items-end gap-xs">
          <label className="field-label min-w-40 flex-1">
            <span className="text-xs text-muted">
              {field.label || field.name}
              {field.required ? ' *' : ''}
              {field.type === 'file_upload' && (
                <span className="ml-2xs">（接上游产物）</span>
              )}
            </span>
            {parsed.isCustom ? (
              <textarea
                className="field font-mono text-xs"
                rows={2}
                value={customText}
                onChange={(e) => setParam(field.name, e.target.value)}
                placeholder="{{ input.xxx }}"
              />
            ) : parsed.source.kind === 'input' ? (
              <div className="field flex items-center bg-paper-2 text-sm text-ink-2">
                取流程输入「
                {inputFields.find((f) => f.name === inputFieldName)?.label ||
                  inputFieldName}
                」
              </div>
            ) : parsed.source.kind === 'upstream' ? (
              <div className="field flex items-center bg-paper-2 text-sm text-ink-2">
                取「{stepTitles[parsed.source.stepId] ?? '上一步'}」的产物
                {parsed.source.pick === 'first' && '（第 1 张）'}
                {parsed.source.pick === 'last' && '（最后一张）'}
                {parsed.source.pick && /^[0-9]+$/.test(parsed.source.pick) &&
                  `（第 ${Number(parsed.source.pick) + 1} 张）`}
                {!canTakeOutput && (
                  <span className="ml-2xs text-warning">
                    这个字段不是图片/文件类型，接不了产物
                  </span>
                )}
              </div>
            ) : (
              <LiteralControl
                field={field}
                value={literal}
                onChange={(v) => setParam(field.name, v)}
              />
            )}
          </label>
        </div>

        <div className="flex items-center gap-xs">
          <span className="text-xs text-muted">取值来自</span>
          <select
            className="field w-64"
            value={current}
            aria-label={`「${field.label || field.name}」的取值来源`}
            onChange={(e) => applyOption(e.target.value)}
          >
            {withPick.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          {parsed.isCustom && (
            <span className="text-xs text-warning">这段是手写表达式，改动请谨慎</span>
          )}
          {!parsed.isCustom && parsed.source.kind === 'literal' && !literal && (
            <span className="text-xs text-muted">留空则用节点自己的默认值</span>
          )}
        </div>
      </div>
    )
  }

  return (
    <div
      className="fixed inset-0 z-modal flex items-center justify-center bg-scrim p-md"
      onClick={onClose}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="step-editor-title"
        tabIndex={-1}
        className="flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg border border-rule bg-paper shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-xs border-b border-rule p-md">
          <div className="flex min-w-0 items-center gap-sm">
            <span aria-hidden="true" className="text-ink-2">
              {visual.icon}
            </span>
            <h3 id="step-editor-title" className="truncate font-display text-title">
              {draft.title?.trim() || cap?.name || '节点设置'}
            </h3>
            <span className="chip shrink-0">
              第 {Math.max(0, selfIndex) + 1} 步
            </span>
            {draft.title?.trim() && cap?.name && (
              <span className="truncate text-xs text-muted">{cap.name}</span>
            )}
          </div>
          <button type="button" onClick={onClose} className="x-btn" aria-label="关闭节点设置">
            ✕
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-md">
          <div className="flex flex-wrap items-end gap-sm">
            <label className="field-label w-48">
              步骤名称
              <input
                className="field"
                value={draft.title ?? ''}
                placeholder="例如：生成参考图"
                onChange={(e) => patch({ title: e.target.value })}
                autoFocus
              />
            </label>
            <label className="field-label flex-1">
              节点
              <select
                className="field"
                value={draft.template_id}
                onChange={(e) => {
                  const next = caps.find((c) => c.id === e.target.value)
                  patch({
                    template_id: e.target.value,
                    // 换节点后字段全变了，参数按同名字段保留、其余清掉
                    params: next
                      ? Object.fromEntries(
                          next.fields.map((f) => [
                            f.name,
                            draft.params?.[f.name] ?? '',
                          ]),
                        )
                      : {},
                  })
                }}
              >
                <option value="">选一个节点…</option>
                {caps.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <p className="mt-2xs text-xs text-muted">
            步骤名称只是给人看的标签，随便改。内部编号（{draft.id}）由系统管理 ——
            改了它会让引用它的步骤断链，所以不开放编辑。
          </p>

          <h4 className="mt-md font-display text-base font-medium">这一步吃什么</h4>
          {!cap ? (
            <p className="mt-2xs text-sm text-muted">先选一个节点</p>
          ) : cap.fields.length === 0 ? (
            <p className="mt-2xs text-sm text-muted">这个节点没有可填的参数</p>
          ) : (
            <div className="mt-sm flex flex-col gap-md">
              {cap.fields.map((f) => (
                <ParamControl key={f.name} field={f} raw={draft.params?.[f.name] ?? ''} />
              ))}
            </div>
          )}

          <h4 className="mt-lg font-display text-base font-medium">这一步产出什么</h4>
          <p className="mt-2xs text-sm text-muted">
            产出由节点决定（{cap ? `${cap.name} 的输出` : '选好节点后可知'}）。
            {consumers.length > 0
              ? ` 下面这些步骤在吃它的产物：${consumers
                  .map((c) =>
                    stepTitle(c, steps.findIndex((s) => s.id === c.id), nodeNameOf(c.template_id)),
                  )
                  .join('、')}`
              : ' 目前没有别的步骤用它的产物。'}
          </p>
          {draft.params && Object.keys(draft.params).length > 0 && (
            <p className="mt-2xs text-xs text-muted">
              当前设置：{paramSummary(draft, cap, inputFields, stepTitles)}
            </p>
          )}
        </div>

        <div className="flex items-center justify-between gap-sm border-t border-rule p-md">
          <button type="button" className="btn btn-danger-quiet" onClick={onDelete}>
            删除这一步
          </button>
          <div className="flex gap-sm">
            <button type="button" className="btn-quiet" onClick={onClose}>
              取消
            </button>
            <button type="button" className="btn btn-primary" onClick={() => onSave(draft)}>
              应用
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

/**
 * 固定值控件 —— 按字段类型给对应的输入框。
 *
 * 下拉就别让用户手打值（「2K」打成「2k」后端会当成非法值）。
 * 控件类型和 DynamicForm 里一致。
 */
function LiteralControl({
  field,
  value,
  onChange,
}: {
  field: TemplateField
  value: string
  onChange: (v: string) => void
}) {
  if (field.type === 'select' && field.options?.length) {
    return (
      <select className="field" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">（不设置）</option>
        {field.options.map((o) => (
          <option key={String(o)} value={String(o)}>
            {String(o)}
          </option>
        ))}
      </select>
    )
  }
  if (field.type === 'number' || field.type === 'random_number') {
    return (
      <input
        className="field"
        type="number"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    )
  }
  if (field.type === 'textarea') {
    return (
      <textarea
        className="field"
        rows={3}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    )
  }
  return (
    <input className="field" value={value} onChange={(e) => onChange(e.target.value)} />
  )
}
