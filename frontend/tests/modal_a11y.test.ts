import assert from 'node:assert/strict'
import test from 'node:test'

import { resolveModalKey } from '../src/components/modalA11y.ts'

test('escape closes the modal', () => {
  assert.deepEqual(resolveModalKey('Escape', false, 3, 0), { type: 'escape' })
})

test('tab moves forward and wraps back to the first control', () => {
  assert.deepEqual(resolveModalKey('Tab', false, 3, 2), { type: 'focus', index: 0 })
  assert.deepEqual(resolveModalKey('Tab', false, 3, 0), { type: 'focus', index: 1 })
})

test('shift+tab moves backward and wraps to the last control', () => {
  assert.deepEqual(resolveModalKey('Tab', true, 3, 0), { type: 'focus', index: 2 })
  assert.deepEqual(resolveModalKey('Tab', true, 3, 2), { type: 'focus', index: 1 })
})

test('other keys are left to the page', () => {
  assert.deepEqual(resolveModalKey('Enter', false, 3, 0), { type: 'none' })
  assert.deepEqual(resolveModalKey('a', false, 3, 0), { type: 'none' })
})

test('a modal without focusable controls keeps focus on the container', () => {
  assert.deepEqual(resolveModalKey('Tab', false, 0, -1), { type: 'focus', index: -1 })
  assert.deepEqual(resolveModalKey('Tab', true, 0, -1), { type: 'focus', index: -1 })
})

test('focus starts at the first control when nothing is focused yet', () => {
  assert.deepEqual(resolveModalKey('Tab', false, 3, -1), { type: 'focus', index: 0 })
  assert.deepEqual(resolveModalKey('Tab', true, 3, -1), { type: 'focus', index: 2 })
})
