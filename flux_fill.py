"""
flux_fill.py —— 调用 Black Forest Labs FLUX.1 Fill 做 mask 式补全

异步流程：POST 提交 -> 拿 polling_url -> 轮询到 status='Ready' -> 下载 result.sample

核心函数：
  flux_fill(image_pil, mask_pil, prompt="", steps=50, guidance=60) -> PIL.Image
    image_pil : 原图 (RGB)
    mask_pil  : 蒙版 (L)，白=要补的区域、黑=保留（与原图同尺寸）
"""

import base64
import io
import os
import sys
import time

import requests
from PIL import Image

SUBMIT_URL = "https://api.bfl.ai/v1/flux-pro-1.0-fill"
POLL_TIMEOUT = 180        # 秒，单次任务最长等待
POLL_INTERVAL = 2


def load_key():
    try:
        import config
        k = getattr(config, "BFL_API_KEY", "")
        if k and not k.startswith("bfl_你的"):
            return k
    except ImportError:
        pass
    k = os.environ.get("BFL_API_KEY", "")
    if k:
        return k
    sys.exit("❌ 没找到 BFL_API_KEY，请在 config.py 填入。")


def _b64_png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def flux_fill(image_pil, mask_pil, prompt="", steps=50, guidance=60,
              seed=None, output_format="png"):
    key = load_key()
    W, H = image_pil.size
    # mask 必须和原图同尺寸、黑白
    mask_pil = mask_pil.convert("L").resize((W, H))

    payload = {
        "image": _b64_png(image_pil.convert("RGB")),
        "mask": _b64_png(mask_pil),
        "prompt": prompt,
        "steps": steps,
        "guidance": guidance,
        "output_format": output_format,
        "safety_tolerance": 2,
    }
    if seed is not None:
        payload["seed"] = seed

    headers = {"x-key": key, "Content-Type": "application/json"}
    r = requests.post(SUBMIT_URL, headers=headers, json=payload, timeout=60)
    if r.status_code != 200:
        sys.exit(f"❌ 提交失败 {r.status_code}: {r.text[:500]}")
    job = r.json()
    polling_url = job["polling_url"]
    print(f"   🛰️  已提交，任务 id={job['id'][:8]}… 轮询中", end="", flush=True)

    # 轮询
    waited = 0
    while waited < POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL
        pr = requests.get(polling_url, headers={"x-key": key},
                          params={"id": job["id"]}, timeout=30)
        data = pr.json()
        status = data.get("status")
        if status == "Ready":
            print(" ✅")
            sample_url = data["result"]["sample"]
            img_bytes = requests.get(sample_url, timeout=60).content
            out = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            if out.size != (W, H):          # 个别情况下尺寸会变，缩回原尺寸
                out = out.resize((W, H), Image.LANCZOS)
            return out
        if status in ("Error", "Failed", "Content Moderated", "Request Moderated"):
            sys.exit(f"\n❌ 任务失败: {data}")
        print(".", end="", flush=True)
    sys.exit(f"\n❌ 轮询超时（>{POLL_TIMEOUT}s）")


# 命令行自测：python flux_fill.py 原图 元素PNG  （移除该元素并补背景）
if __name__ == "__main__":
    import numpy as np
    import cv2
    image_path, piece_path = sys.argv[1], sys.argv[2]
    prompt = sys.argv[3] if len(sys.argv) > 3 else ""
    original = Image.open(image_path).convert("RGB")
    W, H = original.size
    rgb = np.array(original)
    piece = Image.open(piece_path).convert("RGBA")
    res = cv2.matchTemplate(rgb, np.array(piece.convert("RGB")), cv2.TM_CCOEFF_NORMED)
    _, _, _, (x, y) = cv2.minMaxLoc(res)
    mask = np.zeros((H, W), np.uint8)
    pa = np.array(piece.split()[3]); ph, pw = pa.shape
    mask[y:y + ph, x:x + pw] = (pa > 40).astype(np.uint8) * 255
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 13)))
    print(f"📍 移除位置 ({x},{y})，补全面积 {int((mask>0).sum())} 像素")
    out = flux_fill(original, Image.fromarray(mask), prompt=prompt)
    op = os.path.join(os.path.dirname(piece_path),
                      os.path.splitext(os.path.basename(image_path))[0] + "_flux.png")
    out.save(op)
    print(f"🎉 完成 -> {op}")
