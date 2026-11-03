/**
 * 参数来源模型的单测 —— 界面不让人手打表达式，靠的就是这层转换。
 *
 * 最要紧的是 **互逆性**：parse 出来的来源，format 回去必须还是同一个字符串。
 * 不互逆就意味着用户改一下参数，流程的行为悄悄变了。
 */
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  buildSourceOptions,
  describeSource,
  formatParamSource,
  hasEmbeddedReference,
  isWholeReference,
  optionPick,
  optionToSource,
  parseParamSource,
  sourceToOption,
  stepTitle,
  upstreamOptionValue,
} from '../src/components/flowSource'
import type { FlowStep, TemplateField } from '../src/types'

const inputs: TemplateField[] = [
  { name: 'prompt_a', label: '画面甲', type: 'textarea', required: true },
  { name: 'prompt_b', label: '画面乙', type: 'textarea', required: true },
]

const mkStep = (id: string, title = ''): FlowStep => ({
  id,
  title,
  type: 'template',
  template_id: 't1',
  flow_id: '',
  count: 1,
  params: {},
})

// ── 解析 ──────────────────────────────────────────────

test('纯文本解析成固定值', () => {
  const p = parseParamSource('1024')
  assert.equal(p.source.kind, 'literal')
  assert.equal(p.isCustom, false)
  assert.equal(p.source.kind === 'literal' && p.source.value, '1024')
})

test('空值解析成固定值（空）', () => {
  const p = parseParamSource('')
  assert.equal(p.source.kind, 'literal')
  assert.equal(p.isCustom, false)
})

test('input 引用解析成流程输入', () => {
  const p = parseParamSource('{{ input.prompt_a }}')
  assert.equal(p.source.kind, 'input')
  assert.equal(p.source.kind === 'input' && p.source.field, 'prompt_a')
  assert.equal(p.isCustom, false)
})

test('input 引用容忍多余空格', () => {
  const p = parseParamSource('{{input.prompt_a}}')
  assert.equal(p.source.kind, 'input')
  assert.equal(p.source.kind === 'input' && p.source.field, 'prompt_a')
})

test('steps 引用解析成上游产物', () => {
  const p = parseParamSource('{{ steps.gen.outputs }}')
  assert.equal(p.source.kind, 'upstream')
  assert.equal(p.source.kind === 'upstream' && p.source.stepId, 'gen')
  assert.equal(p.source.kind === 'upstream' && p.source.pick, 'all')
})

test('steps 引用的 .first / .last 解析出 pick', () => {
  const a = parseParamSource('{{ steps.gen.outputs.first }}')
  assert.equal(a.source.kind === 'upstream' && a.source.pick, 'first')
  const b = parseParamSource('{{ steps.gen.outputs.last }}')
  assert.equal(b.source.kind === 'upstream' && b.source.pick, 'last')
})

test('文本里夹着 {{ }} 算自定义，不被误判成引用', () => {
  const p = parseParamSource('一只{{ input.prompt_a }}在跑')
  assert.equal(p.isCustom, true)
})

test('认不得的引用形状算自定义', () => {
  const p = parseParamSource('{{ steps.gen.weird }}')
  assert.equal(p.isCustom, true)
})

// ── 生成 ──────────────────────────────────────────────

test('格式化为 input 引用', () => {
  assert.equal(
    formatParamSource({ kind: 'input', field: 'prompt_a' }),
    '{{ input.prompt_a }}',
  )
})

test('格式化为 steps 引用，pick=all 不带后缀', () => {
  assert.equal(
    formatParamSource({ kind: 'upstream', stepId: 'gen', pick: 'all' }),
    '{{ steps.gen.outputs }}',
  )
})

test('格式化为 steps 引用，pick 带后缀', () => {
  assert.equal(
    formatParamSource({ kind: 'upstream', stepId: 'gen', pick: 'first' }),
    '{{ steps.gen.outputs.first }}',
  )
})

test('固定值原样输出', () => {
  assert.equal(formatParamSource({ kind: 'literal', value: '2K' }), '2K')
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
  ]
  for (const raw of samples) {
    const parsed = parseParamSource(raw)
    if (parsed.isCustom) continue
    const back = formatParamSource(parsed.source)
    assert.equal(back, raw, `往返不一致：${raw} → ${back}`)
  }
})

test('格式会规范化引用的空格（语义不变）', () => {
  // 用户手打 {{input.x}}（没空格）→ 规范化成 {{ input.x }}。
  // 这是有意的：界面生成的永远是规范形式，用户手打的原样保留在自定义里。
  const parsed = parseParamSource('{{input.prompt_b}}')
  assert.equal(parsed.source.kind, 'input')
  assert.equal(formatParamSource(parsed.source), '{{ input.prompt_b }}')
  // 再往返一次就稳定了
  const again = parseParamSource(formatParamSource(parsed.source))
  assert.equal(formatParamSource(again.source), '{{ input.prompt_b }}')
})

test('format → parse → format 三次往返稳定', () => {
  const sources = [
    { kind: 'literal' as const, value: 'hello' },
    { kind: 'input' as const, field: 'x' },
    { kind: 'upstream' as const, stepId: 's1', pick: 'all' as const },
    { kind: 'upstream' as const, stepId: 's1', pick: 'first' as const },
  ]
  for (const src of sources) {
    const once = formatParamSource(src)
    const parsed = parseParamSource(once)
    assert.equal(parsed.isCustom, false)
    const twice = formatParamSource(parsed.source)
    assert.equal(twice, once)
  }
})

// ── 选项与来源的映射 ──────────────────────────────────

test('来源能映射回选项值', () => {
  const inputSrc = parseParamSource('{{ input.prompt_a }}').source
  assert.equal(sourceToOption(inputSrc, false), 'input:prompt_a')

  const stepSrc = parseParamSource('{{ steps.gen.outputs }}').source
  assert.equal(sourceToOption(stepSrc, false), 'upstream:gen')

  assert.equal(sourceToOption({ kind: 'literal', value: 'x' }, false), 'literal')
  assert.equal(sourceToOption({ kind: 'literal', value: 'x' }, true), 'custom')
})

test('选项值能映射回来源', () => {
  const s = optionToSource('input:prompt_a', '')
  assert.equal(s.kind === 'input' && s.field, 'prompt_a')

  const u = optionToSource('upstream:gen', '')
  assert.equal(u.kind === 'upstream' && u.stepId, 'gen')

  const lit = optionToSource('literal', '2K')
  assert.equal(lit.kind === 'literal' && lit.value, '2K')

  assert.equal(optionToSource('custom', ''), 'custom')
})

test('上游选项值能带 pick', () => {
  assert.equal(upstreamOptionValue('gen', 'all'), 'upstream:gen')
  assert.equal(upstreamOptionValue('gen', 'first'), 'upstream:gen:first')
  assert.equal(optionPick('upstream:gen:first'), 'first')
  assert.equal(optionPick('upstream:gen'), 'all')
})

// ── 选项列表 ──────────────────────────────────────────

test('选项列表包含固定值、每个流程输入、每个上游', () => {
  const parsed = parseParamSource('')
  const options = buildSourceOptions(
    parsed,
    inputs,
    [mkStep('gen'), mkStep('edit')],
    ['第 1 步', '第 2 步'],
  )
  const values = options.map((o) => o.value)
  assert.ok(values.includes('literal'))
  assert.ok(values.includes('input:prompt_a'))
  assert.ok(values.includes('input:prompt_b'))
  assert.ok(values.includes('upstream:gen'))
  assert.ok(values.includes('upstream:edit'))
})

test('选项标签不出现内部标识', () => {
  const parsed = parseParamSource('')
  const options = buildSourceOptions(parsed, inputs, [mkStep('gen')], ['生成参考图'])
  const labels = options.map((o) => o.label).join(' | ')
  assert.ok(!labels.includes('input.'), `不该出现 input.：${labels}`)
  assert.ok(!labels.includes('steps.'), `不该出现 steps.：${labels}`)
  assert.ok(labels.includes('流程输入 · 画面甲'))
  assert.ok(labels.includes('上一步 · 生成参考图'))
})

test('没有上游时选项只有固定值和流程输入', () => {
  const parsed = parseParamSource('')
  const options = buildSourceOptions(parsed, inputs, [], [])
  assert.equal(options.length, 3) // 固定值 + 两个流程输入
  assert.ok(!options.some((o) => o.kind === 'upstream'))
})

test('自定义表达式时才多一个 custom 选项', () => {
  const plain = buildSourceOptions(parseParamSource('x'), inputs, [], [])
  assert.ok(!plain.some((o) => o.value === 'custom'))

  const custom = buildSourceOptions(parseParamSource('a{{ b }}c'), inputs, [], [])
  assert.ok(custom.some((o) => o.value === 'custom'))
})

// ── 描述（给人看的说法）────────────────────────────────

test('描述用标签而不是字段名', () => {
  const src = parseParamSource('{{ input.prompt_a }}').source
  const text = describeSource(src, false, inputs, {})
  assert.equal(text, '流程输入 · 画面甲')
  assert.ok(!text.includes('prompt_a'))
})

test('描述上游时带上步骤名与取哪张', () => {
  const titles = { gen: '生成参考图' }
  const all = describeSource(
    { kind: 'upstream', stepId: 'gen', pick: 'all' },
    false,
    inputs,
    titles,
  )
  assert.equal(all, '上一步 · 生成参考图')

  const first = describeSource(
    { kind: 'upstream', stepId: 'gen', pick: 'first' },
    false,
    inputs,
    titles,
  )
  assert.equal(first, '上一步 · 生成参考图（第 1 张）')
})

test('自定义表达式描述为「自定义」', () => {
  const src = parseParamSource('a{{ b }}c').source
  assert.equal(describeSource(src, true, inputs, {}), '自定义')
})

test('列表输入能被识别成带序号的来源', () => {
  const listInputs: TemplateField[] = [
    { name: 'prompts', label: '提示词列表', type: 'textarea', required: true, multiple: true },
  ]
  const parsed = parseParamSource('{{ input.prompts[index] }}')
  assert.equal(parsed.source.kind, 'input')
  assert.equal(parsed.source.kind === 'input' && parsed.source.field, 'prompts')
  assert.equal(parsed.source.kind === 'input' && parsed.source.index, 'index')
  assert.equal(parsed.isCustom, false)
})

test('带序号的输入能往返', () => {
  const raw = '{{ input.prompts[index] }}'
  const parsed = parseParamSource(raw)
  assert.equal(formatParamSource(parsed.source), raw)
})

test('带序号的来源能映射回选项值', () => {
  const parsed = parseParamSource('{{ input.prompts[index] }}')
  assert.equal(sourceToOption(parsed.source, false), 'input:prompts[index]')
  const back = optionToSource('input:prompts[index]', '')
  assert.equal(back.kind === 'input' && back.field, 'prompts')
  assert.equal(back.kind === 'input' && back.index, 'index')
})

test('列表输入多一个「第 N 项」选项', () => {
  const listInputs: TemplateField[] = [
    { name: 'prompts', label: '提示词列表', type: 'textarea', required: true, multiple: true },
    { name: 'style', label: '画风', type: 'textarea', required: false },
  ]
  const options = buildSourceOptions(parseParamSource(''), listInputs, [], [])
  const values = options.map((o) => o.value)
  assert.ok(values.includes('input:prompts'))
  assert.ok(values.includes('input:prompts[index]'))
  // 不是列表的字段没有「第 N 项」
  assert.ok(!values.includes('input:style[index]'))
})

test('带序号的输入在摘要里说明是第几项', () => {
  const listInputs: TemplateField[] = [
    { name: 'prompts', label: '提示词列表', type: 'textarea', required: true, multiple: true },
  ]
  const parsed = parseParamSource('{{ input.prompts[index] }}')
  const text = describeSource(parsed.source, false, listInputs, {})
  assert.ok(text.includes('提示词列表'), text)
  assert.ok(text.includes('第 N 项'), text)
})

// ── 步骤标题 ──────────────────────────────────────────

test('步骤标题优先显示名，空则退回序号', () => {
  assert.equal(stepTitle(mkStep('gen', '生成参考图'), 0), '生成参考图')
  assert.equal(stepTitle(mkStep('gen'), 2), '第 3 步')
  assert.equal(stepTitle(mkStep('gen', '   '), 1), '第 2 步')
})

// ── 整体引用判定 ──────────────────────────────────────

test('整体引用与内嵌引用区分得开', () => {
  assert.equal(isWholeReference('{{ input.x }}'), true)
  assert.equal(isWholeReference('a {{ input.x }}'), false)
  assert.equal(hasEmbeddedReference('a {{ input.x }}'), true)
  assert.equal(hasEmbeddedReference('{{ input.x }}'), false)
  assert.equal(hasEmbeddedReference('plain'), false)
})
