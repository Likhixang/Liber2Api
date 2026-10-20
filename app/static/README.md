# 本地 UI 资源

管理页面不运行外部 CDN 脚本。`lucide.min.js` 来自 npm `lucide@1.60.0` 的 `dist/umd/lucide.min.js`；`ui.css` 使用 `tailwindcss@3.4.17` 对 `app/templates/index.html` 编译，`darkMode: "class"`，保留原 brand 色值。许可证见 `THIRD_PARTY_NOTICES.txt`。

更新模板中的 CSS class 后需重新生成 `ui.css`，运行时无需 Node 或 npm。编译输入为 `@tailwind base; @tailwind components; @tailwind utilities;`，扫描模板全文，使用 `darkMode: "class"`，brand 配色依次为 `50=#f0f9ff`、`500=#0ea5e9`、`600=#0284c7`、`700=#0369a1`。

编译工具仅安装在临时目录，不提交 node_modules。资源变更后执行 `pytest -q` 与浏览器冒烟测试。
