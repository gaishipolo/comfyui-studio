/**
 * 真实浏览器验证：流程「运行台」的进度与步骤行。
 *
 * **自包含**（同 verify_task5_ui.mjs 的做法）：拦截 API 造数据，不需要起
 * 真后端、不需要探针账号。跑法：`node tests/verify_flow_run_panel_ui.mjs`
 *
 * 验证的是界面这一侧，且都是**后端算得对、界面显示错**才会暴露的问题：
 * 1. 进度条宽度与百分比跟着 run.progress 走，且不会冲出轨道（脏数据也能兜住）
 * 2. 循环步骤一次运行有 N 行，每行标「第 N 次」（行数不少、序号不重）
 * 3. 子流程步骤没填标题时显示子流程名，不是「未知节点」
 * 4. 失败时进度条转 error 色、错误原因显示出来
 *
 * **进度数值怎么算**由 backend/tests/test_flow_review_fixes.py 负责（完成
 * 步骤数 / 总步骤数，不按任务数）。两边看的是同一件事的两端。
 *
 * 用有头浏览器：无头 Chromium 不占滚动条宽度，量宽度会得出假结论。
 */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromium } from 'playwright'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const PORT = 41738
const BASE_URL = `http://127.0.0.1:${PORT}`

const USER = {
  id: 'owner',
  username: 'owner',
  display_name: 'Owner',
  role: 'admin',
  is_active: true,
  created_at: '2026-10-31T00:00:00',
}

const FIELDS = [{ name: 'prompt', label: '提示词', type: 'textarea', required: true }]

const CAPABILITY = {
  id: 'template-fake',
  name: '假文生图',
  description: '',
  category: 'image',
  engine: 'fake',
  capability: 'text_to_image',
  fields: FIELDS,
}

const FLOW_INPUTS = {
  fields: [{ name: 'prompt', label: '提示词', type: 'textarea', required: true }],
}

const CHILD = {
  id: 'flow-child',
  name: '后处理子流程',
  description: '',
  category: 'image',
  icon: '',
  input_schema: FLOW_INPUTS,
  outputs: [{ name: 'final_image', pick: 'last' }],
  steps: [
    {
      id: 'c1',
      title: '',
      type: 'template',
      template_id: 'template-fake',
      flow_id: '',
      count: 1,
      params: { prompt: '{{ input.prompt }}' },
    },
  ],
  status: 'active',
  version: 1,
  created_by: 'owner',
  created_at: '2026-10-31T00:00:00',
  updated_at: '2026-10-31T00:00:00',
}

const MAIN = {
  id: 'flow-main',
  name: '验证流程',
  description: '',
  category: 'image',
  icon: '',
  input_schema: FLOW_INPUTS,
  outputs: [],
  steps: [
    {
      id: 's1',
      title: '生成参考图',
      type: 'template',
      template_id: 'template-fake',
      flow_id: '',
      count: 1,
      params: { prompt: '{{ input.prompt }}' },
    },
    {
      id: 's2',
      title: '批量变体',
      type: 'repeat',
      template_id: 'template-fake',
      flow_id: '',
      count: 3,
      params: { prompt: '{{ input.prompt }} 变体 {{ index }}' },
    },
    {
      // 故意不填 title —— 界面必须退回子流程名
      id: 's3',
      title: '',
      type: 'flow',
      template_id: '',
      flow_id: 'flow-child',
      count: 1,
      params: {},
    },
  ],
  status: 'active',
  version: 1,
  created_by: 'owner',
  created_at: '2026-10-31T00:00:00',
  updated_at: '2026-10-31T00:00:00',
}

const ASSET = (n) => ({
  asset_id: `asset-${n}`,
  filename: `out-${n}.png`,
  mime_type: 'image/png',
})

/** 第 1 次轮询：只跑完第一步，进度 33.3 */
const RUN_MID = {
  id: 'run-1',
  flow_id: MAIN.id,
  flow_name: MAIN.name,
  status: 'running',
  progress: 100 / 3,
  params: { prompt: '小猫' },
  steps: [
    { step_id: 's1', task_id: 't1', status: 'done', layer: 0, iteration: 0, outputs: [ASSET(1)] },
  ],
  outputs: [],
  error_msg: '',
  created_at: '2026-10-31T00:00:00',
  finished_at: null,
}

/** 第 2 次轮询：全部跑完。循环步骤 3 个实例 + 子流程 1 行 */
const RUN_DONE = {
  ...RUN_MID,
  status: 'done',
  progress: 100,
  steps: [
    { step_id: 's1', task_id: 't1', status: 'done', layer: 0, iteration: 0, outputs: [ASSET(1)] },
    { step_id: 's2', task_id: 't2', status: 'done', layer: 1, iteration: 0, outputs: [ASSET(2)] },
    { step_id: 's2', task_id: 't3', status: 'done', layer: 1, iteration: 1, outputs: [ASSET(3)] },
    { step_id: 's2', task_id: 't4', status: 'done', layer: 1, iteration: 2, outputs: [ASSET(4)] },
    {
      step_id: 's3',
      task_id: '',
      status: 'done',
      layer: 2,
      iteration: 0,
      outputs: [ASSET(5)],
      subflow: [{ step_id: 'c1', task_id: 't5', status: 'done', layer: 0, iteration: 0 }],
    },
  ],
  outputs: [],
  finished_at: '2026-10-31T00:01:00',
}

/** 第二次运行：第二步失败。progress 只到 40（第 1 步算完，第 2 步没算完） */
const RUN_ERROR = {
  ...RUN_MID,
  id: 'run-2',
  status: 'error',
  progress: 40,
  steps: [
    { step_id: 's1', task_id: 't1', status: 'done', layer: 0, iteration: 0, outputs: [ASSET(1)] },
    { step_id: 's2', task_id: 't2', status: 'error', layer: 1, iteration: 0, outputs: [] },
  ],
  error_msg: '批量变体：生成失败，引擎返回超时',
  finished_at: '2026-10-31T00:02:00',
}

/** 第一次轮询故意给一条脏进度，验证前端兜住不冲出轨道、也不显示成 150% */
const RUN_DIRTY = { ...RUN_MID, progress: 150 }

function json(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

const failures = []
function check(name, fn) {
  try {
    fn()
    console.log(`[PASS] ${name}`)
  } catch (err) {
    failures.push(`${name}: ${err.message}`)
    console.log(`[FAIL] ${failures[failures.length - 1]}`)
  }
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
  child.stdout.on('data', (c) => {
    output += c
  })
  child.stderr.on('data', (c) => {
    output += c
  })
  const deadline = Date.now() + 15_000
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`vite preview 提前退出:\n${output}`)
    try {
      const response = await fetch(BASE_URL)
      if (response.ok) return child
    } catch {
      /* 还在启动 */
    }
    await new Promise((r) => setTimeout(r, 100))
  }
  throw new Error(`vite preview 没起来:\n${output}`)
}

async function stopPreview(child) {
  if (child.exitCode !== null) return
  if (process.platform === 'win32') {
    await new Promise((r) => {
      const killer = spawn('taskkill', ['/pid', String(child.pid), '/t', '/f'], { stdio: 'ignore' })
      killer.on('exit', r)
      killer.on('error', r)
    })
  } else {
    child.kill('SIGTERM')
  }
}

/** 运行台的步骤行（不含顶部说明面板里那串步骤定义） */
async function runRows(page) {
  return page.$$eval('[data-run-steps] > li', (nodes) =>
    nodes.map((n) => n.textContent.replace(/\s+/g, ' ').trim()),
  )
}

async function bar(page) {
  const el = await page.$('[data-run-progress]')
  assert.ok(el, '找不到进度条')
  const info = await el.evaluate((node) => {
    const inner = node.firstElementChild
    const outerRect = node.getBoundingClientRect()
    const innerRect = inner.getBoundingClientRect()
    return {
      now: node.getAttribute('aria-valuenow'),
      outerWidth: outerRect.width,
      innerWidth: innerRect.width,
      innerClass: inner.className,
    }
  })
  const percent = await page.$eval('[data-run-percent]', (n) => n.textContent.trim())
  const chip = await page.$eval('[data-run-status]', (n) => n.textContent.trim())
  return { ...info, percent, chip }
}

async function main() {
  const preview = await startPreview()
  const browser = await chromium.launch({ headless: false })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const consoleErrors = []
  page.on('console', (m) => {
    if (m.type() === 'error') consoleErrors.push(m.text())
  })
  page.on('pageerror', (e) => consoleErrors.push(String(e)))

  // 两次运行各自返回一串轮询结果
  const pollQueues = {
    'run-1': [RUN_DIRTY, RUN_DONE],
    'run-2': [RUN_ERROR],
  }
  // 第 1 次「开始运行」→ run-1，第 2 次 → run-2
  let runStarts = 0

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    if (path === '/api/auth/login') {
      return json(route, { access_token: 'ui-token', token_type: 'bearer', user: USER })
    }
    if (path === '/api/health') return json(route, { status: 'ok', comfyui: 'connected' })
    if (path === '/api/flows' && request.method() === 'GET') return json(route, [MAIN, CHILD])
    if (path === '/api/flows' && request.method() === 'POST') {
      return json(route, { message: '已创建', id: 'flow-new' })
    }
    if (path === '/api/flows/capabilities') return json(route, [CAPABILITY])
    if (path === '/api/flows/validate') {
      return json(route, { ok: true, errors: [], warnings: [], layers: [['s1'], ['s2'], ['s3']] })
    }
    if (path === '/api/flows/flow-main/run' && request.method() === 'POST') {
      runStarts += 1
      const id = runStarts === 1 ? 'run-1' : 'run-2'
      return json(route, { ...RUN_MID, id, progress: 0, steps: [] })
    }
    if (path === '/api/flows/flow-child/run' && request.method() === 'POST') {
      return json(route, { ...RUN_MID, progress: 0, steps: [] })
    }
    const runMatch = path.match(/^\/api\/flows\/runs\/(.+)$/)
    if (runMatch) {
      const queue = pollQueues[runMatch[1]]
      // 队列空了就停在最后一个状态 —— 轮询已经停了，这里只是兜底
      const next = queue && queue.length > 1 ? queue.shift() : queue && queue[0]
      return json(route, next ?? RUN_DONE)
    }
    if (path === '/api/templates') return json(route, [])
    if (path === '/api/tasks') return json(route, [])
    if (path === '/api/assets') return json(route, [])
    if (path === '/api/engines') return json(route, [{ name: 'fake', status: 'connected' }])
    console.log('  [未拦截]', request.method(), path)
    return json(route, { detail: `Unhandled API: ${path}` }, 500)
  })

  await page.goto(`${BASE_URL}/`, { waitUntil: 'domcontentloaded' })
  await page.fill('input[name="username"], #username', 'owner')
  await page.fill('input[name="password"], #password', 'pw')
  await page.click('button[type="submit"]')
  await page.waitForTimeout(1500)

  await page.click('button:has-text("流程")')
  await page.waitForTimeout(1200)

  // ── 第一次运行：干净跑完 ──────────────────────────────────────
  // 表单字段的 id 是 `field-<name>`（见 DynamicForm），不是 name 属性
  await page.fill('#field-prompt', '小猫')
  await page.click('button:has-text("开始运行")')

  // 刚起来：进度 0、还没步骤
  await page.waitForTimeout(300)
  const zero = await bar(page)
  check('刚启动时进度是 0%', () => {
    assert.equal(zero.now, '0')
    assert.equal(zero.percent, '0%')
    assert.equal(zero.chip, '运行中')
  })
  const emptyHint = await page.locator('li:has-text("正在执行第一步")').count()
  check('还没有步骤时给出等待提示', () => assert.equal(emptyHint, 1))

  // 第 1 次轮询：脏数据 150 → 界面必须自己兜住
  await page.waitForTimeout(1900)
  const dirty = await bar(page)
  check('后端给出超范围进度时显示 100% 而不是 150%', () => {
    assert.equal(dirty.now, '100')
    assert.equal(dirty.percent, '100%')
  })
  check('进度条不出轨道', () => {
    assert.ok(
      dirty.innerWidth <= dirty.outerWidth + 0.5,
      `内条 ${dirty.innerWidth}px 宽于外轨道 ${dirty.outerWidth}px`,
    )
  })

  // 第 2 次轮询：全部完成
  await page.waitForTimeout(1900)
  const done = await bar(page)
  const finishedRows = await runRows(page)

  check('完成后进度 100% 且状态是已完成', () => {
    assert.equal(done.now, '100')
    assert.equal(done.percent, '100%')
    assert.equal(done.chip, '已完成')
  })

  check('循环步骤 3 个实例各占一行', () => {
    assert.equal(
      finishedRows.length,
      5,
      `步骤行数 ${finishedRows.length}：${finishedRows.join(' / ')}`,
    )
  })

  // 循环实例的**显示**在这里验。key 用 step_id 会撞车（React 复用错行），
  // 但那是 dev 构建才报的 warning，production 构建里观察不到 —— 所以这条
  // 断言守的是显示本身，key 的写法靠代码审查和 React 自己的告警。
  check('每个循环实例分别标出是第几次', () => {
    for (const n of [1, 2, 3]) {
      const hits = finishedRows.filter((r) => r.includes(`第 ${n} 次`)).length
      assert.equal(hits, 1, `「第 ${n} 次」出现 ${hits} 次，原文：${finishedRows.join(' | ')}`)
    }
  })

  check('同层步骤都标同一层、子流程标第三层', () => {
    const layer2 = finishedRows.filter((r) => r.includes('第 2 层')).length
    const layer3 = finishedRows.filter((r) => r.includes('第 3 层')).length
    assert.equal(layer2, 3, `第 2 层的行数 ${layer2}`)
    assert.equal(layer3, 1, `第 3 层的行数 ${layer3}`)
  })

  check('子流程步骤没填标题时显示子流程名', () => {
    const row = finishedRows.find((r) => r.includes('第 3 层'))
    assert.ok(row, '找不到子流程那一行')
    assert.ok(row.includes('后处理子流程'), `子流程行显示成：${row}`)
    assert.ok(!row.includes('未知节点'), `子流程行显示成：${row}`)
  })

  check('循环行把 3 份产物分开显示', () => {
    const loopRows = finishedRows.filter((r) => r.includes('第 2 层'))
    for (const row of loopRows) {
      assert.ok(row.includes('1 个产物'), `循环行产物数不对：${row}`)
    }
  })

  // ── 第二次运行：失败 ─────────────────────────────────────────
  await page.click('button:has-text("开始运行")')
  await page.waitForTimeout(1900)
  const failed = await bar(page)
  const failedRows = await runRows(page)

  check('失败时进度停在 40% 不跳到 100%', () => {
    assert.equal(failed.now, '40')
    assert.equal(failed.percent, '40%')
  })

  check('失败时进度条转 error 色', () => {
    assert.ok(failed.innerClass.includes('bg-error'), `内条样式：${failed.innerClass}`)
  })

  const errText = await page.locator('text=批量变体：生成失败，引擎返回超时').count()
  check('失败原因显示出来', () => assert.ok(errText >= 1, '找不到错误原因'))

  check('失败时状态是失败且失败那步标红', () => {
    assert.equal(failed.chip, '失败')
    assert.ok(
      failedRows.some((r) => r.includes('批量变体') && r.includes('失败')),
      `步骤行：${failedRows.join(' / ')}`,
    )
  })

  check('无 console 错误', () => {
    assert.deepEqual(consoleErrors, [])
  })

  await browser.close()
  await stopPreview(preview)

  if (failures.length) {
    console.log(`\nRESULT: FAIL（${failures.length} 个问题）`)
    for (const f of failures) console.log(`  - ${f}`)
    process.exitCode = 1
  } else {
    console.log('\nRESULT: PASS（0 个问题）')
  }
}

main().catch(async (err) => {
  console.error(err)
  process.exitCode = 1
})
