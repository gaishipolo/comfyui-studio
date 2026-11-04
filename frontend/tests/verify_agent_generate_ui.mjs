/**
 * 真实浏览器验证：「一句话生成流程」这一侧。
 *
 * 自包含（拦截 API，不起真后端、不调真模型）—— 同 verify_agent_flow_ui.mjs。
 *
 * 验证的是界面与治理口径：
 * 1. 编排器侧栏有入口
 * 2. 打开后能看到需求输入框与当前模型
 * 3. 生成请求真的带上了需求
 * 4. 生成后列表出现新草稿，并自动选中
 * 5. 选中的是「待审批」草稿，Agent 说明可见（模型 + 说明）
 * 6. 后端没配模型时，界面直接说明原因，不给一个点了就报错的按钮
 */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromium } from 'playwright'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const PORT = 41739
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

const GENERATED_NOTE = '模型 ui-fake-model · 第 1 次尝试通过校验 · 一步出图，提示词由运营填'

function generatedFlow() {
  return {
    id: 'flow-gen',
    name: '拉布拉多换边牧',
    description: '先生成参考图，再换主体',
    category: 'image',
    icon: '',
    input_schema: {
      fields: [{ name: 'prompt', label: '提示词', type: 'textarea', required: true }],
    },
    outputs: [{ name: 'image', from_step: 's1', pick: 'all' }],
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
    status: 'draft',
    version: 1,
    created_by: 'owner',
    created_at: '2026-11-04T00:00:00',
    updated_at: '2026-11-04T00:00:00',
    agent_note: GENERATED_NOTE,
  }
}

const state = {
  flows: [],
  configured: true,
  generateBody: null,
  generateCalls: 0,
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
    if (path === '/api/flows/agent/status') {
      return json(route, {
        configured: state.configured,
        model: state.configured ? 'ui-fake-model' : '',
      })
    }
    if (path === '/api/flows/agent/generate' && method === 'POST') {
      state.generateCalls += 1
      state.generateBody = request.postDataJSON()
      state.flows = [generatedFlow(), ...state.flows]
      return json(route, {
        ok: true,
        flow_id: 'flow-gen',
        name: '拉布拉多换边牧',
        note: GENERATED_NOTE,
        errors: [],
        warnings: [],
        layers: [['s1']],
        attempts: [{ attempt: 1, stage: 'ok', errors: [] }],
        model: 'ui-fake-model',
        message: '已生成草稿「拉布拉多换边牧」，试跑确认效果后再审批上线',
      })
    }
    if (path === '/api/flows' && method === 'GET') return json(route, state.flows)
    if (path === '/api/flows/capabilities') return json(route, [CAPABILITY])
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
  await page.waitForTimeout(1000)
  await page.click('button:text-is("编排")')
  await page.waitForTimeout(1000)

  const entryCount = await page.locator('button:has-text("一句话生成")').count()
  check('编排器有「一句话生成」入口', () => {
    assert.ok(entryCount, '侧栏没有入口')
  })

  await page.click('button:has-text("一句话生成")')
  await page.waitForTimeout(600)

  const panel = await page.textContent('body')
  check('面板打开后有需求输入框', () => {
    assert.ok(panel.includes('想做什么'), '没有需求输入区')
  })
  check('显示当前模型', () => {
    assert.ok(panel.includes('ui-fake-model'), '没显示模型名')
  })

  const requirement = '先用假文生图生成一张拉布拉多拉拖拉机，再把拉布拉多换成边牧'
  await page.fill('textarea[placeholder*="拉布拉多"]', requirement)
  await page.click('button:has-text("生成草稿")')
  await page.waitForTimeout(1200)

  check('生成请求带上了需求', () => {
    assert.ok(state.generateCalls === 1, `生成了 ${state.generateCalls} 次`)
    assert.equal(state.generateBody?.requirement, requirement, state.generateBody)
  })

  const after = await page.textContent('body')
  check('列表出现新草稿', () => {
    assert.ok(after.includes('拉布拉多换边牧'), '列表里没看到生成的流程')
  })
  check('草稿显示待审批', () => {
    assert.ok(after.includes('待审批'), '没有「待审批」标记')
  })
  check('Agent 说明可见', () => {
    assert.ok(after.includes('ui-fake-model'), '说明里没有模型名')
    assert.ok(after.includes('一步出图'), '说明里没有 Agent 的说明文字')
  })

  // 收起面板 → 模拟后端被拔掉模型配置 → 再打开
  state.configured = false
  await page.click('button:has-text("收起")')
  await page.waitForTimeout(300)
  await page.click('button:has-text("一句话生成")')
  await page.waitForTimeout(600)

  const offline = await page.textContent('body')
  const offlineInputs = await page.locator('textarea[placeholder*="拉布拉多"]').count()
  check('未配置模型时直接说明原因', () => {
    assert.ok(offline.includes('还没配置编排模型'), '没有给出未配置提示')
    assert.equal(offlineInputs, 0, '未配置时不该还给一个点了会失败的输入框')
  })

  check('没有前端报错', () => {
    assert.deepEqual(consoleErrors, [], consoleErrors.join('\n'))
  })

  await page.waitForTimeout(500)
  await browser.close()
  await stopPreview(preview)

  console.log(`\nRESULT: ${9 - failures.length} passed / ${failures.length} failed`)
  if (failures.length) {
    console.log(failures.join('\n'))
    process.exitCode = 1
  }
}

main().catch((err) => {
  console.error(err)
  process.exitCode = 1
})
