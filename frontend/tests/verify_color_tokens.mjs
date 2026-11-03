/**
 * M13 复检：Tailwind 颜色 token 完整性
 *
 * 背景：M13 的原症状是「源码里用了 text-warning / bg-danger/10 这类类名，但
 * tailwind.config.js 没注册对应 token」→ Tailwind 静默跳过，样式失效且构建不报错。
 *
 * 三层验证：
 *   1. 源码（tsx/ts/html）里所有颜色类都有定义（内置色板 or extend.colors）
 *   2. src/index.css 的 @apply 里所有颜色类都有定义
 *   3. tokens.css 里 CSS 变量格式与 config 引用方式匹配
 *      （oklch(var(--x) / <alpha-value>) 要求变量是「52% 0.19 27」这种通道值；
 *        var(--x) 要求变量是完整颜色值 —— 错配会静默失效）
 *   4. 构建产物里关键语义类真的生成了（dist 存在时才查）
 *
 * 用法：node tests/verify_color_tokens.mjs
 */
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

const BUILTIN = new Set(Object.keys(require('tailwindcss/colors')))

const cfgSrc = fs.readFileSync('tailwind.config.js', 'utf8')
const colorBlock = cfgSrc.slice(cfgSrc.indexOf('colors: {'), cfgSrc.indexOf('fontFamily'))
const CUSTOM = new Set(
  [...colorBlock.matchAll(/^\s*'?([a-zA-Z0-9-]+)'?\s*:/gm)].map((m) => m[1]),
)
CUSTOM.delete('colors')

// extend.colors 里哪些走 oklch 通道
const OKLCH_CHANNEL = new Set(
  [...colorBlock.matchAll(/^\s*'?([a-zA-Z0-9-]+)'?\s*:\s*'oklch\(var\(/gm)].map((m) => m[1]),
)
const VAR_REF = new Set(
  [...colorBlock.matchAll(/^\s*'?([a-zA-Z0-9-]+)'?\s*:\s*'var\(--/gm)].map((m) => m[1]),
)

// 非颜色后缀（尺寸/方向/对齐/字号 key 等），命中即跳过
const NON_COLOR = new Set(
  `
0 px 0.5 1 1.5 2 2.5 3 3.5 4 5 6 7 8 9 10 11 12 14 16 20 24 28 32 36 40 44 48 52 56 60 64 72 80 96
xs sm md lg xl 2xl 3xl 4xl 5xl 6xl 7xl 8xl 9xl
none auto full screen min max fit
t r b l x y s e start end
solid dashed dotted double hidden
left center right justify start
top bottom middle baseline
wrap nowrap balance pretty ellipsis clip
cover contain repeat no-repeat fixed local scroll
thin medium thick
transparent current inherit invert inset
base raised dropdown sticky modal toast
card pill container measure lede form
control hit row gutter
display display-s section title lead
collapse separate
`.split(/\s+/).filter(Boolean),
)

const SIDE_WIDTH = /^[tblxyrse]-\d+(\.\d+)?$/
const PROPS = 'text|bg|border|ring|fill|stroke|from|to|via|divide|placeholder|shadow|outline|accent|decoration|caret'
const RE = new RegExp(
  `(?:^|[\\s"'\`(:])(?:[a-z-]+:)*!?(${PROPS})-([a-z][a-z0-9]*(?:-[a-z0-9]+)*)(?:\\/[0-9]+)?`,
  'g',
)

const problems = []
const results = []
function report(name, ok, info = '') {
  results.push([name, ok, info])
}

function isDefined(prop, name) {
  if (NON_COLOR.has(name) || SIDE_WIDTH.test(name)) return true
  if (BUILTIN.has(name) || CUSTOM.has(name)) return true
  return [...CUSTOM].some((c) => name.startsWith(c + '-'))
}

function walk(dir, exts, out = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name)
    if (e.isDirectory()) walk(p, exts, out)
    else if (exts.some((x) => e.name.endsWith(x))) out.push(p)
  }
  return out
}

// ── 1. 源码 tsx/ts/html ──
const codeFiles = walk('src', ['.ts', '.tsx', '.html'])
const codeBad = new Map()
for (const file of codeFiles) {
  const lines = fs.readFileSync(file, 'utf8').split('\n')
  lines.forEach((line, i) => {
    for (const m of line.matchAll(RE)) {
      if (isDefined(m[1], m[2])) continue
      const key = `${m[1]}-${m[2]}`
      if (!codeBad.has(key)) codeBad.set(key, [])
      codeBad.get(key).push(`${file}:${i + 1}`)
    }
  })
}
report(
  `源码（${codeFiles.length} 个文件）无未定义颜色类`,
  codeBad.size === 0,
  codeBad.size ? [...codeBad.keys()].join(', ') : '0 个',
)

// ── 2. index.css 的 @apply ──
const cssFiles = walk('src', ['.css'])
const applyBad = new Map()
let applyCount = 0
for (const file of cssFiles) {
  const lines = fs.readFileSync(file, 'utf8').split('\n')
  lines.forEach((line, i) => {
    const m = line.match(/@apply\s+([^;]+);/)
    if (!m) return
    applyCount++
    for (const tok of m[1].trim().split(/\s+/)) {
      const cm = tok.match(new RegExp(`^(?:[a-z-]+:)*!?(${PROPS})-([a-z][a-z0-9]*(?:-[a-z0-9]+)*)(?:\\/[0-9]+)?$`))
      if (!cm || isDefined(cm[1], cm[2])) continue
      const key = `${cm[1]}-${cm[2]}`
      if (!applyBad.has(key)) applyBad.set(key, [])
      applyBad.get(key).push(`${file}:${i + 1}`)
    }
  })
}
report(
  `@apply 语句（${applyCount} 条）无未定义颜色类`,
  applyBad.size === 0,
  applyBad.size ? [...applyBad.keys()].join(', ') : '0 个',
)

// ── 3. tokens.css 变量格式与 config 引用方式匹配 ──
const tokensPath = cssFiles.find((f) => f.endsWith('tokens.css'))
if (!tokensPath) {
  report('tokens.css 存在', false, '未找到')
} else {
  const tokens = fs.readFileSync(tokensPath, 'utf8')
  const vars = new Map(
    [...tokens.matchAll(/--color-([a-z0-9-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]),
  )
  const mismatched = []
  for (const token of OKLCH_CHANNEL) {
    const cssVar = token.replace(/^color-/, '')
    const raw = vars.get(cssVar)
    if (raw === undefined) {
      mismatched.push(`${token}: 变量 --color-${cssVar} 缺失`)
    } else if (/^oklch\(/.test(raw)) {
      mismatched.push(`${token}: 变量应是通道值（如 "52% 0.19 27"），实际是完整颜色 "${raw}"`)
    }
  }
  for (const token of VAR_REF) {
    const cssVar = token.replace(/^color-/, '')
    const raw = vars.get(cssVar)
    if (raw === undefined) {
      mismatched.push(`${token}: 变量 --color-${cssVar} 缺失`)
    } else if (!/^(oklch|rgb|hsl|#|color-mix|light-dark)/.test(raw)) {
      mismatched.push(`${token}: 变量应是完整颜色值，实际是通道 "${raw}"`)
    }
  }
  report(
    `tokens.css 变量格式匹配（oklch 通道 ${OKLCH_CHANNEL.size} 个 / var 引用 ${VAR_REF.size} 个）`,
    mismatched.length === 0,
    mismatched.join('; ') || '全部匹配',
  )
}

// ── 4. 构建产物（dist 存在时）──
const distCss = fs.existsSync('dist/assets')
  ? fs.readdirSync('dist/assets').filter((f) => f.endsWith('.css')).map((f) => path.join('dist/assets', f))
  : []
if (distCss.length === 0) {
  report('构建产物校验', true, 'dist 不存在，跳过')
} else {
  const css = distCss.map((f) => fs.readFileSync(f, 'utf8')).join('\n')
  const mustHave = ['text-warning', 'bg-danger\\/10', 'bg-secondary', 'border-tertiary']
  const missing = mustHave.filter((c) => !css.includes(`.${c}`))
  report(
    `构建产物包含关键语义类（${distCss.map((f) => path.basename(f)).join(', ')}）`,
    missing.length === 0,
    missing.length ? `缺失: ${missing.join(', ')}` : `全部存在（检查 ${mustHave.length} 个）`,
  )
}

// ── 输出 ──
console.log('=== M13 Tailwind 颜色 token 复检 ===')
console.log(`内置色板 ${BUILTIN.size} 个 | 自定义 token ${CUSTOM.size} 个: ${[...CUSTOM].join(', ')}`)
console.log('')
let ok = true
for (const [name, pass, info] of results) {
  console.log(`[${pass ? 'PASS' : 'FAIL'}] ${name}`)
  console.log(`      -> ${info}`)
  ok = ok && pass
}
console.log('')
console.log('RESULT:', ok ? 'ALL PASS' : 'HAS FAIL')
process.exit(ok ? 0 : 1)
