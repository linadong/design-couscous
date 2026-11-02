"""Local alpha app for Layer Rebuild.

Run with the existing image-processing environment:
    conda run -n sam3 python web_app/server.py

This is deliberately local-only. Uploaded files stay in web_app/runtime_jobs and
are never sent anywhere except to processing providers explicitly configured in
sam3-cutout/config.py when a user starts a build.
"""

from __future__ import annotations

import cgi
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ENGINE_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = Path(__file__).resolve().parent / "static"
JOB_ROOT = Path(__file__).resolve().parent / "runtime_jobs"
VISION_SOURCE = ENGINE_ROOT / "tools" / "local_vision.swift"
VISION_RUNTIME = Path(__file__).resolve().parent / ".runtime" / "local_vision"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_FILL_METHODS = {"smooth", "telea", "flux"}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
VISION_LOCK = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", value.lower()).strip("_") or "layer"


def job_record(job_id: str) -> dict | None:
    with JOBS_LOCK:
        return JOBS.get(job_id)


def public_job(job: dict) -> dict:
    return {
        "id": job["id"],
        "filename": job["filename"],
        "status": job["status"],
        "message": job["message"],
        "created_at": job["created_at"],
        "dimensions": job.get("dimensions"),
        "auto_plan": job.get("auto_plan"),
        "layer_count": job.get("layer_count"),
        "background_restored": job.get("background_restored"),
        "download_ready": bool(job.get("bundle") and job.get("review_confirmed")),
        "preview_ready": bool(job.get("preview") and Path(job["preview"]).exists() and job["status"] in {"review", "complete"}),
        "error": job.get("error"),
        "layers": job.get("layers", []),
        "review_warnings": job.get("review_warnings", []),
        "review_confirmed": bool(job.get("review_confirmed")),
    }


def read_image_dimensions(path: Path) -> dict | None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            dimensions = {"width": image.width, "height": image.height}
            if image.width * image.height > 30_000_000:
                return None
            image.verify()
            return dimensions
    except Exception:
        return None


def ensure_vision_helper() -> Path:
    """Compile the small macOS-only Vision bridge when its source changes."""
    if not VISION_SOURCE.exists():
        raise RuntimeError("The local Vision helper is missing.")
    with VISION_LOCK:
        needs_build = not VISION_RUNTIME.exists() or VISION_RUNTIME.stat().st_mtime < VISION_SOURCE.stat().st_mtime
        if needs_build:
            VISION_RUNTIME.parent.mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                ["swiftc", str(VISION_SOURCE), "-o", str(VISION_RUNTIME)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(f"Could not start local image analysis: {completed.stdout[-800:]}")
    return VISION_RUNTIME


def create_auto_plan(job: dict) -> None:
    """Create the zero-credit first-pass plan: OCR, salient foreground, labels."""
    plan_dir = job["directory"] / "auto_plan"
    plan_dir.mkdir(exist_ok=True)
    helper = ensure_vision_helper()
    completed = subprocess.run(
        [str(helper), str(job["source"]), str(plan_dir)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stdout[-800:] or "Local image analysis stopped unexpectedly.")
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Local image analysis returned an invalid plan.") from exc
    object_masks = []
    for item in report.get("instances", []):
        mask = plan_dir / str(item.get("mask", ""))
        if mask.exists():
            object_masks.append({"name": str(item.get("name", "Detected foreground")), "mask": str(mask)})
    text = []
    for item in report.get("text", []):
        if not item.get("width") or not item.get("height"):
            continue
        region = dict(item)
        content = str(region.get("text", "")).strip()
        region["name"] = f"Text — {content}" if content else str(region.get("name", "Text"))
        text.append(region)
    job["vision_masks"] = object_masks
    job["vision_text"] = text
    job["vision_report"] = report
    job["auto_plan"] = {
        "objects": [item["name"] for item in object_masks],
        "text": [{"name": item.get("name"), "text": item.get("text"), "confidence": item.get("confidence")} for item in text],
        "labels": report.get("labels", []),
        "note": "Draft plan only: foreground groups can contain several objects and OCR can miss text. Optional names emphasise required objects; they do not define everything you want separated. Inspect the output before accepting a draft.",
    }


def zip_outputs(job: dict) -> Path:
    bundle = job["directory"] / "layer-rebuild-export.zip"
    candidates = [job["recipe_path"], job["source"]]
    review = job["directory"] / "draft-limitations.json"
    review.write_text(json.dumps({"export_type": "draft", "warnings": job.get("review_warnings", []), "layers": job.get("layers", [])}, ensure_ascii=False, indent=2), encoding="utf-8")
    candidates.append(review)
    if job.get("vision_report"):
        report = job["directory"] / "auto-plan.json"
        report.write_text(json.dumps(job["vision_report"], ensure_ascii=False, indent=2), encoding="utf-8")
        candidates.append(report)
    for key in ("psd", "preview"):
        if job.get(key):
            candidates.append(job[key])
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in candidates:
            if Path(path).exists():
                archive.write(path, arcname=Path(path).name)
    return bundle


def run_build(job_id: str) -> None:
    job = job_record(job_id)
    if not job:
        return
    with JOBS_LOCK:
        job["status"] = "processing"
        job["message"] = "Building layers and export files…"

    command = [sys.executable, str(ENGINE_ROOT / "run_psd.py"), str(job["recipe_path"])]
    try:
        completed = subprocess.run(
            command,
            cwd=ENGINE_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=900,
            check=False,
        )
        with JOBS_LOCK:
            job["log"] = completed.stdout[-12000:]
        if completed.returncode != 0:
            detail = completed.stdout[-1200:] or "The processing pipeline stopped before export."
            raise RuntimeError(detail)
        if not job["psd"].exists():
            raise RuntimeError("The pipeline completed without creating a PSD export.")
        from psd_tools import PSDImage

        psd = PSDImage.open(job["psd"])
        layer_count = len(list(psd.descendants()))
        if layer_count < 2:
            raise RuntimeError("Validation failed: the export does not contain independent PSD layers.")
        from PIL import Image
        layer_dir = job["directory"] / "layer_previews"
        layer_dir.mkdir(exist_ok=True)
        layers = []
        for index, layer in enumerate(psd.descendants()):
            rendered = layer.composite()
            if rendered is None:
                continue
            canvas = Image.new("RGBA", psd.size)
            canvas.paste(rendered.convert("RGBA"), (layer.left, layer.top))
            canvas.thumbnail((960, 960), Image.Resampling.LANCZOS)
            canvas.save(layer_dir / f"{index}.png")
            layers.append({"id": index, "name": layer.name.rstrip("\x00"), "kind": layer.kind, "visible": layer.visible})
        warnings = [
            "This is a draft reconstruction, not verified separation of every object or text region.",
            "Object layers contain visible pixels only. Hidden portions of overlapping objects have NOT been reconstructed.",
            "Text layers are raster pixels, not editable font/text layers.",
            "Background repair is an attempt, not a quality guarantee. Hide foreground layers to inspect it.",
        ]
        if job.get("vision_masks") and not job.get("required_elements"):
            warnings.insert(0, "Local foreground groups may contain multiple objects. They are not verified independent objects.")
        with JOBS_LOCK:
            job["layer_count"] = layer_count
            job["layers"] = layers
            job["review_warnings"] = warnings
            job["background_restored"] = False
            job["bundle"] = zip_outputs(job)
            job["status"] = "review"
            job["message"] = "Draft layers are ready for inspection. Review omissions and repair quality before downloading."
    except subprocess.TimeoutExpired:
        with JOBS_LOCK:
            job["status"] = "failed"
            job["message"] = "The image took too long to process."
            job["error"] = "Timed out after 15 minutes. Try fewer layers or a smaller image."
    except Exception as exc:  # Surface a useful, safe error to the local tester.
        with JOBS_LOCK:
            job["status"] = "failed"
            job["message"] = "The layered export could not be built."
            job["error"] = str(exc)


class LayerRebuildHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[Layer Rebuild] {self.address_string()} — {fmt % args}")

    def send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def error_json(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self.send_json({"error": message}, status)

    def parse_json(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0 or content_length > 100_000:
            raise ValueError("Invalid request body.")
        return json.loads(self.rfile.read(content_length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/layer-preview":
            query = parse_qs(parsed.query)
            job = job_record(query.get("job", [""])[0])
            try:
                index = int(query.get("layer", [""])[0])
            except ValueError:
                self.error_json("Unknown layer.", HTTPStatus.NOT_FOUND)
                return
            if not job or index not in {layer["id"] for layer in job.get("layers", [])}:
                self.error_json("Unknown layer.", HTTPStatus.NOT_FOUND)
                return
            path = job["directory"] / "layer_previews" / f"{index}.png"
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/status":
            job_id = parse_qs(parsed.query).get("job", [""])[0]
            job = job_record(job_id)
            if not job:
                self.error_json("Unknown job.", HTTPStatus.NOT_FOUND)
                return
            self.send_json(public_job(job))
            return
        if parsed.path == "/api/download":
            job_id = parse_qs(parsed.query).get("job", [""])[0]
            job = job_record(job_id)
            if not job or not job.get("bundle") or not Path(job["bundle"]).exists():
                self.error_json("Export is not ready.", HTTPStatus.NOT_FOUND)
                return
            if not job.get("review_confirmed"):
                self.error_json("Inspect the layers and acknowledge the draft limitations before downloading.", HTTPStatus.CONFLICT)
                return
            path = Path(job["bundle"])
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="layer-rebuild-export.zip"')
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with path.open("rb") as file:
                shutil.copyfileobj(file, self.wfile)
            return
        if parsed.path == "/api/preview":
            job_id = parse_qs(parsed.query).get("job", [""])[0]
            job = job_record(job_id)
            preview = Path(job["preview"]) if job and job.get("preview") else None
            if not preview or not preview.exists():
                self.error_json("Preview is not ready.", HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(preview.name)[0] or "image/png")
            self.send_header("Content-Length", str(preview.stat().st_size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with preview.open("rb") as file:
                shutil.copyfileobj(file, self.wfile)
            return
        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            self.handle_upload()
            return
        if parsed.path == "/api/build":
            self.handle_build()
            return
        if parsed.path == "/api/confirm-review":
            try:
                body = self.parse_json()
                job = job_record(str(body.get("job_id", "")))
                if not job or job["status"] not in {"review", "complete"}:
                    raise ValueError("Build a draft before reviewing it.")
                if body.get("acknowledge_limitations") is not True:
                    raise ValueError("Acknowledge that this draft may contain grouped objects, missed text and incomplete repairs.")
                with JOBS_LOCK:
                    job["review_confirmed"] = True
                    job["status"] = "complete"
                self.send_json(public_job(job))
            except (ValueError, TypeError):
                self.error_json("Review the draft and acknowledge its limitations before downloading.")
            return
        self.error_json("Unknown endpoint.", HTTPStatus.NOT_FOUND)

    def handle_upload(self) -> None:
        if int(self.headers.get("Content-Length", "0")) > MAX_UPLOAD_BYTES:
            self.error_json("Use an image smaller than 20 MB.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        if "multipart/form-data" not in self.headers.get("Content-Type", ""):
            self.error_json("Expected a file upload.")
            return
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers["Content-Type"]},
        )
        field = form["image"] if "image" in form else None
        # cgi.FieldStorage deliberately does not support truth-value testing.
        if field is None or not getattr(field, "filename", None):
            self.error_json("Choose a JPG, PNG, or WebP image.")
            return
        filename = Path(field.filename).name
        extension = Path(filename).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            self.error_json("Layer Rebuild accepts JPG, PNG, and WebP images only.")
            return
        job_id = uuid.uuid4().hex
        job_dir = JOB_ROOT / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        source = job_dir / f"source{extension}"
        with source.open("wb") as output:
            shutil.copyfileobj(field.file, output)
        if source.stat().st_size == 0 or source.stat().st_size > MAX_UPLOAD_BYTES:
            shutil.rmtree(job_dir, ignore_errors=True)
            self.error_json("Use a non-empty image smaller than 20 MB.")
            return
        dimensions = read_image_dimensions(source)
        if dimensions is None:
            shutil.rmtree(job_dir, ignore_errors=True)
            self.error_json("Use a readable JPG, PNG, or WebP image, at most 30 megapixels. This file could not be decoded safely.")
            return
        job = {
            "id": job_id,
            "filename": filename,
            "directory": job_dir,
            "source": source,
            "status": "ready",
            "message": "Image received. Creating a local automatic layer plan…",
            "created_at": now(),
            "dimensions": dimensions,
        }
        try:
            create_auto_plan(job)
            job["message"] = "Layer plan ready. Review it and optionally add must-separate items."
        except Exception as exc:
            job["vision_masks"] = []
            job["vision_text"] = []
            job["auto_plan"] = {
                "objects": [],
                "text": [],
                "labels": [],
                "note": "Automatic local planning was unavailable. Add the items that must be separated.",
            }
            job["plan_error"] = str(exc)
            job["message"] = "Image received. Add the items that must be separated."
        with JOBS_LOCK:
            JOBS[job_id] = job
        self.send_json(public_job(job), HTTPStatus.CREATED)

    def handle_build(self) -> None:
        try:
            body = self.parse_json()
        except Exception:
            self.error_json("Invalid build request.")
            return
        job = job_record(str(body.get("job_id", "")))
        if not job:
            self.error_json("Upload an image before building layers.", HTTPStatus.NOT_FOUND)
            return
        if job["status"] in {"queued", "processing"}:
            self.error_json("This image is already processing.", HTTPStatus.CONFLICT)
            return
        raw_prompts = body.get("required_elements", body.get("elements", []))
        if not isinstance(raw_prompts, list) or any(not isinstance(item, str) for item in raw_prompts):
            self.error_json("Must-separate items must be a list of short descriptions.")
            return
        prompts = [item.strip() for item in raw_prompts]
        prompts = [prompt for prompt in prompts if prompt]
        if len(prompts) > 8 or any(len(prompt) > 120 for prompt in prompts):
            self.error_json("Add at most eight short must-separate descriptions.")
            return
        if prompts and body.get("cloud_segmentation_consent") is not True:
            self.error_json("Required-object segmentation sends the image to SAM3 and may consume credits. Confirm this separately, even when background repair is local.")
            return
        if not job.get("vision_masks") and not job.get("vision_text") and not prompts:
            self.error_json("No automatic layers were found. Add at least one item that must be separated.")
            return
        method = str(body.get("fill_method", "smooth"))
        if method not in ALLOWED_FILL_METHODS:
            self.error_json("Choose a valid background option.")
            return
        fill_prompt = str(body.get("fill_prompt", "")).strip()
        if method == "flux" and body.get("cloud_fill_consent") is not True:
            self.error_json("Confirm the use of the external generative fill service before building.")
            return
        if method == "flux" and not fill_prompt:
            fill_prompt = (
                "Continue the existing scene naturally. Match its material, lighting, and perspective. "
                "Do not add text, drinks, food, people, logos, or decorative objects."
            )
        elements = [
            {"prompt": prompt, "name": prompt, "refine": True, "required": True, "all_instances": True}
            for prompt in prompts
        ]
        fill = {"enabled": True, "method": method, "prompt": fill_prompt}
        with JOBS_LOCK:
            if job["status"] in {"queued", "processing"}:
                self.error_json("This image is already processing.", HTTPStatus.CONFLICT)
                return
            job["status"] = "queued"
        job["psd"] = job["directory"] / "layer-rebuild.psd"
        job["preview"] = job["directory"] / "layer-rebuild-preview.png"
        job["recipe_path"] = job["directory"] / "recipe.json"
        recipe = {
            "image": str(job["source"]),
            "output": str(job["psd"]),
            "preview": str(job["preview"]),
            "elements": elements,
            "local_masks": job.get("vision_masks", []),
            "text_regions": job.get("vision_text", []),
            "fill": fill,
        }
        job["recipe_path"].write_text(json.dumps(recipe, ensure_ascii=False, indent=2), encoding="utf-8")
        job["status"] = "queued"
        job["message"] = "Queued for the local image pipeline…"
        job["error"] = None
        job["required_elements"] = prompts
        job["background_restored"] = False
        job["review_confirmed"] = False
        job["layers"] = []
        job["review_warnings"] = []
        job.pop("bundle", None)
        threading.Thread(target=run_build, args=(job["id"],), daemon=True).start()
        self.send_json(public_job(job), HTTPStatus.ACCEPTED)


def main() -> None:
    JOB_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 8787), LayerRebuildHandler)
    print("Layer Rebuild alpha is running at http://127.0.0.1:8787")
    print("Local-only: no uploads leave this machine until you explicitly start a configured build.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Layer Rebuild alpha.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
