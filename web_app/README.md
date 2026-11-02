# Layer Rebuild local alpha

This is an isolated, local-only UI for the existing `sam3-cutout` pipeline. It
does not change or serve the personal portfolio website.

## Run

```bash
cd /path/to/layer-rebuild
.venv/bin/python web_app/server.py
```

Open `http://127.0.0.1:8787` in a browser.

## What it does today

- accepts JPG, PNG, and WebP files up to 20 MB;
- performs a zero-credit, on-device first pass with macOS Vision: recognised
  text becomes separate pixel layers and salient foreground becomes a clearly
  labelled starter group;
- treats the tester's optional descriptions as **must-separate** requirements;
  a requested item that cannot be extracted fails the job rather than producing
  a deceptively successful PSD;
- attempts background repair, without claiming that it reconstructs hidden object parts;
- requests separate consent for external SAM3 segmentation when required-object names are supplied, even if background repair is local;
- creates a per-job JSON recipe;
- delegates extraction, optional background treatment, PSD generation, preview,
  and ZIP packaging to the established `run_psd.py` pipeline;
- shows individual layer visibility and horizontal movement controls for inspection (these controls do not alter the export);
- requires acknowledgement of the draft limitations before enabling download;
- returns a ZIP containing the source image, recipe, preview PNG, PSD and a draft-limitations manifest.

## Known alpha boundaries

- The on-device foreground model groups complex, overlapping scenes. It must
  not be represented as object-by-object separation. A semantic vision planner
  plus review/click-to-select is the next milestone for true default separation
  of every visible object.
- High-fidelity repair of large, occluded photographic areas needs generative
  inpainting. The app only invokes the configured external fill provider after
  the tester ticks its explicit consent checkbox; local repair is a lower-fidelity
  baseline for small or smooth gaps.
- Jobs are kept on disk in `web_app/runtime_jobs` for local inspection. Add a
  deletion policy before any hosted test.
- Downloads are explicitly drafts. A layer count of two or more is only a file-integrity check, not acceptance of object separation or repair quality.
- Hidden portions of individual objects are not reconstructed by the current pipeline. This is an unresolved core requirement, not a completed feature.
