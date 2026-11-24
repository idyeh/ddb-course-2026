# 分布式数据库开发：课件

2026 版课件使用 Markdown 编写，并由 Marp 生成离线 HTML。课件不依赖 Quarto 或 Jupyter；Python 仅用于课堂实验与示例验证。

## 快速使用

1. 安装 Node.js 20 或更高版本；Node 自带或另行安装 Corepack。
2. 在本目录运行 `corepack pnpm install`。项目固定使用 pnpm 11.19.0，并使用 npm 官方仓库，不受全局 pnpm 及镜像配置影响。
3. 运行 `corepack pnpm present`，在浏览器中选择要演示的课件。
4. 运行 `corepack pnpm build`，在 `dist/` 得到可离线打开的 HTML。

打开 `dist/index.html` 可以进入全部讲座。若需在没有 Chrome 的机器上运行浏览器溢出检查，先执行 `corepack pnpm browser:install`。

也可以安装 VS Code 扩展 **Marp for VS Code**，打开 `slides/` 中任意 Markdown 文件后直接预览和演示。

## 课堂演示

`lab/` 是一个独立的集群模拟演示台，只依赖 Python 标准库与浏览器：

    corepack pnpm lab

或直接运行 `python3 lab/run.py serve`。菜单按讲次列出演示，表单配置参数，
网页动画回放由 Python 生成的确定性事件轨迹。它不改动 `slides/`，也不参与课件构建。
用法与课堂脚本见 `lab/README.md`。

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
- `corepack pnpm test:lab`：运行课堂演示台的契约、正向与反例测试。

## 目录说明

- `slides/`：课堂演示课件。
- `theme/`：共享中文演示主题。
- `diagrams/`：可维护的 Mermaid 图源文件。
- `assets/`：图片和生成的 SVG。
- `examples/`：可运行、可测试的 Python 示例。
- `lab/`：课堂集群模拟演示台，含网页动画与命令行入口。
- `dist/`：生成的离线 HTML。
已修改