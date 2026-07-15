#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Gemini 接入：Nano Banana 生图 + Veo 3.1 图生视频（纯标准库 urllib）。

两者共用一个 Gemini API Key（aistudio.google.com/api-keys 创建）。
- nano_banana(): 用产品参考图 + 文字，生成一张 9:16 首帧图（保持产品一致）
- veo_i2v():     用首帧图 + 提示词，图生视频（异步长任务：创建→轮询→下载）

接口契约来自官方文档（2026-07 核对）：
  基址 https://generativelanguage.googleapis.com/v1beta
  鉴权 header: x-goog-api-key: <KEY>
  生图 POST /models/{model}:generateContent
  视频 POST /models/{model}:predictLongRunning → 轮询 GET /{operation.name} → 下载 video.uri
"""

from __future__ import annotations

import base64
import json
import mimetypes
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://generativelanguage.googleapis.com/v1beta"
MAX_REF_PX = 2048

# Nano Banana 生图模型（新→旧）
IMAGE_MODELS = {
    "gemini-3-pro-image": "Nano Banana Pro（最强）",
    "gemini-3.1-flash-image": "Nano Banana 2（快·质量高）",
    "gemini-2.5-flash-image": "Nano Banana 1（经典·稳定）",
}
# Veo 3.1 视频模型
VIDEO_MODELS = {
    "veo-3.1-generate-preview": "Veo 3.1 标准",
    "veo-3.1-fast-generate-preview": "Veo 3.1 Fast",
    "veo-3.1-lite-generate-preview": "Veo 3.1 Lite（最省）",
}


def _headers(key: str) -> dict:
    return {"x-goog-api-key": key, "Content-Type": "application/json"}


def _raw_b64(path: Path, tmp: Path) -> tuple[str, str]:
    """返回 (纯base64, mime)。大图先压到 MAX_REF_PX。"""
    src = Path(path)
    mime = mimetypes.guess_type(str(src))[0] or "image/jpeg"
    try:
        from PIL import Image
        img = Image.open(src)
        if max(img.size) > MAX_REF_PX:
            dst = tmp / f"g_{src.stem}.jpg"
            img.convert("RGB").thumbnail((MAX_REF_PX, MAX_REF_PX))
            img.save(dst, quality=88)
            src, mime = dst, "image/jpeg"
    except ImportError:
        from shutil import which
        if which("sips"):
            dst = tmp / f"g_{src.stem}.jpg"
            r = subprocess.run(["sips", "-Z", str(MAX_REF_PX), str(src), "--out", str(dst)],
                               capture_output=True)
            if r.returncode == 0:
                src, mime = dst, "image/jpeg"
    return base64.b64encode(src.read_bytes()).decode(), mime


def _post(url: str, body: dict, key: str, timeout: int = 180) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=_headers(key), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:400]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误: {e}")


# ─────────────────────────── Gemini 文本（AI 写脚本）───────────────────────────
TEXT_MODEL = "gemini-2.5-flash"   # 便宜快、有免费额度；更强可换 gemini-2.5-pro / gemini-3.5-flash


def gen_text(prompt: str, key: str, model: str = TEXT_MODEL) -> str:
    """文本生成（generateContent），返回纯文本。"""
    url = f"{BASE}/models/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    resp = _post(url, body, key, timeout=60)
    cands = resp.get("candidates") or []
    if not cands:
        raise RuntimeError(f"没返回内容（可能被内容策略拦截）：{json.dumps(resp, ensure_ascii=False)[:300]}")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError(f"返回为空：{json.dumps(resp, ensure_ascii=False)[:300]}")
    return text


# ─────────────────────────── Nano Banana 生图 ───────────────────────────
def nano_banana(image_paths, prompt: str, key: str, out_path: Path,
                model: str = "gemini-3-pro-image", aspect: str = "9:16") -> Path:
    """用参考图+文字生成一张图，保存到 out_path。出错抛 RuntimeError。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.parent / ".refcache"
    tmp.mkdir(exist_ok=True)

    parts = [{"text": prompt}]
    for p in image_paths:
        b64, mime = _raw_b64(Path(p), tmp)
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})

    url = f"{BASE}/models/{model}:generateContent"
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": aspect},
        },
    }
    try:
        resp = _post(url, body, key)
    except RuntimeError as e:
        # 某些模型对纯 ["IMAGE"] 不接受，退回 ["TEXT","IMAGE"] 再试一次
        if "modal" in str(e).lower() or "IMAGE" in str(e):
            body["generationConfig"]["responseModalities"] = ["TEXT", "IMAGE"]
            resp = _post(url, body, key)
        else:
            raise

    cands = resp.get("candidates") or []
    if not cands:
        raise RuntimeError(f"没返回候选（可能被内容策略拦截）：{json.dumps(resp, ensure_ascii=False)[:300]}")
    parts_out = (cands[0].get("content") or {}).get("parts") or []
    img_part = next((p for p in parts_out if p.get("inlineData") or p.get("inline_data")), None)
    if not img_part:
        raise RuntimeError(f"响应里没有图片：{json.dumps(resp, ensure_ascii=False)[:300]}")
    inl = img_part.get("inlineData") or img_part.get("inline_data")
    out_path.write_bytes(base64.b64decode(inl["data"]))
    return out_path


# ─────────────────────────── Veo 3.1 图生视频 ───────────────────────────
def veo_i2v(image_path, prompt: str, key: str, out_path: Path,
            model: str = "veo-3.1-generate-preview", duration: int = 8,
            resolution: str = "720p", aspect: str = "9:16",
            on_log=None, poll_interval: int = 10, timeout: int = 30 * 60) -> Path:
    """首帧图 + 提示词 → mp4，保存到 out_path。异步：创建→轮询→下载。"""
    def log(m):
        (on_log or (lambda x: None))(m)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.parent / ".refcache"
    tmp.mkdir(exist_ok=True)

    # 1080p/4k 强制 8 秒
    dur = 8 if resolution in ("1080p", "4k") else max(4, min(8, int(duration)))
    b64, mime = _raw_b64(Path(image_path), tmp)

    body = {
        "instances": [{
            "prompt": prompt,
            "image": {"inlineData": {"mimeType": mime, "data": b64}},
        }],
        "parameters": {
            "aspectRatio": aspect,
            "durationSeconds": str(dur),
            "resolution": resolution,
            "personGeneration": "allow_adult",
            "numberOfVideos": 1,
        },
    }
    log(f"提交 Veo 任务（{model}，{dur}s，{aspect} {resolution}）…")
    created = _post(f"{BASE}/models/{model}:predictLongRunning", body, key)
    op = created.get("name")
    if not op:
        raise RuntimeError(f"未拿到任务 name：{json.dumps(created, ensure_ascii=False)[:300]}")
    log(f"任务已创建，生成中（约 1-6 分钟）…")

    start = time.time()
    while True:
        if time.time() - start > timeout:
            raise RuntimeError("Veo 生成超时")
        req = urllib.request.Request(f"{BASE}/{op}", headers=_headers(key))
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                st = json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"轮询失败 HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
        if st.get("done"):
            if st.get("error"):
                raise RuntimeError(f"Veo 失败：{json.dumps(st['error'], ensure_ascii=False)[:300]}")
            try:
                uri = st["response"]["generateVideoResponse"]["generatedSamples"][0]["video"]["uri"]
            except (KeyError, IndexError):
                raise RuntimeError(f"完成但找不到视频 uri：{json.dumps(st, ensure_ascii=False)[:300]}")
            log("生成完成，下载视频 …")
            dl = urllib.request.Request(uri, headers=_headers(key))
            with urllib.request.urlopen(dl, timeout=300) as r, open(out_path, "wb") as f:
                while chunk := r.read(1 << 16):
                    f.write(chunk)
            return out_path
        log(f"生成中……已等待 {int(time.time() - start)} 秒")
        time.sleep(poll_interval)


if __name__ == "__main__":
    import sys
    print("Nano Banana 生图模型：", list(IMAGE_MODELS))
    print("Veo 视频模型：", list(VIDEO_MODELS))
    print("用法见 webapp.py / seedream_firstframe.py 的调用。")
