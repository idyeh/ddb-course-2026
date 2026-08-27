# 分布式数据库开发：课件

2026 版课件使用纯 Markdown 编写，并由 Marp 生成离线 HTML。课件不依赖 Quarto 或 Jupyter 才能演示；Python 仅用于课堂实验与示例验证。

## 快速使用

1. 安装 Node.js 20 或更高版本；Node 自带或另行安装 Corepack。
2. 在本目录运行 `corepack pnpm install`。项目固定使用 pnpm 11.19.0，并使用 npm 官方仓库，不受全局 pnpm 及镜像配置影响。
3. 运行 `corepack pnpm present`，在浏览器中选择要演示的课件。
4. 运行 `corepack pnpm build`，在 `dist/` 得到可离线打开的 HTML。

打开 `dist/index.html` 可以进入全部讲座。若需在没有 Chrome 的机器上运行浏览器溢出检查，先执行 `corepack pnpm browser:install`。

也可以安装 VS Code 扩展 **Marp for VS Code**，打开 `slides/` 中任意 Markdown 文件后直接预览和演示。

## 维护约定

- 一张幻灯片只表达一个主要结论。
- 不通过缩小字号解决溢出；内容过多时拆页或移入讲者备注。
- 正文代码原则上不超过 18 行，完整示例放在 `examples/`。
- 随机故障、延迟和调度示例必须显式设置随机种子。
- 外部事实和系统特性以论文或官方文档为准，记录在 `sources.md`。
- 重要内容订正记录在 `errata.md`，纯排版修改不进入勘误表。

## 检查命令

- `corepack pnpm check:source`：检查日期、署名、文本密度、代码长度和资源路径。
- `corepack pnpm diagrams`：由 `diagrams/*.mmd` 重新生成 SVG。
- `corepack pnpm check`：构建全部 HTML，并检测渲染后的页面越界。
- `corepack pnpm render:qa`：将每一页渲染到 `.qa/slides/`，用于视觉复核。
- `corepack pnpm test:examples`：运行所有 Python 示例测试。

## 目录说明

- `slides/`：课堂演示课件。
- `theme/`：共享中文演示主题。
- `diagrams/`：可维护的 Mermaid 图源文件。
- `assets/`：图片和生成的 SVG。
- `examples/`：可运行、可测试的 Python 示例。
- `dist/`：生成的离线 HTML。
