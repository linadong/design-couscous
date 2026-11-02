"""
run_psd.py —— 读 recipe(JSON) 跑完整流程：抠图 → 精修 →（可选）补背景 → 分层 PSD

用法：
  conda run -n sam3 python run_psd.py recipes/demo.json

补背景是【可选】的：recipe 不写 fill，或写 {"enabled": false} / {"method":"none"}，
就只抠图不补背景，背景层保留原图。
"""

import json
import os
import sys

import cv2
import numpy as np
from PIL import Image

import sam3_cutout as s
import pipeline as P


def run_recipe(recipe):
    """按 recipe(dict) 跑完整流程，返回输出 PSD 路径。"""
    original = Image.open(recipe["image"]).convert("RGBA")
    W, H = original.size
    rgb = np.array(original.convert("RGB"))
    api_key = None
    print(f"📷 {recipe['image']}  {W}x{H}")

    element_layers, photo_alphas = [], []
    graphic_layers, graphic_removal = [], []
    unresolved_required = []

    # ---- 抠图元素 ----
    for el in recipe.get("elements", []):
        print(f"🛰️  抠「{el['prompt']}」...")
        if api_key is None:
            api_key = s.load_api_key()
        alphas = P.cut_element(original, el["prompt"], api_key,
                               refine=el.get("refine", True),
                               conf_min=el.get("conf", 0.30),
                               all_instances=el.get("all_instances", True))
        if not alphas:
            print(f"   ⚠️ 未命中：{el['prompt']}")
            if el.get("required", True):
                unresolved_required.append(el["prompt"])
        base = el.get("name", P.slug(el["prompt"]))
        for i, al in enumerate(alphas):
            nm = base if len(alphas) == 1 else f"{base}_{i+1:02d}"
            element_layers.append((nm, P.alpha_to_layer_arr(original, al), True))
            photo_alphas.append(np.array(al))
            print(f"   ✅ {nm}")

    # ---- 本机视觉对象蒙版（不调用外部服务的自动 layer plan） ----
    # Requested objects must not also remain duplicated in the local group.
    requested_coverage = np.maximum.reduce(photo_alphas) if photo_alphas else None
    for item in recipe.get("local_masks", []):
        mask_path = item.get("mask")
        if not mask_path or not os.path.exists(mask_path):
            print(f"   ⚠️ 本机蒙版不存在：{mask_path}")
            continue
        alpha = Image.open(mask_path).convert("L")
        if alpha.size != (W, H):
            alpha = alpha.resize((W, H), Image.Resampling.LANCZOS)
        if requested_coverage is not None:
            remainder = np.minimum(np.array(alpha), 255 - requested_coverage)
            alpha = Image.fromarray(remainder.astype(np.uint8))
        if (np.array(alpha) > 40).sum() < item.get("min_area", 1500):
            print(f"   ⚠️ 本机蒙版面积过小：{item.get('name', 'object')}")
            continue
        name = item.get("name", "Detected object")
        if requested_coverage is not None:
            name = f"Unresolved foreground remainder — {name}"
        element_layers.append((name, P.alpha_to_layer_arr(original, alpha), True))
        photo_alphas.append(np.array(alpha))
        print(f"   ✅ 本机视觉图层：{name}")

    # ---- OCR 文字图层（所有已识别文字默认分层） ----
    for item in recipe.get("text_regions", []):
        try:
            x0 = int(item["left"]); y0 = int(item["top"])
            x1 = x0 + int(item["width"]); y1 = y0 + int(item["height"])
        except (KeyError, TypeError, ValueError):
            continue
        alpha = P.text_ink_alpha(rgb, (x0, y0, x1, y1))
        if (alpha > 40).sum() < 25:
            continue
        text = str(item.get("text", "")).strip()
        label = item.get("name") or (f"Text — {text}" if text else "Text")
        graphic_layers.append((label, P.alpha_to_layer_arr(original, alpha), True))
        graphic_removal.append(P.union([alpha], 3, (H, W)))
        print(f"   ✅ OCR 文字图层：{label}")

    if unresolved_required:
        missing = "、".join(unresolved_required)
        raise RuntimeError(f"Required layers were not extracted: {missing}. No PSD was exported.")

    # ---- 图文特殊处理 ----
    for g in recipe.get("graphics", []):
        box = g["box"]
        ink = P.detect_ink_color(rgb, box)
        ink_hex = g.get("ink_color") or "#%02x%02x%02x" % ink  # recipe 可覆盖自动检测
        ink_alpha = P.color_ink_alpha(rgb, box)
        print(f"🎨 {g['name']}（{g['type']}）墨色 {ink_hex}")

        if g["type"] == "icon":
            graphic_layers.append((g["name"], P.alpha_to_layer_arr(original, ink_alpha), True))
            if g.get("vectorize"):
                P.vectorize_region(rgb, box, ink_hex, g["vectorize"],
                                   perfect_circle=g.get("perfect_circle", True))
                print(f"   ✅ SVG -> {g['vectorize']}")
            if g.get("remove_from_bg", True):
                circ = P.detect_circle(rgb, box) if g.get("perfect_circle", True) else None
                m = np.zeros((H, W), np.uint8)
                if circ:
                    cv2.circle(m, (int(circ[0]), int(circ[1])), int(circ[2] * 1.15), 255, -1)
                else:
                    m = P.union([ink_alpha], 5, (H, W))
                graphic_removal.append(m)

        elif g["type"] == "foreground":
            fa = P.foreground_alpha(rgb, box, margin=g.get("margin", 12))
            graphic_layers.append((g["name"], P.alpha_to_layer_arr(original, fa), True))
            if g.get("remove_from_bg", False):
                graphic_removal.append(P.union([fa], 5, (H, W)))

        elif g["type"] == "text":
            graphic_layers.append((g["name"] + "_原始像素",
                                   P.alpha_to_layer_arr(original, ink_alpha), False))
            if g.get("editable", True):
                lines = P.measure_text_lines(ink_alpha, box)
                arr = P.render_text_layer((W, H), lines, g["content"],
                                          g["font"], g.get("font_index", 0), ink)
                graphic_layers.append((g["name"] + "_可编辑文本", arr, True))
                print(f"   ✅ 重排版文字 {len(lines)} 行")
            if g.get("remove_from_bg", True):
                graphic_removal.append(P.union([ink_alpha], 5, (H, W)))

    # ---- 补背景（可选）----
    fill = recipe.get("fill") or {}
    fill_on = bool(fill) and fill.get("enabled", True) and fill.get("method") != "none"
    if fill_on:
        photo_dil = fill.get("dilate", 10)   # 外扩，盖住元素阴影/毛边
        photo_mask = P.union(photo_alphas, photo_dil, (H, W)) if photo_alphas else None
        graphic_mask = P.union(graphic_removal, 0, (H, W)) if graphic_removal else None
        clean = P.build_clean_background(original, photo_mask, graphic_mask,
                                         method=fill.get("method", "auto"),
                                         prompt=fill.get("prompt", ""),
                                         flux_cache=recipe.get("flux_cache"),
                                         guidance=fill.get("guidance", 60))
        default_bg_name = "clean_background"
    else:
        print("🚫 不补背景：背景层保留原图")
        clean = original.convert("RGB")
        default_bg_name = "原图背景(未补全)"
    bg_arr = np.dstack([np.array(clean), np.full((H, W), 255, np.uint8)])
    if recipe.get("cleanbg_debug"):
        clean.save(recipe["cleanbg_debug"])

    # A background-only PSD is never a valid Layer Rebuild result.  Failing
    # here stops the web app from delivering a deceptively successful export.
    extracted_count = len(element_layers) + len(graphic_layers)
    if extracted_count == 0:
        raise RuntimeError("No independent layers were extracted. No PSD was exported.")

    # ---- 组装 & 写 PSD ----
    stack = [(recipe.get("background_name", default_bg_name), bg_arr, True)]
    stack += element_layers + graphic_layers
    n = P.write_psd(stack, (W, H), recipe["output"],
                    preview_path=recipe.get("preview"))
    print(f"\n🎉 完成：{n} 层（含 {extracted_count} 个独立图层） -> {recipe['output']}")
    return recipe["output"]


def main():
    recipe = json.load(open(sys.argv[1], encoding="utf-8"))
    run_recipe(recipe)


if __name__ == "__main__":
    main()
