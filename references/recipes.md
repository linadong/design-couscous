# Recipe reference

## Recipe 字段

```jsonc
{
  "image":  "input/demo.png",            // 原图
  "output": "output/demo/demo.psd",      // 输出 PSD
  "preview":"output/demo/_preview.png",  // 可选：合成预览 PNG
  "flux_cache":"output/demo/_flux.png",  // 可选：缓存 Flux 背景，反复微调不烧额度

  "elements": [                          // 要抠出的元素（每个一层）
    { "prompt": "white detergent sheet", // ← SAM3 文字指令（你给）
      "refine": true,                    //   GrabCut 精修边缘
      "all_instances": false,            //   true=命中的多个实例都抠
      "conf": 0.30 }                     //   置信度阈值
  ],

  "fill": {                              // 背景补全（移除上面元素后补背景）
    "method": "auto",                    //   auto / flux / lama / telea
    "prompt": "blooming lavender field"  // ← 填什么（你给）；给了 prompt 时 auto 会选 flux
  },

  "graphics": [                          // 可选：图标矢量化 / 文字转可编辑（需手给区域框）
    { "name":"icon", "type":"icon", "box":[291,646,420,776],
      "perfect_circle": true,            //   圆环用数学正圆重建
      "remove_from_bg": true,
      "vectorize": "output/demo/icon.svg" },
    { "name":"DEEP_CLEANING", "type":"text", "box":[242,778,446,824],
      "content":["DEEP","CLEANING"],     //   文字内容（你给）
      "font":"/System/Library/Fonts/Avenir Next.ttc", "font_index":2,
      "editable": true, "remove_from_bg": true }
  ]
}
```

### 谁来给 / 谁自动

| 自动检测（不用填） | 你来给（写进 recipe / 对话） |
|---|---|
| 墨色（Otsu 采样暗部） | 抠图对象（SAM3 prompt） |
| 圆心 / 半径 / 描边宽（HoughCircles） | 填充内容（Flux prompt） |
| 文字行数 / 字号 / 位置（投影测量） | 图标/文字的区域框 `box`、字体、文字内容 |
| 元素位置（模板匹配） | 图片 / 输出路径 |

> `graphics`（图标矢量化、文字转可编辑）需要手给 `box`，因为"哪块是图标/文字"
> 因图而异。普通抠图+补背景**不需要** graphics 段。

### 补背景方法：`method: "auto"` 自动选

脚本会先分析待填充区域再决定（`choose_fill_method`）：

| 情况 | 自动选 | 为什么 |
|---|---|---|
| 给了 `fill.prompt` | **flux** | 意图是生成新内容（如 demo1 水→薰衣草） |
| 无提示词 + 区域细小（文字/线条/碎块） | **telea** | 经典修复最干净最快 |
| 无提示词 + 较大空缺 + 背景**平滑/渐变** | **smooth** | 金字塔平滑填充，颜色正确无糊斑（如 demo2 大片渐变） |
| 无提示词 + 较大空缺 + 背景**有可续接纹理** | **lama** | 续接重复结构/纹理，免费、不脑补 |

可在 recipe 里写死 `flux` / `lama` / `smooth` / `telea` 覆盖自动判断；
`fill.dilate`(默认10) 调外扩盖阴影、`fill.guidance` 调 Flux 强度。

> 经验：**平滑渐变背景**用 smooth，**有纹理可续接**用 lama，**创造/替换内容**用 flux（给 prompt）。
> 注意：生成式 Flux 在"产品包装"这类强语境图上做"还原"会脑补出产品，**还原优先 smooth/lama**。

## 单独抠图（不进 PSD）

```bash
conda run -n sam3 python sam3_cutout.py input/x.png "按钮" "图标" --refine
```

