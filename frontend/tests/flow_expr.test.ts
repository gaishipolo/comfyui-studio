/**
 * 表达式语言前端侧的单测 —— 界面不让人手打表达式，靠的就是这层转换。
 *
 * 最要紧的是 **互逆性**：parse 出来的值，format 回去必须还是同一个字符串。
 * 不互逆就意味着用户改一下参数，流程的行为悄悄变了 —— 这比报错更糟。
 */
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  COND_OPS,
  TRANSFORMS,
  condOpLabel,
  formatValue,
  parseValue,
  transformLabel,
  transformNeedsArg,
} from '../src/components/flowExpr'
import {
  describeParam,
  formatParsedParam,
  parseParamSource,
} from '../src/components/flowSource'
import type { TemplateField } from '../src/types'

const inputs: TemplateField[] = [
  { name: 'prompt_a', label: '画面甲', type: 'textarea', required: true },
  { name: 'prompt_b', label: '画面乙', type: 'textarea', required: true },
  { name: 'style', label: '画风', type: 'textarea', required: false },
]

/**
 * 表达式契约清单 —— 前端能生成的形态，后端必须都认得。
 *
 * **这是两套实现的最大风险点**：前端 flowExpr.ts 和后端 flow_expr_lang.py
 * 各有一套语法与格式化。两边对「同一个表达式怎么写」的理解一旦不一致，
 * 界面存进去的表达式后端就解析不了 —— 而且要等到跑流程、引擎都花钱了才炸。
 *
 * 所以这里把**前端会生成的全部形态**列成一份清单。后端有一份**逐字相同**
 * 的清单（`backend/tests/test_flow_expr_contract.py` 的 CANONICAL_FORMS），
 * 那边逐条断言能解析、能求值 —— **改一边就要改另一边**。
 *
 * 清单本身就是契约。
 */
export const CANONICAL_FORMS: string[] = [
  // 固定值
  '2K',
  '1024',
  // 流程输入
  '{{ input.prompt_a }}',
  '{{ input.prompts[index] }}',
  // 上游产物
  '{{ steps.gen.outputs }}',
  '{{ steps.gen.outputs.first }}',
  '{{ steps.gen.outputs.last }}',
  // 加工链
  '{{ input.prompt_a | trim }}',
  '{{ input.prompt_a | trim | truncate("80") }}',
  '{{ input.prompt_a | upper }}',
  '{{ input.prompt_a | lower }}',
  '{{ input.prompt_a | default("未填写") }}',
  '{{ input.prompt_a | len }}',
  '{{ input.prompt_a | contains("狗") }}',
  '{{ steps.gen.outputs | len }}',
  // 条件
  '{{ input.style == "写实" ? input.prompt_a : input.prompt_b }}',
  '{{ input.style != "写实" ? input.prompt_a : input.prompt_b }}',
  '{{ input.style == "写实" ? "写实提示词" : input.prompt_b }}',
  // 条件 + 整体加工
  '{{ (input.style == "写实" ? input.prompt_a : input.prompt_b) | trim }}',
]

test('前端能生成清单里的每一种形态（且就是规范形式）', () => {
  for (const raw of CANONICAL_FORMS) {
    const parsed = parseValue(raw)
    assert.ok(parsed, `解析不出来：${raw}`)
    // 关键：格式化回去必须**逐字相同** —— 不然前后端契约就对不上了
    assert.equal(formatValue(parsed!), raw, `不是规范形式：${raw}`)
  }
})

test('清单不是空壳', () => {
  assert.ok(CANONICAL_FORMS.length >= 19, '清单不该这么短')
})
// ── 解析 ──────────────────────────────────────────────

test('裸文本解析成固定值', () => {
  const v = parseValue('1024')
  assert.ok(v)
  assert.equal(v!.base.kind, 'literal')
  assert.equal(v!.base.kind === 'literal' && v!.base.value, '1024')
})

test('空值解析成固定值', () => {
  const v = parseValue('')
  assert.ok(v)
  assert.equal(v!.base.kind, 'literal')
})

test('流程输入引用解析出来', () => {
  const v = parseValue('{{ input.prompt_a }}')
  assert.ok(v)
  assert.equal(v!.base.kind, 'input')
  assert.equal(v!.base.kind === 'input' && v!.base.field, 'prompt_a')
})

test('上游产物引用解析出来', () => {
  const v = parseValue('{{ steps.gen.outputs.first }}')
  assert.ok(v)
  assert.equal(v!.base.kind, 'upstream')
  assert.equal(v!.base.kind === 'upstream' && v!.base.stepId, 'gen')
  assert.equal(v!.base.kind === 'upstream' && v!.base.pick, 'first')
})

test('带下标的流程输入解析出来', () => {
  const v = parseValue('{{ input.prompts[index] }}')
  assert.ok(v)
  assert.equal(v!.base.kind === 'input' && v!.base.index, 'index')
})

test('加工链解析出来', () => {
  const v = parseValue('{{ input.prompt_a | trim | truncate(80) }}')
  assert.ok(v)
  assert.equal(v!.transforms.length, 2)
  assert.equal(v!.transforms[0].fn, 'trim')
  assert.equal(v!.transforms[0].arg, undefined)
  assert.equal(v!.transforms[1].fn, 'truncate')
  assert.equal(v!.transforms[1].arg, '80')
})

test('条件取值解析出来', () => {
  const v = parseValue('{{ input.style == "写实" ? input.prompt_a : input.prompt_b }}')
  assert.ok(v)
  const c = v!.condition
  assert.ok(c)
  assert.equal(c!.field, 'style')
  assert.equal(c!.op, '==')
  assert.equal(c!.value, '写实')
  assert.equal(c!.then.base.kind === 'input' && c!.then.base.field, 'prompt_a')
  assert.equal(
    c!.otherwise.base.kind === 'input' && c!.otherwise.base.field,
    'prompt_b',
  )
})

test('混排文本解析不出来（保持自定义）', () => {
  assert.equal(parseValue('一只{{ input.prompt_a }}在跑'), null)
})

test('认不得的形状解析不出来', () => {
  assert.equal(parseValue('{{ steps.gen.weird }}'), null)
  assert.equal(parseValue('{{ input.a | }}'), null)
  assert.equal(parseValue('{{ "未闭合 }}'), null)
})

// ── 格式化 ──────────────────────────────────────────────

test('格式化固定值不带引号', () => {
  assert.equal(formatValue({ base: { kind: 'literal', value: '2K' }, transforms: [] }), '2K')
})

test('格式化流程输入', () => {
  assert.equal(
    formatValue({ base: { kind: 'input', field: 'prompt_a' }, transforms: [] }),
    '{{ input.prompt_a }}',
  )
})

test('格式化带加工的取值', () => {
  assert.equal(
    formatValue({
      base: { kind: 'input', field: 'prompt_a' },
      transforms: [{ fn: 'trim' }, { fn: 'truncate', arg: '80' }],
    }),
    '{{ input.prompt_a | trim | truncate("80") }}',
  )
})

test('格式化条件取值', () => {
  assert.equal(
    formatValue({
      base: { kind: 'literal', value: '' },
      transforms: [],
      condition: {
        field: 'style',
        op: '==',
        value: '写实',
        then: { base: { kind: 'input', field: 'prompt_a' }, transforms: [] },
        otherwise: { base: { kind: 'input', field: 'prompt_b' }, transforms: [] },
      },
    }),
    '{{ input.style == "写实" ? input.prompt_a : input.prompt_b }}',
  )
})

// ── 互逆性（最要紧的一条）────────────────────────────────

test('parse → format 往返保持不变', () => {
  const samples = [
    '1024',
    '2K',
    '{{ input.prompt_a }}',
    '{{ steps.gen.outputs }}',
    '{{ steps.gen.outputs.first }}',
    '{{ steps.gen.outputs.last }}',
    '{{ input.prompts[index] }}',
    '{{ input.prompt_a | trim }}',
    '{{ input.prompt_a | trim | truncate("80") }}',
    '{{ input.style == "写实" ? input.prompt_a : input.prompt_b }}',
    '{{ input.style != "写实" ? input.prompt_a : input.prompt_b }}',
  ]
  for (const raw of samples) {
    const parsed = parseValue(raw)
    assert.ok(parsed, `解析不出来了：${raw}`)
    const back = formatValue(parsed!)
    assert.equal(back, raw, `往返不一致：${raw} → ${back}`)
  }
})

test('format → parse → format 二次往返稳定', () => {
  const values = [
    { base: { kind: 'literal' as const, value: 'hello' }, transforms: [] },
    { base: { kind: 'input' as const, field: 'x' }, transforms: [] },
    {
      base: { kind: 'upstream' as const, stepId: 's1', pick: 'all' },
      transforms: [],
    },
    {
      base: { kind: 'upstream' as const, stepId: 's1', pick: 'first' },
      transforms: [],
    },
    {
      base: { kind: 'input' as const, field: 'x' },
      transforms: [{ fn: 'trim' as const }],
    },
  ]
  for (const v of values) {
    const once = formatValue(v)
    const parsed = parseValue(once)
    assert.ok(parsed)
    const twice = formatValue(parsed!)
    assert.equal(twice, once)
  }
})

test('条件里的固定值带引号（表达式里裸文本不是合法值）', () => {
  const raw = '{{ input.style == "写实" ? "写实提示词" : input.prompt_b }}'
  const parsed = parseValue(raw)
  assert.ok(parsed)
  assert.equal(formatValue(parsed!), raw)
})

test('顶层纯字面量被规范成裸文本（语义相同）', () => {
  // 规范形式里「没有加工的固定值」就是裸文本 —— 带 {{ }} 的字面量不是
  // 我们生成的形态，解析后统一成裸文本（后端求值结果一样）。
  const parsed = parseValue('{{ "abc" }}')
  assert.ok(parsed)
  assert.equal(formatValue(parsed!), 'abc')
})

// ── 界面模型（parseParamSource / formatParsedParam）─────

test('界面解析保留加工链', () => {
  const p = parseParamSource('{{ input.prompt_a | trim }}')
  assert.equal(p.isCustom, false)
  assert.equal(p.transforms.length, 1)
  assert.equal(p.transforms[0].fn, 'trim')
})

test('界面解析保留条件', () => {
  const p = parseParamSource(
    '{{ input.style == "写实" ? input.prompt_a : input.prompt_b }}',
  )
  assert.equal(p.isCustom, false)
  assert.ok(p.condition)
  assert.equal(p.condition!.field, 'style')
})

test('界面格式化保留加工链', () => {
  const p = parseParamSource('{{ input.prompt_a | trim }}')
  assert.equal(formatParsedParam(p), '{{ input.prompt_a | trim }}')
})

test('界面格式化保留条件', () => {
  const p = parseParamSource(
    '{{ input.style == "写实" ? input.prompt_a : input.prompt_b }}',
  )
  assert.equal(
    formatParsedParam(p),
    '{{ input.style == "写实" ? input.prompt_a : input.prompt_b }}',
  )
})

test('界面往返：改加工链不会把条件弄丢', () => {
  const p = parseParamSource(
    '{{ input.style == "写实" ? input.prompt_a : input.prompt_b }}',
  )
  // 有条件时，界面上的「加工链」属于**否则分支** —— 不是条件整体。
  // 否则会重复应用两遍（顶层一遍、否则分支一遍）。
  p.transforms = [{ fn: 'trim' }]
  const back = formatParsedParam(p)
  assert.ok(back.includes('input.style =='), back)
  assert.ok(back.includes('| trim'), back)
  // 加工只出现一次（否则分支），不套在条件整体外面
  assert.equal(back.match(/\| trim/g)?.length, 1, back)
  // 往返后加工链还在「否则分支」上
  const again = parseParamSource(back)
  assert.equal(again.condition?.otherwise.transforms.length, 1)
  assert.equal(again.transforms.length, 1)
})

// ── 描述（给人看的说法）────────────────────────────────

test('描述里有加工时说得清', () => {
  const p = parseParamSource('{{ input.prompt_a | trim | truncate("80") }}')
  const text = describeParam(p, inputs, {})
  assert.ok(text.includes('流程输入 · 画面甲'), text)
  assert.ok(text.includes('去掉首尾空格'), text)
  assert.ok(text.includes('截断到'), text)
  // 内部表示不能出现
  assert.ok(!text.includes('prompt_a'), text)
})

test('描述里有条件时说得清', () => {
  const p = parseParamSource(
    '{{ input.style == "写实" ? input.prompt_a : input.prompt_b }}',
  )
  const text = describeParam(p, inputs, {})
  assert.ok(text.includes('画风'), text)
  assert.ok(text.includes('等于'), text)
  assert.ok(text.includes('写实'), text)
  assert.ok(!text.includes('input.'), text)
})

test('描述自定义表达式', () => {
  const p = parseParamSource('a{{ b }}c')
  assert.equal(describeParam(p, inputs, {}), '自定义')
})

// ── 选项清单 ────────────────────────────────────────────

test('加工函数清单里有常用的那几个', () => {
  const fns = TRANSFORMS.map((t) => t.fn)
  assert.ok(fns.includes('trim'))
  assert.ok(fns.includes('truncate'))
  assert.ok(fns.includes('default'))
})

test('加工函数的参数需求说得清', () => {
  assert.equal(transformNeedsArg('trim'), false)
  assert.equal(transformNeedsArg('truncate'), true)
  assert.equal(transformNeedsArg('default'), true)
})

test('加工函数与运算符都有中文标签', () => {
  assert.equal(transformLabel('trim'), '去掉首尾空格')
  assert.equal(condOpLabel('=='), '等于')
  assert.equal(COND_OPS.length >= 2, true)
})

test('清单里没有危险函数', () => {
  for (const forbidden of ['eval', 'exec', 'open', 'import', 'getattr']) {
    assert.ok(
      !TRANSFORMS.some((t) => t.fn === forbidden),
      `不该有 ${forbidden}`,
    )
  }
})
