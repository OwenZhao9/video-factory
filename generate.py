#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
产品图 → 15秒 TikTok 带货视频（Seedance 2.0）

支持两个平台（--provider）：
  notoken（默认）：NoToken 中转 https://notoken.pro ，令牌在其控制台「令牌管理」创建
  ark            ：火山方舟官方 https://console.volcengine.com/ark

用法：
  export NOTOKEN_API_KEY="你的NoToken令牌"
  python3 generate.py input/product.png
  python3 generate.py input/product.png --mode segments   # 分段生成再拼接
  python3 generate.py input/product.png --provider ark    # 走方舟官方

流程：
  1. 读取产品图（中转：先上传拿 URL；方舟：转 base64）
  2. 提交 Seedance 图生视频任务（多镜头提示词，见 prompts.py）
  3. 轮询任务状态直到完成
  4. 下载视频，ffmpeg 统一为 精确15s / H.264+AAC / faststart
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import prompts

# ─────────────────────────── 平台配置 ───────────────────────────
PROVIDERS = {
    "notoken": {
        "name": "NoToken 中转",
        "base": "https://notoken.pro/api/v3",
        "key_env": "NOTOKEN_API_KEY",
        "model": "seedance-2.0",          # 该分组实际模型：seedance-2.0 / -fast / -mini
        "upload_media": True,             # 素材先上传 /files/uploads 拿 URL
        "key_help": "在 https://notoken.pro 控制台 → 令牌管理 → 添加令牌，然后：\n"
                    '  export NOTOKEN_API_KEY="sk-xxxxxxxx"',
    },
    "ark": {
        "name": "火山方舟官方",
        "base": "https://ark.cn-beijing.volces.com/api/v3",
        "key_env": "ARK_API_KEY",
        "model": "doubao-seedance-2-0-260128",
        "upload_media": False,            # 直接 base64 data URL
        "key_help": "在 https://console.volcengine.com/ark → API Key 管理 中创建，然后：\n"
                    '  export ARK_API_KEY="xxxxxxxx"',
    },
}
PROVIDER: dict = PROVIDERS["notoken"]     # main() 里按 --provider 覆盖

TARGET_DURATION = 15          # 目标时长（秒），2.0 支持 4-15 的整数
RATIO = "9:16"                # 竖屏
POLL_INTERVAL = 10            # 轮询间隔（秒），文档建议 3-10s
POLL_TIMEOUT = 30 * 60        # 轮询超时（秒），15s 视频生成通常需要几分钟
# ────────────────────────────────────────────────────────────────


def api_key() -> str:
    """令牌来源优先级：环境变量 > 脚本目录下的 apikey.txt（跳过 # 注释行）。"""
    key = os.environ.get(PROVIDER["key_env"], "").strip()
    if not key:
        f = Path(__file__).resolve().parent / "apikey.txt"
        if f.is_file():
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip().strip('"').strip("'")
                if line and not line.startswith("#") and "粘贴" not in line:
                    key = line
                    break
    if not key:
        sys.exit(f"错误：没找到令牌。两种配置方式任选一：\n"
                 f"① 把令牌粘贴进 {Path(__file__).resolve().parent / 'apikey.txt'}\n"
                 f"② 设置环境变量 {PROVIDER['key_env']}\n{PROVIDER['key_help']}")
    return key


def http_json(url: str, payload: dict | None = None, method: str = "POST",
              retry_5xx: bool = True) -> dict:
    """retry_5xx=False 用于创建任务：5xx 时任务可能已建成，盲目重试会双倍扣费。"""
    data = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(3):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key()}",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            retryable = e.code == 429 or (retry_5xx and e.code in (500, 502, 503))
            if retryable and attempt < 2:
                wait = 10 * (attempt + 1)
                print(f"  HTTP {e.code}，{wait}s 后重试… {body[:200]}")
                time.sleep(wait)
                continue
            hint = ""
            if e.code in (500, 502, 503) and not retry_5xx:
                hint = "\n提示：任务可能已创建成功，去平台控制台确认后再重跑，避免重复扣费。"
            sys.exit(f"API 请求失败 HTTP {e.code}: {body}{hint}")
        except urllib.error.URLError as e:
            if attempt < 2:
                time.sleep(5)
                continue
            sys.exit(f"网络错误: {e}")
    raise RuntimeError("unreachable")


def file_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{b64}"


def upload_file(path: Path) -> str:
    """NoToken 中转：上传素材 → 查询文件 → 返回可引用的 URL。"""
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    body = (
        (f"--{boundary}\r\n"
         f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
         f"Content-Type: {mime}\r\n\r\n").encode()
        + path.read_bytes()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(f"{PROVIDER['base']}/files/uploads", data=body,
                                 method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": f"Bearer {api_key()}",
    })
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            up = json.load(resp)
    except urllib.error.HTTPError as e:
        sys.exit(f"上传 {path.name} 失败 HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    file_id = up.get("id")
    if not file_id:
        sys.exit(f"上传响应缺少 id：{json.dumps(up, ensure_ascii=False)[:300]}")
    info = http_json(f"{PROVIDER['base']}/files/{file_id}", method="GET")
    url = info.get("url")
    if not url:
        sys.exit(f"查询文件 {file_id} 未返回 url：{json.dumps(info, ensure_ascii=False)[:300]}")
    print(f"  已上传 {path.name} → {url}")
    return url


def media_url(path: Path) -> str:
    """按平台把本地素材变成 API 可用的 URL。"""
    return upload_file(path) if PROVIDER["upload_media"] else file_data_url(path)


def create_task(model: str, prompt_text: str, image_url: str, duration: int,
                resolution: str, image_role: str = "reference_image",
                ref_video_url: str | None = None,
                extra_image_urls: list[str] | None = None) -> str:
    """提交生成任务，返回 task_id。image_role: reference_image=参考图（保产品一致，
    镜头可自由切换）；first_frame=首帧（视频从这张图开始）。
    extra_image_urls: 追加参考图（如纹理特写），按顺序对应提示词里的 @图片2、@图片3…"""
    content = [
        {"type": "text", "text": prompt_text},
        {"type": "image_url", "image_url": {"url": image_url}, "role": image_role},
    ]
    for u in extra_image_urls or []:
        content.append({"type": "image_url", "image_url": {"url": u},
                        "role": "reference_image"})
    if ref_video_url:
        content.append({"type": "video_url", "video_url": {"url": ref_video_url},
                        "role": "reference_video"})
    resp = http_json(f"{PROVIDER['base']}/contents/generations/tasks", {
        "model": model,
        "content": content,
        "resolution": resolution,
        "ratio": RATIO,
        "duration": duration,
        "generate_audio": True,
        "watermark": False,
    }, retry_5xx=False)
    task_id = resp.get("id")
    if not task_id:
        sys.exit(f"未拿到 task id，响应：{json.dumps(resp, ensure_ascii=False)[:500]}")
    print(f"  任务已提交：{task_id}")
    return task_id


def wait_task(task_id: str) -> str:
    """轮询直到任务完成，返回视频 URL。"""
    start = time.time()
    poll_url = f"{PROVIDER['base']}/contents/generations/tasks/{task_id}"
    while True:
        if time.time() - start > POLL_TIMEOUT:
            sys.exit(f"任务 {task_id} 超时（>{POLL_TIMEOUT}s）")
        resp = http_json(poll_url, method="GET")
        status = resp.get("status")
        if status == "succeeded":
            url = (resp.get("content") or {}).get("video_url")
            if not url:
                sys.exit(f"任务成功但没有 video_url：{json.dumps(resp, ensure_ascii=False)[:500]}")
            return url
        if status in ("failed", "cancelled", "expired"):
            sys.exit(f"任务 {status}：{json.dumps(resp.get('error') or resp, ensure_ascii=False)[:500]}")
        elapsed = int(time.time() - start)
        print(f"  状态 {status}，已等待 {elapsed}s …")
        time.sleep(POLL_INTERVAL)


def download(url: str, dest: Path) -> Path:
    print(f"  下载 → {dest.name}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as f:
        while chunk := resp.read(1 << 16):
            f.write(chunk)
    return dest


def ffmpeg(*args: str) -> None:
    cmd = ["ffmpeg", "-v", "error", "-y", *args]
    subprocess.run(cmd, check=True)


def standardize(src: Path, dest: Path, duration: int = TARGET_DURATION) -> Path:
    """保持原生分辨率，统一为 精确时长 / H.264+AAC / faststart，适配 TikTok 上传。"""
    ffmpeg(
        "-i", str(src),
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-movflags", "+faststart",
        str(dest),
    )
    return dest


def concat(parts: list[Path], dest: Path) -> Path:
    """无损拼接（各段先统一编码参数）。"""
    lst = dest.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(dest))
    lst.unlink()
    return dest


def main() -> None:
    global PROVIDER
    ap = argparse.ArgumentParser(description="产品图 → 15s TikTok 带货视频（Seedance 2.0）")
    ap.add_argument("image", help="产品图路径（png/jpg/webp）")
    ap.add_argument("--provider", choices=list(PROVIDERS), default="notoken",
                    help="notoken=NoToken中转（默认）；ark=火山方舟官方")
    ap.add_argument("--mode", choices=["single", "segments"], default="single",
                    help="single=一次生成15s多镜头（默认）；segments=分3段生成再拼接")
    ap.add_argument("--prompt", default=None, help="自定义提示词（覆盖 prompts.PROMPT）")
    ap.add_argument("--model", default=None,
                    help="模型名（默认按平台：中转=seedance-2.0，方舟=doubao-seedance-2-0-260128）")
    ap.add_argument("--resolution", default="720p", choices=["480p", "720p", "1080p", "4k"],
                    help="分辨率（默认720p；9:16下 720p=720x1280，1080p=1080x1920）")
    ap.add_argument("--duration", type=int, default=TARGET_DURATION,
                    help=f"时长（秒，4-15，默认{TARGET_DURATION}；仅 single 模式生效）")
    ap.add_argument("--image-role", default="reference_image",
                    choices=["reference_image", "first_frame"],
                    help="产品图的用法：参考图（默认）或首帧")
    ap.add_argument("--extra-image", action="append", default=[],
                    help="可选：追加参考图（可多次使用，如产品纹理特写），"
                         "按顺序对应提示词里的 @图片2、@图片3…")
    ap.add_argument("--ref-video", default=None,
                    help="可选：实拍参考视频路径，让模型参考其运镜节奏。"
                         "注意：含真人脸部的素材可能被 Seedance 2.0 拒绝")
    ap.add_argument("-o", "--outdir", default="output", help="输出目录")
    args = ap.parse_args()

    PROVIDER = PROVIDERS[args.provider]
    model = args.model or PROVIDER["model"]
    api_key()  # 先检查 Key，避免白等
    img = Path(args.image)
    if not img.is_file():
        sys.exit(f"找不到图片：{img}")
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d_%H%M%S")
    print(f"平台：{PROVIDER['name']}（{PROVIDER['base']}）  模型：{model}")
    img_url = media_url(img)

    extra_urls = []
    for p in args.extra_image:
        ep = Path(p)
        if not ep.is_file():
            sys.exit(f"找不到追加参考图：{ep}")
        extra_urls.append(media_url(ep))

    ref_video_url = None
    if args.ref_video:
        rv = Path(args.ref_video)
        if not rv.is_file():
            sys.exit(f"找不到参考视频：{rv}")
        if rv.stat().st_size > 45 * 1024 * 1024:
            sys.exit("参考视频超过 45MB，请先压缩")
        ref_video_url = media_url(rv)

    if args.mode == "single":
        prompt_text = args.prompt or prompts.PROMPT
        if ref_video_url:
            prompt_text += prompts.REF_VIDEO_HINT
        print(f"[1/3] 提交任务（{model}，{args.duration}s，{RATIO} {args.resolution}）")
        task_id = create_task(model, prompt_text, img_url, args.duration,
                              args.resolution, args.image_role, ref_video_url,
                              extra_urls)
        print("[2/3] 等待生成…")
        video_url = wait_task(task_id)
        raw = download(video_url, outdir / f"raw_{stamp}.mp4")
        print(f"[3/3] 后处理（精确{args.duration}s / H.264 / faststart）")
        final = standardize(raw, outdir / f"tiktok_{stamp}.mp4", args.duration)
    else:
        seg_prompts = prompts.SEGMENT_PROMPTS
        per = 5
        parts: list[Path] = []
        for i, sp in enumerate(seg_prompts, 1):
            print(f"[段 {i}/{len(seg_prompts)}] 提交任务（{per}s）")
            sp_text = args.prompt or sp
            task_id = create_task(model, sp_text, img_url, per,
                                  args.resolution, args.image_role)
            video_url = wait_task(task_id)
            raw = download(video_url, outdir / f"seg{i}_raw_{stamp}.mp4")
            parts.append(standardize(raw, outdir / f"seg{i}_{stamp}.mp4", per))
        print("[拼接] 合并3段 → 15s")
        final = concat(parts, outdir / f"tiktok_{stamp}.mp4")

    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(final)], capture_output=True, text=True
                         ).stdout.strip()
    print(f"\n✅ 完成：{final}（{float(dur):.1f}s）")


if __name__ == "__main__":
    main()
