---
name: layer-rebuild
description: Extract requested objects from an image into a layered PSD, with optional background repair, preview PNG, and SVG extraction. Use for image layer rebuilding, product image cutouts, or PSD decomposition.
---

# Layer Rebuild (Alpha)

Use the Python engine beside this SKILL.md. Resolve its directory from the loaded skill location; never assume the user's working directory is the engine directory. Store source images, recipes, and outputs in the user's project or a dedicated writable task directory, using absolute paths in recipes.

## Prepare

Use Python 3.11 or 3.12 with `requirements.txt`. Reuse a compatible environment or create a virtual environment in the skill directory and install with `<python> -m pip install -r requirements.txt`. Do not assume a Conda environment named sam3 exists. Python 3.13+ is unsupported by the optional web UI (it uses cgi).

SAM3 segmentation sends an image to sam3.ai; FLUX repair sends image data to Black Forest Labs. Explain the relevant upload and possible API charges and obtain consent before an external call unless the user has already authorized it. Use SAM3_API_KEY and BFL_API_KEY environment variables, or local config.py copied from config.example.py. Never print or commit keys. Local graphics/mask extraction and smooth/telea repair require no service keys.

## Build

1. Inspect the supplied image. Use objects explicitly requested by the user; ask about the target objects only when unclear. Do not require confirmation of choices the user already made.
2. Use `elements` with concise English prompts for semantic object extraction. Required objects must have `required: true`; a missed object must fail export. For a bounded logo on a simple background use `graphics` type `foreground`; use `icon` or `text` for suitable monochrome regions. Coordinates are image pixels, `[left, top, right, bottom]`.
3. Background repair is optional. Default to `fill: {"enabled": false}` unless requested. Use smooth for smooth backgrounds, telea for small gaps, optional lama for texture, and flux only for authorized generative repair. Avoid auto when external repair is not authorized: a prompt can route auto to flux.
4. Write a JSON recipe with absolute image/output/preview paths, and create the output directories. See [references/recipes.md](references/recipes.md) for advanced fields. Example:

   ```json
   {
     "image": "/absolute/task/source.png",
     "output": "/absolute/task/result.psd",
     "preview": "/absolute/task/preview.png",
     "elements": [{"prompt": "red bottle", "required": true, "refine": true}],
     "fill": {"enabled": false}
   }
   ```

5. Run `<python> <skill-directory>/run_psd.py <absolute-recipe-path>`. Resolve fonts to installed font files when requesting editable text; do not assume macOS fonts. SVG extraction additionally needs the potrace executable.
6. Verify the PSD exists and opens using psd-tools, check the requested layers, and inspect the preview. Report output paths and any extraction/repair defects. A background-only file is a failure. Re-running external calls may incur charges; avoid blind retry loops.

## Limits and optional UI

This is a draft extraction tool. Overlapping objects may be grouped; hidden object parts are not reconstructed. OCR regions are pixel layers unless explicit editable text reconstruction is configured. Preserving the original background leaves the original object underneath the extracted layer; disclose this when users expect movable objects on a clean background.

The optional local browser UI is `web_app/server.py` at http://127.0.0.1:8787. It uses macOS Vision and requires macOS plus Swift command line tools. Read [web_app/README.md](web_app/README.md) when running that mode. It stores jobs under web_app/runtime_jobs and has no automatic deletion policy. Keep it local.
