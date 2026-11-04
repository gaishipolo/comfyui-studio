/**
 * 真实浏览器验证：应用列表 + 三栏编辑器。
 *
 * **自包含**：拦截 API 造数据，不需要真后端。跑法：`node tests/verify_app_workspace_ui.mjs`
 *
 * 验证的是这次新引入的「应用」这一层：
 * 1. 应用列表是卡片，**双击**进编辑器（不是单击 —— 单击只是选中）
 * 2. 编辑器是三栏：左节点/素材 · 中步骤 · 右子流程/agent
 * 3. 左栏点节点 → 中栏真的多一步；右栏点子流程 → 中栏多一个子流程步骤
 * 4. 左右两栏的 tab 能来回切
 * 5. 「← 应用」能退回列表
 *
 * 用有头浏览器（无头 Chromium 不占滚动条宽度，量布局会得出假结论）。
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

const FIELDS = [{ name: 'prompt', label: '提示词', type: 'textarea', required: true }]

const CAPS = [
  {
    id: 't1',
    name: 'Flux2 文生图',
    description: '',
    category: 'image',
    engine: 'comfyui',
    capability: 'text_to_image',
    fields: FIELDS,
  },
  {
    id: 't2',
    name: '高清放大 2x',
    description: '',
    category: 'image',
    engine: 'comfyui',
    capability: 'upscale',
    fields: FIELDS,
  },
]

const INPUTS = { fields: FIELDS }

const APP_A = {
  id: 'app-a',
  name: '宠物换装',
  description: '生成一张宠物图再换风格',
  category: 'image',
  icon: '',
  input_schema: INPUTS,
  outputs: [],
  steps: [
    {
      id: 's1',
      title: '生成参考图',
      type: 'template',
      template_id: 't1',
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

const APP_B = {
  id: 'app-b',
  name: '高清放大',
  description: '',
  category: 'image',
  icon: '',
  input_schema: INPUTS,
  outputs: [],
  steps: [
    {
      id: 's1',
      title: '',
      type: 'template',
      template_id: 't2',
      flow_id: '',
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

const ASSETS = [
  {
    id: 'as1',
    task_id: 't1',
    file_path: 'a.png',
    file_name: '参考图.png',
    file_size: 1024,
    mime_type: 'image/png',
    tags: [],
    meta: {},
    created_by: 'owner',
    created_at: '2026-10-31T00:00:00',
  },
  {
    id: 'as2',
    task_id: 't2',
    file_path: 'b.png',
    file_name: '成片.png',
    file_size: 2048,
    mime_type: 'image/png',
    tags: [],
    meta: {},
    created_by: 'owner',
    created_at: '2026-10-31T00:00:00',
  },
]

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
    if (path === '/api/flows' && method === 'GET') return json(route, [APP_A, APP_B])
    if (path === '/api/flows/capabilities') return json(route, CAPS)
    if (path === '/api/flows/validate') {
      return json(route, { ok: true, errors: [], warnings: [], layers: [['s1']] })
    }
    if (path === '/api/flows/agent/status') {
      return json(route, { configured: true, model: 'ui-fake-model' })
    }
    if (path === '/api/assets') return json(route, ASSETS)
    // 缩略图/原图：给一张 1x1 的 PNG。不 mock 的话浏览器会记一条 500，
    // 「无 console 错误」这条断言就会红 —— 那是测试环境的问题，不是页面的
    if (path.startsWith('/api/assets/')) {
      return route.fulfill({
        status: 200,
        contentType: 'image/png',
        body:
          'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
      })
    }
    if (path === '/api/templates') return json(route, [])
    if (path === '/api/tasks') return json(route, [])
    if (path === '/api/engines') return json(route, [{ name: 'comfyui', status: 'connected' }])
    console.log('  [未拦截]', method, path)
    return json(route, { detail: `Unhandled API: ${path}` }, 500)
  })

  await page.goto(`${BASE_URL}/`, { waitUntil: 'domcontentloaded' })
  await page.fill('input[name="username"], #username', 'owner')
  await page.fill('input[name="password"], #password', 'pw')
  await page.click('button[type="submit"]')
  await page.waitForTimeout(1500)

  // ── 应用列表 ────────────────────────────────────────────────
  await page.click('button:text-is("应用")')
  await page.waitForTimeout(1000)

  const cards = await page.$$('[data-app-card]')
  check('应用列表是卡片网格', () => {
    assert.equal(cards.length, 2, `卡片数 ${cards.length}`)
  })

  const listText = await page.textContent('body')
  check('卡片上能看到应用名和状态', () => {
    assert.ok(listText.includes('宠物换装'), '没看到应用名')
    assert.ok(listText.includes('已启用'), '没看到状态')
  })

  // 单击只是选中，不该打开编辑器
  await page.click('[data-app-card="app-a"]')
  await page.waitForTimeout(400)
  const stillList = await page.$('[data-app-workspace="list"]')
  check('单击后还在列表（单击只选中）', () => assert.ok(stillList, '单击就把编辑器打开了'))

  // ── 双击进编辑器 ────────────────────────────────────────────
  await page.dblclick('[data-app-card="app-a"]')
  await page.waitForTimeout(800)

  const editor = await page.$('[data-app-editor]')
  check('双击打开编辑器', () => assert.ok(editor, '编辑器没出现'))

  const leftRail = await page.$('[data-app-rail="left"]')
  const rightRail = await page.$('[data-app-rail="right"]')
  check('左栏（节点/素材）在', () => assert.ok(leftRail, '没有左栏'))
  check('右栏（子流程/agent）在', () => assert.ok(rightRail, '没有右栏'))

  const railTexts = await Promise.all([
    leftRail ? leftRail.textContent() : '',
    rightRail ? rightRail.textContent() : '',
  ])
  check('左栏有「节点」和「素材」两个 tab', () => {
    assert.ok(railTexts[0].includes('节点'), '左栏没有节点 tab')
    assert.ok(railTexts[0].includes('素材'), '左栏没有素材 tab')
  })
  check('右栏有「子流程」和「agent」两个 tab', () => {
    assert.ok(railTexts[1].includes('子流程'), '右栏没有子流程 tab')
    assert.ok(railTexts[1].includes('agent'), '右栏没有 agent tab')
  })

  const columns = await page.evaluate(() => {
    const box = (sel) => {
      const el = document.querySelector(sel)
      if (!el) return null
      const r = el.getBoundingClientRect()
      return { top: Math.round(r.top), h: Math.round(r.height), w: Math.round(r.width) }
    }
    return {
      left: box('[data-app-rail="left"]'),
      mid: box('[data-app-editor]'),
      right: box('[data-app-rail="right"]'),
    }
  })
  check('三栏并排且等高', () => {
    assert.ok(columns.left && columns.mid && columns.right, `栏没渲染全：${JSON.stringify(columns)}`)
    assert.equal(columns.left.top, columns.mid.top, '左栏和中栏没对齐')
    assert.equal(columns.right.top, columns.mid.top, '右栏和中栏没对齐')
    assert.equal(columns.left.h, columns.mid.h, '左栏和中栏不等高')
    assert.equal(columns.right.h, columns.mid.h, '右栏和中栏不等高')
  })

  // ── 左栏点节点 → 中栏多一步 ─────────────────────────────────
  const before = await page.$$('[data-step-id]')
  await page.click('[data-add-node="t2"]')
  await page.waitForTimeout(400)
  const after = await page.$$('[data-step-id]')
  check('左栏点节点能往流程里加一步', () => {
    assert.equal(after.length, before.length + 1, `步骤数 ${before.length} → ${after.length}`)
  })

  // ── 右栏点子流程 → 中栏多一个子流程步骤 ─────────────────────
  const beforeSub = await page.$$('[data-step-id]')
  await page.click('[data-add-subflow="app-b"]')
  await page.waitForTimeout(400)
  const afterSub = await page.$$('[data-step-id]')
  const bodyText = await page.textContent('body')
  check('右栏点子流程能接进来一步', () => {
    assert.equal(afterSub.length, beforeSub.length + 1, `步骤数没有增加`)
    assert.ok(bodyText.includes('子流程'), '步骤上没有子流程标记')
  })

  // ── 切 tab ─────────────────────────────────────────────────
  await page.click('[data-app-rail="left"] button:has-text("素材")')
  await page.waitForTimeout(600)
  const assetLinks = await page.$$('[data-app-rail="left"] a')
  check('切到素材能看到素材缩略图', () => {
    assert.ok(assetLinks.length >= 2, `素材数量 ${assetLinks.length}`)
  })

  await page.click('[data-app-rail="right"] button:has-text("agent")')
  await page.waitForTimeout(400)
  const agentText = await page.$eval('[data-app-rail="right"]', (n) => n.textContent)
  check('切到 agent 有需求输入框', () => {
    assert.ok(agentText.includes('想做什么'), 'agent 面板没内容')
  })

  // ── 返回列表 ───────────────────────────────────────────────
  await page.click('[data-app-back]')
  await page.waitForTimeout(500)
  const backToList = await page.$('[data-app-workspace="list"]')
  check('点「← 应用」退回列表', () => assert.ok(backToList, '没退回列表'))

  check('无 console 错误', () => {
    assert.deepEqual(consoleErrors, [], consoleErrors.join('\n'))
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
