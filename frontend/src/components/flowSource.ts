/**
 * 参数取值的「来源」模型 —— 让界面不用让人手打表达式
 *
 * 问题：参数取值原本写成 `{{ input.prompt_a }}` / `{{ steps.gen.outputs }}`。
 * 这套语法对非技术人员（内容同事）不友好，而且**界面暴露的是数据结构的
 * 内部表示** —— 用户不该知道流程内部叫 `input.prompt_a`。
 *
 * 做法：界面层把「这个字段的值从哪来」拆成几个用户能理解的选择：
 *   固定值      —— 就写死一个值
 *   流程输入    —— 运行时由运营填（挑一个输入字段）
 *   上一步产物  —— 吃上游的产出（挑一个步骤，可选取第几张）
 *   再加工      —— 对取到的值做处理（去空格、截断字数…）
 *   按条件取值  —— 满足条件用一边、不满足用另一边
 *
 * 底层仍然存表达式字符串 —— 后端、Agent 接口、数据模型一行都不用改。
 * **换的是界面，不是数据。**
 *
 * **解析只有一处**：`flowExpr.ts` 是唯一真相源（parseValue / formatValue），
 * 本文件只做「结构化模型 ↔ 界面控件」的映射。两套解析器必然漂移 ——
 * 后端加了管道，前端正则认不出，用户改一下参数流程行为就悄悄变了。
 *
 * 这个模块是纯逻辑，好测：解析（字符串 → 来源）与生成（来源 → 字符串）
 * 必须严格互逆，否则用户改一下就悄悄改坏流程。
 */
import type { FlowStep, TemplateField } from '../types'
import {
  type ExprBase,
  type ExprBranch,
  type ExprTransform,
  type ExprValue,
  condOpLabel,
  formatValue,
  parseValue,
  transformLabel,
} from './flowExpr'

/** 参数取值的三种基础来源 */
export type ParamSource =
  /** 固定值：直接写死 */
  | { kind: 'literal'; value: string }
  /** 流程输入：运行时由运营填。带 index 表示取列表的第 N 项 */
  | { kind: 'input'; field: string; index?: string }
  /** 上一步产物：吃某一步的产出 */
  | { kind: 'upstream'; stepId: string; pick: 'all' | 'first' | 'last' | string }

/** 解析出来的取值 */
export interface ParsedParam {
  /**
   * 基础来源。
   *
   * **有条件时它代表「否则分支」的来源** —— 条件开启后，界面上的
   * 「取值来自」和「取到之后再处理」编辑的就是否则分支（条件不成立时
   * 用什么）。这样只有一处要编辑，不会出现「顶层一套、条件里又一套」
   * 的重复。
   */
  source: ParamSource
  /** 再加工链（同上，有条件时属于否则分支） */
  transforms: ExprTransform[]
  /** 按条件取值（没有则 undefined） */
  condition?: {
    field: string
    op: '==' | '!=' | '>' | '<' | '>=' | '<='
    value: string
    then: { source: ParamSource; transforms: ExprTransform[] }
    otherwise: { source: ParamSource; transforms: ExprTransform[] }
  }
  /** 原始字符串（用于「自定义表达式」这类无法归类的情形） */
  raw: string
  /** true = 这是混排文本或无法识别的表达式，不是纯引用 */
  isCustom: boolean
}

/** ExprBase → ParamSource（两个模型字段一致，直接转即可） */
function toSource(base: ExprBase): ParamSource {
  switch (base.kind) {
    case 'input':
      return base.index
        ? { kind: 'input', field: base.field, index: base.index }
        : { kind: 'input', field: base.field }
    case 'upstream':
      return { kind: 'upstream', stepId: base.stepId, pick: base.pick }
    default:
      return { kind: 'literal', value: base.value }
  }
}

/** ParamSource → ExprBase */
function fromSource(source: ParamSource): ExprBase {
  switch (source.kind) {
    case 'input':
      return source.index
        ? { kind: 'input', field: source.field, index: source.index }
        : { kind: 'input', field: source.field }
    case 'upstream':
      return { kind: 'upstream', stepId: source.stepId, pick: source.pick }
    default:
      return { kind: 'literal', value: source.value }
  }
}

/**
 * 把一个取值表达式解析成来源。
 *
 * 认得这些形状（其余一律 isCustom = true，界面原样保留，不静默改写）：
 *   纯文本            → literal
 *   {{ input.x }}     → input
 *   {{ steps.s.outputs }} / .first / .last → upstream
 *   {{ input.x | trim | truncate(80) }}  → input + 加工链
 *   {{ input.a == "写实" ? input.b : input.c }} → 条件
 */
export function parseParamSource(raw: string): ParsedParam {
  const text = raw ?? ''
  const value = parseValue(text)
  if (!value) {
    return {
      source: { kind: 'literal', value: text },
      transforms: [],
      raw: text,
      isCustom: true,
    }
  }
  if (value.condition) {
    // 条件存在时，界面上的「来源 / 加工」编辑的是**否则分支**
    // —— 条件不成立时用什么。这样只有一处要编辑，不会重复。
    return {
      source: toSource(value.condition.otherwise.base),
      transforms: value.condition.otherwise.transforms,
      raw: text,
      isCustom: false,
      condition: {
        field: value.condition.field,
        op: value.condition.op,
        value: value.condition.value,
        then: {
          source: toSource(value.condition.then.base),
          transforms: value.condition.then.transforms,
        },
        otherwise: {
          source: toSource(value.condition.otherwise.base),
          transforms: value.condition.otherwise.transforms,
        },
      },
    }
  }
  return {
    source: toSource(value.base),
    transforms: value.transforms,
    raw: text,
    isCustom: false,
  }
}

/**
 * 来源 → 表达式字符串。parseParamSource 的逆运算。
 *
 * **带加工或带条件时不能只靠 source 还原** —— 所以这个函数接受完整的
 * ParsedParam 而不是单个 source。旧的 `formatParamSource(source)` 保留给
 * 只关心基础来源的调用方（它会丢掉加工链，所以只在明确知道没有加工时用）。
 */
export function formatParsedParam(parsed: ParsedParam): string {
  if (parsed.condition) {
    // 条件存在：整体由条件决定。
    //
    // **顶层的 source/transforms 就是「否则分支」的**（parseParamSource
    // 读回来时就是这样映射的）—— 界面上的「取值来自」「再加工」编辑的
    // 是否则分支。所以这里用顶层的值写否则分支，不另外套一层加工
    // （套两层就会重复应用两遍）。
    const value: ExprValue = {
      base: { kind: 'literal', value: '' },
      transforms: [],
      condition: {
        field: parsed.condition.field,
        op: parsed.condition.op,
        value: parsed.condition.value,
        then: branchValue(parsed.condition.then),
        otherwise: {
          base: fromSource(parsed.source),
          transforms: parsed.transforms,
        },
      },
    }
    return formatValue(value)
  }
  const value: ExprValue = {
    base: fromSource(parsed.source),
    transforms: parsed.transforms,
  }
  return formatValue(value)
}

function branchValue(branch: { source: ParamSource; transforms: ExprTransform[] }): ExprBranch {
  return { base: fromSource(branch.source), transforms: branch.transforms }
}

/**
 * 来源 → 表达式字符串（只处理基础来源）。
 *
 * **只给「确定没有加工链」的调用方用** —— 传进来的 source 若来自带加工的
 * 表达式，格式化结果会丢掉加工链。要保留加工链就用 formatParsedParam。
 */
export function formatParamSource(source: ParamSource): string {
  return formatValue({ base: fromSource(source), transforms: [] })
}

/**
 * 来源选项 —— 给界面下拉用。
 *
 * label 是给内容同事看的说法，不出现 `input.` / `steps.` 这类内部词。
 */
export interface SourceOption {
  value: string
  label: string
  kind: 'literal' | 'input' | 'upstream' | 'custom'
}

/**
 * 步骤在界面上的名字 —— 优先显示名，退回节点名，最后才用序号。
 *
 * 裸 id（gen / edit）绝不出现在界面上 —— 内容同事看到「gen」不知道是哪一步，
 * 看到「生成参考图」才知道。
 */
export function stepTitle(
  step: FlowStep,
  index: number,
  nodeName?: string,
): string {
  const title = (step.title ?? '').trim()
  if (title) return title
  if (nodeName) return nodeName
  return `第 ${index + 1} 步`
}

/** 输入字段在界面上的名字 */
export function inputTitle(field: TemplateField): string {
  return (field.label || '').trim() || field.name
}

/**
 * 给一个参数字段列出所有可选来源。
 *
 * @param current  当前取值（用于判断「自定义」）
 * @param inputs   流程输入字段
 * @param upstreams 可作为上游的步骤（按顺序）
 * @param stepTitles 每个步骤的显示名（与 upstreams 同序）—— **不能为空**，
 *        否则下拉里会露出裸 id（gen / edit），内容同事认不出来
 */
export function buildSourceOptions(
  current: ParsedParam,
  inputs: TemplateField[],
  upstreams: FlowStep[],
  stepTitles: string[],
): SourceOption[] {
  const options: SourceOption[] = [
    { value: 'literal', label: '固定值', kind: 'literal' },
  ]
  for (const f of inputs) {
    options.push({
      value: `input:${f.name}`,
      label: `流程输入 · ${inputTitle(f)}`,
      kind: 'input',
    })
    // 列表输入在循环里要能按序号取 —— 「列表[序号]」
    if (f.multiple) {
      options.push({
        value: `input:${f.name}[index]`,
        label: `流程输入 · ${inputTitle(f)}（第 N 项）`,
        kind: 'input',
      })
    }
  }
  upstreams.forEach((s, i) => {
    options.push({
      value: `upstream:${s.id}`,
      label: `上一步 · ${stepTitles[i] ?? `第 ${i + 1} 步`}`,
      kind: 'upstream',
    })
  })
  if (current.isCustom) {
    options.push({ value: 'custom', label: '自定义表达式', kind: 'custom' })
  }
  return options
}

/**
 * 下拉的选中值 → 来源。
 *
 * 'literal' 表示「固定值」，此时 value 由调用方给的字面量决定。
 */
export function optionToSource(
  optionValue: string,
  literalValue: string,
): ParamSource | 'custom' {
  if (optionValue === 'literal') return { kind: 'literal', value: literalValue }
  if (optionValue === 'custom') return 'custom'
  if (optionValue.startsWith('input:')) {
    // 带下标的：`input:prompts[index]` —— 循环里取列表的第 N 项
    const rest = optionValue.slice('input:'.length)
    const bracket = rest.indexOf('[')
    if (bracket >= 0) {
      return {
        kind: 'input',
        field: rest.slice(0, bracket),
        index: rest.slice(bracket + 1, -1),
      }
    }
    return { kind: 'input', field: rest }
  }
  if (optionValue.startsWith('upstream:')) {
    return { kind: 'upstream', stepId: optionValue.slice('upstream:'.length), pick: 'all' }
  }
  return { kind: 'literal', value: literalValue }
}

/**
 * 当前来源在选项里的对应值 —— 让下拉能正确显示选中项。
 */
export function sourceToOption(source: ParamSource, isCustom: boolean): string {
  if (isCustom) return 'custom'
  switch (source.kind) {
    case 'input':
      return source.index ? `input:${source.field}[${source.index}]` : `input:${source.field}`
    case 'upstream':
      return `upstream:${source.stepId}`
    default:
      return 'literal'
  }
}

/**
 * 取「固定值」时该显示什么。
 *
 * 上游引用被切成 first/last 之后，上游选项得能区分 —— 所以选项值带上 pick。
 */
export function upstreamOptionValue(stepId: string, pick: string): string {
  return pick && pick !== 'all' ? `upstream:${stepId}:${pick}` : `upstream:${stepId}`
}

/** 从选项值里拆出 pick */
export function optionPick(optionValue: string): string {
  const parts = optionValue.split(':')
  return parts.length > 2 ? parts[2] : 'all'
}

/**
 * 来源的简短描述 —— 给节点卡片的参数摘要用。
 *
 * 摘要里不该出现 `input.prompt_a`，应该是「流程输入 · 初始画面」。
 */
export function describeSource(
  source: ParamSource,
  isCustom: boolean,
  inputs: TemplateField[],
  stepTitles: Record<string, string>,
): string {
  if (isCustom) return '自定义'
  switch (source.kind) {
    case 'input': {
      const field = inputs.find((f) => f.name === source.field)
      const base = field ? `流程输入 · ${inputTitle(field)}` : '流程输入'
      return source.index ? `${base}（第 N 项）` : base
    }
    case 'upstream': {
      const title = stepTitles[source.stepId]
      const base = title ? `上一步 · ${title}` : '上一步'
      if (source.pick === 'first') return `${base}（第 1 张）`
      if (source.pick === 'last') return `${base}（最后一张）`
      if (source.pick && /^\d+$/.test(source.pick)) return `${base}（第 ${Number(source.pick) + 1} 张）`
      return base
    }
    default:
      return source.value
  }
}

/**
 * 取值的完整描述 —— 摘要里要把「加工」和「条件」也说清楚。
 *
 * 只说来源不说加工会骗人：「取流程输入 · 画面甲」但实际截断了 80 字，
 * 用户看到的和跑出来的不一样。
 */
export function describeParam(
  parsed: ParsedParam,
  inputs: TemplateField[],
  stepTitles: Record<string, string>,
): string {
  if (parsed.isCustom) return '自定义'
  if (parsed.condition) {
    const c = parsed.condition
    const field = inputs.find((f) => f.name === c.field)
    const fieldLabel = field ? inputTitle(field) : '某个输入'
    const thenText = describeSource(c.then.source, false, inputs, stepTitles)
    const elseText = describeSource(c.otherwise.source, false, inputs, stepTitles)
    return `当${fieldLabel}${condOpLabel(c.op)}「${c.value}」→ ${thenText}，否则 → ${elseText}`
  }
  const base = describeSource(parsed.source, false, inputs, stepTitles)
  if (!parsed.transforms.length) return base
  const ops = parsed.transforms
    .map((t) => (t.arg !== undefined ? `${transformLabel(t.fn)} ${t.arg}` : transformLabel(t.fn)))
    .join('、')
  return `${base} → ${ops}`
}

/**
 * 判断一个引用是不是「纯引用」（整个字符串就是一个 {{...}}）。
 *
 * 界面据此决定「固定值」输入框要不要跟着显示。
 */
export function isWholeReference(raw: string): boolean {
  const text = (raw ?? '').trim()
  return text.startsWith('{{') && text.endsWith('}}')
}

/** 文本里夹着 {{ }} 的引用 —— 界面提示「这是插值表达式」 */
export function hasEmbeddedReference(raw: string): boolean {
  const text = raw ?? ''
  if (!text.includes('{{')) return false
  return !isWholeReference(text)
}

/** 从一段文本里抽出所有 {{...}} 里的路径 —— 用于提示「这段文字引用了 N 个变量」 */
export function embeddedRefs(raw: string): string[] {
  const out: string[] = []
  const re = /\{\{\s*([^{}]+?)\s*\}\}/g
  for (const m of (raw ?? '').matchAll(re)) {
    out.push(m[1].trim())
  }
  return out
}
