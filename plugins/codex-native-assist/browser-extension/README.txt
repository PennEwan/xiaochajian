Codex Native Assist 浏览器扩展
===============================

充值窗口点击 session 链接时，扩展会用当前 ChatGPT 登录状态读取：
https://chatgpt.com/api/auth/session

安装（Chrome / Edge）：
1. 打开 chrome://extensions 或 edge://extensions。
2. 开启「开发者模式」。
3. 点击「加载已解压的扩展程序」，选择本目录。
4. 保持一个已登录 ChatGPT 的页面打开；充值窗口点击链接后会自动复制返回内容。

扩展只在 ChatGPT 页面运行，并只把一次请求的响应通过 127.0.0.1 回传给本机充值窗口。
