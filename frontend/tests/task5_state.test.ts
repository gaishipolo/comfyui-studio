import assert from 'node:assert/strict'
import test from 'node:test'

import {
  rawFailureState,
  thumbnailFailureAction,
} from '../src/components/authMediaState.ts'
import {
  AssetFetchError,
  fetchAssetBlob,
  getStoredUser,
  login,
  openAssetInNewTab,
  uploadInput,
  uploadPlugin,
  uploadWorkflow,
} from '../src/api/client.ts'
import { completeUpload } from '../src/components/uploadTransaction.ts'
import {
  applyTemplateEngine,
  normalizeTemplateEngineFields,
} from '../src/components/templateFormState.ts'
import { resolveModalKey } from '../src/components/modalA11y.ts'
import {
  clearTaskStatusHistory,
  localMonthStart,
  missingRequiredList,
  missingRequiredUpload,
  normalizeBatchCount,
  taskPollDelay,
} from '../src/components/uiBehavior.ts'

async function withFetch(response, run, options = {}) {
  const descriptors = {
    fetch: Object.getOwnPropertyDescriptor(globalThis, 'fetch'),
    localStorage: Object.getOwnPropertyDescriptor(globalThis, 'localStorage'),
    window: Object.getOwnPropertyDescriptor(globalThis, 'window'),
  }
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key) => key === 'user' ? (options.userRaw ?? null) : (options.token ?? null),
      removeItem: (key) => options.removed?.push(key),
    },
  })
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { location: { reload: () => options.onReload?.() } },
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

test('asset tab is opened synchronously before the authenticated fetch resolves', async () => {
  let releaseFetch
  const fetchPending = new Promise((resolve) => { releaseFetch = resolve })
  const popup = { opener: {}, location: { href: '' }, closeCalled: false, close() { this.closeCalled = true } }
  const descriptors = {
    fetch: Object.getOwnPropertyDescriptor(globalThis, 'fetch'),
    localStorage: Object.getOwnPropertyDescriptor(globalThis, 'localStorage'),
    window: Object.getOwnPropertyDescriptor(globalThis, 'window'),
    URL: Object.getOwnPropertyDescriptor(globalThis, 'URL'),
    setTimeout: Object.getOwnPropertyDescriptor(globalThis, 'setTimeout'),
  }
  let opened = 0
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: { getItem: () => 'token' } })
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { open: () => { opened += 1; return popup }, location: { reload: () => {} } },
  })
  Object.defineProperty(globalThis, 'URL', {
    configurable: true,
    value: { createObjectURL: () => 'blob:asset', revokeObjectURL: () => {} },
  })
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => { await fetchPending; return new Response(new Blob(['ok']), { status: 200 }) },
  })
  Object.defineProperty(globalThis, 'setTimeout', {
    configurable: true,
    value: () => 0,
  })
  try {
    const pending = openAssetInNewTab('/asset')
    assert.equal(opened, 1, 'window.open must happen before the first await')
    releaseFetch()
    await pending
    assert.equal(popup.location.href, 'blob:asset')
    assert.equal(popup.opener, null)
  } finally {
    for (const [name, descriptor] of Object.entries(descriptors)) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else delete globalThis[name]
    }
  }
})

test('task polling backs off when no task is active', () => {
  assert.equal(taskPollDelay([]), 15_000)
  assert.equal(taskPollDelay([{ status: 'done' }]), 15_000)
  assert.equal(taskPollDelay([{ status: 'pending' }]), 3_000)
  assert.equal(taskPollDelay([{ status: 'running' }]), 3_000)
})

test('required upload fields are validated before submitting', () => {
  const fields = [
    { name: 'image', type: 'file_upload', required: true },
    { name: 'optional', type: 'file_upload', required: false },
  ]
  assert.equal(missingRequiredUpload(fields, {}), 'image')
  assert.equal(missingRequiredUpload(fields, { image: '' }), 'image')
  assert.equal(missingRequiredUpload(fields, { image: [] }), 'image')
  assert.equal(missingRequiredUpload(fields, { image: 'engine-file.png' }), null)
})

test('month shortcuts use local calendar components instead of UTC ISO date', () => {
  const localNovember = {
    getFullYear: () => 2026,
    getMonth: () => 10,
    // UTC+8 本地 11-01 00:30 对应 UTC 10-31；旧实现 toISOString 会差一天
    toISOString: () => '2026-10-31T16:30:00.000Z',
  } as Date
  assert.equal(localMonthStart(0, localNovember), '2026-11-01')
  assert.equal(localMonthStart(-1, localNovember), '2026-10-01')
})

test('local month start handles year rollover', () => {
  const january = {
    getFullYear: () => 2026,
    getMonth: () => 0,
  } as Date
  assert.equal(localMonthStart(-1, january), '2025-12-01')
})

test('login 401 keeps backend credential error without clearing or reloading', async () => {
  let reloads = 0
  const removed = []
  await withFetch(
    {
      status: 401,
      ok: false,
      json: async () => ({ detail: 'Invalid username or password' }),
    },
    async () => {
      await assert.rejects(login('admin', 'wrong'), /Invalid username or password/)
    },
    { token: 'stale-token', removed, onReload: () => { reloads += 1 } },
  )
  assert.equal(reloads, 0)
  assert.deepEqual(removed, [])
})

test('multipart upload 401 clears auth and reloads consistently', async () => {
  const file = new File(['x'], 'x.png', { type: 'image/png' })
  for (const upload of [
    () => uploadWorkflow(file),
    () => uploadInput(file),
    () => uploadPlugin(file),
  ]) {
    let reloads = 0
    const removed = []
    await withFetch(
      { status: 401, ok: false, json: async () => ({ detail: 'unauthorized' }) },
      async () => {
        await assert.rejects(upload(), /登录已过期/)
      },
      { token: 'expired-token', removed, onReload: () => { reloads += 1 } },
    )
    assert.equal(reloads, 1)
    assert.deepEqual(removed, ['token', 'user'])
  }
})

test('corrupt stored user JSON is cleared instead of crashing App render', async () => {
  const removed = []
  await withFetch(
    { status: 200, ok: true },
    async () => {
      assert.equal(getStoredUser(), null)
    },
    { userRaw: '{broken-json', removed },
  )
  assert.deepEqual(removed, ['user'])
})

test('batch count is rounded and clamped before submit', () => {
  assert.equal(normalizeBatchCount(3.5), 4)
  assert.equal(normalizeBatchCount(0), 1)
  assert.equal(normalizeBatchCount(99), 32)
  assert.equal(normalizeBatchCount(Number.NaN), 1)
})

test('required non-file lists reject empty arrays and empty rows', () => {
  const fields = [
    { name: 'prompts', type: 'text', required: true, multiple: true },
    { name: 'optional', type: 'text', required: false, multiple: true },
  ]
  assert.equal(missingRequiredList(fields, { prompts: [] }), 'prompts')
  assert.equal(missingRequiredList(fields, { prompts: ['', null] }), 'prompts')
  assert.equal(missingRequiredList(fields, { prompts: ['hello'] }), null)
})

test('logout clears previous task status history', () => {
  const history = new Map([['old-user-task', 'running']])
  clearTaskStatusHistory(history)
  assert.equal(history.size, 0)
})

test('switching away from ComfyUI clears hidden workflow configuration', () => {
  const form = {
    engine: 'comfyui',
    capability: 'text_to_image',
    workflow_file: 'workflow.json',
    param_mapping: { prompt: '1.inputs.text' },
    output_config: { nodes: ['9'] },
  }
  const switched = applyTemplateEngine(form, 'generic_api')
  assert.deepEqual(switched, {
    engine: 'generic_api',
    capability: '',
    workflow_file: '',
    param_mapping: {},
    output_config: { nodes: [] },
  })
})

test('non-ComfyUI save payload is defensively normalized', () => {
  const normalized = normalizeTemplateEngineFields({
    engine: 'generic_api',
    workflow_file: 'stale.json',
    param_mapping: { prompt: '1.inputs.text' },
    output_config: { nodes: ['9'] },
  })
  assert.equal(normalized.workflow_file, '')
  assert.deepEqual(normalized.param_mapping, {})
  assert.deepEqual(normalized.output_config, { nodes: [] })
})

test('modal focus navigation wraps in both directions', () => {
  assert.deepEqual(resolveModalKey('Tab', true, 3, 0), { type: 'focus', index: 2 })
  assert.deepEqual(resolveModalKey('Tab', false, 3, 2), { type: 'focus', index: 0 })
  assert.deepEqual(resolveModalKey('Tab', false, 3, 1), { type: 'focus', index: 2 })
  assert.deepEqual(resolveModalKey('Tab', false, 3, -1), { type: 'focus', index: 0 })
  assert.deepEqual(resolveModalKey('Tab', true, 3, -1), { type: 'focus', index: 2 })
})
