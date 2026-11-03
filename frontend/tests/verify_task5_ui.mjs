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
const mediaFile = (name, mimeType) => ({ name, mimeType, buffer: Buffer.from('task-5-media') })

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

function asset(fileName = 'asset.png', mimeType = 'image/png', id = 'asset-task-5') {
  return {
    id,
    task_id: 'asset-task',
    file_path: `asset-task/${fileName}`,
    file_name: fileName,
    file_size: PNG.length,
    mime_type: mimeType,
    tags: [],
    meta: {},
    is_shared: false,
    created_by: 'owner',
    creator_name: 'Owner',
    created_at: '2026-10-31T00:00:00',
  }
}

async function dispatchFiles(page, inputId, files) {
  await page.evaluate(
    ({ inputId: id, files: incoming }) => {
      const input = document.querySelector(id)
      for (const file of incoming) {
        const transfer = new DataTransfer()
        transfer.items.add(new File([new Uint8Array([1, 2, 3])], file.name, { type: file.type }))
        input.files = transfer.files
        input.dispatchEvent(new Event('change', { bubbles: true }))
      }
    },
    { inputId, files },
  )
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

/** 应用内提示（toast）的文本。
 *
 *  失败反馈已从原生 `alert()` 换成右上角 toast —— 原生弹窗样式由浏览器决定，
 *  和界面完全脱节。所以断言不能再等 `page.on('dialog')`，得读 DOM。
 *  `aria-live="polite"` 只有 toast 容器在用（顶部状态 chip 没有这个属性）。 */
function toastTexts(page) {
  return page.locator('[aria-live="polite"] button').allTextContents()
}

async function waitForToast(page, message) {
  await waitUntil(page, async () => (await toastTexts(page)).length > 0, message)
}

/** 应用内对话框（DialogHost）：标题元素出现即为「弹出来了」。
 *  多行失败原因（批量选素材、磁盘文件没清干净）走这里，不用 toast —— 需要用户读完。 */
async function waitForDialog(page, message) {
  await waitUntil(page, async () => (await page.locator('#app-dialog-title').count()) > 0, message)
}

/** 关掉信息型对话框。它是 modal，不关会挡住后面的点击。 */
async function dismissDialog(page) {
  await page.getByRole('button', { name: '知道了' }).click()
  await page.locator('#app-dialog-title').waitFor({ state: 'detached' })
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

async function openForm(browser, { fields, assets = [], initialValues, apiHandler }) {
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
    window.__renderedMedia = []
    const mediaObserver = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
          if (!(node instanceof Element)) continue
          const media = [
            ...(node.matches('audio, video') ? [node] : []),
            ...node.querySelectorAll('audio, video'),
          ]
          for (const element of media) {
            window.__renderedMedia.push({
              tag: element.tagName.toLowerCase(),
              label: element.getAttribute('aria-label'),
              controls: element.controls,
            })
          }
        }
      }
    })
    mediaObserver.observe(document, { childList: true, subtree: true })
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
  const prefillTask = initialValues
    ? {
        id: 'task-5-prefill',
        template_id: activeTemplate.id,
        created_by: USER.id,
        batch_id: 'task-5-batch',
        status: 'done',
        progress: 100,
        params: initialValues,
        provider_task_id: 'provider-task-5',
        result: { outputs: [] },
        error_msg: '',
        created_at: '2026-10-31T00:00:00',
        started_at: '2026-10-31T00:00:00',
        finished_at: '2026-10-31T00:01:00',
      }
    : null
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
    if (url.pathname === '/api/tasks' && request.method() === 'GET') {
      return json(route, prefillTask ? [prefillTask] : [])
    }
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
  if (prefillTask) {
    await page.getByRole('button', { name: '任务', exact: true }).click()
    await page.getByRole('button', { name: '生成任务', exact: true }).click()
    await page.getByRole('button', { name: '用这些参数再生成', exact: true }).click()
    await page.getByText('已带入原任务参数，改一改再提交即可', { exact: true }).waitFor()
  } else {
    await page.getByText(activeTemplate.name, { exact: true }).click()
  }
  return { context, page, submitted, dialogs, consoleErrors }
}

async function closeChecked(state) {
  await state.context.close()
  // 原生弹窗现在是「不该出现」的东西：反馈统一走 DialogHost / toast。
  // 留着这条断言当护栏 —— 哪天又写了 window.alert，这里会直接红。
  assert.deepEqual(state.dialogs, [], `native browser dialogs came back: ${state.dialogs.join(' | ')}`)
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
    await waitForToast(state.page, 'failure alert missing')
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
    await waitForToast(state.page, 'middle failure alert missing')
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

async function testPendingUploadsBlockSubmission(browser) {
  const scenarios = [
    {
      name: 'initial upload',
      pendingAttempt: 1,
      fields: [
        { name: 'single', label: 'Initial upload', type: 'file_upload', accept: 'image', required: false },
      ],
      start: async (state) => {
        await state.page.locator('#upload-single').setInputFiles(imageFile('initial.png'))
      },
    },
    {
      name: 'replacement upload',
      pendingAttempt: 2,
      fields: [
        { name: 'single', label: 'Replacement upload', type: 'file_upload', accept: 'image', required: false },
      ],
      prepare: async (state) => {
        await state.page.locator('#upload-single').setInputFiles(imageFile('existing.png'))
        await state.page.locator('img[alt="existing.png"]').waitFor()
      },
      start: async (state) => {
        await state.page.locator('#upload-single').setInputFiles(imageFile('replacement.png'))
      },
    },
    {
      name: 'multi upload with accepted prefix',
      pendingAttempt: 2,
      fields: [
        {
          name: 'multiple',
          label: 'Prefix upload',
          type: 'file_upload',
          accept: 'image',
          required: false,
          multiple: true,
        },
      ],
      start: async (state) => {
        await state.page.locator('#upload-multiple').setInputFiles([
          imageFile('prefix.png'),
          imageFile('pending.png'),
        ])
      },
    },
  ]

  for (const scenario of scenarios) {
    const started = deferred()
    const release = deferred()
    let attempts = 0
    const state = await openForm(browser, {
      fields: scenario.fields,
      apiHandler: async ({ route, url }) => {
        if (url.pathname !== '/api/upload/file') return false
        attempts += 1
        if (attempts === scenario.pendingAttempt) {
          started.resolve()
          await release.promise
        }
        await json(route, { filename: attempts === 1 ? 'engine-existing.png' : `engine-${attempts}.png` })
        return true
      },
    })

    try {
      if (scenario.prepare) await scenario.prepare(state)
      await scenario.start(state)
      await started.promise
      await state.page.waitForTimeout(0)

      const submitButton = state.page.getByRole('button', { name: /开始生成|文件上传中/ })
      const label = await submitButton.textContent()
      const disabled = await submitButton.isDisabled()
      await state.page.locator('form').evaluate((form) => form.requestSubmit())
      await state.page.waitForTimeout(100)
      const submitCalls = state.submitted.length

      release.resolve()
      await waitUntil(
        state.page,
        () => Promise.resolve(attempts >= scenario.pendingAttempt),
        'upload did not finish',
      )
      assert.equal(label?.trim(), '文件上传中…', `${scenario.name} did not show pending label`)
      assert.equal(disabled, true, `${scenario.name} submit button stayed enabled`)
      assert.equal(submitCalls, 0, `${scenario.name} reached onSubmit while upload was pending`)
    } finally {
      release.resolve()
      await closeChecked(state)
    }
  }
}

async function testSingleReplacementGenerations(browser) {
  const releases = new Map()
  const started = new Map()
  const finished = new Map()
  for (const attempt of [2, 3, 4, 5]) {
    releases.set(attempt, deferred())
    started.set(attempt, deferred())
    finished.set(attempt, deferred())
  }
  let attempts = 0
  const state = await openForm(browser, {
    fields: [
      { name: 'single', label: 'Racing replacement', type: 'file_upload', accept: 'image', required: false },
    ],
    apiHandler: async ({ route, url }) => {
      if (url.pathname !== '/api/upload/file') return false
      attempts += 1
      const attempt = attempts
      if (attempt === 1) {
        await json(route, { filename: 'engine-baseline.png' })
        return true
      }
      started.get(attempt).resolve()
      await releases.get(attempt).promise
      await json(route, { filename: `engine-race-${attempt}.png` })
      finished.get(attempt).resolve()
      return true
    },
  })

  try {
    await state.page.locator('#upload-single').setInputFiles(imageFile('baseline.png'))
    await state.page.locator('img[alt="baseline.png"]').waitFor()

    await dispatchFiles(state.page, '#upload-single', [
      { name: 'race-2.png', type: 'image/png' },
      { name: 'race-3.png', type: 'image/png' },
    ])
    await Promise.all([started.get(2).promise, started.get(3).promise])
    await state.page.waitForTimeout(0)

    const field = state.page.locator('#upload-single').locator('..')
    const controls = {
      input: await state.page.locator('#upload-single').isDisabled(),
      picker: await field.getByRole('button', { name: /从素材库/ }).isDisabled(),
      remove: await field.getByRole('button', { name: '移除' }).isDisabled(),
      label: await field.locator('label[for="upload-single"]').getAttribute('aria-disabled'),
    }

    releases.get(2).resolve()
    await finished.get(2).promise
    await state.page.waitForTimeout(50)
    const olderFinishedState = {
      submitDisabled: await state.page.getByRole('button', { name: /开始生成|文件上传中/ }).isDisabled(),
      submitLabel: (await state.page.getByRole('button', { name: /开始生成|文件上传中/ }).textContent())?.trim(),
      baseline: await state.page.locator('img[alt="baseline.png"]').count(),
      stale: await state.page.locator('img[alt="race-2.png"]').count(),
    }
    releases.get(3).resolve()
    await finished.get(3).promise
    await state.page.locator('img[alt="race-3.png"]').waitFor()

    await dispatchFiles(state.page, '#upload-single', [
      { name: 'race-4.png', type: 'image/png' },
      { name: 'race-5.png', type: 'image/png' },
    ])
    await Promise.all([started.get(4).promise, started.get(5).promise])
    releases.get(5).resolve()
    await finished.get(5).promise
    await state.page.locator('img[alt="race-5.png"]').waitFor()
    releases.get(4).resolve()
    await finished.get(4).promise
    await state.page.waitForTimeout(50)

    assert.deepEqual(controls, { input: true, picker: true, remove: true, label: 'true' })
    assert.deepEqual(
      olderFinishedState,
      { submitDisabled: true, submitLabel: '文件上传中…', baseline: 1, stale: 0 },
      'older completion changed state or cleared the newer busy operation',
    )
    assert.equal(await state.page.locator('img[alt="race-5.png"]').count(), 1)
    assert.equal(await state.page.locator('img[alt="race-4.png"]').count(), 0)
  } finally {
    for (const release of releases.values()) release.resolve()
    await closeChecked(state)
  }
}

async function testRemoveInvalidatesPendingReplacement(browser) {
  const replacementStarted = deferred()
  const releaseReplacement = deferred()
  const replacementFinished = deferred()
  let attempts = 0
  const state = await openForm(browser, {
    fields: [
      { name: 'single', label: 'Remove race', type: 'file_upload', accept: 'image', required: false },
    ],
    apiHandler: async ({ route, url }) => {
      if (url.pathname !== '/api/upload/file') return false
      attempts += 1
      if (attempts === 1) {
        await json(route, { filename: 'engine-existing.png' })
      } else {
        replacementStarted.resolve()
        await releaseReplacement.promise
        await json(route, { filename: 'engine-restored.png' })
        replacementFinished.resolve()
      }
      return true
    },
  })

  try {
    await state.page.locator('#upload-single').setInputFiles(imageFile('existing.png'))
    await state.page.locator('img[alt="existing.png"]').waitFor()
    await state.page.evaluate(() => {
      const input = document.querySelector('#upload-single')
      const transfer = new DataTransfer()
      transfer.items.add(new File([new Uint8Array([1])], 'pending.png', { type: 'image/png' }))
      input.files = transfer.files
      input.dispatchEvent(new Event('change', { bubbles: true }))
      const remove = input.parentElement.querySelector('.btn-danger-quiet')
      // Controls are asserted disabled separately; invoke the rendered handler to model an
      // already-queued remove event crossing the upload state update.
      const propsKey = Object.keys(remove).find((key) => key.startsWith('__reactProps$'))
      remove[propsKey].onClick()
    })
    await replacementStarted.promise
    releaseReplacement.resolve()
    await replacementFinished.promise
    await state.page.waitForTimeout(50)

    assert.equal(await state.page.locator('img[alt="existing.png"]').count(), 0)
    assert.equal(await state.page.locator('img[alt="pending.png"]').count(), 0)
    await state.page.locator('form').evaluate((form) => form.requestSubmit())
    await waitUntil(state.page, () => Promise.resolve(state.submitted.length === 1), 'removed form did not submit')
    assert.equal(Object.hasOwn(state.submitted[0].params, 'single'), false)
  } finally {
    releaseReplacement.resolve()
    await closeChecked(state)
  }
}

async function testPendingDirectUploadUnmountDoesNotCreatePreview(browser) {
  const uploadStarted = deferred()
  const releaseUpload = deferred()
  const uploadFinished = deferred()
  const state = await openForm(browser, {
    fields: [
      { name: 'single', label: 'Unmount upload', type: 'file_upload', accept: 'image', required: false },
    ],
    apiHandler: async ({ route, url }) => {
      if (url.pathname !== '/api/upload/file') return false
      uploadStarted.resolve()
      await releaseUpload.promise
      await json(route, { filename: 'engine-after-unmount.png' })
      uploadFinished.resolve()
      return true
    },
  })

  try {
    await state.page.locator('#upload-single').setInputFiles(imageFile('after-unmount.png'))
    await uploadStarted.promise
    const before = await state.page.evaluate(() => window.__objectUrlRecords.length)
    await state.page.getByRole('button', { name: '任务', exact: true }).click()
    releaseUpload.resolve()
    await uploadFinished.promise
    await state.page.waitForTimeout(50)
    const records = await state.page.evaluate(() => window.__objectUrlRecords)
    assert.equal(records.length, before, 'upload completion created an object URL after unmount')
    assert(records.every((record) => record.revoked), 'unmount left an object URL active')
  } finally {
    releaseUpload.resolve()
    await closeChecked(state)
  }
}

async function testAssetMultipleUploadsStaySerialAndAligned(browser) {
  const secondStarted = deferred()
  const releaseSecond = deferred()
  let attempts = 0
  const assets = [
    asset('asset-first.png', 'image/png', 'asset-first'),
    asset('asset-failed.png', 'image/png', 'asset-failed'),
    asset('asset-third.png', 'image/png', 'asset-third'),
  ]
  const state = await openForm(browser, {
    fields: [
      {
        name: 'multiple',
        label: 'Multiple assets',
        type: 'file_upload',
        accept: 'image',
        required: false,
        multiple: true,
      },
    ],
    assets,
    apiHandler: async ({ route, url }) => {
      if (url.pathname === '/api/upload/from-asset') {
        attempts += 1
        if (attempts === 2) {
          secondStarted.resolve()
          await releaseSecond.promise
          await json(route, { detail: 'middle asset failed' }, 502)
        } else {
          await json(route, { filename: attempts === 1 ? 'engine-asset-first.png' : 'engine-asset-third.png' })
        }
        return true
      }
      if (url.pathname.endsWith('/thumb')) {
        await route.fulfill({ status: 200, contentType: 'image/png', body: PNG })
        return true
      }
      return false
    },
  })

  try {
    const field = state.page.locator('#upload-multiple').locator('..')
    await field.getByRole('button', { name: /从素材库/ }).click()
    for (const item of assets) {
      await state.page.locator(`button[title="${item.file_name}"]`).click()
    }
    await state.page.getByRole('button', { name: /确定/ }).click()
    await secondStarted.promise
    assert.equal(attempts, 2, 'asset multi upload was not serial')
    releaseSecond.resolve()
    await waitUntil(state.page, () => Promise.resolve(attempts === 3), 'third asset was not forwarded')
    await waitForDialog(state.page, 'asset multi failure alert missing')
    await dismissDialog(state.page)
    await state.page.locator('img[alt="asset-third.png"]').waitFor()
    assert.equal(await state.page.locator('img[alt="asset-first.png"]').count(), 1)
    assert.equal(await state.page.locator('img[alt="asset-failed.png"]').count(), 0)

    await state.page.getByRole('button', { name: '开始生成' }).click()
    await waitUntil(state.page, () => Promise.resolve(state.submitted.length === 1), 'asset multi form did not submit')
    assert.deepEqual(state.submitted[0].params.multiple, [
      'engine-asset-first.png',
      'engine-asset-third.png',
    ])
  } finally {
    releaseSecond.resolve()
    await closeChecked(state)
  }
}

async function testPrefilledImageListAlignment(browser) {
  let attempts = 0
  const state = await openForm(browser, {
    fields: [
      {
        name: 'images',
        label: 'Prefilled images',
        type: 'file_upload',
        accept: 'image',
        required: false,
        multiple: true,
      },
    ],
    initialValues: { images: ['old-image.png'] },
    apiHandler: async ({ route, url }) => {
      if (url.pathname !== '/api/upload/file') return false
      attempts += 1
      await json(route, { filename: `engine-new-${attempts}.png` })
      return true
    },
  })

  try {
    assert.equal(await state.page.getByText('old-image.png', { exact: true }).count(), 1)
    await state.page.locator('#upload-images').setInputFiles(imageFile('new-image.png'))
    await state.page.locator('img[alt="new-image.png"]').waitFor()
    await state.page.getByRole('button', { name: '移除第 2 张' }).click()
    await state.page.getByRole('button', { name: '开始生成' }).click()
    await waitUntil(state.page, () => Promise.resolve(state.submitted.length === 1), 'image prefill first submit missing')
    assert.deepEqual(state.submitted[0].params.images, ['old-image.png'])

    await state.page.locator('#upload-images').setInputFiles(imageFile('new-image-2.png'))
    await state.page.locator('img[alt="new-image-2.png"]').waitFor()
    await state.page.getByRole('button', { name: '移除第 1 张' }).click()
    await state.page.getByRole('button', { name: '开始生成' }).click()
    await waitUntil(state.page, () => Promise.resolve(state.submitted.length === 2), 'image prefill second submit missing')
    assert.deepEqual(state.submitted[1].params.images, ['engine-new-2.png'])
  } finally {
    await closeChecked(state)
  }
}

async function testPrefilledAudioListAlignment(browser) {
  let attempts = 0
  const state = await openForm(browser, {
    fields: [
      {
        name: 'audio',
        label: 'Prefilled audio',
        type: 'file_upload',
        accept: 'audio',
        required: false,
        multiple: true,
      },
    ],
    initialValues: { audio: ['old-audio.wav'] },
    apiHandler: async ({ route, url }) => {
      if (url.pathname !== '/api/upload/file') return false
      attempts += 1
      await json(route, { filename: `engine-audio-${attempts}.wav` })
      return true
    },
  })

  try {
    assert.equal(await state.page.getByText('old-audio.wav', { exact: true }).count(), 1)
    await state.page.locator('#upload-audio').setInputFiles(mediaFile('new-audio.wav', 'audio/wav'))
    await state.page.getByText('new-audio.wav', { exact: true }).waitFor()
    await state.page.getByRole('button', { name: '移除new-audio.wav' }).click()
    await state.page.getByRole('button', { name: '开始生成' }).click()
    await waitUntil(state.page, () => Promise.resolve(state.submitted.length === 1), 'audio prefill first submit missing')
    assert.deepEqual(state.submitted[0].params.audio, ['old-audio.wav'])

    await state.page.locator('#upload-audio').setInputFiles(mediaFile('new-audio-2.wav', 'audio/wav'))
    await state.page.getByText('new-audio-2.wav', { exact: true }).waitFor()
    await state.page.getByRole('button', { name: '移除old-audio.wav' }).click()
    await state.page.getByRole('button', { name: '开始生成' }).click()
    await waitUntil(state.page, () => Promise.resolve(state.submitted.length === 2), 'audio prefill second submit missing')
    assert.deepEqual(state.submitted[1].params.audio, ['engine-audio-2.wav'])
  } finally {
    await closeChecked(state)
  }
}

async function testRemoteAudioAndVideoPreviewAfterForward(browser) {
  for (const scenario of [
    { kind: 'audio', mime: 'audio/wav', fileName: 'remote.wav', tag: 'audio' },
    { kind: 'video', mime: 'video/mp4', fileName: 'remote.mp4', tag: 'video' },
  ]) {
    const forwardStarted = deferred()
    const releaseForward = deferred()
    let forwarded = false
    let rawAfterForward = 0
    let thumbAfterForward = 0
    const item = asset(scenario.fileName, scenario.mime, `asset-${scenario.kind}`)
    const state = await openForm(browser, {
      fields: [
        {
          name: 'media',
          label: `Remote ${scenario.kind}`,
          type: 'file_upload',
          accept: scenario.kind,
          required: false,
        },
      ],
      assets: [item],
      apiHandler: async ({ route, url }) => {
        if (url.pathname === '/api/upload/from-asset') {
          forwardStarted.resolve()
          await releaseForward.promise
          forwarded = true
          await json(route, { filename: `engine-${scenario.fileName}` })
          return true
        }
        if (url.pathname.endsWith('/thumb')) {
          if (forwarded) thumbAfterForward += 1
          await route.fulfill({ status: 200, contentType: 'image/png', body: PNG })
          return true
        }
        if (url.pathname.includes('/assets/')) {
          if (forwarded) rawAfterForward += 1
          await route.fulfill({ status: 200, contentType: scenario.mime, body: Buffer.from('invalid-media') })
          return true
        }
        return false
      },
    })

    try {
      const field = state.page.locator('#upload-media').locator('..')
      await field.getByRole('button', { name: /从素材库/ }).click()
      await state.page.locator(`button[title="${scenario.fileName}"]`).click()
      await forwardStarted.promise
      assert.equal(rawAfterForward, 0, `${scenario.kind} preview requested before forwarding`)
      releaseForward.resolve()
      await waitUntil(
        state.page,
        () =>
          state.page.evaluate(
            ({ tag, fileName }) =>
              window.__renderedMedia.some(
                (entry) => entry.tag === tag && entry.label === fileName && entry.controls,
              ),
            { tag: scenario.tag, fileName: scenario.fileName },
          ),
        `${scenario.kind} AuthMedia element was not rendered`,
      )
      assert.equal(rawAfterForward, 1, `${scenario.kind} did not request authenticated original`)
      assert.equal(thumbAfterForward, 0, `${scenario.kind} form preview incorrectly requested a thumbnail`)
      await state.page.getByText(scenario.fileName, { exact: true }).waitFor()
    } finally {
      releaseForward.resolve()
      await closeChecked(state)
    }
  }
}

async function testDirectLocalVideoRendersPreview(browser) {
  const state = await openForm(browser, {
    fields: [
      {
        name: 'video',
        label: 'Local video',
        type: 'file_upload',
        accept: 'video',
        required: false,
      },
    ],
    apiHandler: async ({ route, url }) => {
      if (url.pathname !== '/api/upload/file') return false
      await json(route, { filename: 'engine-local.mp4' })
      return true
    },
  })

  try {
    await state.page.locator('#upload-video').setInputFiles(
      mediaFile('local-preview.mp4', 'video/mp4'),
    )
    await waitUntil(
      state.page,
      () =>
        state.page.evaluate(() =>
          window.__renderedMedia.some(
            (entry) =>
              entry.tag === 'video' &&
              entry.label === 'local-preview.mp4' &&
              entry.controls,
          ),
        ),
      'direct local video preview was not rendered',
    )
    await state.page.getByText('local-preview.mp4', { exact: true }).waitFor()
  } finally {
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
    await waitForDialog(state.page, 'asset failure alert missing')
    await dismissDialog(state.page)

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
  // 缩略图 URL 用的是**素材 id**（`/api/assets/{id}/thumb`），不含文件名。
  // 两个素材必须给不同的 id，否则两个 thumb 地址一样，分不出「旧的」和「新的」。
  const oldAsset = asset('old-src.png', 'image/png', 'asset-old-src')
  const newAsset = asset('new-src.png', 'image/png', 'asset-new-src')
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
      if (url.pathname === '/api/assets/asset-old-src/thumb') {
        oldRequestStarted.resolve()
        await releaseOldRequest.promise
        try {
          await route.fulfill({ status: 200, contentType: 'image/png', body: PNG })
        } catch {
          // The source-change abort can close the old request first.
        }
        return true
      }
      if (url.pathname === '/api/assets/asset-new-src/thumb') {
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
            (record) => record.url.includes('/asset-old-src/thumb') && record.aborted,
          ),
        ),
      'source change did not abort old media request',
    )
  } finally {
    releaseOldRequest.resolve()
    await closeChecked(state)
  }
}

async function testLoginTransitionDoesNotCrash(browser) {
  const context = await browser.newContext()
  const page = await context.newPage()
  const errors = []
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text())
  })
  page.on('pageerror', (error) => errors.push(error.message))

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/auth/login') {
      return json(route, { access_token: 'login-transition-token', token_type: 'bearer', user: USER })
    }
    if (path === '/api/templates') return json(route, [])
    if (path === '/api/health') return json(route, { status: 'ok', comfyui: 'connected' })
    if (path === '/api/engines') return json(route, [])
    if (path === '/api/tasks' && request.method() === 'GET') return json(route, [])
    return json(route, { detail: `Unhandled intercepted API: ${path}` }, 500)
  })

  try {
    await page.goto(BASE_URL)
    await page.getByLabel('请输入用户名').fill('owner')
    await page.getByLabel('请输入密码').fill('pw123456')
    await page.getByRole('button', { name: '登 录' }).click()
    await page.getByRole('button', { name: '素材库', exact: true }).waitFor()
    assert.equal(await page.evaluate(() => localStorage.getItem('token')), 'login-transition-token')
    assert.deepEqual(errors, [], `login transition errors: ${errors.join('\n')}`)
  } finally {
    await context.close()
  }
}

const tests = [
  ['login transition keeps hook order stable', testLoginTransitionDoesNotCrash],
  ['single replacement is atomic', testSingleReplacementIsAtomic],
  ['multiple uploads stay aligned', testMultipleUploadsStayAligned],
  ['pending uploads block submission', testPendingUploadsBlockSubmission],
  ['single replacement generations are race-safe', testSingleReplacementGenerations],
  ['remove invalidates a pending replacement', testRemoveInvalidatesPendingReplacement],
  ['pending direct upload unmount does not create preview', testPendingDirectUploadUnmountDoesNotCreatePreview],
  ['asset multiple uploads stay serial and aligned', testAssetMultipleUploadsStaySerialAndAligned],
  ['prefilled image list stays aligned', testPrefilledImageListAlignment],
  ['prefilled audio list stays aligned', testPrefilledAudioListAlignment],
  ['remote audio and video preview after forwarding', testRemoteAudioAndVideoPreviewAfterForward],
  ['direct local video renders a controlled preview', testDirectLocalVideoRendersPreview],
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
