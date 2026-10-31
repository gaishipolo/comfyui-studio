import assert from 'node:assert/strict'
import test from 'node:test'

import {
  rawFailureState,
  thumbnailFailureAction,
} from '../src/components/authMediaState.ts'
import { AssetFetchError, fetchAssetBlob } from '../src/api/client.ts'
import { completeUpload } from '../src/components/uploadTransaction.ts'

async function withFetch(response, run) {
  const descriptors = {
    fetch: Object.getOwnPropertyDescriptor(globalThis, 'fetch'),
    localStorage: Object.getOwnPropertyDescriptor(globalThis, 'localStorage'),
    window: Object.getOwnPropertyDescriptor(globalThis, 'window'),
  }
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: { getItem: () => null, removeItem: () => {} },
  })
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { location: { reload: () => {} } },
  })
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => response,
  })
  try {
    await run()
  } finally {
    for (const [name, descriptor] of Object.entries(descriptors)) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else delete globalThis[name]
    }
  }
}

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

test('stale upload completion skips preview construction', async () => {
  let previewCalls = 0
  const result = await completeUpload(
    async () => 'stale-engine-value',
    () => {
      previewCalls += 1
      return 'stale-preview'
    },
    'fallback-preview',
    { isCurrent: () => false },
  )
  assert.equal(result, null)
  assert.equal(previewCalls, 0)
})

test('preview created just before invalidation is disposed', async () => {
  let checks = 0
  const disposed = []
  const result = await completeUpload(
    async () => 'engine-value',
    () => 'created-preview',
    'fallback-preview',
    {
      isCurrent: () => {
        checks += 1
        return checks === 1
      },
      disposePreview: (preview) => disposed.push(preview),
    },
  )
  assert.equal(result, null)
  assert.deepEqual(disposed, ['created-preview'])
})

test('response blob failures become status-zero AssetFetchError', async () => {
  const blobFailure = new TypeError('body stream failed')
  await withFetch(
    { status: 200, ok: true, blob: async () => Promise.reject(blobFailure) },
    async () => {
      await assert.rejects(
        fetchAssetBlob('/api/tasks/task/assets/file.png'),
        (error) => error instanceof AssetFetchError && error.status === 0,
      )
    },
  )
})

test('silent response blob failure returns null', async () => {
  await withFetch(
    { status: 200, ok: true, blob: async () => Promise.reject(new TypeError('body failed')) },
    async () => {
      assert.equal(
        await fetchAssetBlob('/api/tasks/task/assets/file.png', { silent: true }),
        null,
      )
    },
  )
})

test('response blob AbortError is preserved', async () => {
  const aborted = new DOMException('cancelled', 'AbortError')
  await withFetch(
    { status: 200, ok: true, blob: async () => Promise.reject(aborted) },
    async () => {
      await assert.rejects(
        fetchAssetBlob('/api/tasks/task/assets/file.png'),
        (error) => error === aborted,
      )
    },
  )
})
