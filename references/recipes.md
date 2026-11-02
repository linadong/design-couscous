# Recipe Reference

## Fields

The following JSONC example includes comments for explanation. Remove comments before saving a JSON recipe. Use absolute paths when invoking the engine from an agent; relative paths resolve against the process working directory.

```jsonc
{
  "image": "/absolute/task/source.png",
  "output": "/absolute/task/result.psd",
  "preview": "/absolute/task/preview.png", // Optional composite preview
  "flux_cache": "/absolute/task/flux.png", // Optional cached FLUX background
  "elements": [
    {
      "prompt": "white detergent sheet", // Semantic object description
      "required": true,                  // Fail export if extraction misses
      "refine": true,                    // GrabCut edge refinement
      "all_instances": false,            // Set true to extract all matched instances
      "conf": 0.30                       // Confidence threshold
    }
  ],
  "fill": {"enabled": false},            // Background repair is optional
  "graphics": [
    {
      "name": "icon", "type": "icon", "box": [291, 646, 420, 776],
      "perfect_circle": true,             // Reconstruct a circular ring mathematically
      "remove_from_bg": true,
      "vectorize": "/absolute/task/icon.svg"
    },
    {
      "name": "DEEP_CLEANING", "type": "text", "box": [242, 778, 446, 824],
      "content": ["DEEP", "CLEANING"],
      "font": "/absolute/path/to/installed-font.ttf", "font_index": 0,
      "editable": true, "remove_from_bg": true
    }
  ]
}
```

Use coordinates and fonts appropriate to the actual image and environment; the example regions are illustrative. Create output directories before running. Graphics marked `remove_from_bg` are removed during enabled background repair; disabling repair preserves the source background.

## Choosing Inputs

The agent supplies object prompts, image/output paths, graphic bounding boxes, and any text content or fonts. Suitable image-processing helpers estimate ink color, circle geometry, text layout, and element placement. Review their results rather than assuming every image can be reconstructed accurately.

`graphics` is optional for ordinary object extraction. Use it when extracting bounded graphics or reconstructing text. Boxes use image pixels: `[left, top, right, bottom]`.

## Background Repair

Omit `fill` or set `{"enabled": false}` to preserve the original background. To enable repair, use a fill configuration such as `{"enabled": true, "method": "smooth"}`.

With `method: "auto"`, `choose_fill_method` selects a method based on the prompt and image region:

| Condition | Method | Purpose |
|---|---|---|
| A `fill.prompt` is supplied | flux | Generate new content using an external service |
| No prompt; small gaps such as text or thin lines | telea | Local repair of small regions |
| No prompt; larger gap on a smooth or gradient background | smooth | Local smooth-background reconstruction |
| No prompt; larger gap with repeatable texture | lama | Local texture inpainting; optional dependency |

Set `flux`, `lama`, `smooth`, or `telea` explicitly to override automatic selection. `fill.dilate` (default 10) expands the object mask to cover surrounding shadows; `fill.guidance` controls FLUX guidance.

FLUX may invent product-like details when repairing packaging imagery. Prefer smooth or lama when suitable for background restoration. Generative repair is not a guarantee of accurate reconstruction. Obtain authorization for external uploads and possible API charges before using FLUX or SAM3.

## Standalone Cutouts Without PSD Export

```bash
.venv/bin/python sam3_cutout.py /absolute/task/source.png "button" "icon" --refine
```

Run from a writable task directory; standalone cutouts are saved under `output/<image-name>/` relative to that directory.
