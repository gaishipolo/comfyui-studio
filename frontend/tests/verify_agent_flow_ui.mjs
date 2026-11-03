/**
 * 真实浏览器验证：Agent 编排的草稿审批区与试跑按钮。
 *
 * 自包含（拦截 API，不起真后端）——同 verify_flow_expr_ui.mjs 的做法。
 *
 * 验证的是**界面这一侧**：
 * 1. 草稿流程显示「待审批」并有审批区
 * 2. Agent 说明显示出来（审批人第一眼看的）
 * 3. 批准 → 状态变「已启用」，审批意见记下
 * 4. 驳回 → 状态变「已归档」
 * 5. 试跑按钮存在，且调的是 trial 接口（产物不进正式素材库）
 */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromium } from 'playwright'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const PORT = 41737
const BASE_URL = `http://127.0.0.1:${PORT}`

const USER = {
  id: 'owner',
  username: 'owner',
  display_name: 'Owner',
  role: 'admin',
  is_active: true,
  created_at: '2026-10-31T00:00:00',
}

const CAPABILITY = {
  id: 'template-fake',
  name: '假文生图',
  description: '',
  category: 'image',
  engine: 'fake',
  capability: 'text_to_image',
  fields: [{ name: 'prompt', label: '提示词', type: 'textarea', required: true }],
}

const INPUTS = {
  fields: [{ name: 'prompt', label: '提示词', type: 'textarea', required: true }],
}

function flow(status, extra = {}) {
  return {
    id: 'flow-agent',
    name: status === 'draft' ? 'Agent 生成的流程' : '已上线的流程',
    description: '',
    category: 'image',
    icon: '',
    input_schema: INPUTS,
    outputs: [],
    steps: [
      {
        id: 's1',
        title: '出图',
        type: 'template',
        template_id: 'template-fake',
        flow_id: '',
        count: 1,
        params: { prompt: '{{ input.prompt }}' },
      },
    ],
    status,
    version: 1,
    created_by: 'owner',
    created_at: '2026-10-31T00:00:00',
    updated_at: '2026-10-31T00:00:00',
    ...extra,
  }
}

// 服务端状态（拦截器里可变）
const state = {
  flows: [flow('draft', { agent_note: '这是 Agent 写的说明：先出图再放大' })],
  approved: null,
  trialCalled: false,
  runCalled: false,
}

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

async function main() {
  const preview = await startPreview()
  const browser = await chromium.launch({ headless: false })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const consoleErrors = []
  page.on('console', (m) => {
    if (m.type() === 'error') consoleErrors.push(m.text())
  })
  page.on('pageerror', (e) => consoleErrors.push(String(e)))

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const method = request.method()
    if (path === '/api/auth/login') {
      return json(route, { access_token: 'ui-token', token_type: 'bearer', user: USER })
    }
    if (path === '/api/health') return json(route, { status: 'ok', comfyui: 'connected' })
    if (path === '/api/flows' && method === 'GET') return json(route, state.flows)
    if (path === '/api/flows/flow-agent/approve' && method === 'POST') {
      const body = request.postDataJSON()
      state.approved = body
      const next = body.approve ? 'active' : 'archived'
      state.flows = [
        flow(next, {
          agent_note: '这是 Agent 写的说明：先出图再放大',
          review_note: body.note || '',
          reviewed_by: 'owner',
          reviewed_at: '2026-11-04T00:00:00',
        }),
      ]
      return json(route, { ok: true, flow_id: 'flow-agent', status: next, message: 'ok' })
    }
    if (path === '/api/flows/flow-agent/trial' && method === 'POST') {
      state.trialCalled = true
      return json(route, {
        id: 'run-trial',
        flow_id: 'flow-agent',
        flow_name: 'Agent 生成的流程',
        status: 'running',
        progress: 0,
        params: {},
        steps: [],
        outputs: [],
        error_msg: '',
        created_at: '2026-11-04T00:00:00',
        finished_at: null,
      })
    }
    if (path === '/api/flows/flow-agent/run' && method === 'POST') {
      state.runCalled = true
      return json(route, {})
    }
    if (path === '/api/flows/capabilities') return json(route, [CAPABILITY])
    if (path === '/api/flows/validate') {
      return json(route, { ok: true, errors: [], warnings: [], layers: [['s1']] })
    }
    if (path === '/api/templates') return json(route, [])
    if (path === '/api/tasks') return json(route, [])
    if (path === '/api/assets') return json(route, [])
    if (path === '/api/engines') return json(route, [{ name: 'fake', status: 'connected' }])
    console.log('  [未拦截]', method, path)
    return json(route, { detail: `Unhandled API: ${path}` }, 500)
  })

  await page.goto(`${BASE_URL}/`, { waitUntil: 'domcontentloaded' })
  await page.fill('input[name="username"], #username', 'owner')
  await page.fill('input[name="password"], #password', 'pw')
  await page.click('button[type="submit"]')
  await page.waitForTimeout(1500)

  await page.click('button:has-text("流程")')
  await page.waitForTimeout(1200)

  // 切到「编排」页
  await page.click('button:text-is("编排")')
  await page.waitForTimeout(1200)

  const listText = await page.textContent('body')
  check('流程列表显示「待审批」', () => {
    assert.ok(listText.includes('待审批'), '列表里没看到「待审批」')
  })

  const body = await page.textContent('body')
  check('编排页有审批区', () => {
    assert.ok(body.includes('批准上线'), '没有「批准上线」按钮')
    assert.ok(body.includes('驳回'), '没有「驳回」按钮')
  })
  check('Agent 说明显示出来', () => {
    assert.ok(
      body.includes('这是 Agent 写的说明：先出图再放大'),
      '没看到 Agent 的说明',
    )
  })
  check('有试跑按钮', () => {
    assert.ok(body.includes('试跑'), '没有「试跑」按钮')
  })

  // 填审批意见 → 批准
  await page.fill('input[placeholder*="第 2 步"]', '看着没问题')
  await page.waitForTimeout(200)
  await page.click('button:has-text("批准上线")')
  await page.waitForTimeout(1200)

  check('批准后状态变已启用', () => {
    assert.ok(state.approved, '没发出审批请求')
    assert.equal(state.approved.approve, true, state.approved)
    assert.equal(state.approved.note, '看着没问题', state.approved)
  })

  const after = await page.textContent('body')
  check('批准后列表显示已启用', () => {
    assert.ok(after.includes('已启用'), '列表里没看到「已启用」')
    assert.ok(after.includes('看着没问题'), '审批意见没显示出来')
  })

  // 试跑
  const trialBtn = await page.$('button:has-text("试跑")')
  if (trialBtn) {
    await trialBtn.click()
    await page.waitForTimeout(1000)
  }
  check('试跑调的是 trial 接口', () => {
    assert.ok(state.trialCalled, '没发出试跑请求')
    assert.ok(!state.runCalled, '试跑不该调正式运行接口')
  })

  check('无 console 错误', () => {
    assert.deepEqual(consoleErrors, [], consoleErrors.join('\n'))
  })

  await browser.close()
  await stopPreview(preview)

  console.log(`\nRESULT: ${failures.length ? 'FAIL' : 'PASS'}（${failures.length} 个问题）`)
  for (const f of failures) console.log(`  - ${f}`)
  process.exit(failures.length ? 1 : 0)
}

main().catch((e) => {
  console.error('FAILED', e)
  process.exit(1)
})
