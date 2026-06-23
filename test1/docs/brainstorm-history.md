# Brainstorm history

> Read-only record of the design conversation before development began.

## Original goal

帮我写一个类似typora的md阅读编辑软件，实现全部typora的核心功能，ui 配色类似。使用方便。

## Conversation

### AI · Question 1

这个 Markdown 编辑器的目标平台是什么？

### User · Answer 1

桌面应用 (Electron/Tauri，类似 Typora 原生体验)

### AI · Question 2

Typora 的核心功能很多，你希望 MVP（最小可用版本）优先实现哪些功能？

### User · Answer 2

两者都要：默认所见即所得，可切换到源码模式

### AI · Question 3

技术栈选择：Electron 还是 Tauri？

### User · Answer 3

Electron (生态成熟，类似 Typora 原生实现)

### AI · Question 4

Typora 支持数学公式 (LaTeX)、Mermaid/流程图、表格编辑、代码高亮、导出 (PDF/HTML/Word)、主题切换等功能。MVP 版本你希望优先实现哪些？

### User · Answer 4

全部都要（一步到位，实现 Typora 全部核心功能）

### AI · Question 5

Typora 支持多种主题（包括暗黑模式）。你的 MVP 需要支持暗黑模式切换吗？还是只做默认的浅色主题？

### User · Answer 5

只做默认浅色主题，暗黑后续再加

### AI · Question 6

目标用户主要是哪类人群？这会影响UI复杂度和功能优先级

### User · Answer 6

两者兼顾（类似Typora的通用定位）

### AI · Question 7

文件管理方式：Typora 支持打开单个文件，也支持打开整个文件夹（左侧文件树）。你的 MVP 需要支持哪种模式？

### User · Answer 7

两者都要（单文件打开 + 可选展开文件夹侧边栏）

### AI · Question 8

界面语言定位是什么？这会影响菜单、提示文字、快捷键说明等所有UI文案。

### User · Answer 8

中英双语支持（可切换语言）

### AI · Question 9

WYSIWYG 渲染引擎的选择：Typora 使用自研渲染引擎实现所见即所得，你倾向于哪种技术方案？

### User · Answer 9

基于 ProseMirror / TipTap（成熟的富文本编辑框架，Markdown 适配好）

### AI · Question 10

关于文件格式支持：Typora 支持多种导入导出格式（如 .md, .html, .pdf, .docx 等）。你的 MVP 需要支持哪些格式？

### User · Answer 10

支持 .md + 导出为 PDF/HTML

## Initial AI proposal

# Rmd — Typora-like Markdown Editor

## Purpose
构建一个功能完整、体验流畅的桌面 Markdown 编辑器，定位为 Typora 的开源替代品。

## Core Features
### 编辑体验
- **所见即所得（WYSIWYG）**：默认模式，基于 TipTap/ProseMirror 实现
- **源码模式**：可切换，支持语法高亮的纯 Markdown 编辑
- **实时渲染**：输入即时呈现格式效果，无需手动切换预览

### Markdown 扩展
- **数学公式**：LaTeX 行内/块级公式，基于 KaTeX 或 MathJax
- **Mermaid 图表**：流程图、时序图、甘特图等，实时渲染
- **表格**：可视化编辑，支持对齐、增删行列
- **代码块**：语法高亮（基于 Shiki/Prism），支持 100+ 语言
- **图片**：支持插入本地/网络图片，拖拽粘贴
- **HTML 嵌入**：支持在 Markdown 中嵌入原始 HTML

### 文件管理
- **单文件打开**：打开/编辑/保存单个 .md 文件
- **文件夹侧边栏**：可选展开，显示目录树，快速切换文件
- **最近文件**：记录最近打开的文件列表

### 导入导出
- **原生格式**：.md 文件读写
- **导出 PDF**：基于 Electron 的打印功能，保留样式
- **导出 HTML**：生成独立 HTML 文件，内联样式

### 界面与国际化
- **UI 风格**：简洁优雅，参考 Typora 浅色主题配色
- **中英双语**：支持界面语言切换
- **标准菜单栏**：文件/编辑/视图/帮助等标准菜单

### 其他
- **快捷键**：标准 Markdown 快捷键（Ctrl+B 加粗、Ctrl+I 斜体等）
- **自动保存**：可选的自动保存功能
- **拼写检查**：基础拼写检查支持

## Out of Scope (MVP)
- 暗黑模式（后续版本）
- 自定义主题
- 插件系统
- 版本控制集成
- 多标签页（后续考虑）
- Word/其他格式导出
- 移动端/网页版

## Constraints
- 基于 Electron 构建，类似 Typora 的原生体验
- 前端使用 TipTap + ProseMirror 作为 WYSIWYG 引擎
- 后端（主进程）负责文件 I/O、菜单、原生对话框
- UI 框架选择：React + Tailwind CSS（或类似方案）
- 代码编辑器组件：CodeMirror 6（源码模式）

## Tech Stack
- **Runtime**: Electron
- **Frontend**: React + TypeScript
- **WYSIWYG Engine**: TipTap (基于 ProseMirror)
- **Source Editor**: CodeMirror 6
- **Math**: KaTeX
- **Diagrams**: Mermaid.js
- **Code Highlight**: Shiki
- **Styling**: Tailwind CSS
- **Build**: Vite + electron-builder
- **i18n**: i18next

## Success Criteria
1. 能流畅打开/编辑/保存 .md 文件，无明显延迟
2. WYSIWYG 模式下实时渲染 Markdown 格式
3. 数学公式、Mermaid 图表、表格、代码块均正确渲染
4. PDF/HTML 导出保留完整样式
5. 中英文界面切换正常
6. 文件夹侧边栏可正常展开/收起
7. 界面风格与 Typora 相似，简洁美观
