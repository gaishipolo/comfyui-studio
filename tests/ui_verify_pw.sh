#!/usr/bin/env bash
# UI 端到端验证 —— 必须在【单个任务】内完成 open→交互→截图→close
# 原因：浏览器会话随宿主任务结束而销毁，跨任务调用会报 "please run playwright-cli open"
PW="npx -y @playwright/cli@latest"
OUT="E:/xx/ai/comfyui-studio/docs/reviews/ui-screenshots"
SNAP="E:/xx/ai/comfyui-studio/tests/.ui-snap.yaml"  # 用盘符绝对路径（/tmp 在 Git Bash 与 Node 间映射不一致）
URL="http://127.0.0.1:8000/"
mkdir -p "$OUT"

step() { echo; echo "═══════ $1 ═══════"; }
# 从 snapshot 里按文本找【交互元素】的 ref。
# 必须先过滤到 textbox/button/link/tab —— 否则会匹配到 label/paragraph 等纯文本节点，
# 拿到的 ref 点不动（教训：`用户名` 先命中 label 的 ref，而不是输入框）。
ref() {
  grep -iE "$1" "$SNAP" \
    | grep -iE '(textbox|button|link|tab|checkbox|combobox|radio)[ /"]' \
    | grep -oE 'e[0-9]+' | head -1
}
page_text() { $PW eval "() => document.body.innerText.slice(0,${1:-600})" 2>&1 \
  | sed -n '/### Result/,$p' | tail -n +2 | sed '/^```/d' | head -30; }

# 取 eval 的返回值正文（去掉 ### Result 包装）
# ⚠️ playwright-cli 的 eval 返回值是 **JSON 字符串字面量**：外层带引号、内部 \" 转义，
# 例如 "{\"total\":16}"。直接 grep '"total"' 会落空（实际文本是 \"total\"），
# 必须先反转义再解析 —— 踩过一次，误以为变量是空的。
ev() { $PW eval "$1" 2>&1 | sed -n '/### Result/,$p' | tail -n +2 | sed '/^```/d' | head -5 \
  | sed 's/\\"/"/g' | sed 's/^"//; s/"$//'; }

# 图片真实加载统计 —— B30 断言：缩略图必须真渲染出来，而不是只看"没有报错"
img_stats() { ev "() => { const i=[...document.querySelectorAll('img')]; return JSON.stringify({total:i.length, loaded:i.filter(e=>e.naturalWidth>0).length, broken:i.filter(e=>e.complete&&e.naturalWidth===0).length, pending:i.filter(e=>!e.complete).length}); }"; }

step "1. 打开（同一任务内）"
$PW open "$URL" 2>&1 | tail -2
$PW resize 1440 900 2>&1 | tail -1
echo "title: $($PW eval "() => document.title" 2>&1 | sed -n '/### Result/,$p' | tail -n +2 | head -1)"

step "2. 登录页快照与截图"
$PW snapshot --filename="$SNAP" 2>&1 | tail -1
page_text 300
$PW screenshot --filename="$OUT/pw-01-login.png" 2>&1 | tail -1

step "3. 取 ref 并登录"
U=$(ref '请输入用户名'); P=$(ref '请输入密码'); B=$(ref '"登 *录"')
echo "refs: user=$U pass=$P btn=$B"
if [ -z "$U" ] || [ -z "$P" ] || [ -z "$B" ]; then
  echo "!! 未能定位登录表单，快照内容："; cat "$SNAP"; $PW close >/dev/null 2>&1; exit 1
fi
$PW fill "$U" "admin" 2>&1 | tail -1
$PW fill "$P" "admin123" 2>&1 | tail -1
$PW click "$B" 2>&1 | tail -1
$PW run-code "async page => { await page.waitForTimeout(2500); return 'ok'; }" 2>&1 | tail -1

step "4. 登录后状态"
echo "url: $($PW eval "() => location.href" 2>&1 | sed -n '/### Result/,$p' | tail -n +2 | head -1)"
page_text 700

# 显式断言登录成功：登录表单必须消失，否则后续步骤全部白跑
$PW snapshot --filename="$SNAP" 2>&1 | tail -1
if grep -q '请输入用户名' "$SNAP"; then
  echo "!! 登录未成功，仍是登录页。当前 DOM："
  cat "$SNAP"
  $PW screenshot --filename="$OUT/pw-FAIL-login.png" 2>&1 | tail -1
  $PW close >/dev/null 2>&1
  exit 1
fi
echo "  [OK] 登录成功（登录表单已消失）"
$PW screenshot --filename="$OUT/pw-02-dashboard.png" 2>&1 | tail -1

step "5. 控制台错误"
$PW console error 2>&1 | tail -8

# ── 依次切三个模块 ────────────────────────────────
FAIL=0
declare -A IMG
for NAME in 生成:pw-03-generate:GEN 素材:pw-04-assets:ASSETS 任务:pw-05-tasks:TASKS; do
  KEY=$(echo "$NAME" | cut -d: -f1); PIC=$(echo "$NAME" | cut -d: -f2); TAG=$(echo "$NAME" | cut -d: -f3)
  step "6.$KEY 切到「$KEY」"
  $PW snapshot --filename="$SNAP" 2>&1 | tail -1
  R=$(ref "${KEY}")
  echo "  nav ref=$R"
  if [ -n "$R" ]; then
    $PW click "$R" 2>&1 | tail -1
    $PW run-code "async page => { await page.waitForTimeout(2200); return 'ok'; }" 2>&1 | tail -1
    page_text 800
    IMG[$TAG]=$(img_stats)
    echo "  图片加载: ${IMG[$TAG]}"
  else
    echo "  !! 未找到导航项；可见文本："
    $PW eval "() => [...document.querySelectorAll('button,a')].map(e=>(e.innerText||'').trim()).filter(Boolean).slice(0,20).join(' | ')" 2>&1 \
      | sed -n '/### Result/,$p' | tail -n +2 | head -3
    FAIL=1
  fi
  $PW screenshot --filename="$OUT/$PIC.png" 2>&1 | tail -1
done

# ── 断言（控制台检查必须在探测之前：探测会故意发出一个匿名请求，必然产生一条 401）──
step "7. 控制台错误断言"
assert() { # assert "描述" 实际值 期望值
  if [ "$2" = "$3" ]; then echo "  [PASS] $1"; else echo "  [FAIL] $1 —— 实际=$2 期望=$3"; FAIL=1; fi
}
CONSOLE_OUT=$($PW console error 2>&1)
assert "无 401 残留"          "$(echo "$CONSOLE_OUT" | grep -c '401')"            "0"
assert "无 /api/api 双前缀"    "$(echo "$CONSOLE_OUT" | grep -c '/api/api/')"      "0"
assert "无任何控制台错误"      "$(echo "$CONSOLE_OUT" | grep -c '^\[ERROR\]')"     "0"

# ── 图片渲染断言 ──────────────────────────────
step "7.1 图片渲染断言"
TOTAL=$(echo "${IMG[ASSETS]}" | grep -oE '"total":[0-9]+' | grep -oE '[0-9]+')
LOADED=$(echo "${IMG[ASSETS]}" | grep -oE '"loaded":[0-9]+' | grep -oE '[0-9]+')
BROKEN=$(echo "${IMG[ASSETS]}" | grep -oE '"broken":[0-9]+' | grep -oE '[0-9]+')
echo "  素材库 img 统计: total=$TOTAL loaded=$LOADED broken=$BROKEN"
if [ "${TOTAL:-0}" -ge 1 ] 2>/dev/null; then echo "  [PASS] 素材库挂载了 $TOTAL 张 <img>"; else echo "  [FAIL] 素材库一张 <img> 都没挂载（AuthImage 全部取图失败）"; FAIL=1; fi
if [ "${LOADED:-0}" -ge 1 ] 2>/dev/null; then echo "  [PASS] 素材库已真实渲染 $LOADED 张缩略图"; else echo "  [FAIL] 素材库缩略图一张都没渲染出来"; FAIL=1; fi
if [ "${BROKEN:-0}" = "0" ] && [ -n "$BROKEN" ]; then echo "  [PASS] 无加载失败图片"; else echo "  [FAIL] 有 ${BROKEN:-?} 张图片加载失败"; FAIL=1; fi

# ── 删除入口渲染断言（B31）────────────────────────
# 删除行为由 verify_delete_assets_tasks.py 的 25 项 API 断言覆盖（含级联/磁盘/权限），
# 这里只验「入口真的渲染出来了」，且不点删除 —— 库里是真实生成图，不能为了测试删掉。
step "7.1b 删除入口渲染断言"
# 上面的模块循环结束时停在「任务」页，必须先切回素材库再验（否则找不到 img，扫到的是任务按钮）
$PW snapshot --filename="$SNAP" 2>&1 | tail -1
R=$(ref "素材")
[ -n "$R" ] && { $PW click "$R" 2>&1 | tail -1; $PW run-code "async page => { await page.waitForTimeout(2200); return 'ok'; }" 2>&1 | tail -1; }
# 素材库的删除按钮按设计只在「选中素材」或「打开详情」时出现，
# 所以必须先点开一张图的详情再找（空状态去找必然找不到，那是断言写错）。
# React 的 onClick 挂在祖先上，直接 click() 冒泡即可触发。
EVOPEN=$(ev "() => { const img = document.querySelector('img'); if (!img) return 'no-img'; img.click(); return 'clicked'; }")
echo "  打开详情: $EVOPEN"
$PW run-code "async page => { await page.waitForTimeout(900); return 'ok'; }" 2>&1 | tail -1
DELBTN=$(ev "() => { const c=[...document.querySelectorAll('button')].map(b=>(b.innerText||'').trim()).filter(Boolean); return JSON.stringify({del:c.filter(t=>t.includes('删除')).length, dl:c.filter(t=>t.includes('下载')).length, all:c}); }")
echo "  详情面板按钮扫描: $(echo "$DELBTN" | head -c 200)"
if echo "$DELBTN" | grep -qE '"del":[1-9]'; then echo "  [PASS] 素材详情存在删除入口"; else echo "  [FAIL] 素材详情没有删除入口"; FAIL=1; fi
# 任务页：每条非运行中任务都应有删除按钮。
# ⚠️ 上面刚打开了素材详情弹窗，它会盖住导航栏 —— 必须先关掉再切页，
# 否则 Playwright 的 click 会点到遮罩上，页面根本没切过去（踩过一次）。
ev "() => { const b=[...document.querySelectorAll('button')].find(x=>(x.innerText||'').trim()==='✕'); if(b){b.click(); return 'closed';} return 'no-close-btn'; }" | head -2
$PW run-code "async page => { await page.waitForTimeout(700); return 'ok'; }" 2>&1 | tail -1
$PW snapshot --filename="$SNAP" 2>&1 | tail -1
R=$(ref "任务")
echo "  任务导航 ref=$R"
if [ -n "$R" ]; then
  $PW click "$R" 2>&1 | tail -1
  $PW run-code "async page => { await page.waitForTimeout(1800); return 'ok'; }" 2>&1 | tail -1
fi
TASKDEL=$(ev "() => { const b=[...document.querySelectorAll('button[title*=\"删除\"]')]; return JSON.stringify({taskDelete:b.length}); }")
echo "  任务页按钮扫描: $TASKDEL"
if echo "$TASKDEL" | grep -qE '"taskDelete":[1-9]'; then echo "  [PASS] 任务页存在删除入口"; else echo "  [FAIL] 任务页没有删除入口"; FAIL=1; fi

# ── 鉴权探测（会故意产生 1 条 401，故必须排在控制台断言之后）──
step "7.2 B30 鉴权探测 —— 带 token 应 200，匿名应 401"
# cache:no-store + credentials:omit —— 否则第一次带 token 的响应会被浏览器 HTTP 缓存住，
# 第二次"匿名"请求根本没到服务端，会误报成 200（踩过一次）。
PROBE="async () => { const t = localStorage.getItem('token'); const h = t ? { Authorization: 'Bearer ' + t } : {}; const r = await fetch('/api/assets?limit=5', { headers: h, cache: 'no-store' }); const list = await r.json(); const items = Array.isArray(list) ? list : (list.items || list.assets || []); const a = items.find(x => x.task_id && x.file_name); if (!a) return JSON.stringify({ error: 'no asset with task_id/file_name', sample: list }); const u = '/api/tasks/' + encodeURIComponent(a.task_id) + '/assets/' + encodeURIComponent(a.file_name); const ok = await fetch(u, { headers: h, cache: 'no-store' }); const no = await fetch(u, { cache: 'no-store', credentials: 'omit' }); return JSON.stringify({ asset: a.file_name, withToken: ok.status, withoutToken: no.status, ctype: ok.headers.get('content-type') }); }"
PROBE_OUT=$(ev "$PROBE")
echo "  $PROBE_OUT"
assert "带 token 取素材 = 200"       "$(echo "$PROBE_OUT" | grep -oE '"withToken":[0-9]+' | grep -oE '[0-9]+')" "200"
assert "匿名取素材 = 401（安全未削弱）" "$(echo "$PROBE_OUT" | grep -oE '"withoutToken":[0-9]+' | grep -oE '[0-9]+')" "401"

$PW close 2>&1 | tail -1
echo
if [ "$FAIL" = "0" ]; then
  echo "═══════ playwright UI 验证通过 ✓ ═══════"
else
  echo "═══════ playwright UI 验证失败 ✗ ═══════"
  exit 1
fi
