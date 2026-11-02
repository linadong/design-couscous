# Layer Rebuild · Alpha

把图片中指定的物体提取为独立图层，输出分层 PSD 和预览 PNG。支持可选背景修复，以及部分图标 SVG / 文字重建流程。

这个仓库同时包含一个可安装的 **Agent Skill**、Python 图像处理引擎和可选的本地网页界面。

## 安装 Skill

将整个仓库克隆到 Agent 的技能目录，不能只复制 SKILL.md：它会调用同目录的 Python 引擎。

Codex 默认目录示例（自定义 CODEX_HOME 时请使用对应目录）：

```bash
git clone https://github.com/linadong/layer-rebuild.git ~/.codex/skills/layer-rebuild
cd ~/.codex/skills/layer-rebuild
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Claude Code 可将克隆目标换成 `~/.claude/skills/layer-rebuild`。Windows 使用 `.venv\Scripts\python.exe`。需要 Python 3.11 或 3.12。

重启或重新加载 Agent 后，示例请求：

> 使用 layer-rebuild，把这张图片中的红色瓶子拆成独立图层，导出 PSD 和预览图，不补背景。

## 配置服务密钥

语义物体分割通过 sam3.ai 外部 API 完成；生成式背景修复通过 Black Forest Labs 完成。它们会上传图片数据，并可能产生费用。这不是本地运行 SAM3 模型的实现。

在运行 Agent 的环境中设置 `SAM3_API_KEY`；只有使用 FLUX 时才需要 `BFL_API_KEY`。也可以复制 `config.example.py` 为 `config.py` 后填写自己的密钥。不要提交密钥。

本地 graphics / mask 提取和 smooth / telea 背景修复无需服务密钥。LaMa 为可选依赖；SVG 矢量化另需安装 potrace。

## 手动运行

创建包含绝对图片和输出路径的 JSON recipe（格式见 [Skill](SKILL.md) 和 [Recipe 参考](references/recipes.md)），然后运行：

```bash
.venv/bin/python run_psd.py /absolute/path/to/recipe.json
```

## 本地网页（可选）

需要 macOS、Swift 命令行工具和上述 Python 环境：

```bash
.venv/bin/python web_app/server.py
```

打开 http://127.0.0.1:8787 。详见 [网页说明](web_app/README.md)。上传和生成结果保存在 `web_app/runtime_jobs/`，不会自动删除。这个入口用于本地运行，未配置公网托管。

## 当前限制

- Alpha 版本，不能保证自动拆开每一个重叠物体，也不重建被遮挡部分。
- 背景修复质量依赖图片和方法；不补背景时，原图中的物体仍在背景层里。
- 自动 OCR 文字默认是像素图层；可编辑文字需要显式配置内容、区域和可用字体。
- PSD 完整性和图层数量不代表分割质量，需要查看预览与图层。
- 此仓库不包含私人原图、历史任务、API 密钥或模型权重。

## 验证

在仓库根目录、已安装依赖的环境中运行现有工作流测试：

```bash
PYTHONPATH=.:web_app .venv/bin/python -m unittest discover -s web_app -p 'test_*.py'
```
