import assert from 'node:assert/strict'
import test from 'node:test'

import {
  rawFailureState,
  thumbnailFailureAction,
} from '../src/components/authMediaState.ts'
import { completeUpload } from '../src/components/uploadTransaction.ts'

test('only thumbnail 404 falls back to the original', () => {
  assert.equal(thumbnailFailureAction({ status: 404 }), 'fallback')
  for (const status of [0, 401, 403, 500]) {
    assert.equal(thumbnailFailureAction({ status }), 'failed')
  }
  assert.equal(thumbnailFailureAction(new TypeError('network failed')), 'failed')
})

test('raw 404 is missing and other failures are load failures', () => {
  assert.equal(rawFailureState({ status: 404 }), 'missing')
  for (const status of [0, 401, 403, 500]) assert.equal(rawFailureState({ status }), 'failed')
})

test('abort failures stay silent for thumbnail and raw requests', () => {
  const aborted = new DOMException('cancelled', 'AbortError')
  assert.equal(thumbnailFailureAction(aborted), 'aborted')
  assert.equal(rawFailureState(aborted), 'aborted')
})

test('failed forwarding never constructs or returns a new preview', async () => {
  let previewCalls = 0
  await assert.rejects(
    completeUpload(
      async () => {
        throw new Error('provider failed')
      },
      () => {
        previewCalls += 1
        return 'new-preview'
      },
      'fallback-preview',
    ),
    /provider failed/,
  )
  assert.equal(previewCalls, 0)
})

test('preview construction failure preserves the accepted engine value', async () => {
  const result = await completeUpload(
    async () => 'engine-value',
    () => {
      throw new Error('preview failed')
    },
    'name-only-preview',
  )
  assert.deepEqual(result, { value: 'engine-value', preview: 'name-only-preview' })
})
