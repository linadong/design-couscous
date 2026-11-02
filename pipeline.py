"""
pipeline.py —— 全参数化的核心流程库（无任何 demo 专属硬编码）

提供：
  抠图          cut_element()         —— SAM3 文字 prompt 抠图 + 可选精修
  墨色/线稿     detect_ink_color() / color_ink_alpha()   —— Otsu 自适应，无需手填阈值
  圆检测        detect_circle()
  补背景        build_clean_background()   —— flux / lama / telea 三选一
  文字          measure_text_lines() / render_text_layer()  —— 自动测行数/字号/位置
  矢量化        vectorize_region()    —— 任意纯色图标转 SVG（圆形可选完美重建）
  PSD 组装      write_psd()

所有"魔法数字"都成了函数参数并给了自适应默认值。具体某张图要做什么，由 recipe 描述。
"""

import os
import re
import subprocess

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

import pytoshop
from pytoshop.user import nested_layers
from pytoshop.image_data import ImageData
from pytoshop.enums import ColorMode, Compression

import sam3_cutout as s
import flux_fill

LUM = np.array([0.299, 0.587, 0.114])


# ===================== 工具 =====================

def slug(text):
    return re.sub(r"[^\w一-鿿-]+", "_", str(text).strip()).strip("_") or "x"


def find_position(rgb_np, piece_rgba):
    """模板匹配：在原图里找回某个透明 PNG 的左上角位置。"""
    res = cv2.matchTemplate(rgb_np, np.array(piece_rgba.convert("RGB")),
                            cv2.TM_CCOEFF_NORMED)
    _, _, _, maxloc = cv2.minMaxLoc(res)
    return maxloc


def _otsu(lum):
    t, _ = cv2.threshold(lum.astype(np.uint8), 0, 255,
                         cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return float(t)


# ===================== 抠图（SAM3） =====================

def segment(image_pil, prompt, api_key):
    b64, scale = s.encode_for_api(image_pil)
    resp = s.call_sam3(b64, [prompt], api_key)
    out = []
    for pr in resp.get("prompt_results", []):
        for p in pr.get("predictions", []):
            polys = [[(x * scale, y * scale) for x, y in poly]
                     for poly in p.get("masks", [])]
            if polys:
                out.append({"conf": p.get("confidence", 0), "polys": polys})
    return out


def cut_element(original, prompt, api_key, refine=True, conf_min=0.30,
                all_instances=False, min_area=1500):
    """返回该 prompt 的整图 alpha 列表（PIL L）。默认取置信度最高的一个实例。"""
    W, H = original.size
    preds = sorted([p for p in segment(original, prompt, api_key) if p["conf"] >= conf_min],
                   key=lambda d: d["conf"], reverse=True)
    if not preds:
        return []
    chosen = preds if all_instances else preds[:1]
    out = []
    for p in chosen:
        alpha = s.polygons_to_alpha(p["polys"], (W, H))
        if refine:
            alpha = s.grabcut_refine(original, alpha)
        if np.array(alpha).astype(bool).sum() >= min_area:
            out.append(alpha)
    return out


# ===================== 墨色 / 线稿 =====================

def detect_ink_color(rgb_np, box):
    """自动检测某区域里"墨色"，返回 (r,g,b)。
    若暗部均值亮度 > 0.45（即背景本身就很暗，icon 是亮色），
    则改为返回亮部均值色（白色图标在深色底上的场景）。"""
    x0, y0, x1, y1 = box
    sub = rgb_np[y0:y1, x0:x1].reshape(-1, 3).astype(np.float32)
    lum = sub @ LUM
    t = _otsu(lum)
    dark = sub[lum < t]
    if len(dark) == 0:
        return (0, 0, 0)
    dark_lum = (dark @ LUM).mean() / 255.0
    if dark_lum > 0.45:
        # 背景本身是深色，icon 是亮色 → 返回亮部颜色
        bright = sub[lum >= t]
        if len(bright) == 0:
            return (255, 255, 255)
        return tuple(int(v) for v in bright.mean(0).round())
    return tuple(int(v) for v in dark.mean(0).round())


def foreground_alpha(rgb_np, box, margin=12):
    """抠出 box 内"非背景色"的所有像素（背景色=框边缘中位数）。
    适合纯色/单一背景上的整块图形（如 logo），能把深浅多色一起拿到。"""
    H, W = rgb_np.shape[:2]
    x0, y0, x1, y1 = box
    sub = rgb_np[y0:y1, x0:x1].astype(np.float32)
    border = np.concatenate([sub[0], sub[-1], sub[:, 0], sub[:, -1]], 0)
    bg = np.median(border, 0)
    dist = np.sqrt(((sub - bg) ** 2).sum(2))
    t = _otsu(dist)
    low, high = max(8.0, t - margin), t + margin
    a = np.clip((dist - low) / (high - low), 0, 1)
    alpha = np.zeros((H, W), np.float32)
    alpha[y0:y1, x0:x1] = a * 255
    return alpha.astype(np.uint8)


def color_ink_alpha(rgb_np, box, margin=24):
    """在 box 内把深色墨提取成抗锯齿 alpha；阈值用 Otsu 自适应。"""
    H, W = rgb_np.shape[:2]
    x0, y0, x1, y1 = box
    sub = rgb_np[y0:y1, x0:x1].astype(np.float32)
    lum = sub @ LUM
    t = _otsu(lum)
    low, high = t - margin, t + margin
    a = np.clip((high - lum) / max(1e-3, (high - low)), 0, 1)
    alpha = np.zeros((H, W), np.float32)
    alpha[y0:y1, x0:x1] = a * 255
    return alpha.astype(np.uint8)


def text_ink_alpha(rgb_np, box, margin=24, pad=2):
    """Extract the ink inside a text-recognition box.

    OCR gives us reliable text *geometry* but not a pixel alpha.  Within each
    recognised region, typography is normally the smaller of the two tonal
    groups: dark ink on a light surface, or light ink on a dark label.  This
    chooses that minority group and retains anti-aliased edges.  It is not a
    font reconstruction; it produces a faithful pixel text layer.
    """
    H, W = rgb_np.shape[:2]
    x0, y0, x1, y1 = box
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(W, x1 + pad), min(H, y1 + pad)
    if x1 <= x0 or y1 <= y0:
        return np.zeros((H, W), np.uint8)
    lum = rgb_np[y0:y1, x0:x1].astype(np.float32) @ LUM
    threshold = _otsu(lum)
    dark_ratio = float((lum < threshold).mean())
    bright_ratio = 1.0 - dark_ratio
    use_dark_ink = dark_ratio <= bright_ratio
    if use_dark_ink:
        # Dark type on a light surface.
        alpha_crop = np.clip((threshold + margin - lum) / max(1, margin * 2), 0, 1)
    else:
        # Light type on a dark label / banner.
        alpha_crop = np.clip((lum - (threshold - margin)) / max(1, margin * 2), 0, 1)
        # OCR boxes around white-on-dark lettering may also include a light
        # surface outside the banner. That surface forms a large component
        # connected to the crop boundary, unlike the letter strokes.
        binary = (alpha_crop > 0.55).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
        keep = np.zeros(binary.shape, np.uint8)
        crop_area = binary.size
        crop_h, crop_w = binary.shape
        for label in range(1, count):
            x, y, w, h, area = stats[label]
            touches_edge = x == 0 or y == 0 or x + w >= crop_w or y + h >= crop_h
            if (touches_edge and area > crop_area * 0.01) or area > crop_area * 0.35:
                continue
            keep[labels == label] = 1
        alpha_crop *= keep
    alpha = np.zeros((H, W), np.uint8)
    alpha[y0:y1, x0:x1] = (alpha_crop * 255).astype(np.uint8)
    return alpha


# ===================== 圆检测 =====================

def detect_circle(rgb_np, box):
    """在 box 内检测圆环，返回 (cx,cy,r) 全图坐标；没有返回 None。"""
    x0, y0, x1, y1 = box
    a = color_ink_alpha(rgb_np, box)
    crop = a[y0:y1, x0:x1]
    ow, oh = crop.shape[1], crop.shape[0]
    u8 = cv2.GaussianBlur((crop > 100).astype(np.uint8) * 255, (0, 0), 1)
    c = cv2.HoughCircles(u8, cv2.HOUGH_GRADIENT, 1, max(ow, oh),
                         param1=120, param2=18,
                         minRadius=int(ow * 0.30), maxRadius=int(ow * 0.55))
    if c is None:
        return None
    cx, cy, r = c[0][0]
    return (cx + x0, cy + y0, r)


# ===================== 补背景 =====================

def union(alphas, dilate, shape):
    H, W = shape
    m = np.zeros((H, W), np.uint8)
    for a in alphas:
        aa = a if isinstance(a, np.ndarray) else np.array(a)
        m = np.maximum(m, (aa > 40).astype(np.uint8) * 255)
    if dilate > 0:
        m = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                    (dilate * 2 + 1,) * 2))
    return m


def smooth_fill(rgb_np, mask, downscale=0.045, feather=4):
    """金字塔平滑填充：缩小→inpaint→放大，专补大面积平滑渐变（无糊斑、颜色正确）。"""
    H, W = mask.shape[:2]
    sw, sh = max(8, int(W * downscale)), max(8, int(H * downscale))
    bgr = cv2.cvtColor(rgb_np, cv2.COLOR_RGB2BGR)
    small = cv2.resize(bgr, (sw, sh), interpolation=cv2.INTER_AREA)
    smask = cv2.resize(mask, (sw, sh), interpolation=cv2.INTER_NEAREST)
    smask = cv2.dilate(smask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    fill = cv2.inpaint(small, smask, 3, cv2.INPAINT_TELEA)
    up = cv2.cvtColor(cv2.resize(fill, (W, H), interpolation=cv2.INTER_LINEAR),
                      cv2.COLOR_BGR2RGB).astype(np.float32)
    w = cv2.GaussianBlur((mask > 0).astype(np.float32), (0, 0), feather)[..., None]
    out = up * w + rgb_np.astype(np.float32) * (1 - w)
    return out.clip(0, 255).astype(np.uint8)


def choose_fill_method(rgb_np, mask, prompt=""):
    """根据"待填充区域 + 是否有提示词"自动选补全方法，返回 (method, 原因)。
    规则：有提示词→flux(生成式)；否则 细小→telea、较大→lama(忠实还原)。"""
    H, W = mask.shape[:2]
    area = int((mask > 0).sum())
    if area == 0:
        return ("none", "无需补全")
    if prompt and prompt.strip():
        return ("flux", "给了填充提示词 → 生成式(Flux)")

    ys, xs = np.where(mask > 0)
    bw, bh = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
    fill_ratio = area / (bw * bh)               # 实心程度（线条/碎块→低）
    thin = min(bw, bh) < 0.02 * min(H, W)
    if area < 0.0008 * H * W or thin or fill_ratio < 0.22:
        return ("telea", f"区域细小(占比{area/(H*W):.2%}, 实心度{fill_ratio:.2f}) → 经典修复")

    # 评估空缺周围一圈的纹理复杂度（拉普拉斯高频能量）
    big = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (51, 51)))
    small = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)))
    ring = (big > 0) & (small == 0)
    gray = cv2.cvtColor(rgb_np, cv2.COLOR_RGB2GRAY)
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    detail = float(lap[ring].std()) if ring.any() else 0.0
    if detail < 12:
        return ("smooth", f"较大空缺、背景平滑(细节度{detail:.0f}) → 平滑渐变填充(无糊斑)")
    return ("lama", f"较大空缺、背景有可续接纹理(细节度{detail:.0f}) → LaMa；如要更多细节可改 flux+提示词")


def build_clean_background(original, photo_mask, graphic_mask,
                           method="auto", prompt="", flux_cache=None, guidance=60):
    """照片区域用 method(auto/flux/lama/telea)补；图标/文字细线区域始终用 telea。"""
    clean = original.convert("RGB")
    if photo_mask is not None and photo_mask.max() > 0:
        if method == "auto":
            method, reason = choose_fill_method(np.array(clean), photo_mask, prompt)
            print(f"   🤖 自动选择补全方式：{method} —— {reason}")
        if method in ("none",):
            method = "lama"
        if method == "flux":
            if flux_cache and os.path.exists(flux_cache):
                clean = Image.open(flux_cache).convert("RGB")
                print("   ♻️  复用缓存的 Flux 背景")
            else:
                clean = flux_fill.flux_fill(clean, Image.fromarray(photo_mask),
                                            prompt=prompt, guidance=guidance)
                if flux_cache:
                    os.makedirs(os.path.dirname(flux_cache) or ".", exist_ok=True)
                    clean.save(flux_cache)
        elif method == "lama":
            from simple_lama_inpainting import SimpleLama
            clean = SimpleLama()(clean, Image.fromarray(photo_mask)).convert("RGB")
        elif method == "smooth":
            clean = Image.fromarray(smooth_fill(np.array(clean), photo_mask))
        else:
            bgr = cv2.cvtColor(np.array(clean), cv2.COLOR_RGB2BGR)
            bgr = cv2.inpaint(bgr, photo_mask, 4, cv2.INPAINT_TELEA)
            clean = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    if graphic_mask is not None and graphic_mask.max() > 0:
        bgr = cv2.cvtColor(np.array(clean), cv2.COLOR_RGB2BGR)
        bgr = cv2.inpaint(bgr, graphic_mask, 4, cv2.INPAINT_TELEA)
        clean = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    return clean


# ===================== 文字 =====================

def measure_text_lines(mask, box, min_cap=4):
    """从文字 alpha 里量出每一行的 (中心x, 基线y, 字高)，自动分行（支持任意行数）。"""
    x0, y0, x1, y1 = box
    sub = mask[y0:y1, x0:x1] > 40
    # 每行墨点数超过阈值才算文字行（滤掉零星杂点纵向连成假带）
    on = sub.sum(axis=1) > max(3, int(sub.shape[1] * 0.05))
    bands, start = [], None
    for i, v in enumerate(on):
        if v and start is None:
            start = i
        elif not v and start is not None:
            bands.append((start, i - 1)); start = None
    if start is not None:
        bands.append((start, len(on) - 1))
    bands = [(a, b) for (a, b) in bands if b - a + 1 >= min_cap]
    bands.sort()
    lines = []
    for r0, r1 in bands:
        cols = np.where(sub[r0:r1 + 1].any(axis=0))[0]
        lines.append((x0 + (cols[0] + cols[-1]) / 2, y0 + r1, r1 - r0 + 1))
    return lines


def render_text_layer(size, lines, texts, font_path, font_index, ink_rgb,
                      cap_ratio=0.72, tracking_ratio=0.10):
    """按测得的位置/字高重排版文字，返回整画布 RGBA numpy。"""
    W, H = size
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for (cx, baseline, cap), text in zip(lines, texts):
        fs = max(6, round(cap / cap_ratio))
        font = ImageFont.truetype(font_path, fs, index=font_index)
        tracking = fs * tracking_ratio
        widths = [d.textlength(c, font=font) for c in text]
        total = sum(widths) + tracking * (len(text) - 1)
        x = cx - total / 2
        for c, w in zip(text, widths):
            d.text((x, baseline), c, font=font, fill=tuple(ink_rgb) + (255,), anchor="ls")
            x += w + tracking
    return np.array(layer)


# ===================== 矢量化 =====================

def vectorize_region(rgb_np, box, ink_hex, out_svg, perfect_circle=False, ss=8):
    """把 box 内的纯色线稿矢量化成 SVG。perfect_circle=True 时圆环用数学正圆重建。"""
    # 自动判断亮/暗背景：用 foreground_alpha（以框边缘为背景参照）
    # 比 color_ink_alpha 更通用，白图标在深色背景上也能正确提取
    a = Image.fromarray(foreground_alpha(rgb_np, box, margin=12))
    bbox = a.getbbox(); pad = 4
    crop = a.crop((max(0, bbox[0] - pad), max(0, bbox[1] - pad), bbox[2] + pad, bbox[3] + pad))
    ow, oh = crop.size
    binary = np.array(crop) > 100
    extra = ""           # 完美圆环（可选）
    trace_src = binary

    if perfect_circle:
        u8 = cv2.GaussianBlur((binary * 255).astype(np.uint8), (0, 0), 1)
        c = cv2.HoughCircles(u8, cv2.HOUGH_GRADIENT, 1, max(ow, oh), param1=120,
                             param2=18, minRadius=int(ow * 0.34), maxRadius=int(ow * 0.52))
        if c is not None:
            xc, yc, _ = c[0][0]
            core = np.array(crop) > 155
            ysc, xsc = np.where(core)
            dc = np.sqrt((xsc - xc) ** 2 + (ysc - yc) ** 2)
            rc = dc[dc > dc.max() * 0.80]
            r_out, r_in = np.percentile(rc, 90), np.percentile(rc, 10)
            r_mid, stroke = (r_out + r_in) / 2, max(1.0, r_out - r_in)
            extra = (f'<circle cx="{xc:.2f}" cy="{yc:.2f}" r="{r_mid:.2f}" '
                     f'fill="none" stroke="{ink_hex}" stroke-width="{stroke:.2f}"/>')
            yy, xx = np.mgrid[0:oh, 0:ow]
            dist = np.sqrt((xx - xc) ** 2 + (yy - yc) ** 2)
            ys2, xs2 = np.where(binary)
            d_all = np.sqrt((xs2 - xc) ** 2 + (ys2 - yc) ** 2)
            r_in_wide = np.percentile(d_all[d_all > d_all.max() * 0.78], 2)
            trace_src = binary & (dist < r_in_wide - 1.5)   # 去圆环只留内部图案

    star = Image.fromarray((trace_src * 255).astype(np.uint8), "L")
    big = star.resize((ow * ss, oh * ss), Image.LANCZOS).filter(ImageFilter.GaussianBlur(ss * 0.9))
    binimg = np.where(np.array(big) > 128, 0, 255).astype(np.uint8)
    bmp, tmp = out_svg + ".bmp", out_svg + ".tmp.svg"
    Image.fromarray(binimg, "L").convert("1").save(bmp)
    subprocess.run(["potrace", bmp, "-b", "svg", "-C", ink_hex, "--turdsize", str(ss * 3),
                    "--alphamax", "1.334", "--opttolerance", "0.6",
                    "-W", f"{ow}pt", "-H", f"{oh}pt", "-o", tmp], check=True)
    g = re.search(r"<g [^>]*>.*?</g>", open(tmp).read(), re.S)
    g_block = g.group(0) if g else ""
    os.remove(bmp); os.remove(tmp)
    with open(out_svg, "w") as f:
        f.write(f'<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{ow}pt" height="{oh}pt" '
                f'viewBox="0 0 {ow} {oh}">\n{extra}\n{g_block}\n</svg>\n')
    return out_svg


# ===================== PSD =====================

def alpha_to_layer_arr(original, alpha):
    """整图 alpha(np 或 PIL) -> 整画布 RGBA numpy（RGB 用原图像素）。"""
    a = alpha if isinstance(alpha, Image.Image) else Image.fromarray(alpha)
    rgba = original.copy(); rgba.putalpha(a.convert("L"))
    return np.array(rgba)


def _rgba_to_layer(name, arr, visible):
    ch = {i: np.ascontiguousarray(arr[:, :, i]) for i in range(3)}
    ch[-1] = np.ascontiguousarray(arr[:, :, 3])
    return nested_layers.Image(name=name, visible=visible, opacity=255,
                               top=0, left=0, channels=ch, color_mode=ColorMode.rgb)


def write_psd(stack, size, out_path, preview_path=None):
    """stack: list of (name, RGBA-numpy, visible)，自下而上。"""
    W, H = size
    merged = stack[0][1][:, :, :3].astype(np.float32)
    for _n, arr, vis in stack[1:]:
        if not vis:
            continue
        a = arr[:, :, 3:4].astype(np.float32) / 255.0
        merged = arr[:, :, :3] * a + merged * (1 - a)
    merged = merged.clip(0, 255).astype(np.uint8)

    layers = [_rgba_to_layer(n, a, v) for (n, a, v) in stack]
    psd = nested_layers.nested_layers_to_psd(
        list(reversed(layers)), color_mode=ColorMode.rgb, size=(W, H),
        compression=Compression.raw)
    psd.image_data = ImageData(
        channels=np.ascontiguousarray(np.transpose(merged, (2, 0, 1))),
        compression=Compression.raw)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as f:
        psd.write(f)
    if preview_path:
        Image.fromarray(merged).save(preview_path)
    return len(layers)
