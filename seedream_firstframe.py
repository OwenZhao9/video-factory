#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Seedream 首帧图生成器（火山方舟 Ark）

作用：用真实产品图当参考，生成一张"产品在场景里、画准了"的图片，
再把它当作 Seedance 视频的首帧（first_frame）——这样产品在开头就是准的、
硬挺不变形。这是"图像先锁一致 → 首帧锚定动起来"工作流的第一步。

为什么要这一步：Seedance 直接图生视频（参考图模式）会把产品"重新想象"，
人手举着翻转时容易变软/变形。先用 Seedream 在图像阶段把产品锁死，
比在视频阶段硬压稳得多。

用法：
  export ARK_API_KEY="你的方舟key"    # 或写进 ark_key.txt
  # 一张主图：
  python3 seedream_firstframe.py input/产品.jpg
  # 多角度实拍图一起喂（花纹更准，最多可传多张）：
  python3 seedream_firstframe.py input/俯视.jpg input/左角.jpg input/右角.jpg
  # 自定义画面：
  python3 seedream_firstframe.py input/产品.jpg --prompt "你的场景描述"

产出：output/首帧_时间戳.png（默认 9:16 竖屏），拿它去 generate.py 当首帧：
  python3 generate.py output/首帧_xxx.png --image-role first_frame --prompt "让它动起来的描述"
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ARK_URL = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
# 方舟可用生图模型（新→旧）：5.0-pro 最强，4.5 也很好、更便宜
DEFAULT_MODEL = "doubao-seedream-5-0-pro-260628"
MAX_REF_PX = 2048          # 参考图超过此边长先压缩（原图常十几MB，会撑爆请求体）

DEFAULT_PROMPT = (
    "竖屏9:16手机实拍UGC风格照片。明亮车库里一辆白色特斯拉Model Y主驾车门敞开，"
    "一位穿浅灰色连帽衫的年轻欧美白人男子坐在主驾驶座，面对镜头自然微笑，一只手对镜头竖起大拇指，"
    "另一只手扶住参考图里那件产品、让它竖直正对镜头完整展示正面。"
    "【构图】产品在画面里又大又醒目、占据画面主要位置（约三分之二高度），是画面的绝对主角；"
    "人物位于产品的上方/侧后方，露出上半身和竖大拇指的手；产品下缘接近画面底部。"
    "产品必须与参考图完全一致：纹理、形状、轮廓、材质、颜色全部1:1还原参考实拍图，硬挺笔直不变形。"
    "背景可见特斯拉全景玻璃车顶和车库，自然光，真实手机照片质感，画面干净，无任何文字、字母或水印。"
)


def ark_key() -> str:
    import os
    key = os.environ.get("ARK_API_KEY", "").strip()
    if not key:
        f = Path(__file__).resolve().parent / "ark_key.txt"
        if f.is_file():
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip().strip('"').strip("'")
                if line and not line.startswith("#"):
                    key = line
                    break
    if not key:
        sys.exit("错误：没找到方舟 key。把它写进 ark_key.txt，或 export ARK_API_KEY=\"...\"\n"
                 "在 https://console.volcengine.com/ark → API Key 管理 创建。")
    return key


def data_url(path: Path, tmp: Path) -> str:
    """大图先压到 MAX_REF_PX 再转 base64，避免请求体过大。"""
    src = path
    try:
        from PIL import Image  # 若无 Pillow 则用 sips（macOS 自带）
        img = Image.open(path)
        if max(img.size) > MAX_REF_PX:
            small = tmp / f"ref_{path.stem}.jpg"
            img.convert("RGB").thumbnail((MAX_REF_PX, MAX_REF_PX))
            img.save(small, quality=88)
            src = small
    except ImportError:
        if _dim(path) > MAX_REF_PX and _has("sips"):
            small = tmp / f"ref_{path.stem}.jpg"
            subprocess.run(["sips", "-Z", str(MAX_REF_PX), str(path), "--out", str(small)],
                           check=True, capture_output=True)
            src = small
    mime = mimetypes.guess_type(str(src))[0] or "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(src.read_bytes()).decode()


def _has(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None


def _dim(path: Path) -> int:
    try:
        out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                             capture_output=True, text=True).stdout
        return max(int(x.split(":")[1]) for x in out.splitlines() if ":" in x and x.split(":")[1].strip().isdigit())
    except Exception:
        return 0


def generate_firstframe(image_paths, prompt: str, key: str, out_path: Path,
                        model: str = DEFAULT_MODEL, size: str = "1440x2560") -> Path:
    """核心：用 Seedream 生成一张首帧图，存到 out_path 并返回。CLI 和网页版共用。
    出错抛 RuntimeError（消息可直接展示给用户）。"""
    paths = [Path(p) for p in image_paths]
    for p in paths:
        if not p.is_file():
            raise RuntimeError(f"找不到参考图：{p}")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.parent / ".refcache"
    tmp.mkdir(exist_ok=True)

    body = {
        "model": model,
        "prompt": prompt,
        "image": [data_url(p, tmp) for p in paths],
        "size": size,
        "response_format": "url",
        "watermark": False,
    }
    req = urllib.request.Request(ARK_URL, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Seedream 生成失败 HTTP {e.code}: {e.read().decode(errors='replace')[:400]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误: {e}")

    url = (resp.get("data") or [{}])[0].get("url")
    if not url:
        raise RuntimeError(f"未拿到图片地址：{json.dumps(resp, ensure_ascii=False)[:300]}")
    urllib.request.urlretrieve(url, out_path)
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Seedream 生成 Seedance 首帧图（产品锁一致）")
    ap.add_argument("images", nargs="+", help="产品参考图（1张或多张多角度，第1张为主）")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT, help="画面描述（默认：特斯拉车内举脚垫）")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"方舟生图模型（默认 {DEFAULT_MODEL}）")
    ap.add_argument("--size", default="1440x2560", help="尺寸（默认 9:16 竖屏 1440x2560）")
    ap.add_argument("-o", "--outdir", default="output", help="输出目录")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    out = outdir / f"首帧_{time.strftime('%m%d_%H%M%S')}.png"
    print(f"[1/2] 提交 Seedream 生成（{args.model}，{args.size}，参考图 {len(args.images)} 张）…")
    try:
        generate_firstframe(args.images, args.prompt, ark_key(), out, args.model, args.size)
    except RuntimeError as e:
        sys.exit(str(e))
    print(f"[2/2] 已保存 → {out}")
    print(f"\n✅ 首帧图已生成：{out}")
    print(f"   下一步用它生成视频：")
    print(f'   python3 generate.py "{out}" --image-role first_frame --prompt "让画面动起来的描述"')


if __name__ == "__main__":
    main()
