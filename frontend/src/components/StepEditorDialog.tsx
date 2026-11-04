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
import type { Flow, FlowStep, NodeCapability, TemplateField } from '../types'
import { useModalKeyboard } from './useModalKeyboard'
import { nodeVisual, paramSummary, stepDependencies } from './flowEditor'
import {
  COND_OPS,
  TRANSFORMS,
  type CondOp,
  type ExprTransform,
} from './flowExpr'
import {
  buildSourceOptions,
  formatParsedParam,
  optionPick,
  optionToSource,
  parseParamSource,
  sourceToOption,
  stepTitle,
  upstreamOptionValue,
  type ParamSource,
  type SourceOption,
} from './flowSource'

interface Props {
  step: FlowStep
  steps: FlowStep[]
  caps: NodeCapability[]
  inputFields: TemplateField[]
  /** 可被调用的子流程（排除自己） */
  flows: Flow[]
  onSave: (step: FlowStep) => void
  onDelete: () => void
  onClose: () => void
}

export default function StepEditorDialog({
  step,
  steps,
  caps,
  inputFields,
  flows,
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

  /** 删掉一个键 = 回到「子流程同名输入自动传」 */
  const removeParam = (name: string) => {
    const next = { ...draft.params }
    delete next[name]
    patch({ params: next })
  }

  const subFlow = useMemo(
    () => (draft.type === 'flow' ? flows.find((f) => f.id === draft.flow_id) ?? null : null),
    [draft.type, draft.flow_id, flows],
  )
  const subInputFields = subFlow?.input_schema?.fields ?? []

  /**
   * 参数取值编辑器。
   *
   * 三种来源各自一个控件：
   * - 固定值 → 按字段类型给对应的输入控件（下拉就别让人手打「2k」）
   * - 流程输入 → 列出流程输入字段，选一个
   * - 上一步产物 → 列出能作为上游的步骤，选一个（可选取第几张）
   *
   * 外加两块可选的进阶设置：
   * - **再加工**：取到值之后做处理（去空格、截断字数…）
   * - **按条件取值**：满足条件用一边、不满足用另一边
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

    /** 写回：任何改动都要经过这里，保证「加工链/条件」不丢 */
    const write = (next: Partial<typeof parsed>) => {
      const merged = { ...parsed, ...next }
      setParam(field.name, formatParsedParam(merged))
    }

    const applyOption = (optionValue: string) => {
      const p = optionPick(optionValue)
      const src = optionToSource(optionValue, literal)
      if (src === 'custom') {
        // 切回自定义：原文不动
        return
      }
      if (src.kind === 'upstream') {
        write({ source: { ...src, pick: p } })
        return
      }
      if (src.kind === 'input') {
        write({ source: src })
        return
      }
      // 固定值：保留已输入的字面量
      write({ source: src })
    }

    // ── 再加工 ──
    const setTransform = (index: number, next: ExprTransform) => {
      const transforms = parsed.transforms.map((t, i) => (i === index ? next : t))
      write({ transforms })
    }
    const removeTransform = (index: number) => {
      write({ transforms: parsed.transforms.filter((_, i) => i !== index) })
    }
    const addTransform = (fn: string) => {
      const spec = TRANSFORMS.find((t) => t.fn === fn)
      const item: ExprTransform = spec?.argLabel
        ? { fn, arg: spec.argPlaceholder ?? '' }
        : { fn }
      write({ transforms: [...parsed.transforms, item] })
    }

    // ── 按条件取值 ──
    const cond = parsed.condition
    const setCondition = (next: NonNullable<typeof cond>) => write({ condition: next })
    const clearCondition = () => write({ condition: undefined })

    // 条件的两个分支也用来源下拉 —— 和主区一样的选法，
    // 内容同事不用学第二套语法。
    // 直接算不用 useMemo：ParamControl 是内联组件（每次渲染新实例），
    // 在里面用 hook 是坏味道；选项列表也就十几个，重算无妨。
    const branchOptions = buildSourceOptions(
      { source: { kind: 'literal', value: '' }, transforms: [], raw: '', isCustom: false },
      inputFields,
      canTakeOutput ? upstreams : [],
      upstreams.map((u) => stepTitles[u.id] ?? ''),
    )

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
                onChange={(v) => write({ source: { kind: 'literal', value: v } })}
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

        {!parsed.isCustom && (
          <div className="flex flex-col gap-2xs border-l-2 border-rule pl-sm">
            <div className="flex flex-wrap items-center gap-xs">
              <span className="text-xs text-muted">取到之后再处理</span>
              {parsed.transforms.length === 0 && (
                <span className="text-xs text-muted">（不用处理）</span>
              )}
              {parsed.transforms.map((t, i) => {
                const spec = TRANSFORMS.find((s) => s.fn === t.fn)
                return (
                  <span
                    key={`${t.fn}-${i}`}
                    className="inline-flex items-center gap-2xs rounded border border-rule bg-paper-2 px-2xs py-1-xs"
                  >
                    <select
                      className="field h-7 w-auto py-0 text-xs"
                      value={t.fn}
                      aria-label={`第 ${i + 1} 道处理的函数`}
                      onChange={(e) => {
                        const nextSpec = TRANSFORMS.find((s) => s.fn === e.target.value)
                        setTransform(i, {
                          fn: e.target.value,
                          ...(nextSpec?.argLabel
                            ? { arg: t.arg ?? nextSpec.argPlaceholder ?? '' }
                            : {}),
                        })
                      }}
                    >
                      {TRANSFORMS.map((s) => (
                        <option key={s.fn} value={s.fn}>
                          {s.label}
                        </option>
                      ))}
                    </select>
                    {spec?.argLabel && (
                      <input
                        className="field h-7 w-28 py-0 text-xs"
                        value={t.arg ?? ''}
                        placeholder={spec.argPlaceholder}
                        aria-label={`${spec.argLabel}`}
                        onChange={(e) => setTransform(i, { ...t, arg: e.target.value })}
                      />
                    )}
                    <button
                      type="button"
                      className="text-xs text-muted hover:text-danger"
                      aria-label={`去掉第 ${i + 1} 道处理`}
                      onClick={() => removeTransform(i)}
                    >
                      ✕
                    </button>
                  </span>
                )
              })}
              <select
                className="field h-7 w-32 py-0 text-xs"
                value=""
                aria-label="添加一道处理"
                onChange={(e) => {
                  if (e.target.value) addTransform(e.target.value)
                }}
              >
                <option value="">＋ 处理…</option>
                {TRANSFORMS.map((s) => (
                  <option key={s.fn} value={s.fn}>
                    {s.label}
                  </option>
                ))}
              </select>
            </div>

            <div className="flex flex-wrap items-center gap-xs">
              <span className="text-xs text-muted">按条件取值</span>
              {!cond ? (
                <button
                  type="button"
                  className="btn-quiet text-xs"
                  onClick={() =>
                    setCondition({
                      // 判断依据默认挑一个**不同于当前取值来源**的输入 ——
                      // 默认选自己会变成「X 等于 X」，条件永远成立，没意义。
                      field:
                        inputFields.find(
                          (f) => !(parsed.source.kind === 'input' && f.name === parsed.source.field),
                        )?.name ??
                        inputFields[0]?.name ??
                        '',
                      op: '==',
                      value: '',
                      then: {
                        source: { kind: 'literal', value: literal },
                        transforms: [],
                      },
                      otherwise: {
                        source: parsed.source,
                        transforms: parsed.transforms,
                      },
                    })
                  }
                >
                  ＋ 加一个条件
                </button>
              ) : (
                <span className="flex flex-col gap-2xs text-xs">
                  <span className="flex flex-wrap items-center gap-xs">
                    <span>当流程输入「</span>
                    <select
                      className="field h-7 w-32 py-0 text-xs"
                      value={cond.field}
                      aria-label="条件判断的流程输入"
                      onChange={(e) => setCondition({ ...cond, field: e.target.value })}
                    >
                      {inputFields.map((f) => (
                        <option key={f.name} value={f.name}>
                          {f.label || f.name}
                        </option>
                      ))}
                    </select>
                    <span>」</span>
                    <select
                      className="field h-7 w-24 py-0 text-xs"
                      value={cond.op}
                      aria-label="条件的运算符"
                      onChange={(e) =>
                        setCondition({ ...cond, op: e.target.value as CondOp })
                      }
                    >
                      {COND_OPS.map((o) => (
                        <option key={o.op} value={o.op}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                    <span>「</span>
                    <input
                      className="field h-7 w-28 py-0 text-xs"
                      value={cond.value}
                      placeholder="比较的值"
                      aria-label="条件的比较值"
                      onChange={(e) => setCondition({ ...cond, value: e.target.value })}
                    />
                    <span>」时用</span>
                  </span>
                  <span className="flex flex-wrap items-center gap-xs">
                    <span className="text-muted">成立时</span>
                    <BranchSourcePicker
                      label="条件成立时用的值"
                      source={cond.then.source}
                      options={branchOptions}
                      literalValue={
                        cond.then.source.kind === 'literal' ? cond.then.source.value : ''
                      }
                      onChange={(src) =>
                        setCondition({
                          ...cond,
                          then: { ...cond.then, source: src },
                        })
                      }
                      onLiteral={(v) =>
                        setCondition({
                          ...cond,
                          then: {
                            ...cond.then,
                            source: { kind: 'literal', value: v },
                          },
                        })
                      }
                    />
                    <span className="text-muted">否则时</span>
                    <BranchSourcePicker
                      label="条件不成立时用的值"
                      source={cond.otherwise.source}
                      options={branchOptions}
                      literalValue={
                        cond.otherwise.source.kind === 'literal'
                          ? cond.otherwise.source.value
                          : ''
                      }
                      onChange={(src) =>
                        setCondition({
                          ...cond,
                          otherwise: { ...cond.otherwise, source: src },
                        })
                      }
                      onLiteral={(v) =>
                        setCondition({
                          ...cond,
                          otherwise: {
                            ...cond.otherwise,
                            source: { kind: 'literal', value: v },
                          },
                        })
                      }
                    />
                    <button
                      type="button"
                      className="text-xs text-muted hover:text-danger"
                      onClick={clearCondition}
                    >
                      ✕
                    </button>
                  </span>
                </span>
              )}
            </div>
          </div>
        )}
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
            <label className="field-label w-32">
              类型
              <select
                className="field"
                value={draft.type}
                onChange={(e) => {
                  const nextType = e.target.value as FlowStep['type']
                  // 普通节点和子流程的参数是两套字段；跨类型切换还留着旧键，
                  // 会变成子流程的意外覆盖（或普通节点的自造字段），直接清掉。
                  const crossesFlow =
                    (nextType === 'flow') !== (draft.type === 'flow')
                  patch({
                    type: nextType,
                    // 切成循环时给个默认次数；切回来时次数保留（改回去不用重设）
                    count: nextType === 'repeat' ? draft.count || 1 : draft.count,
                    params: crossesFlow ? {} : draft.params,
                    template_id: nextType === 'flow' ? '' : draft.template_id,
                    flow_id: nextType === 'flow' ? draft.flow_id : '',
                  })
                }}
              >
                <option value="template">普通</option>
                <option value="repeat">循环</option>
                <option value="flow">子流程</option>
              </select>
            </label>
            {draft.type === 'flow' ? (
              <label className="field-label flex-1">
                调用哪条流程
                <select
                  className="field"
                  value={draft.flow_id}
                  onChange={(e) =>
                    // 换一条子流程，输入字段就换了一套 —— 旧绑定留着只会
                    // 变成「不存在的字段」，清掉后同名输入仍会自动传
                    patch({ flow_id: e.target.value, params: {} })
                  }
                >
                  <option value="">选一条流程…</option>
                  {flows.map((f) => (
                    <option key={f.id} value={f.id}>
                      {f.name}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
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
            )}
          </div>

          {draft.type === 'repeat' && (
            <div className="mt-sm panel px-md py-sm">
              <div className="flex flex-wrap items-end gap-sm">
                <label className="field-label w-32">
                  跑几次
                  <input
                    className="field"
                    type="number"
                    min={0}
                    max={200}
                    value={draft.count}
                    onChange={(e) =>
                      patch({ count: Number(e.target.value) || 0, count_expr: '' })
                    }
                  />
                </label>
                <p className="flex-1 text-xs text-muted">
                  填 0 = 由列表长度决定（下面的参数里引用
                  「列表[序号]」就自动按列表长度跑）。
                  参数里可以用 {'{{ index }}'}（第几次，从 0 起）、
                  {'{{ total }}'}（一共几次）、{'{{ item }}'}（当前这一项）。
                </p>
              </div>
            </div>
          )}
          <p className="mt-2xs text-xs text-muted">
            步骤名称只是给人看的标签，随便改。内部编号（{draft.id}）由系统管理 ——
            改了它会让引用它的步骤断链，所以不开放编辑。
          </p>

          <h4 className="mt-md font-display text-base font-medium">这一步吃什么</h4>
          {draft.type === 'flow' ? (
            !subFlow ? (
              <p className="mt-2xs text-sm text-muted">先选一条子流程</p>
            ) : subInputFields.length === 0 ? (
              <p className="mt-2xs text-sm text-muted">这条子流程没有输入</p>
            ) : (
              <>
                <p className="mt-2xs text-xs text-muted">
                  同名字段会自动从父流程传进来。只有想换来源时才需要单独设置。
                </p>
                <div className="mt-sm flex flex-col gap-md">
                  {subInputFields.map((f) => {
                    const overridden = Object.prototype.hasOwnProperty.call(
                      draft.params ?? {},
                      f.name,
                    )
                    return (
                      <div key={f.name}>
                        <div className="flex flex-wrap items-center justify-between gap-sm">
                          <span className="text-sm">
                            {f.label || f.name}
                            {f.required && <span className="text-error"> *</span>}
                          </span>
                          <button
                            type="button"
                            className="btn btn-quiet"
                            onClick={() => {
                              if (overridden) {
                                removeParam(f.name)
                                return
                              }
                              // 默认填「同名的父流程输入」—— 和自动传一致，
                              // 用户接着改来源即可，不用从空白开始
                              const sameName = inputFields.some((i) => i.name === f.name)
                              setParam(f.name, sameName ? `{{ input.${f.name} }}` : '')
                            }}
                          >
                            {overridden ? '恢复同名自动传' : '改用其他来源'}
                          </button>
                        </div>
                        {overridden ? (
                          <div className="mt-2xs">
                            <ParamControl field={f} raw={draft.params?.[f.name] ?? ''} />
                          </div>
                        ) : (
                          <p className="mt-2xs text-xs text-muted">
                            运行时取同名的父流程输入
                          </p>
                        )}
                      </div>
                    )
                  })}
                </div>
              </>
            )
          ) : !cap ? (
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
            {draft.type === 'flow'
              ? subFlow
                ? `子流程产出：${
                    (subFlow.outputs ?? []).map((o) => o.name).join('、') ||
                    '未声明（默认交付最后一步的产物）'
                  }。`
                : '先选一条子流程。'
              : `产出由节点决定（${cap ? `${cap.name} 的输出` : '选好节点后可知'}）。`}
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
 * 条件的分支取值 —— 一个来源下拉 + 固定值输入框。
 *
 * 和主区的「取值来自」是同一套选法 —— 内容同事不用学第二套语法。
 */
function BranchSourcePicker({
  label,
  source,
  options,
  literalValue,
  onChange,
  onLiteral,
}: {
  label: string
  source: ParamSource
  options: SourceOption[]
  literalValue: string
  onChange: (s: ParamSource) => void
  onLiteral: (v: string) => void
}) {
  const isLiteral = source.kind === 'literal'
  const current = isLiteral ? 'literal' : sourceToOption(source, false)
  return (
    <span className="inline-flex items-center gap-2xs">
      <select
        className="field h-7 w-40 py-0 text-xs"
        value={current}
        aria-label={label}
        onChange={(e) => {
          const picked = optionToSource(e.target.value, literalValue)
          if (picked !== 'custom') onChange(picked)
        }}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      {isLiteral && (
        <input
          className="field h-7 w-32 py-0 text-xs"
          value={literalValue}
          placeholder="固定值"
          aria-label={`${label}的内容`}
          onChange={(e) => onLiteral(e.target.value)}
        />
      )}
    </span>
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
