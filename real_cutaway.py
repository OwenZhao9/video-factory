#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实产品特写混剪：把 AI 成片中的一段画面替换成真实产品的特写。

支持两种素材：
  图片（jpg/png/webp）→ 原图缓推动效（像素级真实，但本质是静态图）
  视频（mp4/mov）     → 直接嵌入该视频片段（推荐：用首帧模式生成的真图特写视频，
                        第一帧即原图，全程是真实运动画面）

音频全程不动（AI 成片的口播/BGM 保留，特写段的口播变画外音）。

用法：
  python3 real_cutaway.py output/tiktok_xxx.mp4 output/tiktok_特写.mp4
  python3 real_cutaway.py output/tiktok_xxx.mp4 input/脚垫.jpg --start 1.8 --dur 2.2
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

FPS = 24
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}


def main() -> None:
    ap = argparse.ArgumentParser(description="AI成片中插入真实产品特写（图片动效或视频片段）")
    ap.add_argument("video", help="AI 成片路径")
    ap.add_argument("insert", help="插入素材：真实产品图（缓推动效）或特写视频（直接嵌入）")
    ap.add_argument("--start", type=float, default=1.8,
                    help="特写开始时间（秒，默认1.8，落在开场口播上，口播变画外音）")
    ap.add_argument("--dur", type=float, default=2.2, help="特写时长（秒，默认2.2）")
    ap.add_argument("--zoom", type=float, default=0.12,
                    help="仅图片模式：推近幅度（默认0.12=缓推12%%）")
    ap.add_argument("--insert-offset", type=float, default=0.0,
                    help="仅视频模式：从素材的第几秒开始取（默认0=从头即首帧原图）")
    ap.add_argument("-o", "--output", default=None, help="输出路径（默认加 _real 后缀）")
    args = ap.parse_args()

    video, insert_src = Path(args.video), Path(args.insert)
    if not video.is_file():
        sys.exit(f"找不到视频：{video}")
    if not insert_src.is_file():
        sys.exit(f"找不到插入素材：{insert_src}")
    out = Path(args.output) if args.output else video.with_name(video.stem + "_real.mp4")
    is_video = insert_src.suffix.lower() in VIDEO_EXTS

    if is_video:
        insert = (
            f"[1:v]trim=start={args.insert_offset}:duration={args.insert_offset + args.dur},"
            f"scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,fps={FPS},"
            f"setpts=PTS-STARTPTS+{args.start}/TB[ins]"
        )
        inputs = ["-i", str(insert_src)]
    else:
        n_frames = int(FPS * args.dur)
        # 原图放大4倍再 zoompan 防抖动；白底 pad 成 9:16 与电商白底图风格一致
        insert = (
            f"[1:v]scale=2880:5120:force_original_aspect_ratio=decrease,"
            f"pad=2880:5120:(ow-iw)/2:(oh-ih)/2:color=white,"
            f"zoompan=z='1+{args.zoom}*on/{n_frames}':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d=1:s=720x1280:fps={FPS},"
            f"trim=duration={args.dur},"
            f"setpts=PTS-STARTPTS+{args.start}/TB[ins]"
        )
        inputs = ["-loop", "1", "-framerate", str(FPS), "-t", str(args.dur + 0.5),
                  "-i", str(insert_src)]

    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-i", str(video),
        *inputs,
        "-filter_complex", f"{insert};[0:v][ins]overlay=0:0:eof_action=pass[v]",
        "-map", "[v]", "-map", "0:a",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(out),
    ]
    subprocess.run(cmd, check=True)
    kind = "真图特写视频（首帧即原图）" if is_video else "真实产品图动效（像素级真实）"
    print(f"✅ 完成：{out}")
    print(f"   {args.start:.1f}s-{args.start + args.dur:.1f}s 为{kind}，音频全程未动")


if __name__ == "__main__":
    main()
