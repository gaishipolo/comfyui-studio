#!/usr/bin/env bash
# UI 端到端验证 —— 必须在【同一个任务】里 open + 交互
# 原因：agent-browser 的 daemon 会随宿主任务结束被回收，跨任务调用会拿到 about:blank
AB="npx -y agent-browser --session wbui2"
OUT="E:/xx/ai/comfyui-studio/docs/reviews/ui-screenshots"
URL="http://[::1]:5173/"
mkdir -p "$OUT"

step() { echo; echo "═══════ $1 ═══════"; }

step "1. 打开页面（同一任务内，daemon 才活着）"
$AB open "$URL" 2>&1 | tail -3
$AB wait --load networkidle 2>&1 | tail -2
echo "title: $($AB get title 2>&1 | tail -1)"
echo "url:   $($AB get url 2>&1 | tail -1)"

step "2. 登录页 DOM"
$AB eval "document.body.innerText.slice(0,300)" 2>&1 | tail -12
$AB screenshot "$OUT/01-login.png" 2>&1 | tail -1

step "3. 用 JS 填表并提交（不依赖 refs）"
$AB eval "
(() => {
  const ins = [...document.querySelectorAll('input')];
  const out = ins.map(i => i.type + ':' + (i.placeholder||i.name||'?'));
  const u = ins.find(i => /user|用户名/i.test(i.placeholder+i.name)) || ins[0];
  const p = ins.find(i => /pass|密码/i.test(i.placeholder+i.name)) || ins[1];
  if (!u || !p) return 'NO_INPUTS found=' + JSON.stringify(out);
  const set = (el, v) => {
    const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    d.call(el, v); el.dispatchEvent(new Event('input', {bubbles:true}));
  };
  set(u, 'admin'); set(p, 'admin123');
  const btn = [...document.querySelectorAll('button')].find(b => /登录|login/i.test(b.innerText));
  if (!btn) return 'NO_LOGIN_BUTTON buttons=' + JSON.stringify([...document.querySelectorAll('button')].map(b=>b.innerText));
  btn.click();
  return 'OK inputs=' + JSON.stringify(out) + ' clicked=' + btn.innerText;
})()
" 2>&1 | tail -4

$AB wait 2500 2>&1 | tail -1
echo "登录后 url: $($AB get url 2>&1 | tail -1)"
echo "登录后 title: $($AB get title 2>&1 | tail -1)"

step "4. 登录后 DOM"
$AB eval "document.body.innerText.slice(0,500)" 2>&1 | tail -18
$AB screenshot "$OUT/02-after-login.png" 2>&1 | tail -1

step "5. 页面错误"
$AB errors 2>&1 | tail -12

step "6. 前端发出的 API 请求"
$AB network requests --filter api 2>&1 | tail -25

step "7. 切到生成工作台"
$AB eval "
(() => {
  const t = [...document.querySelectorAll('button,a,li,div[role=tab],span')]
    .find(e => /^生成|文生图|生成工作/.test((e.innerText||'').trim()) && e.offsetParent);
  if (!t) return 'NO_NAV: ' + JSON.stringify([...document.querySelectorAll('button,a')].map(e=>(e.innerText||'').trim()).filter(Boolean).slice(0,12));
  t.click(); return 'CLICKED: ' + t.innerText.trim();
})()
" 2>&1 | tail -3
$AB wait 2000 2>&1 | tail -1
$AB eval "document.body.innerText.slice(0,500)" 2>&1 | tail -18
$AB screenshot "$OUT/03-generate.png" 2>&1 | tail -1

step "8. 切到素材库"
$AB eval "
(() => {
  const t = [...document.querySelectorAll('button,a,li,div[role=tab],span')]
    .find(e => /素材|图库|作品/.test((e.innerText||'').trim()) && e.offsetParent);
  if (!t) return 'NO_NAV';
  t.click(); return 'CLICKED: ' + t.innerText.trim();
})()
" 2>&1 | tail -3
$AB wait 2500 2>&1 | tail -1
$AB eval "document.body.innerText.slice(0,500)" 2>&1 | tail -18
$AB screenshot "$OUT/04-assets.png" 2>&1 | tail -1

step "9. 切到任务"
$AB eval "
(() => {
  const t = [...document.querySelectorAll('button,a,li,div[role=tab],span')]
    .find(e => /^任务|任务列表|任务队列/.test((e.innerText||'').trim()) && e.offsetParent);
  if (!t) return 'NO_NAV';
  t.click(); return 'CLICKED: ' + t.innerText.trim();
})()
" 2>&1 | tail -3
$AB wait 2500 2>&1 | tail -1
$AB eval "document.body.innerText.slice(0,500)" 2>&1 | tail -18
$AB screenshot "$OUT/05-tasks.png" 2>&1 | tail -1

step "10. 轮询行为检查（F2 回归）"
echo "GET /api/tasks 请求数："
$AB network requests --filter /api/tasks 2>&1 | grep -c "GET" || echo 0
$AB network requests --filter /api/tasks 2>&1 | tail -12

step "11. 最终错误"
$AB errors 2>&1 | tail -12

echo
echo "═══════ UI 验证完成 ═══════"
