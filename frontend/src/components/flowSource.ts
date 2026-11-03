/**
 * 参数取值的「来源」模型 —— 让界面不用让人手打表达式
 *
 * 问题：参数取值原本写成 `{{ input.prompt_a }}` / `{{ steps.gen.outputs }}`。
 * 这套语法对非技术人员（内容同事）不友好，而且**界面暴露的是数据结构的
 * 内部表示** —— 用户不该知道流程内部叫 `input.prompt_a`。
 *
 * 做法：界面层把「这个字段的值从哪来」拆成三个用户能理解的选择：
 *   固定值      —— 就写死一个值
 *   流程输入    —— 运行时由运营填（挑一个输入字段）
 *   上一步产物  —— 吃上游的产出（挑一个步骤，可选取第几张）
 *
 * 底层仍然存表达式字符串 —— 后端、Agent 接口、数据模型一行都不用改。
 * **换的是界面，不是数据。**
 *
 * 这个模块是纯逻辑，好测：解析（字符串 → 来源）与生成（来源 → 字符串）
 * 必须严格互逆，否则用户改一下就悄悄改坏流程。
 */
import type { FlowStep, TemplateField } from '../types'

/** 整体就是一个 `{{ ... }}`（前后没有别的文本）→ 求值后保留原始类型 */
const WHOLE_REF = /^\{\{\s*([^{}]+?)\s*\}\}$/

/** 文本中夹着 `{{ ... }}` → 按字符串插值处理 */
const PART_REF = /\{\{\s*([^{}]+?)\s*\}\}/g

/** 输入引用：{{ input.xxx }} */
const INPUT_REF = /^input\.([A-Za-z0-9_]+)$/
/** 带下标的输入引用：input.prompts[index] */
const INPUT_INDEX_REF = /^input\.([A-Za-z0-9_]+)\[([A-Za-z0-9_]+)\]$/
/** 步骤引用：{{ steps.<id>.outputs }} 或 {{ steps.<id>.outputs.first }} */
const STEP_REF = /^steps\.([A-Za-z0-9_]+)\.outputs(?:\.(first|last|\d+))?$/

/** 参数取值的三种来源 */
export type ParamSource =
  /** 固定值：直接写死 */
  | { kind: 'literal'; value: string }
  /** 流程输入：运行时由运营填。带 index 表示取列表的第 N 项 */
  | { kind: 'input'; field: string; index?: string }
  /** 上游产物：吃某一步的产出 */
  | { kind: 'upstream'; stepId: string; pick: 'all' | 'first' | 'last' | string }

export interface ParsedParam {
  source: ParamSource
  /** 原始字符串（用于「自定义表达式」这类无法归类的情形） */
  raw: string
  /** true = 这是混排文本或无法识别的表达式，不是纯引用 */
  isCustom: boolean
}

/**
 * 把一个取值表达式解析成来源。
 *
 * 认得三种形状：
 *   纯文本            → literal
 *   {{ input.x }}     → input
 *   {{ steps.s.outputs }} / .first / .last → upstream
 *
 * 认不得的（文本里夹着 {{ }}、或手写的复杂表达式）→ isCustom = true，
 * 界面原样交给用户编辑，但**不让它被静默改写**。
 */
export function parseParamSource(raw: string): ParsedParam {
  const text = raw ?? ''
  const whole = text.trim().match(WHOLE_REF)
  if (whole) {
    const path = whole[1].trim()
    const input = path.match(INPUT_REF)
    if (input) {
      return { source: { kind: 'input', field: input[1] }, raw: text, isCustom: false }
    }
    const indexed = path.match(INPUT_INDEX_REF)
    if (indexed) {
      return {
        source: { kind: 'input', field: indexed[1], index: indexed[2] },
        raw: text,
        isCustom: false,
      }
    }
    const step = path.match(STEP_REF)
    if (step) {
      return {
        source: {
          kind: 'upstream',
          stepId: step[1],
          pick: step[2] ?? 'all',
        },
        raw: text,
        isCustom: false,
      }
    }
    // 认不得的引用形状 —— 当自定义处理，别乱改
    return { source: { kind: 'literal', value: text }, raw: text, isCustom: true }
  }
  // 文本里夹着 {{ }} —— 插值表达式，同样当自定义
  if (text.includes('{{')) {
    return { source: { kind: 'literal', value: text }, raw: text, isCustom: true }
  }
  return { source: { kind: 'literal', value: text }, raw: text, isCustom: false }
}

/** 来源 → 表达式字符串。parseParamSource 的逆运算。 */
export function formatParamSource(source: ParamSource): string {
  switch (source.kind) {
    case 'input':
      return source.index
        ? `{{ input.${source.field}[${source.index}] }}`
        : `{{ input.${source.field} }}`
    case 'upstream': {
      const suffix = source.pick && source.pick !== 'all' ? `.${source.pick}` : ''
      return `{{ steps.${source.stepId}.outputs${suffix} }}`
    }
    default:
      return source.value
  }
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
 * 判断一个引用是不是「纯引用」（整个字符串就是一个 {{...}}）。
 *
 * 界面据此决定「固定值」输入框要不要跟着显示。
 */
export function isWholeReference(raw: string): boolean {
  return WHOLE_REF.test((raw ?? '').trim())
}

/** 文本里夹着 {{ }} 的引用 —— 界面提示「这是插值表达式」 */
export function hasEmbeddedReference(raw: string): boolean {
  const text = raw ?? ''
  if (!text.includes('{{')) return false
  return !WHOLE_REF.test(text.trim())
}

/** 从一段文本里抽出所有 {{...}} 里的路径 —— 用于提示「这段文字引用了 N 个变量」 */
export function embeddedRefs(raw: string): string[] {
  const out: string[] = []
  for (const m of (raw ?? '').matchAll(PART_REF)) {
    out.push(m[1].trim())
  }
  return out
}
