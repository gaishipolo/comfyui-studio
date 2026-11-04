/** 流程编排器的纯逻辑测试 —— 拖动排序不能破坏依赖是重点 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  isOrderValid,
  nodeVisual,
  paramSummary,
  reorderSteps,
  stepDependencies,
  wouldBreakDependency,
} from '../src/components/flowEditor.ts'
import type { FlowStep, NodeCapability } from '../src/types/index.ts'

const step = (id: string, params: Record<string, string> = {}): FlowStep => ({
  id,
  type: 'template',
  template_id: `tpl-${id}`,
  flow_id: '',
  count: 1,
  params,
})

const REF = '{{ steps.gen.outputs }}'

test('stepDependencies 解析上游引用', () => {
  const steps = [
    step('gen'),
    step('edit', { image: REF }),
    step('retouch', { image: REF, base: '{{ input.base }}' }),
  ]
  assert.deepEqual(stepDependencies(steps, 'edit'), ['gen'])
  assert.deepEqual(stepDependencies(steps, 'retouch'), ['gen'])
  assert.deepEqual(stepDependencies(steps, 'gen'), [])
})

test('stepDependencies 认出取单张与子流程具名产出', () => {
  const steps = [
    step('gen'),
    step('sub'),
    step('edit', {
      image: '{{ steps.gen.outputs.first }}',
      ref: '{{ steps.sub.final_image }}',
    }),
  ]
  assert.deepEqual(stepDependencies(steps, 'edit').sort(), ['gen', 'sub'])
})

test('isOrderValid 认出顺序被破坏的情况', () => {
  const good = [step('gen'), step('edit', { image: REF })]
  assert.equal(isOrderValid(good), true)

  // 引用者排在上游前面 = 依赖断了
  const broken = [step('edit', { image: REF }), step('gen')]
  assert.equal(isOrderValid(broken), false)
})

test('reorderSteps 移动位置', () => {
  const steps = [step('a'), step('b'), step('c')]
  assert.deepEqual(
    reorderSteps(steps, 0, 2).map((s) => s.id),
    ['b', 'c', 'a'],
  )
  // 越界不炸
  assert.equal(reorderSteps(steps, 0, 99).length, 3)
  assert.equal(reorderSteps(steps, -1, 1).length, 3)
})

test('wouldBreakDependency 拦住会断链的拖动', () => {
  const steps = [step('gen'), step('edit', { image: REF })]
  // 把 edit 拖到 gen 前面 → 断链，要拦住
  assert.equal(wouldBreakDependency(steps, 1, 0), true)
  // 往下拖（无依赖的两项互换）→ 允许
  const free = [step('a'), step('b')]
  assert.equal(wouldBreakDependency(free, 0, 1), false)
  // 原地不动不算破坏
  assert.equal(wouldBreakDependency(steps, 0, 0), false)
})

test('paramSummary 对子流程说人话', () => {
  const sub: FlowStep = {
    ...step('sub'),
    type: 'flow',
    template_id: '',
    flow_id: 'flow-1',
  }
  assert.equal(paramSummary(sub, null), '输入按同名自动传')
  assert.equal(
    paramSummary({ ...sub, params: { prompt: '{{ input.prompt }}' } }, null),
    '已单独设置 1 个输入',
  )
})

test('nodeVisual 按能力给图标', () => {
  const mk = (over: Partial<NodeCapability>): NodeCapability => ({
    id: 'x',
    name: '',
    description: '',
    category: 'image',
    engine: 'e',
    capability: '',
    fields: [],
    ...over,
  })
  assert.equal(nodeVisual(mk({ capability: 'text_to_image' })).tone, 'image')
  assert.equal(nodeVisual(mk({ capability: 'image_to_video' })).tone, 'video')
  assert.equal(nodeVisual(mk({ name: '某某配音' })).tone, 'audio')
  assert.equal(nodeVisual(null).tone, 'generic')
})

test('paramSummary 摘出参数摘要', () => {
  const cap: NodeCapability = {
    id: 'tpl',
    name: '文生图',
    description: '',
    category: 'image',
    engine: 'e',
    capability: 'text_to_image',
    fields: [
      { name: 'prompt', label: '提示词', type: 'textarea', required: true, multiple: false },
      { name: 'width', label: '宽度', type: 'select', required: false, multiple: false },
    ],
  }
  const s = step('gen', { prompt: '{{ input.prompt }}', width: '1024' })
  // 摘要用「流程输入 · 标签」的说法，不露 input.prompt 这种内部标识
  const inputs = [
    { name: 'prompt', label: '初始画面', type: 'textarea' as const, required: true },
  ]
  const summary = paramSummary(s, cap, inputs, {})
  assert.ok(summary.includes('提示词'))
  assert.ok(summary.includes('流程输入 · 初始画面'))
  assert.ok(!summary.includes('input.prompt'))
  assert.ok(summary.includes('1024'))
  // 没设参数时要说清楚
  assert.equal(paramSummary(step('x'), cap), '未设置参数')
})
