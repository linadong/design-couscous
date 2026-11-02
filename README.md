# Layer Rebuild · Alpha

**Layer Rebuild helps you edit print artwork when the original layered design file is unavailable.** Extract selected objects, text, or graphics from a flattened image into a layered PSD and preview PNG, then adjust the layout, replace elements, or revise content in Photoshop or another editor.

Optional features include background repair, SVG extraction for suitable icons, and text reconstruction. Inputs are raster images; PDF and AI artwork must be converted to images separately. The exported PSD is a starting point for editing. Check dimensions, resolution, bleed, and color mode before sending the artwork to print.

This repository includes an installable **Agent Skill**, a Python image-processing engine, and an optional local web interface.

## Install the Skill

Clone the entire repository into your agent's skills directory. Copying only SKILL.md is insufficient because the skill calls the Python engine alongside it.

For Codex's default directory (use your configured directory if CODEX_HOME is customized):

```bash
git clone https://github.com/linadong/layer-rebuild.git ~/.codex/skills/layer-rebuild
cd ~/.codex/skills/layer-rebuild
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

For Claude Code, use `~/.claude/skills/layer-rebuild` as the clone destination. On Windows, use `.venv\Scripts\python.exe`. Python 3.11 or 3.12 is required.

Restart or reload your agent, then try:

> Use layer-rebuild to prepare this flattened print artwork for editing. Extract the red bottle into a separate layer and export a PSD and preview so I can adjust the layout. Leave the background unchanged.

## Configure API Keys

Semantic object segmentation uses the external sam3.ai API; generative background repair uses Black Forest Labs. These services receive image data and may incur charges. This implementation does not run the SAM3 model locally.

Set `SAM3_API_KEY` in the environment used to run your agent. Set `BFL_API_KEY` only if you need FLUX. Alternatively, copy `config.example.py` to `config.py` and enter your own keys. Never commit credentials.

Local graphics/mask extraction and smooth/telea background repair require no service keys. LaMa is optional; SVG vectorization additionally requires potrace.

## Run Manually

Create a JSON recipe with absolute image and output paths; see the [Skill](SKILL.md) and [Recipe Reference](references/recipes.md). Then run:

```bash
.venv/bin/python run_psd.py /absolute/path/to/recipe.json
```

## Local Web Interface (Optional)

Requires macOS, Swift command line tools, and the Python environment above:

```bash
.venv/bin/python web_app/server.py
```

Open http://127.0.0.1:8787. See the [web interface guide](web_app/README.md). Uploads and generated files are stored in `web_app/runtime_jobs/` without automatic deletion. This interface is intended for local use; public hosting is not configured.

## Current Limitations

- This is an alpha. It cannot reliably separate every overlapping object or reconstruct hidden object parts.
- Background repair quality depends on the image and method. Without repair, the original object remains in the background layer.
- Automatic OCR produces pixel layers by default. Editable text requires explicit content, regions, and an available font.
- A valid PSD and a sufficient layer count do not establish extraction quality. Inspect the preview and individual layers.
- This repository excludes private source images, historical jobs, API keys, and model weights.

## Validation

From the repository root, with dependencies installed:

```bash
PYTHONPATH=.:web_app .venv/bin/python -m unittest discover -s web_app -p 'test_*.py'
```
