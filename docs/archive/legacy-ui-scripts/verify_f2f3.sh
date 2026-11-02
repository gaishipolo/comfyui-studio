#!/bin/bash
# F2 轮询死循环 + F3 表单串场 浏览器实测脚本
cd "E:\xx\ai\comfyui-studio\frontend"
AB="npx -y agent-browser"

echo "=== 1. 打开页面并登录 ==="
$AB open http://localhost:5173/ || exit 1
$AB find placeholder "请输入用户名" fill "admin" || exit 1
$AB find placeholder "请输入密码" fill "admin123" || exit 1
$AB find text "登 录" click || exit 1
sleep 2
echo "URL: $($AB get url)"

echo "=== 2. F2: 注入计数器并点进任务 Tab ==="
$AB eval "window.__taskReq=0; const o=window.fetch; window.fetch=function(...a){ if(String(a[0]).includes('/api/tasks')&&!String(a[0]).includes('assets')) window.__taskReq++; return o.apply(this,a) }; 'counter installed'" || exit 1
$AB eval "[...document.querySelectorAll('button')].find(b=>b.textContent.includes('任务')).click(); 'clicked tasks tab'" || exit 1
echo "等待 10 秒收集请求..."
sleep 10
echo "F2 结果 => 10秒内 /api/tasks 请求数: $($AB eval "window.__taskReq")"
echo "F2 参照 => 若为 3~5 次正常(1次初始+3s轮询)；若 >15 次即请求风暴"

echo "=== 3. F3: 生成 Tab 切换模板看表单是否串场 ==="
$AB eval "[...document.querySelectorAll('button')].find(b=>b.textContent.includes('生成')).click(); 'clicked generate tab'" || exit 1
sleep 1
$AB eval "[...document.querySelectorAll('button')].find(b=>b.textContent.includes('Flux2')).click(); 'clicked template A'" || exit 1
sleep 1
$AB find placeholder "请输入提示词" fill "AAA_TEST_串场检测" 2>/dev/null || $AB eval "const t=document.querySelector('textarea'); const setter=Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set; setter.call(t,'AAA_TEST_串场检测'); t.dispatchEvent(new Event('input',{bubbles:true})); 'typed into textarea'"
sleep 1
echo "模板A输入完成，当前 textarea 值: $($AB eval "document.querySelector('textarea')?.value")"
$AB eval "[...document.querySelectorAll('button')].find(b=>b.textContent.includes('测试模板-M5')).click(); 'clicked template B'" || exit 1
sleep 1
echo "F3 结果 => 切换模板B后 textarea 值: $($AB eval "document.querySelector('textarea')?.value")"
echo "F3 参照 => 若仍为 AAA_TEST_串场检测 即串场确认；若为空/undefined 即正常重置"

$AB screenshot "E:/xx/ai/comfyui-studio/.workbuddy-ai/verify_f2f3.png" 2>/dev/null
echo "=== 验证完成 ==="
