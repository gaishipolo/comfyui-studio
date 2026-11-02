import assert from 'node:assert/strict'
import test from 'node:test'

import {
  assetListViewState,
  createLatestRequestGate,
  taskListViewState,
} from '../src/components/listViewState.ts'

test('load failure renders a distinct error state, not the empty state', () => {
  assert.equal(assetListViewState({ loading: false, failed: true, count: 0 }), 'error')
})

test('successful empty result renders the empty state', () => {
  assert.equal(assetListViewState({ loading: false, failed: false, count: 0 }), 'empty')
})

test('results render the list regardless of prior failure flag', () => {
  assert.equal(assetListViewState({ loading: false, failed: false, count: 3 }), 'list')
  assert.equal(assetListViewState({ loading: false, failed: true, count: 3 }), 'list')
})

test('loading wins over everything else', () => {
  assert.equal(assetListViewState({ loading: true, failed: true, count: 0 }), 'loading')
})

test('only the latest request may commit list state', () => {
  const gate = createLatestRequestGate()
  const first = gate.start()
  const second = gate.start()
  assert.equal(gate.isLatest(first), false)
  assert.equal(gate.isLatest(second), true)
})

test('task load failure is not rendered as an empty task list', () => {
  assert.equal(taskListViewState({ failed: true, count: 0 }), 'error')
  assert.equal(taskListViewState({ failed: false, count: 0 }), 'empty')
  assert.equal(taskListViewState({ failed: true, count: 2 }), 'list')
})
