#!/bin/bash
cd "E:/xx/ai/comfyui-studio/frontend"
AB="npx -y agent-browser"
$AB open http://localhost:5173/ || exit 1
$AB find placeholder "请输入用户名" fill "admin" || exit 1
$AB find placeholder "请输入密码" fill "admin123" || exit 1
$AB find text "登 录" click || exit 1
sleep 2
# 生成 Tab（默认激活），选第一个模板
$AB eval "[...document.querySelectorAll('button')].find(b=>b.textContent.includes('Flux2')).click(); 'template A selected'" || exit 1
sleep 1
# 在 textarea 输入测试值（React 受控组件需要原生 setter + input 事件）
$AB eval "const t=document.querySelector('textarea'); const s=Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set; s.call(t,'AAA_TEST_CROSS'); t.dispatchEvent(new Event('input',{bubbles:true})); 'typed: '+t.value" || exit 1
sleep 1
echo "模板A(Fux2)输入后 textarea: $($AB eval "document.querySelector('textarea')?.value")"
# 切换到第二个模板
$AB eval "[...document.querySelectorAll('button')].find(b=>b.textContent.includes('测试模板-M5')).click(); 'template B selected'" || exit 1
sleep 1
echo "切换到模板B(M5)后 textarea: $($AB eval "document.querySelector('textarea')?.value")"
echo "(若仍显示 AAA_TEST_CROSS => 串场确认; 若为空 => 正常)"
$AB close 2>/dev/null
