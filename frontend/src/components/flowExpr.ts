/**
 * 流程参数的取值表达式 —— 前端侧（**唯一真相源**）
 *
 * 后端有一套受限表达式语言（管道 / 比较 / 条件），见
 * `backend/app/services/flow_expr_lang.py`。前端这边**不是**把语法原样
 * 暴露给用户 —— 上一轮已经定了原则：界面层不让人手打 `{{ input.prompt_a }}`。
 *
 * 所以这里的职责是：
 *   1. **解析**：把存好的表达式读回结构化模型（界面要显示「取流程输入 X，
 *      再截断到 80 字」）
 *   2. **格式化**：把结构化模型写回表达式（存进数据库的仍是表达式字符串，
 *      后端一行没动）
 *   3. **给选项**：列出可选的加工函数和条件运算符，配中文标签
 *
 * **只有这一处解析器** —— 界面层（flowSource.ts）基于它做转换，不再自己
 * 写一套正则。两套解析器必然漂移：后端加了管道，前端正则认不出，
 * 用户改一下参数流程行为就悄悄变了。
 *
 * **只支持我们自己生成的规范形式**（canonical form）。认不出来的
 * （用户手写的复杂表达式）→ 返回 null，界面按「自定义表达式」原样保留，
 * 不静默改写 —— 这是上一轮定的规矩。
 *
 * 规范形式：
 *   固定值            `一段文字`
 *   流程输入          `{{ input.提示词 }}`
 *   上一步产物        `{{ steps.gen.outputs }}` / `.first` / `.last`
 *   带加工            `{{ input.提示词 | trim | truncate("80") }}`
 *   条件取值          `{{ input.风格 == "写实" ? input.提示词A : input.提示词B }}`
 *   条件 + 整体加工    `{{ (input.风格 == "写实" ? input.提示词A : input.提示词B) | trim }}`
 *
 * 加工链里的参数一律写成带引号的字符串（长度也是字符串，数字在比较时自动转换）。
 */

/** 取值的基础来源 */
export type ExprBase =
  /** 固定值：直接写死一段文字 */
  | { kind: 'literal'; value: string }
  /** 流程输入：运行时由运营填 */
  | { kind: 'input'; field: string; index?: string }
  /** 上一步产物：吃某一步的产出 */
  | { kind: 'upstream'; stepId: string; pick: string }

/** 一个加工步骤（管道里的一节） */
export interface ExprTransform {
  /** 函数名 —— 必须在后端白名单里 */
  fn: string
  /** 参数原文（不含引号）。没有参数的函数不带这个键 */
  arg?: string
}

/** 条件的一个分支 */
export interface ExprBranch {
  base: ExprBase
  transforms: ExprTransform[]
}

/**
 * 解析出来的分支 —— 可能是普通分支，也可能是一个「分组」。
 *
 * 分组 = 括号里装着一个完整条件，外面再接加工链：
 *   `(input.style == "写实" ? input.a : input.b) | trim`
 *
 * 单独一个类型而不是往 ExprBranch 上塞隐藏字段 —— 隐藏字段靠类型断言
 * 撑着，早晚有人改错一边。
 */
export type ParsedBranch =
  | { kind: 'branch'; branch: ExprBranch }
  | { kind: 'group'; condition: ExprCondition; transforms: ExprTransform[] }

/** 条件取值：判断某个流程输入，满足就用一边、不满足用另一边 */
export interface ExprCondition {
  /** 判断依据的流程输入字段 */
  field: string
  /** 运算符 */
  op: CondOp
  /** 比较值 */
  value: string
  /** 成立时取什么 */
  then: ExprBranch
  /** 不成立时取什么 */
  otherwise: ExprBranch
}

/** 完整的一个参数取值 */
export interface ExprValue {
  /** 基础来源 */
  base: ExprBase
  /** 加工链（按顺序应用） */
  transforms: ExprTransform[]
  /**
   * 条件取值 —— 有它时整体由条件决定。
   *
   * `base`/`transforms` 在有条件时只用于回显（界面上显示「基础取值」用的）。
   */
  condition?: ExprCondition
}

export type CondOp = '==' | '!=' | '>' | '<' | '>=' | '<='

/** 可用的加工函数 —— 与后端白名单一致 */
export interface TransformSpec {
  fn: string
  /** 中文标签 */
  label: string
  /** 参数标签（没有参数则为 undefined） */
  argLabel?: string
  /** 参数占位提示 */
  argPlaceholder?: string
}

/**
 * 加工函数清单。
 *
 * **只列内容同事真会用得上的** —— 全量 18 个函数摆出来是技术自豪感，
 * 不是可用性。后端仍然全量支持（Agent 可以用），界面只给常用的。
 */
export const TRANSFORMS: TransformSpec[] = [
  { fn: 'trim', label: '去掉首尾空格' },
  { fn: 'truncate', label: '截断到', argLabel: '字数', argPlaceholder: '80' },
  { fn: 'upper', label: '转成大写' },
  { fn: 'lower', label: '转成小写' },
  { fn: 'default', label: '为空时用', argLabel: '这个值', argPlaceholder: '未填写' },
  { fn: 'len', label: '算长度' },
  { fn: 'contains', label: '包含', argLabel: '这段文字', argPlaceholder: '关键词' },
]

/** 条件运算符 —— 界面给等号类就够，字符串比大小不好解释 */
export const COND_OPS: { op: CondOp; label: string }[] = [
  { op: '==', label: '等于' },
  { op: '!=', label: '不等于' },
]

/** 加工函数需要参数吗 */
export function transformNeedsArg(fn: string): boolean {
  return Boolean(TRANSFORMS.find((t) => t.fn === fn)?.argLabel)
}

// ── 词法 ──────────────────────────────────────────────────────────────

type Token =
  | { t: 'ident'; v: string }
  | { t: 'string'; v: string }
  | { t: 'number'; v: string }
  | { t: 'op'; v: string }
  | { t: 'punct'; v: string }
  | { t: 'eof' }

const PUNCT = new Set(['|', '(', ')', ',', ':', '?', '.', '[', ']'])

function tokenize(src: string): Token[] | null {
  const out: Token[] = []
  let i = 0
  while (i < src.length) {
    const ch = src[i]
    if (ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r') {
      i += 1
      continue
    }
    if (ch === '"' || ch === "'") {
      const quote = ch
      let j = i + 1
      let buf = ''
      let closed = false
      while (j < src.length) {
        if (src[j] === '\\' && j + 1 < src.length) {
          const nxt = src[j + 1]
          const mapped: Record<string, string> = {
            n: '\n',
            t: '\t',
            r: '\r',
            '\\': '\\',
            '"': '"',
            "'": "'",
          }
          buf += mapped[nxt] ?? nxt
          j += 2
          continue
        }
        if (src[j] === quote) {
          closed = true
          j += 1
          break
        }
        buf += src[j]
        j += 1
      }
      if (!closed) return null // 引号没闭合
      out.push({ t: 'string', v: buf })
      i = j
      continue
    }
    if (/[0-9]/.test(ch)) {
      let j = i
      while (j < src.length && /[0-9.]/.test(src[j])) j += 1
      out.push({ t: 'number', v: src.slice(i, j) })
      i = j
      continue
    }
    // 标识符：ASCII 字母/下划线，或任何非 ASCII 字母（中文）。
    // **首字符不能是数字** —— 数字单独走 number 分支。
    // 字段名是用户起的，用中文很自然（界面标签本来就是中文），
    // 所以这里必须认中文标识符，否则中文字段名在表达式里没法引用。
    if (/[A-Za-z_]/.test(ch) || (ch.charCodeAt(0) > 127 && /[\p{L}_]/u.test(ch))) {
      let j = i
      while (j < src.length && /[A-Za-z0-9_\u0080-\uffff]/.test(src[j])) j += 1
      out.push({ t: 'ident', v: src.slice(i, j) })
      i = j
      continue
    }
    const two = src.slice(i, i + 2)
    if (two === '==' || two === '!=' || two === '>=' || two === '<=') {
      out.push({ t: 'op', v: two })
      i += 2
      continue
    }
    if (ch === '>' || ch === '<') {
      out.push({ t: 'op', v: ch })
      i += 1
      continue
    }
    if (PUNCT.has(ch)) {
      out.push({ t: 'punct', v: ch })
      i += 1
      continue
    }
    return null // 不认识的字符
  }
  out.push({ t: 'eof' })
  return out
}

// ── 解析 ──────────────────────────────────────────────────────────────

class Parser {
  pos = 0
  constructor(private tokens: Token[]) {}

  get cur(): Token {
    return this.tokens[this.pos]
  }
  advance(): Token {
    return this.tokens[this.pos++]
  }
  isPunct(v: string): boolean {
    return this.cur.t === 'punct' && this.cur.v === v
  }

  /**
   * 解析一个分支 —— 基础来源 + 加工链。
   *
   * 也支持「分组」：`(条件)` 整体再接加工链。
   */
  parseBranch(): ParsedBranch | null {
    // 分组：括号里是一个完整条件，外面再接加工链
    if (this.isPunct('(')) {
      this.advance()
      const cond = parseCondition(this)
      if (!cond) return null
      if (!this.isPunct(')')) return null
      this.advance()
      const outer = this.parseTransforms()
      if (!outer) return null
      return { kind: 'group', condition: cond, transforms: outer }
    }
    const base = this.parseBase()
    if (!base) return null
    const transforms = this.parseTransforms()
    if (!transforms) return null
    return { kind: 'branch', branch: { base, transforms } }
  }

  private parseBase(): ExprBase | null {
    const tok = this.cur
    // 字符串/数字字面量 —— 条件的分支里可以写死一个值：
    //   {{ input.style == "写实" ? "写实提示词" : input.art }}
    if (tok.t === 'string') {
      this.advance()
      return { kind: 'literal', value: tok.v }
    }
    if (tok.t === 'number') {
      this.advance()
      return { kind: 'literal', value: tok.v }
    }
    if (tok.t !== 'ident') return null
    if (tok.v === 'input') {
      this.advance()
      if (!this.isPunct('.')) return null
      this.advance()
      const name = this.advance()
      if (name.t !== 'ident') return null
      // 可选下标 input.prompts[index]
      if (this.isPunct('[')) {
        this.advance()
        const idx = this.advance()
        if (idx.t !== 'ident') return null
        if (!this.isPunct(']')) return null
        this.advance()
        return { kind: 'input', field: name.v, index: idx.v }
      }
      return { kind: 'input', field: name.v }
    }
    if (tok.v === 'steps') {
      this.advance()
      if (!this.isPunct('.')) return null
      this.advance()
      const sid = this.advance()
      if (sid.t !== 'ident') return null
      if (!this.isPunct('.')) return null
      this.advance()
      const kw = this.advance()
      if (kw.t !== 'ident' || kw.v !== 'outputs') return null
      // 可选取第几张：.first / .last / .0
      let pick = 'all'
      if (this.isPunct('.')) {
        this.advance()
        const p = this.advance()
        if (p.t !== 'ident' && p.t !== 'number') return null
        pick = p.v
      }
      return { kind: 'upstream', stepId: sid.v, pick }
    }
    return null
  }

  private parseTransforms(): ExprTransform[] | null {
    const out: ExprTransform[] = []
    while (this.isPunct('|')) {
      this.advance()
      const fn = this.advance()
      if (fn.t !== 'ident') return null
      const item: ExprTransform = { fn: fn.v }
      if (this.isPunct('(')) {
        this.advance()
        const arg = this.advance()
        if (arg.t !== 'string' && arg.t !== 'number') return null
        item.arg = arg.v
        if (!this.isPunct(')')) return null
        this.advance()
      }
      out.push(item)
    }
    return out
  }
}

/**
 * 解析一个取值表达式。
 *
 * 返回 null = **认不出来**（用户手写的复杂表达式）→ 界面按「自定义」原样保留，
 * 不静默改写。
 */
export function parseValue(raw: string): ExprValue | null {
  const text = (raw ?? '').trim()
  if (!text) return { base: { kind: 'literal', value: '' }, transforms: [] }

  // 裸文本 → 固定值
  if (!text.startsWith('{{')) {
    // 里面夹着 {{ }} 的混排文本 → 认不出来（界面要保留原样）
    if (text.includes('{{')) return null
    return { base: { kind: 'literal', value: raw ?? '' }, transforms: [] }
  }
  if (!text.endsWith('}}')) return null
  const inner = text.slice(2, -2).trim()
  if (!inner) return null

  const tokens = tokenize(inner)
  if (!tokens) return null
  const parser = new Parser(tokens)

  // 条件形式：input.<字段> <op> "<值>" ? <分支> : <分支>
  const cond = parseCondition(parser)
  if (cond) {
    if (parser.cur.t !== 'eof') return null
    return { base: { kind: 'literal', value: '' }, transforms: [], condition: cond }
  }

  // 普通形式（含分组：`(条件) | 加工`）
  const parsed = parser.parseBranch()
  if (!parsed) return null
  if (parser.cur.t !== 'eof') return null
  if (parsed.kind === 'group') {
    return {
      base: { kind: 'literal', value: '' },
      transforms: parsed.transforms,
      condition: parsed.condition,
    }
  }
  return { base: parsed.branch.base, transforms: parsed.branch.transforms }
}

function parseCondition(parser: Parser): ExprCondition | null {
  // 从头判断是不是 input.<字段> <op> "<值>" ? ...
  const start = parser.pos
  const tok = parser.cur
  if (tok.t !== 'ident' || tok.v !== 'input') return null
  parser.advance()
  if (!parser.isPunct('.')) {
    parser.pos = start
    return null
  }
  parser.advance()
  const name = parser.advance()
  if (name.t !== 'ident') {
    parser.pos = start
    return null
  }
  const opTok = parser.cur
  if (opTok.t !== 'op') {
    parser.pos = start
    return null
  }
  parser.advance()
  const valTok = parser.cur
  if (valTok.t !== 'string') {
    parser.pos = start
    return null
  }
  parser.advance()
  if (!parser.isPunct('?')) {
    parser.pos = start
    return null
  }
  parser.advance()
  const thenParsed = parser.parseBranch()
  if (!thenParsed || thenParsed.kind !== 'branch') return null
  if (!parser.isPunct(':')) return null
  parser.advance()
  const elseParsed = parser.parseBranch()
  if (!elseParsed || elseParsed.kind !== 'branch') return null
  return {
    field: name.v,
    op: opTok.v as CondOp,
    value: valTok.v,
    then: thenParsed.branch,
    otherwise: elseParsed.branch,
  }
}

// ── 格式化 ────────────────────────────────────────────────────────────

function quote(text: string): string {
  return `"${text.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`
}

function formatBase(base: ExprBase): string {
  switch (base.kind) {
    case 'input':
      return base.index
        ? `input.${base.field}[${base.index}]`
        : `input.${base.field}`
    case 'upstream': {
      const suffix = base.pick && base.pick !== 'all' ? `.${base.pick}` : ''
      return `steps.${base.stepId}.outputs${suffix}`
    }
    default:
      return quote(base.value)
  }
}

function formatTransforms(transforms: ExprTransform[]): string {
  if (!transforms.length) return ''
  return (
    ' ' +
    transforms
      .map((t) => (t.arg === undefined ? `| ${t.fn}` : `| ${t.fn}(${quote(t.arg)})`))
      .join(' ')
  )
}

/** 分支 → 表达式片段 */
function formatBranch(branch: ExprBranch): string {
  return formatBase(branch.base) + formatTransforms(branch.transforms)
}

/**
 * 分支 → 表达式片段（对外）。
 *
 * **与顶层的区别**：顶层固定值是裸文本（`2K`），但分支里的固定值必须带引号
 * （`{{ input.a == "x" ? "用这个" : input.b }}`）—— 表达式里裸文本不是合法值。
 */
export function formatBranchValue(branch: ExprBranch): string {
  return formatBranch(branch)
}

/** 条件本体 → `input.a == "x" ? b : c` */
function formatCondition(cond: ExprCondition): string {
  return (
    `input.${cond.field} ${cond.op} ${quote(cond.value)} ` +
    `? ${formatBranch(cond.then)} : ${formatBranch(cond.otherwise)}`
  )
}

/** 取值模型 → 表达式字符串。parseValue 的逆运算。 */
export function formatValue(value: ExprValue): string {
  if (value.condition) {
    const body = formatCondition(value.condition)
    // 条件外面还有加工链 → 用括号把条件括起来（后端支持分组）
    if (value.transforms.length) {
      return `{{ (${body})${formatTransforms(value.transforms)} }}`
    }
    return `{{ ${body} }}`
  }
  if (value.base.kind === 'literal') {
    // 固定值：没有加工链就写裸文本（和旧行为一致），有加工链才进 {{ }}
    if (!value.transforms.length) return value.base.value
    return `{{ ${quote(value.base.value)}${formatTransforms(value.transforms)} }}`
  }
  return `{{ ${formatBase(value.base)}${formatTransforms(value.transforms)} }}`
}

/** 加工函数的中文标签 */
export function transformLabel(fn: string): string {
  return TRANSFORMS.find((t) => t.fn === fn)?.label ?? fn
}

/** 运算符的中文标签 */
export function condOpLabel(op: CondOp): string {
  return COND_OPS.find((o) => o.op === op)?.label ?? op
}
