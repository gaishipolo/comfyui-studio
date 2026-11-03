import assert from 'node:assert/strict'
import test from 'node:test'

import {
  DEFAULT_RANDOM_MAX,
  RANDOM_SENTINEL,
  initialRandomValue,
  randomFieldInputMin,
  randomInt,
} from '../src/components/uiBehavior.ts'

/** 「随机数」字段类型的取值边界 —— Math.random() 本身不测，
 *  要测的是边界归一化：返回值必须落在区间内、必须是整数、
 *  以及被传坏的区间（反了 / NaN / Infinity / 小数）不会产出非法值。 */

test('random int always lands inside [min, max]', () => {
  for (let i = 0; i < 500; i += 1) {
    const value = randomInt(1, 6)
    assert.ok(value >= 1 && value <= 6, `越界: ${value}`)
    assert.ok(Number.isInteger(value), `不是整数: ${value}`)
  }
})

test('random int can reach both ends of a tiny range', () => {
  // 区间只有两个值时必须两个都能取到，否则说明取整方式有偏
  const seen = new Set<number>()
  for (let i = 0; i < 400; i += 1) seen.add(randomInt(3, 4))
  assert.deepEqual([...seen].sort((a, b) => a - b), [3, 4])
})

test('swapped bounds are normalized instead of producing an empty range', () => {
  for (let i = 0; i < 200; i += 1) {
    const value = randomInt(10, 2)
    assert.ok(value >= 2 && value <= 10, `交换上下界后越界: ${value}`)
  }
})

test('degenerate range returns the only value', () => {
  assert.equal(randomInt(7, 7), 7)
})

test('fractional bounds are rounded inwards', () => {
  for (let i = 0; i < 200; i += 1) {
    const value = randomInt(1.2, 4.8)
    assert.ok(value >= 2 && value <= 4, `小数边界未向内取整: ${value}`)
    assert.ok(Number.isInteger(value), `不是整数: ${value}`)
  }
})

test('non-finite bounds fall back to the default range', () => {
  for (let i = 0; i < 200; i += 1) {
    const lower = randomInt(Number.NaN, Number.POSITIVE_INFINITY)
    assert.ok(lower >= 0 && lower <= DEFAULT_RANDOM_MAX, `NaN 退化失败: ${lower}`)
  }
})

test('default range stays within JavaScript safe integers', () => {
  // 超过 2^53 的种子过一遍 JSON 会丢精度，末位被抹平 —— 随机等于没随机
  assert.ok(DEFAULT_RANDOM_MAX <= Number.MAX_SAFE_INTEGER)
  const value = randomInt(0, DEFAULT_RANDOM_MAX)
  assert.ok(Number.isSafeInteger(value), `超出安全整数范围: ${value}`)
})

/** -1 哨兵：填 -1 ＝ 每次生成自动换一个，由后端展开。
 *  输入框的 min 属性必须放行 -1，否则浏览器原生校验会直接拦住提交。 */

test('sentinel is minus one', () => {
  assert.equal(RANDOM_SENTINEL, -1)
})

test('input min allows the sentinel even when the field starts at zero', () => {
  assert.equal(randomFieldInputMin(0), RANDOM_SENTINEL)
  assert.equal(randomFieldInputMin(undefined), RANDOM_SENTINEL)
  assert.equal(randomFieldInputMin(Number.NaN), RANDOM_SENTINEL)
})

test('input min keeps a genuinely negative lower bound', () => {
  assert.equal(randomFieldInputMin(-5), -5)
  assert.equal(randomFieldInputMin(-1), -1)
  assert.equal(randomFieldInputMin(1), RANDOM_SENTINEL)
})

/** 初始值归一：模板 default 常常是字符串（存量模板里就有 "-1"）。
 *  原样留在表单里的话，提交出去是字符串，后端认不出哨兵。 */

test('initial value parses the string default from legacy templates', () => {
  assert.equal(initialRandomValue('-1', 0, 100), -1)
  assert.equal(initialRandomValue(' 42 ', 0, 100), 42)
  assert.equal(initialRandomValue('0', 0, 100), 0)
})

test('initial value generates a number when the default is unusable', () => {
  for (const raw of ['', '   ', 'abc', true, false, null, undefined, Number.NaN]) {
    const value = initialRandomValue(raw, 0, 100)
    assert.ok(Number.isInteger(value) && value >= 0 && value <= 100, `${String(raw)} → ${value}`)
  }
})

test('initial value keeps a numeric prefill as is', () => {
  assert.equal(initialRandomValue(123456789, 0, 100), 123456789)
})
