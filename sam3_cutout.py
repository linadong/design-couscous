"""
sam3_cutout.py —— 方案 A：SAM3 文字定位 + 后处理修边 + 透明 PNG 自动编号

做的事：
  1) 读一张图
  2) 把图缩到 1024 以内发给 SAM3，按你给的文字指令拿回每个元素的轮廓（多边形坐标）
  3) 把坐标按比例放大，套回你的【原始高清大图】上抠图（所以边缘不掉质）
  4) 多边形 -> 抗锯齿的透明边 -> 裁切 -> 存成独立 PNG，自动编号

用法（在 sam3 环境里跑）：
  conda run -n sam3 python sam3_cutout.py input/你的图.jpg "按钮" "红色的车"

  - 第 1 个参数：图片路径
  - 后面的参数：一个或多个文字指令（每个用引号括起来）

输出：output/<图片名>/<指令>_01.png, _02.png ...
"""

import base64
import io
import os
import re
import sys

import requests
from PIL import Image, ImageDraw, ImageFilter

try:
    import cv2
    import numpy as np
    _HAS_CV = True
except ImportError:
    _HAS_CV = False

# ---------- 一些可调参数（先用默认值，需要再改）----------

API_URL = "https://sam3.ai/api/v1/segment"
MAX_SIDE = 1024          # SAM3 限制：图片最长边不超过 1024
MIN_CONFIDENCE = 0.30    # 置信度低于这个值的识别结果丢弃（过滤误判）
SUPERSAMPLE = 4          # 修边：先放大 N 倍画轮廓再缩小，得到抗锯齿的平滑边
CONTRACT_PX = 1          # 修边：把边缘向内收 N 像素，避免抠出背景的"白边/毛边"。设 0 关闭
FEATHER_PX = 0.0         # 修边：边缘羽化（柔化）像素。硬边缘场景设 0；想要软一点可设 0.5~1.0
REFINE_PAD = 24          # --refine 精修时，元素四周多留的像素


# ---------- 第 0 步：读取 API key ----------

def load_api_key():
    """优先从 config.py 读，其次从环境变量 SAM3_API_KEY 读。"""
    try:
        import config
        key = getattr(config, "SAM3_API_KEY", "")
        if key and not key.startswith("sk_live_你的"):
            return key
    except ImportError:
        pass
    key = os.environ.get("SAM3_API_KEY", "")
    if key:
        return key
    sys.exit("❌ 没找到 API key。请把 config.example.py 复制成 config.py 并填入你的 key。")


# ---------- 第 1 步：把图缩小并编码成 base64 发给 API ----------

def encode_for_api(img):
    """
    返回 (base64字符串, 缩放比例scale)。
    scale = 原图尺寸 / 发送尺寸，后面用它把坐标放大回原图。
    """
    w, h = img.size
    scale = max(w, h) / MAX_SIDE
    if scale > 1:
        sent = img.resize((round(w / scale), round(h / scale)), Image.LANCZOS)
    else:
        scale = 1.0
        sent = img

    # 用 JPEG 编码（只为了让 API 识别轮廓，真正抠图用的是原图，所以这里压缩无所谓）
    buf = io.BytesIO()
    sent.convert("RGB").save(buf, format="JPEG", quality=90)
    data = buf.getvalue()

    # API 限制 base64 不超过 2MB，太大就逐步降质
    quality = 90
    while len(base64.b64encode(data)) > 2_000_000 and quality > 40:
        quality -= 15
        buf = io.BytesIO()
        sent.convert("RGB").save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()

    return base64.b64encode(data).decode("ascii"), scale


# ---------- 第 2 步：调用 SAM3 ----------

def call_sam3(image_b64, prompts, api_key):
    resp = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={"image": image_b64, "prompts": prompts},
        timeout=120,
    )
    if resp.status_code != 200:
        sys.exit(f"❌ API 返回错误 {resp.status_code}: {resp.text[:500]}")
    return resp.json()


# ---------- 第 3 步：多边形 -> 抗锯齿透明边 ----------

def polygons_to_alpha(polygons, size):
    """
    把一个元素的若干多边形画成一张透明度蒙版（L 模式，0=透明 255=不透明）。
    用 SUPERSAMPLE 放大后再缩小来抗锯齿，边缘更平滑。
    """
    w, h = size
    big = Image.new("L", (w * SUPERSAMPLE, h * SUPERSAMPLE), 0)
    draw = ImageDraw.Draw(big)
    for poly in polygons:
        pts = [(x * SUPERSAMPLE, y * SUPERSAMPLE) for x, y in poly]
        if len(pts) >= 3:
            draw.polygon(pts, fill=255)
    mask = big.resize((w, h), Image.LANCZOS)  # 缩回原尺寸 -> 抗锯齿

    if CONTRACT_PX > 0:                         # 向内收边，去白边
        mask = mask.filter(ImageFilter.MinFilter(CONTRACT_PX * 2 + 1))
    if FEATHER_PX > 0:                          # 羽化（可选）
        mask = mask.filter(ImageFilter.GaussianBlur(FEATHER_PX))
    return mask


# ---------- 第 4 步：抠图 + 裁切 ----------

# ---------- 可选：GrabCut 边缘吸附（--refine）----------

def grabcut_refine(original, mask):
    """
    用图像真实的颜色边界把 mask 吸附到物体/背景的真实交界线上，边缘更细致清晰。
    对高对比硬边缘（UI、产品图、规整物体）提升最明显。
    传入整图 alpha，返回精修后的整图 alpha。
    """
    if not _HAS_CV:
        print("   ⚠️ 没装 opencv/numpy，--refine 已跳过（pip install opencv-python-headless numpy）")
        return mask

    bbox = mask.getbbox()
    if bbox is None:
        return mask
    W, H = original.size
    x0 = max(0, bbox[0] - REFINE_PAD); y0 = max(0, bbox[1] - REFINE_PAD)
    x1 = min(W, bbox[2] + REFINE_PAD); y1 = min(H, bbox[3] + REFINE_PAD)

    crop_bgr = cv2.cvtColor(np.array(original.crop((x0, y0, x1, y1)).convert("RGB")),
                            cv2.COLOR_RGB2BGR)
    alpha_crop = np.array(mask.crop((x0, y0, x1, y1)))
    h, w = alpha_crop.shape

    fg = (alpha_crop > 127).astype(np.uint8)
    if fg.sum() == 0:
        return mask
    k = max(3, round(min(h, w) * 0.03)) | 1          # 奇数核
    K = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    sure_fg = cv2.erode(fg, K)                        # 肯定是前景
    sure_bg = cv2.erode(1 - fg, K)                    # 肯定是背景

    gc = np.full((h, w), cv2.GC_PR_BGD, np.uint8)
    gc[fg == 1] = cv2.GC_PR_FGD
    gc[sure_fg == 1] = cv2.GC_FGD
    gc[sure_bg == 1] = cv2.GC_BGD

    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(crop_bgr, gc, None, bgd, fgd, 5, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return mask                                   # 极端情况下 grabcut 会失败，回退原 mask
    out = np.where((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    out = cv2.GaussianBlur(out, (0, 0), 0.6)          # 1px 抗锯齿，边不生硬

    refined = Image.new("L", (W, H), 0)
    refined.paste(Image.fromarray(out), (x0, y0))
    return refined


# ---------- 第 4 步：抠图 + 裁切 ----------

def cutout(original, polygons, refine=False):
    """在原图上按多边形抠出元素，返回裁切到外接框的 RGBA 小图。"""
    mask = polygons_to_alpha(polygons, original.size)
    if refine:
        mask = grabcut_refine(original, mask)
    rgba = original.convert("RGBA")
    rgba.putalpha(mask)
    bbox = mask.getbbox()        # 元素的外接矩形
    if bbox is None:
        return None
    return rgba.crop(bbox)


# ---------- 工具：把指令文字变成安全的文件名 ----------

def safe_name(text):
    name = re.sub(r"[^\w一-鿿-]+", "_", text.strip())
    return name.strip("_") or "element"


# ---------- 主流程 ----------

def main():
    args = sys.argv[1:]
    refine = "--refine" in args                       # 加 --refine 就开启 GrabCut 精修
    args = [a for a in args if a != "--refine"]
    if len(args) < 2:
        sys.exit('用法: python sam3_cutout.py <图片路径> "<指令1>" "<指令2>" ... [--refine]')

    image_path = args[0]
    prompts = args[1:]
    if not os.path.exists(image_path):
        sys.exit(f"❌ 找不到图片: {image_path}")
    if refine:
        print("✨ 已开启 --refine：每个元素做 GrabCut 边缘精修（慢一些，边更细）")

    api_key = load_api_key()
    original = Image.open(image_path).convert("RGBA")
    print(f"📷 原图: {original.size[0]}x{original.size[1]}  指令: {prompts}")

    image_b64, scale = encode_for_api(original)
    if scale > 1:
        print(f"   (已缩到 1024 以内发送，缩放比例 {scale:.2f}，抠图仍用原图分辨率)")

    print("🛰️  调用 SAM3 ...")
    result = call_sam3(image_b64, prompts, api_key)

    # 输出目录：output/<图片名>/
    stem = os.path.splitext(os.path.basename(image_path))[0]
    out_dir = os.path.join("output", stem)
    os.makedirs(out_dir, exist_ok=True)

    total = 0
    # 按指令分组，每个指令下的多个实例自动编号
    for pr in result.get("prompt_results", []):
        prompt_text = pr.get("echo", {}).get("text", "element")
        base = safe_name(prompt_text)

        # 按置信度从高到低排序，编号更直观
        preds = sorted(pr.get("predictions", []),
                       key=lambda p: p.get("confidence", 0), reverse=True)

        idx = 0
        for p in preds:
            if p.get("confidence", 0) < MIN_CONFIDENCE:
                continue
            # 把多边形坐标按比例放大回原图尺寸
            polys = [[(x * scale, y * scale) for x, y in poly]
                     for poly in p.get("masks", [])]
            if not polys:
                continue

            piece = cutout(original, polys, refine=refine)
            if piece is None:
                continue

            idx += 1
            total += 1
            fname = f"{base}_{idx:02d}.png"
            piece.save(os.path.join(out_dir, fname))
            print(f"   ✅ {fname}  (置信度 {p.get('confidence', 0):.2f}, "
                  f"尺寸 {piece.size[0]}x{piece.size[1]})")

        if idx == 0:
            print(f"   ⚠️ 指令「{prompt_text}」没有命中达标的元素")

    print(f"\n🎉 完成，共抠出 {total} 个元素 -> {out_dir}/")


if __name__ == "__main__":
    main()
