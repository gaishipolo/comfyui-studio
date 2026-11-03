/**
 * 真实浏览器验证：流程节点设置弹窗里的「再加工」与「按条件取值」。
 *
 * **自包含**（同 verify_task5_ui.mjs 的做法）：拦截 API 造数据，不需要起
 * 真后端、不需要探针账号。跑法：`node tests/verify_flow_expr_ui.mjs`
 *
 * 验证的是**界面这一侧**：
 * 1. 两块新设置在弹窗里出现
 * 2. 加工函数与运算符都是中文标签（内容同事不认 `truncate`）
 * 3. 用户操作后存下去的是**规范表达式**（不是内部表示的乱拼）
 * 4. 条件区引号配对、布局不重叠
 *
 * **后端认不认这些表达式**由 backend/tests/test_flow_expr_contract.py
 * 负责（那边有一份逐字相同的清单）。两边清单必须一致。
 *
 * 用有头浏览器（无头 Chromium 不占滚动条宽度，测布局会得出假结论）。
 */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromium } from 'playwright'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const PORT = 41736
const BASE_URL = `http://127.0.0.1:${PORT}`

const USER = {
  id: 'owner',
  username: 'owner',
  display_name: 'Owner',
  role: 'admin',
  is_active: true,
  created_at: '2026-10-31T00:00:00',
}

const FIELDS = [
  { name: 'prompt', label: '提示词', type: 'textarea', required: true },
  { name: 'seed', label: '种子', type: 'number', required: false },
]

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
  fields: [
    { name: 'prompt', label: '提示词', type: 'textarea', required: true },
    { name: 'style', label: '画风', type: 'text', required: false },
    { name: 'clean', label: '写实提示词', type: 'textarea', required: false },
  ],
}

const FLOW = {
  id: 'flow-fake',
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
      params: { prompt: '{{ input.prompt }}', seed: '-1' },
    },
  ],
  status: 'active',
  version: 1,
  created_by: 'owner',
  created_at: '2026-10-31T00:00:00',
  updated_at: '2026-10-31T00:00:00',
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

/** 起 vite preview（只提供静态页面，API 全部拦截） */
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

  // 记下保存时提交给后端的步骤
  const saved = []

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    if (path === '/api/auth/login') {
      return json(route, { access_token: 'ui-token', token_type: 'bearer', user: USER })
    }
    if (path === '/api/health') return json(route, { status: 'ok', comfyui: 'connected' })
    if (path === '/api/flows' && request.method() === 'GET') return json(route, [FLOW])
    if (
      (path === '/api/flows' || path === '/api/flows/flow-fake') &&
      request.method() === 'PUT'
    ) {
      saved.push(request.postDataJSON())
      return json(route, FLOW)
    }
    if (path === '/api/flows/capabilities') return json(route, [CAPABILITY])
    if (path === '/api/flows/validate') return json(route, { ok: true, errors: [], warnings: [], layers: [['s1']] })
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
  await page.click('button:has-text("编排")')
  await page.waitForTimeout(1200)

  // 双击节点卡片
  const step = await page.$('ol li[draggable="true"]')
  assert.ok(step, '找不到节点卡片')
  await step.dblclick()
  await page.waitForTimeout(1000)

  const dialog = await page.$('[role="dialog"]')
  assert.ok(dialog, '节点设置弹窗没打开')
  const text = await dialog.textContent()

  check('弹窗里有「取到之后再处理」', () => {
    assert.ok(text.includes('取到之后再处理'), '缺「取到之后再处理」')
  })
  check('弹窗里有「按条件取值」', () => {
    assert.ok(text.includes('按条件取值'), '缺「按条件取值」')
  })

  // 加工函数与运算符都是中文标签
  const transformSelect = await page.$('select[aria-label="添加一道处理"]')
  assert.ok(transformSelect, '找不到「添加一道处理」下拉')
  const transformLabels = await transformSelect.$$eval('option', (els) =>
    els.map((e) => e.textContent.trim()).filter((t) => t !== '＋ 处理…'),
  )
  check('加工函数是中文标签', () => {
    assert.ok(transformLabels.length >= 5, `选项太少：${transformLabels}`)
    for (const label of transformLabels) {
      assert.ok(
        /[一-龥]/.test(label),
        `「${label}」不是中文标签`,
      )
    }
    assert.ok(transformLabels.includes('去掉首尾空格'), transformLabels.join('|'))
    assert.ok(transformLabels.includes('截断到'), transformLabels.join('|'))
  })

  // 加工链的操作
  await transformSelect.selectOption('truncate')
  await page.waitForTimeout(400)
  const argInput = await page.$('[role="dialog"] input[aria-label="字数"]')
  check('带参数的加工有参数输入框', () => {
    assert.ok(argInput, '找不到「字数」输入框')
  })
  if (argInput) await argInput.fill('80')
  await page.waitForTimeout(300)

  // 条件的操作
  const condBtn = await page.$('button:has-text("加一个条件")')
  check('有「加一个条件」按钮', () => {
    assert.ok(condBtn, '找不到「加一个条件」按钮')
  })
  if (condBtn) {
    await condBtn.click()
    await page.waitForTimeout(600)
    const fieldSel = await page.$('select[aria-label="条件判断的流程输入"]')
    assert.ok(fieldSel, '找不到条件判断字段下拉')
    const fieldOpts = await fieldSel.$$eval('option', (els) =>
      els.map((e) => ({ v: e.value, t: e.textContent.trim() })),
    )
    const style = fieldOpts.find((o) => o.t.includes('画风'))
    if (style) await fieldSel.selectOption(style.v)
    await page.waitForTimeout(300)

    const cmp = await page.$('input[aria-label="条件的比较值"]')
    if (cmp) await cmp.fill('写实')
    await page.waitForTimeout(300)

    const thenSel = await page.$('select[aria-label="条件成立时用的值"]')
    assert.ok(thenSel, '找不到「条件成立时用的值」下拉')
    const thenOpts = await thenSel.$$eval('option', (els) =>
      els.map((e) => ({ v: e.value, t: e.textContent.trim() })),
    )
    const clean = thenOpts.find((o) => o.t.includes('写实提示词'))
    if (clean) await thenSel.selectOption(clean.v)
    await page.waitForTimeout(400)

    // 条件区的引号要配对
    const condText = await dialog.textContent()
    const openCount = (condText.match(/「/g) || []).length
    const closeCount = (condText.match(/」/g) || []).length
    check('条件区引号配对', () => {
      assert.equal(openCount, closeCount, `「${openCount} 个 / 」${closeCount} 个`)
    })
  }

  // 应用 + 保存
  await page.click('[role="dialog"] button:has-text("应用")')
  await page.waitForTimeout(600)
  await page.click('button:has-text("保存")')
  await page.waitForTimeout(1200)

  check('保存后有提交给后端', () => {
    assert.ok(saved.length > 0, '没有发出 PUT 请求')
  })

  const params = saved[saved.length - 1]?.steps?.[0]?.params
  check('存下去的是规范表达式', () => {
    assert.ok(params, '没拿到保存的参数')
    assert.equal(
      params.prompt,
      '{{ input.style == "写实" ? input.clean : input.prompt | truncate("80") }}',
      `表达式不对：${params.prompt}`,
    )
  })
  check('没被改坏的参数保持原样', () => {
    assert.equal(params.seed, '-1', `seed 被改了：${params.seed}`)
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
