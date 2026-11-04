/** 自动接上（同名绑定）的规则 —— 与后端 `flow_bindings.py` 逐字对应
 *
 * **两边规则必须一样**，否则界面显示「接上了流程输入·提示词」，实际跑的却是
 * 别的值 —— 这种错不会报错，只会让用户拿到错的图。
 *
 * 后端对应 `backend/tests/test_flow_bindings.py`，清单逐条对齐。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  autoBinding,
  matchInput,
  matchUpstream,
  sameName,
  stepOutputName,
} from '../src/components/flowBindings.ts'
import type { TemplateField } from '../src/types/index.ts'

const field = (name: string, label: string, type = 'textarea'): TemplateField =>
  ({ name, label, type } as TemplateField)

test('标签相同算同名', () => {
  assert.equal(sameName('提示词', 'prompt', '提示词', 'input1'), true)
})

test('标签不同不算同名', () => {
  assert.equal(sameName('提示词', 'prompt', '画面', 'input1'), false)
})

test('标签空时看内部标识', () => {
  assert.equal(sameName('', 'prompt', '', 'prompt'), true)
})

test('标签有值时不会退化到内部标识', () => {
  // 标签是人的意图。标识相同却标签不同，说明本来就是两回事
  assert.equal(sameName('提示词', 'prompt', '别的', 'prompt'), false)
})

test('字段标签能匹配到流程输入', () => {
  const hit = matchInput({ name: 'prompt', label: '提示词' }, [field('input1', '提示词')])
  assert.ok(hit)
  assert.equal(hit?.name, 'input1')
})

test('匹配不到时返回 null', () => {
  const hit = matchInput({ name: 'prompt', label: '风格' }, [field('input1', '提示词')])
  assert.equal(hit, null)
})

test('字段标签能匹配到上游产出名', () => {
  const hit = matchUpstream({ name: 'image', label: '参考图' }, { 参考图: 's1' })
  assert.ok(hit)
  assert.equal(hit?.step_id ?? hit?.stepId, 's1')
  assert.equal(hit?.name, '参考图')
})

test('自动优先接流程输入', () => {
  const hit = autoBinding(
    { name: 'prompt', label: '提示词', type: 'textarea' },
    [field('input1', '提示词')],
    { 提示词: 's1' },
  )
  assert.equal(hit?.kind, 'input')
})

test('流程输入对不上时接上游产出', () => {
  const hit = autoBinding(
    { name: 'image', label: '参考图', type: 'file_upload' },
    [field('input1', '提示词')],
    { 参考图: 's1' },
  )
  assert.equal(hit?.kind, 'upstream')
})

test('文本字段接不了上游产物', () => {
  // 文本字段喂一堆素材路径没有意义 —— 不接，也不报错
  const hit = autoBinding(
    { name: 'prompt', label: '参考图', type: 'textarea' },
    [],
    { 参考图: 's1' },
  )
  assert.equal(hit, null)
})

test('都对不上时不接（用节点默认值）', () => {
  const hit = autoBinding({ name: 'seed', label: '种子', type: 'number' }, [], {})
  assert.equal(hit, null)
})

test('产出名默认取步骤标题', () => {
  assert.equal(stepOutputName({ title: '生成参考图' }), '生成参考图')
  assert.equal(stepOutputName({ title: '生成参考图', output_name: '参考图' }), '参考图')
  assert.equal(stepOutputName({}), '')
})
