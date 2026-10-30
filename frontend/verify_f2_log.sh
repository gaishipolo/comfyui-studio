#!/bin/bash
cd "E:/xx/ai/comfyui-studio/frontend"
AB="npx -y agent-browser"
$AB open http://localhost:5173/ || exit 1
$AB find placeholder "请输入用户名" fill "admin" || exit 1
$AB find placeholder "请输入密码" fill "admin123" || exit 1
$AB find text "登 录" click || exit 1
sleep 2
$AB eval "[...document.querySelectorAll('button')].find(b=>b.textContent.includes('任务')).click(); 'tasks tab opened'" || exit 1
echo "任务 Tab 已打开，停留 8 秒..."
sleep 8
$AB close 2>/dev/null
echo "浏览器已关闭"
