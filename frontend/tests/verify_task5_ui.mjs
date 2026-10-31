// One-time runner dependency: npm install --no-save --package-lock=false playwright@1.64.1
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromium } from 'playwright'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const PORT = 41735
const BASE_URL = `http://127.0.0.1:${PORT}`
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
  'base64',
)

const USER = {
  id: 'owner',
  username: 'owner',
  display_name: 'Owner',
  role: 'member',
  is_active: true,
  created_at: '2026-10-31T00:00:00',
}

const imageFile = (name) => ({ name, mimeType: 'image/png', buffer: PNG })

function template(fields) {
  return {
    id: 'template-task-5',
    name: 'Task 5 atomic form',
    description: 'intercepted browser verification',
    category: 'image',
    icon: '',
    input_schema: { fields },
    engine: 'fake',
    capability: 'text_to_image',
    workflow_file: 'task-5.json',
    param_mapping: {},
    output_config: { nodes: ['1'] },
    is_active: true,
    sort_order: 0,
    created_at: '2026-10-31T00:00:00',
    updated_at: '2026-10-31T00:00:00',
  }
}

function asset(fileName = 'asset.png') {
  return {
    id: 'asset-task-5',
    task_id: 'asset-task',
    file_path: `asset-task/${fileName}`,
    file_name: fileName,
    file_size: PNG.length,
    mime_type: 'image/png',
    tags: [],
    meta: {},
    is_shared: false,
    created_by: 'owner',
    creator_name: 'Owner',
    created_at: '2026-10-31T00:00:00',
  }
}

function deferred() {
  let resolvePromise
  const promise = new Promise((resolveValue) => {
    resolvePromise = resolveValue
  })
  return { promise, resolve: resolvePromise }
}

async function waitUntil(page, predicate, message, timeout = 5000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    if (await predicate()) return
    await page.waitForTimeout(25)
  }
  throw new Error(message)
}

async function json(route, value, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
}

async function startPreview() {
  const child = spawn(
    process.execPath,
    [
      resolve(ROOT, 'node_modules/vite/bin/vite.js'),
      'preview',
      '--host',
      '127.0.0.1',
      '--port',
      String(PORT),
      '--strictPort',
    ],
    { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] },
  )
  let output = ''
  child.stdout.on('data', (chunk) => {
    output += chunk
  })
  child.stderr.on('data', (chunk) => {
    output += chunk
  })

  const deadline = Date.now() + 15_000
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`Vite preview exited early:\n${output}`)
    try {
      const response = await fetch(BASE_URL)
      if (response.ok) return child
    } catch {
      // Preview is still starting.
    }
    await new Promise((resolveValue) => setTimeout(resolveValue, 100))
  }
  throw new Error(`Vite preview did not start:\n${output}`)
}

async function stopPreview(child) {
  if (child.exitCode !== null) return
  if (process.platform === 'win32') {
    await new Promise((resolveValue) => {
      const killer = spawn('taskkill', ['/pid', String(child.pid), '/t', '/f'], { stdio: 'ignore' })
      killer.on('exit', resolveValue)
      killer.on('error', resolveValue)
    })
  } else {
    child.kill('SIGTERM')
  }
}

async function openForm(browser, { fields, assets = [], apiHandler }) {
  const context = await browser.newContext()
  const page = await context.newPage()
  const submitted = []
  const dialogs = []
  const consoleErrors = []

  page.on('console', (message) => {
    if (
      message.type() === 'error' &&
      !message.text().startsWith('Failed to load resource: the server responded with a status of') &&
      message.text() !== 'Failed to load resource: net::ERR_FAILED'
    ) {
      consoleErrors.push(message.text())
    }
  })
  page.on('pageerror', (error) => consoleErrors.push(error.message))
  page.on('dialog', async (dialog) => {
    dialogs.push(dialog.message())
    await dialog.dismiss()
  })

  await page.addInitScript((storedUser) => {
    if (!sessionStorage.getItem('task-5-auth-seeded')) {
      localStorage.setItem('token', 'task-5-token')
      localStorage.setItem('user', JSON.stringify(storedUser))
      sessionStorage.setItem('task-5-auth-seeded', 'true')
    }
    window.__assetFetchSignals = []
    window.__objectUrlRecords = []
    const nativeCreateObjectUrl = URL.createObjectURL.bind(URL)
    const nativeRevokeObjectUrl = URL.revokeObjectURL.bind(URL)
    URL.createObjectURL = (object) => {
      const url = nativeCreateObjectUrl(object)
      window.__objectUrlRecords.push({ url, revoked: false })
      return url
    }
    URL.revokeObjectURL = (url) => {
      for (const record of window.__objectUrlRecords) {
        if (record.url === url) record.revoked = true
      }
      nativeRevokeObjectUrl(url)
    }
    const nativeFetch = window.fetch.bind(window)
    window.fetch = (input, init = {}) => {
      const url = typeof input === 'string' ? input : input.url
      if (url.includes('/assets/')) {
        const record = { url, hasSignal: Boolean(init.signal), aborted: Boolean(init.signal?.aborted) }
        init.signal?.addEventListener(
          'abort',
          () => {
            record.aborted = true
          },
          { once: true },
        )
        window.__assetFetchSignals.push(record)
      }
      return nativeFetch(input, init)
    }
  }, USER)

  const activeTemplate = template(fields)
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (apiHandler && (await apiHandler({ route, request, url, page }))) return

    if (url.pathname === '/api/templates') return json(route, [activeTemplate])
    if (url.pathname === '/api/health') {
      return json(route, { status: 'ok', comfyui: 'connected' })
    }
    if (url.pathname === '/api/engines') {
      return json(route, [{ name: 'fake', status: 'connected' }])
    }
    if (url.pathname === '/api/assets') return json(route, assets)
    if (url.pathname === '/api/tasks' && request.method() === 'GET') return json(route, [])
    if (url.pathname === '/api/tasks' && request.method() === 'POST') {
      submitted.push(request.postDataJSON())
      return json(route, [])
    }
    if (url.pathname.startsWith('/api/tasks/') && url.pathname.includes('/assets/')) {
      return json(route, { detail: 'not found' }, 404)
    }
    return json(route, { detail: `Unhandled intercepted API: ${url.pathname}` }, 500)
  })

  await page.goto(BASE_URL)
  await page.getByText(activeTemplate.name, { exact: true }).click()
  return { context, page, submitted, dialogs, consoleErrors }
}

async function closeChecked(state) {
  await state.context.close()
  assert.deepEqual(state.consoleErrors, [], `browser console errors: ${state.consoleErrors.join('\n')}`)
}

async function testSingleReplacementIsAtomic(browser) {
  const secondStarted = deferred()
  const releaseSecond = deferred()
  let attempts = 0
  const state = await openForm(browser, {
    fields: [
      { name: 'single', label: 'Single image', type: 'file_upload', accept: 'image', required: false },
    ],
    apiHandler: async ({ route, url }) => {
      if (url.pathname !== '/api/upload/file') return false
      attempts += 1
      if (attempts === 1) {
        await json(route, { filename: 'engine-old.png', engine: 'fake', kind: 'image' })
      } else {
        secondStarted.resolve()
        await releaseSecond.promise
        await json(route, { detail: 'provider rejected replacement' }, 502)
      }
      return true
    },
  })

  try {
    const input = state.page.locator('#upload-single')
    await input.setInputFiles(imageFile('old.png'))
    await state.page.locator('img[alt="old.png"]').waitFor()
    const oldSrc = await state.page.locator('img[alt="old.png"]').getAttribute('src')

    await input.setInputFiles(imageFile('replacement.png'))
    await secondStarted.promise
    const oldVisibleWhilePending = await state.page.locator('img[alt="old.png"]').count()
    const replacementVisibleWhilePending = await state.page.locator('img[alt="replacement.png"]').count()

    releaseSecond.resolve()
    await waitUntil(state.page, () => Promise.resolve(state.dialogs.length === 1), 'failure alert missing')
    assert.equal(oldVisibleWhilePending, 1, 'pending replacement hid the previous preview')
    assert.equal(replacementVisibleWhilePending, 0, 'pending replacement published a new preview')
    assert.equal(await state.page.locator('img[alt="old.png"]').count(), 1)
    assert.equal(await state.page.locator('img[alt="old.png"]').getAttribute('src'), oldSrc)

    await state.page.getByRole('button', { name: '开始生成' }).click()
    await waitUntil(state.page, () => Promise.resolve(state.submitted.length === 1), 'form was not submitted')
    assert.equal(state.submitted[0].params.single, 'engine-old.png')
  } finally {
    releaseSecond.resolve()
    await closeChecked(state)
  }
}

async function testMultipleUploadsStayAligned(browser) {
  const firstStarted = deferred()
  const releaseFirst = deferred()
  let attempts = 0
  const state = await openForm(browser, {
    fields: [
      {
        name: 'multiple',
        label: 'Multiple images',
        type: 'file_upload',
        accept: 'image',
        required: false,
        multiple: true,
      },
    ],
    apiHandler: async ({ route, url }) => {
      if (url.pathname !== '/api/upload/file') return false
      attempts += 1
      if (attempts === 1) {
        firstStarted.resolve()
        await releaseFirst.promise
        await json(route, { filename: 'engine-first.png' })
      } else if (attempts === 2) {
        await json(route, { detail: 'provider rejected middle item' }, 502)
      } else {
        await json(route, { filename: 'engine-third.png' })
      }
      return true
    },
  })

  try {
    await state.page.locator('#upload-multiple').setInputFiles([
      imageFile('first.png'),
      imageFile('failed.png'),
      imageFile('third.png'),
    ])
    await firstStarted.promise
    const previewCountWhilePending = await state.page.locator('img[alt="first.png"]').count()
    releaseFirst.resolve()

    await waitUntil(state.page, () => Promise.resolve(attempts === 3), 'not all uploads ran')
    await waitUntil(state.page, () => Promise.resolve(state.dialogs.length === 1), 'middle failure alert missing')
    await state.page.locator('img[alt="third.png"]').waitFor()
    assert.equal(previewCountWhilePending, 0, 'multi upload published before engine acceptance')
    assert.equal(await state.page.locator('img[alt="failed.png"]').count(), 0)
    assert.equal(await state.page.locator('img[alt="first.png"]').count(), 1)
    assert.equal(await state.page.locator('img[alt="third.png"]').count(), 1)

    await state.page.getByRole('button', { name: '开始生成' }).click()
    await waitUntil(state.page, () => Promise.resolve(state.submitted.length === 1), 'form was not submitted')
    assert.deepEqual(state.submitted[0].params.multiple, [
      'engine-first.png',
      'engine-third.png',
    ])
  } finally {
    releaseFirst.resolve()
    await closeChecked(state)
  }
}

async function testFailedAssetReplacementKeepsOldState(browser) {
  const forwardStarted = deferred()
  const releaseForward = deferred()
  let rawRequests = 0
  let thumbRequests = 0
  const pickedAsset = asset()
  const state = await openForm(browser, {
    fields: [
      { name: 'single', label: 'Single asset', type: 'file_upload', accept: 'image', required: false },
    ],
    assets: [pickedAsset],
    apiHandler: async ({ route, url }) => {
      if (url.pathname === '/api/upload/file') {
        await json(route, { filename: 'engine-old.png' })
        return true
      }
      if (url.pathname === '/api/upload/from-asset') {
        forwardStarted.resolve()
        await releaseForward.promise
        await json(route, { detail: 'provider rejected asset' }, 502)
        return true
      }
      if (url.pathname.endsWith('/thumb')) {
        thumbRequests += 1
        await route.fulfill({ status: 200, contentType: 'image/png', body: PNG })
        return true
      }
      if (url.pathname.includes('/assets/')) {
        rawRequests += 1
        await route.fulfill({ status: 200, contentType: 'image/png', body: PNG })
        return true
      }
      return false
    },
  })

  try {
    const field = state.page.locator('#upload-single').locator('..')
    await state.page.locator('#upload-single').setInputFiles(imageFile('old.png'))
    await state.page.locator('img[alt="old.png"]').waitFor()
    const oldSrc = await state.page.locator('img[alt="old.png"]').getAttribute('src')

    await field.getByRole('button', { name: /从素材库/ }).click()
    await state.page.locator('button[title="asset.png"]').waitFor()
    await waitUntil(state.page, () => Promise.resolve(thumbRequests > 0), 'picker thumbnail missing')
    const rawBefore = rawRequests
    const thumbBefore = thumbRequests
    await state.page.locator('button[title="asset.png"]').click()
    await forwardStarted.promise

    const rawBeforeAcceptance = rawRequests - rawBefore
    const thumbBeforeAcceptance = thumbRequests - thumbBefore
    const oldVisibleWhilePending = await state.page.locator('img[alt="old.png"]').count()
    releaseForward.resolve()
    await waitUntil(state.page, () => Promise.resolve(state.dialogs.length === 1), 'asset failure alert missing')

    assert.equal(rawBeforeAcceptance, 0, 'raw asset preview downloaded before forwarding succeeded')
    assert.equal(thumbBeforeAcceptance, 0, 'new thumbnail rendered before forwarding succeeded')
    assert.equal(oldVisibleWhilePending, 1, 'pending asset replacement hid old preview')
    assert.equal(await state.page.locator('img[alt="old.png"]').count(), 1)
    assert.equal(await state.page.locator('img[alt="old.png"]').getAttribute('src'), oldSrc)

    await state.page.getByRole('button', { name: '开始生成' }).click()
    await waitUntil(state.page, () => Promise.resolve(state.submitted.length === 1), 'form was not submitted')
    assert.equal(state.submitted[0].params.single, 'engine-old.png')
  } finally {
    releaseForward.resolve()
    await closeChecked(state)
  }
}

async function testAcceptedAssetUsesThumbWithoutRollingBackValue(browser) {
  let forwarded = false
  let rawAfterForward = 0
  let thumbAfterForward = 0
  const pickedAsset = asset('accepted.png')
  const state = await openForm(browser, {
    fields: [
      { name: 'single', label: 'Accepted asset', type: 'file_upload', accept: 'image', required: false },
    ],
    assets: [pickedAsset],
    apiHandler: async ({ route, url }) => {
      if (url.pathname === '/api/upload/from-asset') {
        forwarded = true
        await json(route, { filename: 'engine-accepted.png', engine: 'fake' })
        return true
      }
      if (url.pathname.endsWith('/thumb')) {
        if (forwarded) {
          thumbAfterForward += 1
          await json(route, { detail: 'thumbnail failed' }, 500)
        } else {
          await route.fulfill({ status: 200, contentType: 'image/png', body: PNG })
        }
        return true
      }
      if (url.pathname.includes('/assets/')) {
        if (forwarded) rawAfterForward += 1
        await route.fulfill({ status: 200, contentType: 'image/png', body: PNG })
        return true
      }
      return false
    },
  })

  try {
    const field = state.page.locator('#upload-single').locator('..')
    await field.getByRole('button', { name: /从素材库/ }).click()
    await state.page.locator('button[title="accepted.png"]').waitFor()
    await state.page.locator('button[title="accepted.png"]').click()

    await waitUntil(state.page, () => Promise.resolve(thumbAfterForward > 0), 'accepted asset did not prefer thumb')
    await state.page.getByText('素材加载失败', { exact: true }).waitFor()
    assert.equal(rawAfterForward, 0, 'thumbnail server failure fell back to the raw asset')

    await state.page.getByRole('button', { name: '开始生成' }).click()
    await waitUntil(state.page, () => Promise.resolve(state.submitted.length === 1), 'form was not submitted')
    assert.equal(state.submitted[0].params.single, 'engine-accepted.png')
  } finally {
    await closeChecked(state)
  }
}

async function openMediaScenario(browser, { thumb, raw }) {
  let thumbRequests = 0
  let rawRequests = 0
  const item = asset('status.png')
  const state = await openForm(browser, {
    fields: [
      { name: 'single', label: 'Media status', type: 'file_upload', accept: 'image', required: false },
    ],
    assets: [item],
    apiHandler: async ({ route, url }) => {
      if (url.pathname.endsWith('/thumb')) {
        thumbRequests += 1
        await thumb(route)
        return true
      }
      if (url.pathname.includes('/assets/')) {
        rawRequests += 1
        await raw(route)
        return true
      }
      return false
    },
  })
  const field = state.page.locator('#upload-single').locator('..')
  await field.getByRole('button', { name: /从素材库/ }).click()
  return {
    ...state,
    counts: () => ({ thumb: thumbRequests, raw: rawRequests }),
  }
}

async function testMediaStatuses(browser) {
  const cases = [
    {
      name: 'thumbnail 404 falls back to raw exactly once',
      thumb: (route) => json(route, { detail: 'missing thumb' }, 404),
      raw: (route) => route.fulfill({ status: 200, contentType: 'image/png', body: PNG }),
      verify: async (state) => {
        await state.page.locator('img[alt="status.png"]').waitFor()
        assert.deepEqual(state.counts(), { thumb: 1, raw: 1 })
      },
    },
    {
      name: 'thumbnail 500 is load-failed without raw fallback',
      thumb: (route) => json(route, { detail: 'broken thumb' }, 500),
      raw: (route) => route.fulfill({ status: 200, contentType: 'image/png', body: PNG }),
      verify: async (state) => {
        await state.page.getByText('素材加载失败', { exact: true }).waitFor()
        assert.deepEqual(state.counts(), { thumb: 1, raw: 0 })
      },
    },
    {
      name: 'thumbnail 403 is load-failed without raw fallback',
      thumb: (route) => json(route, { detail: 'forbidden thumb' }, 403),
      raw: (route) => route.fulfill({ status: 200, contentType: 'image/png', body: PNG }),
      verify: async (state) => {
        await state.page.getByText('素材加载失败', { exact: true }).waitFor()
        assert.deepEqual(state.counts(), { thumb: 1, raw: 0 })
      },
    },
    {
      name: 'thumbnail network failure is load-failed without raw fallback',
      thumb: (route) => route.abort('failed'),
      raw: (route) => route.fulfill({ status: 200, contentType: 'image/png', body: PNG }),
      verify: async (state) => {
        await state.page.getByText('素材加载失败', { exact: true }).waitFor()
        assert.deepEqual(state.counts(), { thumb: 1, raw: 0 })
      },
    },
    {
      name: 'raw 404 is missing',
      thumb: (route) => json(route, { detail: 'missing thumb' }, 404),
      raw: (route) => json(route, { detail: 'missing raw' }, 404),
      verify: async (state) => {
        await state.page.getByText('文件已丢失', { exact: true }).waitFor()
        assert.deepEqual(state.counts(), { thumb: 1, raw: 1 })
      },
    },
    {
      name: 'raw 500 is load-failed',
      thumb: (route) => json(route, { detail: 'missing thumb' }, 404),
      raw: (route) => json(route, { detail: 'broken raw' }, 500),
      verify: async (state) => {
        await state.page.getByText('素材加载失败', { exact: true }).waitFor()
        assert.deepEqual(state.counts(), { thumb: 1, raw: 1 })
      },
    },
    {
      name: 'raw network failure is load-failed',
      thumb: (route) => json(route, { detail: 'missing thumb' }, 404),
      raw: (route) => route.abort('failed'),
      verify: async (state) => {
        await state.page.getByText('素材加载失败', { exact: true }).waitFor()
        assert.deepEqual(state.counts(), { thumb: 1, raw: 1 })
      },
    },
    {
      name: 'decode failure does not retry or fall back',
      thumb: (route) =>
        route.fulfill({ status: 200, contentType: 'image/jpeg', body: Buffer.from('not-an-image') }),
      raw: (route) => route.fulfill({ status: 200, contentType: 'image/png', body: PNG }),
      verify: async (state) => {
        await state.page.getByText('素材加载失败', { exact: true }).waitFor()
        assert.deepEqual(state.counts(), { thumb: 1, raw: 0 })
        const records = await state.page.evaluate(() => window.__objectUrlRecords)
        assert(records.length > 0, 'decode fixture did not create an object URL')
        assert(records.every((record) => record.revoked), 'decode failure leaked an object URL')
      },
    },
  ]

  for (const scenario of cases) {
    const state = await openMediaScenario(browser, scenario)
    try {
      await scenario.verify(state)
    } catch (error) {
      error.message = `${scenario.name}: ${error.message}`
      throw error
    } finally {
      await closeChecked(state)
    }
  }

  const unauthorized = await openMediaScenario(browser, {
    thumb: (route) => json(route, { detail: 'expired' }, 401),
    raw: (route) => route.fulfill({ status: 200, contentType: 'image/png', body: PNG }),
  })
  try {
    try {
      await unauthorized.page.getByText('登录以继续使用', { exact: true }).waitFor({ timeout: 5000 })
    } catch (error) {
      const evidence = await unauthorized.page.evaluate(() => ({
        href: window.location.href,
        token: localStorage.getItem('token'),
        user: localStorage.getItem('user'),
        body: document.body.innerText.slice(0, 500),
      }))
      throw new Error(`401 evidence: ${JSON.stringify({ ...evidence, counts: unauthorized.counts() })}`, {
        cause: error,
      })
    }
    assert.deepEqual(unauthorized.counts(), { thumb: 1, raw: 0 })
  } finally {
    await closeChecked(unauthorized)
  }
}

async function testObjectUrlRevokedOnUnmount(browser) {
  const state = await openMediaScenario(browser, {
    thumb: (route) => route.fulfill({ status: 200, contentType: 'image/png', body: PNG }),
    raw: (route) => route.fulfill({ status: 200, contentType: 'image/png', body: PNG }),
  })
  try {
    await state.page.locator('img[alt="status.png"]').waitFor()
    let records = await state.page.evaluate(() => window.__objectUrlRecords)
    assert(records.some((record) => !record.revoked), 'loaded media URL was not active')
    await state.page.getByRole('button', { name: '关闭素材选择' }).click()
    await waitUntil(
      state.page,
      async () => {
        records = await state.page.evaluate(() => window.__objectUrlRecords)
        return records.length > 0 && records.every((record) => record.revoked)
      },
      'unmount leaked an object URL',
    )
    assert.deepEqual(state.counts(), { thumb: 1, raw: 0 })
  } finally {
    await closeChecked(state)
  }
}

async function testAbortOnUnmount(browser) {
  const requestStarted = deferred()
  const releaseRequest = deferred()
  const state = await openForm(browser, {
    fields: [
      { name: 'single', label: 'Abort media', type: 'file_upload', accept: 'image', required: false },
    ],
    assets: [asset('abort.png')],
    apiHandler: async ({ route, url }) => {
      if (!url.pathname.endsWith('/thumb')) return false
      requestStarted.resolve()
      await releaseRequest.promise
      try {
        await route.fulfill({ status: 200, contentType: 'image/png', body: PNG })
      } catch {
        // The browser may already have cancelled the route.
      }
      return true
    },
  })

  try {
    const field = state.page.locator('#upload-single').locator('..')
    await field.getByRole('button', { name: /从素材库/ }).click()
    await requestStarted.promise
    await state.page.getByRole('button', { name: '关闭素材选择' }).click()
    await waitUntil(
      state.page,
      () =>
        state.page.evaluate(() =>
          window.__assetFetchSignals.some((record) => record.url.endsWith('/thumb') && record.aborted),
        ),
      'unmount did not abort media request',
    )
    const signals = await state.page.evaluate(() => window.__assetFetchSignals)
    assert(signals.some((record) => record.hasSignal), 'media fetch did not receive an AbortSignal')
  } finally {
    releaseRequest.resolve()
    await closeChecked(state)
  }
}

async function testAbortOnSourceChange(browser) {
  const oldRequestStarted = deferred()
  const releaseOldRequest = deferred()
  const oldAsset = asset('old-src.png')
  const newAsset = { ...asset('new-src.png'), id: oldAsset.id }
  const state = await openForm(browser, {
    fields: [
      { name: 'single', label: 'Change media', type: 'file_upload', accept: 'image', required: false },
    ],
    assets: [oldAsset],
    apiHandler: async ({ route, url }) => {
      if (url.pathname === '/api/assets' && url.searchParams.has('keyword')) {
        await json(route, [newAsset])
        return true
      }
      if (url.pathname.endsWith('/old-src.png/thumb')) {
        oldRequestStarted.resolve()
        await releaseOldRequest.promise
        try {
          await route.fulfill({ status: 200, contentType: 'image/png', body: PNG })
        } catch {
          // The source-change abort can close the old request first.
        }
        return true
      }
      if (url.pathname.endsWith('/new-src.png/thumb')) {
        await route.fulfill({ status: 200, contentType: 'image/png', body: PNG })
        return true
      }
      return false
    },
  })

  try {
    const field = state.page.locator('#upload-single').locator('..')
    await field.getByRole('button', { name: /从素材库/ }).click()
    await oldRequestStarted.promise
    await state.page.getByPlaceholder('搜索素材（文件名 / 提示词）').fill('new')
    await state.page.locator('button[title="new-src.png"]').waitFor()
    await waitUntil(
      state.page,
      () =>
        state.page.evaluate(() =>
          window.__assetFetchSignals.some(
            (record) => record.url.includes('/old-src.png/thumb') && record.aborted,
          ),
        ),
      'source change did not abort old media request',
    )
  } finally {
    releaseOldRequest.resolve()
    await closeChecked(state)
  }
}

const tests = [
  ['single replacement is atomic', testSingleReplacementIsAtomic],
  ['multiple uploads stay aligned', testMultipleUploadsStayAligned],
  ['failed asset replacement keeps old state', testFailedAssetReplacementKeepsOldState],
  ['accepted asset keeps value when preview fails', testAcceptedAssetUsesThumbWithoutRollingBackValue],
  ['authenticated media status matrix', testMediaStatuses],
  ['media object URLs are revoked', testObjectUrlRevokedOnUnmount],
  ['media aborts on unmount', testAbortOnUnmount],
  ['media aborts on source change', testAbortOnSourceChange],
]

let preview
let browser
const failures = []
try {
  preview = await startPreview()
  browser = await chromium.launch({ channel: 'msedge', headless: true })
  for (const [name, run] of tests) {
    try {
      await run(browser)
      console.log(`[PASS] ${name}`)
    } catch (error) {
      failures.push(`${name}: ${error.stack || error}`)
      console.error(`[FAIL] ${name}: ${error.message}`)
    }
  }
} finally {
  await browser?.close()
  if (preview) await stopPreview(preview)
}

if (failures.length) {
  console.error(`\n${failures.join('\n\n')}`)
  process.exitCode = 1
}
