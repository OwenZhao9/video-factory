#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网页版视频工厂 —— 给不懂代码的客户用的本地网页界面。

双击「启动视频工厂.command」即可使用：
  浏览器打开 → 填 API 令牌 → 上传产品图 → 改卖点台词 → 点生成 → 下载视频

技术说明：纯 Python 标准库（无需 pip 安装任何东西），只监听本机 127.0.0.1，
生成逻辑复用 generate.py。一次只跑一个任务。
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import generate
import prompts
import seedream_firstframe
import gemini_api

ROOT = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "input"
OUTPUT_DIR = ROOT / "output"
KEY_FILE = ROOT / "apikey.txt"
ARK_KEY_FILE = ROOT / "ark_key.txt"
GEMINI_KEY_FILE = ROOT / "gemini_key.txt"

# Seedream 首帧默认画面描述
FIRSTFRAME_PROMPT = seedream_firstframe.DEFAULT_PROMPT

# 生图引擎 → 模型列表（给页面下拉用）
SEEDREAM_MODELS = [
    {"id": "doubao-seedream-5-0-pro-260628", "label": "5.0 Pro（最强·推荐）"},
    {"id": "doubao-seedream-5-0-260128", "label": "5.0"},
    {"id": "doubao-seedream-4-5-251128", "label": "4.5（更便宜）"},
]
NANOBANANA_MODELS = [{"id": k, "label": v} for k, v in gemini_api.IMAGE_MODELS.items()]
# Veo 视频模型 + 每秒美元价（720p）
VEO_MODELS = [
    {"id": "veo-3.1-generate-preview", "label": "Veo 3.1 标准", "usd_per_sec": 0.40},
    {"id": "veo-3.1-fast-generate-preview", "label": "Veo 3.1 Fast", "usd_per_sec": 0.10},
    {"id": "veo-3.1-lite-generate-preview", "label": "Veo 3.1 Lite（最省）", "usd_per_sec": 0.05},
]

MODELS = {
    "seedance-2.0":      {"label": "标准版", "desc": "效果最好，花纹还原最准", "tag": "推荐", "rate": 46},
    "seedance-2.0-fast": {"label": "快速版", "desc": "速度快些，质量略降", "tag": "", "rate": 37},
    "seedance-2.0-mini": {"label": "省钱版 mini", "desc": "花纹还原差，不建议带货用", "tag": "慎用", "rate": 23},
}
GROUP_MULTIPLIER = 0.95  # NoToken「SeeDance满血」分组倍率

# ── 任务状态（单任务串行） ─────────────────────────────────────────
JOB: dict = {"state": "idle", "log": [], "video": None, "started": 0.0}
JOB_LOCK = threading.Lock()


def log(msg: str) -> None:
    print(msg, flush=True)
    JOB["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")


def read_key() -> str:
    if KEY_FILE.is_file():
        for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip().strip('"').strip("'")
            if line and not line.startswith("#") and "粘贴" not in line:
                return line
    return ""


def save_key(key: str) -> None:
    KEY_FILE.write_text(
        "# 此文件由网页版自动维护，保存你的 NoToken 令牌（请勿外传）\n" + key + "\n",
        encoding="utf-8")


def read_ark_key() -> str:
    if ARK_KEY_FILE.is_file():
        for line in ARK_KEY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip().strip('"').strip("'")
            if line and not line.startswith("#") and "粘贴" not in line:
                return line
    return ""


def save_ark_key(key: str) -> None:
    ARK_KEY_FILE.write_text(
        "# 此文件由网页版自动维护，保存你的火山方舟 Key（生图用，请勿外传）\n" + key + "\n",
        encoding="utf-8")


def read_gemini_key() -> str:
    if GEMINI_KEY_FILE.is_file():
        for line in GEMINI_KEY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip().strip('"').strip("'")
            if line and not line.startswith("#") and "粘贴" not in line:
                return line
    return ""


def save_gemini_key(key: str) -> None:
    GEMINI_KEY_FILE.write_text(
        "# 此文件由网页版自动维护，保存你的 Google Gemini Key（Nano Banana / Veo 用，请勿外传）\n" + key + "\n",
        encoding="utf-8")


def estimate_points(model: str, resolution: str, duration: int) -> float:
    w, h = (720, 1280) if resolution == "720p" else (1080, 1920)
    tokens = w * h * 24 * duration / 1024
    rate = MODELS.get(model, MODELS["seedance-2.0"])["rate"]
    return round(tokens / 1_000_000 * rate * GROUP_MULTIPLIER, 1)


# ── 生成任务（后台线程） ──────────────────────────────────────────
def run_job(key: str, image: Path, extras: list[Path], prompt_text: str,
            model: str, resolution: str, duration: int,
            image_role: str = "reference_image",
            video_engine: str = "seedance", gemini_key: str = "",
            veo_model: str = "veo-3.1-generate-preview") -> None:
    try:
        # ── Veo 3.1（Google）图生视频分支 ──
        if video_engine == "veo":
            save_gemini_key(gemini_key)
            log(f"引擎：Veo 3.1（{veo_model}），{resolution}，首帧图 {image.name}")
            stamp = time.strftime("%m%d_%H%M%S")
            raw = OUTPUT_DIR / f"网页版_raw_{stamp}.mp4"
            gemini_api.veo_i2v(image, prompt_text, gemini_key, raw,
                               model=veo_model, duration=duration,
                               resolution=resolution, aspect="9:16", on_log=log)
            final = OUTPUT_DIR / f"网页版_{stamp}.mp4"
            if shutil.which("ffmpeg"):
                log("后处理（统一编码，适配 TikTok 上传）…")
                generate.standardize(raw, final, duration)
            else:
                shutil.copyfile(raw, final)
            with JOB_LOCK:
                JOB["video"] = final.name
                JOB["state"] = "done"
            log(f"✅ 完成：{final.name}")
            return

        # ── Seedance（NoToken）分支 ──
        os.environ["NOTOKEN_API_KEY"] = key
        save_key(key)
        est = estimate_points(model, resolution, duration)
        mode_txt = "首帧模式(产品更稳)" if image_role == "first_frame" else "参考图模式"
        log(f"参数：{model} / {resolution} / {duration}s / {mode_txt}，预估消耗 ≈{est} 创作点")

        who = "首帧图" if image_role == "first_frame" else "产品图"
        log(f"上传{who} {image.name} …")
        img_url = generate.media_url(image)
        extra_urls = []
        if image_role == "first_frame":
            if extras:
                log("ℹ️ 首帧模式下忽略细节特写图（首尾/参考不可混用），只用首帧那张")
        else:
            for p in extras:
                log(f"上传细节特写 {p.name} …")
                extra_urls.append(generate.media_url(p))
            if ("@图片2" in prompt_text or "@图片3" in prompt_text) and not extra_urls:
                log("⚠️ 脚本里提到 @图片2/@图片3 但没有上传细节特写图，建议补传或删掉相关句子")

        log("提交生成任务 …")
        task_id = generate.create_task(model, prompt_text, img_url, duration,
                                       resolution, image_role,
                                       extra_image_urls=extra_urls)
        log(f"任务已提交：{task_id}，生成中（通常需要几分钟，请勿关闭本页）…")

        poll_url = f"{generate.PROVIDER['base']}/contents/generations/tasks/{task_id}"
        start = time.time()
        video_url = None
        while True:
            if time.time() - start > generate.POLL_TIMEOUT:
                raise RuntimeError("生成超时，请去平台控制台查看任务状态")
            resp = generate.http_json(poll_url, method="GET")
            status = resp.get("status")
            if status == "succeeded":
                video_url = (resp.get("content") or {}).get("video_url")
                if not video_url:
                    raise RuntimeError(f"任务成功但没有视频地址：{json.dumps(resp, ensure_ascii=False)[:300]}")
                break
            if status in ("failed", "cancelled", "expired"):
                raise RuntimeError(f"任务{status}：{json.dumps(resp.get('error') or resp, ensure_ascii=False)[:300]}")
            log(f"生成中……已等待 {int(time.time() - start)} 秒")
            time.sleep(generate.POLL_INTERVAL)

        stamp = time.strftime("%m%d_%H%M%S")
        raw = OUTPUT_DIR / f"网页版_raw_{stamp}.mp4"
        log("生成完成，下载视频 …")
        generate.download(video_url, raw)

        final = OUTPUT_DIR / f"网页版_{stamp}.mp4"
        if shutil.which("ffmpeg"):
            log("后处理（统一时长/编码，适配 TikTok 上传）…")
            generate.standardize(raw, final, duration)
        else:
            log("⚠️ 本机没有 ffmpeg，跳过后处理，直接使用原始视频")
            shutil.copyfile(raw, final)

        with JOB_LOCK:
            JOB["video"] = final.name
            JOB["state"] = "done"
        log(f"✅ 完成：{final.name}")
    except BaseException as e:  # 包括 generate.py 里的 sys.exit
        err = str(e) or traceback.format_exc(limit=1)
        with JOB_LOCK:
            JOB["state"] = "error"
        log(f"❌ 出错：{err}")


# ── 组合镜头：逐镜头生成 + 拼接 ──────────────────────────────────
def _gen_firstframe(engine: str, scene: str, refs: list[Path], out: Path,
                    ark_key: str, gemini_key: str, model: str) -> Path:
    """生成一张首帧图（seedream / nanobanana）。"""
    if engine == "nanobanana":
        gemini_api.nano_banana(refs, scene, gemini_key, out,
                               model=model or "gemini-3-pro-image")
    else:
        seedream_firstframe.generate_firstframe(refs, scene, ark_key, out,
                                                model=model or seedream_firstframe.DEFAULT_MODEL)
    return out


def _seedance_i2v(ff: Path, motion: str, notoken_key: str, duration: int,
                  resolution: str, out_raw: Path) -> Path:
    """Seedance（NoToken）首帧图生视频，下载到 out_raw。"""
    os.environ["NOTOKEN_API_KEY"] = notoken_key
    img_url = generate.media_url(ff)
    task_id = generate.create_task("seedance-2.0", motion, img_url, duration,
                                   resolution, "first_frame")
    poll_url = f"{generate.PROVIDER['base']}/contents/generations/tasks/{task_id}"
    start = time.time()
    while True:
        if time.time() - start > generate.POLL_TIMEOUT:
            raise RuntimeError("Seedance 生成超时")
        resp = generate.http_json(poll_url, method="GET")
        status = resp.get("status")
        if status == "succeeded":
            url = (resp.get("content") or {}).get("video_url")
            if not url:
                raise RuntimeError("任务成功但没有 video_url")
            return generate.download(url, out_raw)
        if status in ("failed", "cancelled", "expired"):
            raise RuntimeError(f"Seedance 任务{status}")
        log(f"    生成中……已等待 {int(time.time() - start)} 秒")
        time.sleep(generate.POLL_INTERVAL)


def _gen_video(engine: str, ff: Path, motion: str, out_raw: Path, duration: int,
               resolution: str, notoken_key: str, gemini_key: str, veo_model: str) -> Path:
    """首帧图 → 视频（seedance / veo）。"""
    if engine == "veo":
        gemini_api.veo_i2v(ff, motion, gemini_key, out_raw,
                           model=veo_model or "veo-3.1-generate-preview",
                           duration=duration, resolution=resolution, aspect="9:16", on_log=log)
    else:
        _seedance_i2v(ff, motion, notoken_key, duration, resolution, out_raw)
    return out_raw


def run_combo(combo: dict, car: str, refs: list[Path], img_engine: str, video_engine: str,
              ark_key: str, gemini_key: str, notoken_key: str, ff_model: str,
              veo_model: str, resolution: str) -> None:
    try:
        shots = combo["shots"]
        log(f"组合镜头「{combo['name']}」：共 {len(shots)} 个镜头，逐个生成再拼接")
        stamp = time.strftime("%m%d_%H%M%S")
        parts: list[Path] = []
        for i, shot in enumerate(shots, 1):
            scene = shot["scene"].replace("{车型}", car or "该车型")
            motion = shot["motion"].replace("{车型}", car or "该车型")
            dur = int(shot.get("duration", 4))
            log(f"── 镜头 {i}/{len(shots)}：{shot['name']} ──")
            log(f"  [1/2] 生成首帧图（{img_engine}）…")
            ff = OUTPUT_DIR / f"combo_{stamp}_ff{i}.png"
            _gen_firstframe(img_engine, scene, refs, ff, ark_key, gemini_key, ff_model)
            log(f"  [2/2] 生成视频（{video_engine}，{dur}s）…")
            raw = OUTPUT_DIR / f"combo_{stamp}_raw{i}.mp4"
            _gen_video(video_engine, ff, motion, raw, dur, resolution,
                       notoken_key, gemini_key, veo_model)
            std = OUTPUT_DIR / f"combo_{stamp}_shot{i}.mp4"
            if shutil.which("ffmpeg"):
                generate.standardize(raw, std, dur)
            else:
                shutil.copyfile(raw, std)
            parts.append(std)
        log("拼接所有镜头 → 成片 …")
        final = OUTPUT_DIR / f"组合视频_{stamp}.mp4"
        if len(parts) > 1 and shutil.which("ffmpeg"):
            generate.concat(parts, final)
        else:
            shutil.copyfile(parts[0], final)
        with JOB_LOCK:
            JOB["video"] = final.name
            JOB["state"] = "done"
        log(f"✅ 组合视频完成：{final.name}（{len(parts)} 个镜头拼接）")
    except BaseException as e:
        err = str(e) or traceback.format_exc(limit=1)
        with JOB_LOCK:
            JOB["state"] = "error"
        log(f"❌ 出错：{err}")


# ── multipart 表单解析（不依赖 cgi 模块：Python 3.13 已移除 cgi） ──
def parse_multipart(headers, rfile) -> tuple[dict, dict]:
    """返回 (fields, files)：fields[name]=str，files[name]=[(filename, bytes), …]"""
    ctype = headers.get("Content-Type", "")
    m = re.search(r'boundary="?([^";]+)"?', ctype)
    if not m:
        raise ValueError("请求格式错误（缺少 multipart boundary）")
    delim = b"--" + m.group(1).encode()
    body = rfile.read(int(headers.get("Content-Length", 0)))
    fields: dict = {}
    files: dict = {}
    for raw in body.split(delim)[1:]:
        if raw.startswith(b"--"):          # 结束标记
            break
        if raw.startswith(b"\r\n"):
            raw = raw[2:]
        head, sep, data = raw.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        hd = head.decode("utf-8", "replace")
        nm = re.search(r'name="([^"]*)"', hd)
        if not nm:
            continue
        fn = re.search(r'filename="([^"]*)"', hd)
        if fn and fn.group(1):
            files.setdefault(nm.group(1), []).append((fn.group(1), data))
        elif nm.group(1) not in fields:
            fields[nm.group(1)] = data.decode("utf-8", "replace")
    return fields, files


# ── HTTP 服务 ─────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # 静默访问日志
        pass

    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            page = PAGE.replace("__PROMPT__", html.escape(prompts.PROMPT))
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/config":
            self._json({
                "key": read_key(),
                "ark_key": read_ark_key(),
                "gemini_key": read_gemini_key(),
                "car_default": prompts.CAR_MODEL_DEFAULT,
                "firstframe_scene": prompts.FIRSTFRAME_SCENE,
                "scripts": prompts.SCRIPTS,
                "combos": [{"id": c["id"], "name": c["name"], "desc": c["desc"],
                            "shots": [s["name"] for s in c["shots"]],
                            "script_preview": "\n".join(
                                f"镜头{i}｜{s['name']}（{s.get('duration', 4)}s）：{s['motion']}"
                                for i, s in enumerate(c["shots"], 1))}
                           for c in prompts.COMBO_SCRIPTS],
                "seedream_models": SEEDREAM_MODELS,
                "nanobanana_models": NANOBANANA_MODELS,
                "veo_models": VEO_MODELS,
                "models": [{"id": k, "label": v["label"], "desc": v["desc"],
                            "tag": v["tag"], "rate": v["rate"]}
                           for k, v in MODELS.items()],
                "multiplier": GROUP_MULTIPLIER,
            })
        elif self.path == "/api/status":
            with JOB_LOCK:
                self._json({"state": JOB["state"], "log": JOB["log"][-100:],
                            "video": JOB["video"]})
        elif self.path.startswith("/video/"):
            raw = urllib.parse.unquote(self.path[len("/video/"):])
            try:  # http.server 按 latin-1 解码请求行，中文需还原
                raw = raw.encode("iso-8859-1").decode("utf-8")
            except (UnicodeDecodeError, UnicodeEncodeError):
                pass
            name = Path(raw).name  # 防目录穿越
            f = OUTPUT_DIR / name
            ctype = {".mp4": "video/mp4", ".png": "image/png",
                     ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(f.suffix.lower())
            if f.is_file() and ctype:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(f.stat().st_size))
                self.end_headers()
                with open(f, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile)
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/api/seedream":
            self.handle_seedream()
            return
        if self.path == "/api/gen_script":
            self.handle_gen_script()
            return
        if self.path == "/api/generate_combo":
            self.handle_combo()
            return
        if self.path != "/api/generate":
            self.send_error(404)
            return
        with JOB_LOCK:
            if JOB["state"] == "running":
                self._json({"ok": False, "error": "已有任务进行中，请等它完成"}, 409)
                return
            JOB.update({"state": "running", "log": [], "video": None,
                        "started": time.time()})
        try:
            fields, files = parse_multipart(self.headers, self.rfile)
            key = (fields.get("key") or "").strip()
            prompt_text = (fields.get("prompt") or "").strip()
            model = fields.get("model") or "seedance-2.0"
            resolution = fields.get("resolution") or "720p"
            duration = max(4, min(15, int(fields.get("duration") or 15)))
            image_role = "first_frame" if fields.get("mode") == "first_frame" else "reference_image"
            video_engine = fields.get("video_engine") or "seedance"
            gemini_key = (fields.get("gemini_key") or "").strip()
            veo_model = fields.get("veo_model") or "veo-3.1-generate-preview"

            if not prompt_text:
                raise ValueError("分镜脚本不能为空")
            if video_engine == "veo":
                if not gemini_key:
                    raise ValueError("Veo 需要 Google Gemini Key")
                image_role = "first_frame"  # Veo 只做图生视频，必须有首帧图
            elif not key.startswith("sk-"):
                raise ValueError("请先填写有效的 NoToken 令牌（sk- 开头）")

            stamp = time.strftime("%m%d_%H%M%S")

            def save_upload(item: tuple, tag: str) -> Path:
                filename, data = item
                suffix = Path(filename).suffix.lower() or ".png"
                dest = INPUT_DIR / f"网页版_{stamp}_{tag}{suffix}"
                dest.write_bytes(data)
                return dest

            # 首帧模式且指定了「刚用 Seedream 生成的首帧图」：直接用 output 里那张，不用重传
            ff_name = (fields.get("firstframe_file") or "").strip()
            if image_role == "first_frame" and ff_name:
                ff = OUTPUT_DIR / Path(ff_name).name
                if not ff.is_file():
                    raise ValueError("找不到刚生成的首帧图，请重新生成")
                image = ff
            else:
                main_imgs = files.get("image") or []
                if not main_imgs:
                    raise ValueError("请上传产品主图（或首帧图）")
                image = save_upload(main_imgs[0], "首帧" if image_role == "first_frame" else "主图")

            extras: list[Path] = []
            for i, it in enumerate(files.get("extras") or [], 2):
                extras.append(save_upload(it, f"特写{i}"))
        except BaseException as e:
            with JOB_LOCK:
                JOB["state"] = "error"
            log(f"❌ {e}")
            self._json({"ok": False, "error": str(e)}, 400)
            return

        threading.Thread(target=run_job, daemon=True,
                         args=(key, image, extras, prompt_text,
                               model, resolution, duration, image_role,
                               video_engine, gemini_key, veo_model)).start()
        self._json({"ok": True})

    def handle_combo(self) -> None:
        """组合镜头：逐镜头生成 + 拼接（后台任务）。"""
        with JOB_LOCK:
            if JOB["state"] == "running":
                self._json({"ok": False, "error": "已有任务进行中，请等它完成"}, 409)
                return
            JOB.update({"state": "running", "log": [], "video": None, "started": time.time()})
        try:
            fields, files = parse_multipart(self.headers, self.rfile)
            combo_id = fields.get("combo_id") or ""
            combo = next((c for c in prompts.COMBO_SCRIPTS if c["id"] == combo_id), None)
            if not combo:
                raise ValueError("找不到该组合镜头模板")
            car = (fields.get("car_model") or "").strip() or prompts.CAR_MODEL_DEFAULT
            img_engine = fields.get("img_engine") or "seedream"
            video_engine = fields.get("video_engine") or "seedance"
            ark_key = (fields.get("ark_key") or "").strip()
            gemini_key = (fields.get("gemini_key") or "").strip()
            notoken_key = (fields.get("key") or "").strip()
            ff_model = fields.get("ff_model") or ""
            veo_model = fields.get("veo_model") or "veo-3.1-generate-preview"
            resolution = fields.get("resolution") or "720p"

            if img_engine == "nanobanana" or video_engine == "veo":
                if not gemini_key:
                    raise ValueError("用 Nano Banana / Veo 需要 Google Gemini Key")
                save_gemini_key(gemini_key)
            if img_engine == "seedream" and not ark_key.startswith("ark-"):
                raise ValueError("Seedream 生图需要火山方舟 Key（ark- 开头）")
            if video_engine == "seedance" and not notoken_key.startswith("sk-"):
                raise ValueError("Seedance 生视频需要 NoToken 令牌（sk- 开头）")
            if ark_key:
                save_ark_key(ark_key)
            if notoken_key:
                save_key(notoken_key)

            main_imgs = files.get("image") or []
            if not main_imgs:
                raise ValueError("请上传产品图")
            stamp = time.strftime("%m%d_%H%M%S")
            refs = []
            for i, it in enumerate(main_imgs, 1):
                fn, data = it
                dest = INPUT_DIR / f"combo参考_{stamp}_{i}{Path(fn).suffix.lower() or '.jpg'}"
                dest.write_bytes(data)
                refs.append(dest)
        except BaseException as e:
            with JOB_LOCK:
                JOB["state"] = "error"
            log(f"❌ {e}")
            self._json({"ok": False, "error": str(e)}, 400)
            return

        threading.Thread(target=run_combo, daemon=True,
                         args=(combo, car, refs, img_engine, video_engine, ark_key,
                               gemini_key, notoken_key, ff_model, veo_model, resolution)).start()
        self._json({"ok": True})

    def handle_gen_script(self) -> None:
        """用 Gemini 文本模型根据车型+卖点生成一段视频脚本，同步返回。"""
        try:
            fields, _ = parse_multipart(self.headers, self.rfile)
            gk = (fields.get("gemini_key") or "").strip()
            car = (fields.get("car_model") or "").strip() or prompts.CAR_MODEL_DEFAULT
            selling = (fields.get("selling") or "").strip()
            if not gk:
                raise ValueError("请先填写 Google Gemini Key（第 5 或 6 步）")
            if not selling:
                raise ValueError("请先填写产品卖点")
            save_gemini_key(gk)
            instr = prompts.SCRIPT_GEN_INSTRUCTION.replace("{车型}", car).replace("{卖点}", selling)
            print(f"[AI脚本] 生成中（卖点：{selling[:20]}…）", flush=True)
            script = gemini_api.gen_text(instr, gk)
            self._json({"ok": True, "script": script})
        except BaseException as e:
            self._json({"ok": False, "error": str(e)}, 400)

    def handle_seedream(self) -> None:
        """生成一张首帧图（引擎：seedream=火山方舟 / nanobanana=Google），同步返回文件名。"""
        try:
            fields, files = parse_multipart(self.headers, self.rfile)
            engine = fields.get("engine") or "seedream"
            prompt_text = (fields.get("prompt") or FIRSTFRAME_PROMPT).strip()
            model = fields.get("model") or ""
            imgs = files.get("image") or []
            if not imgs:
                raise ValueError("请至少上传一张产品参考图")

            stamp = time.strftime("%m%d_%H%M%S")
            ref_paths = []
            for i, it in enumerate(imgs, 1):
                fn, data = it
                dest = INPUT_DIR / f"参考_{stamp}_{i}{Path(fn).suffix.lower() or '.jpg'}"
                dest.write_bytes(data)
                ref_paths.append(dest)
            out = OUTPUT_DIR / f"首帧_网页版_{stamp}.png"

            if engine == "nanobanana":
                gk = (fields.get("gemini_key") or "").strip()
                if not gk:
                    raise ValueError("请先填写 Google Gemini Key")
                save_gemini_key(gk)
                model = model or "gemini-3-pro-image"
                print(f"[Nano Banana] 生成中（{model}，参考图 {len(ref_paths)} 张）…", flush=True)
                gemini_api.nano_banana(ref_paths, prompt_text, gk, out, model=model)
            else:
                ark = (fields.get("ark_key") or "").strip()
                if not ark.startswith("ark-"):
                    raise ValueError("请先填写有效的火山方舟 Key（ark- 开头）")
                save_ark_key(ark)
                model = model or seedream_firstframe.DEFAULT_MODEL
                print(f"[Seedream] 生成中（{model}，参考图 {len(ref_paths)} 张）…", flush=True)
                seedream_firstframe.generate_firstframe(ref_paths, prompt_text, ark, out, model=model)
            print(f"[首帧] 完成：{out.name}", flush=True)
            self._json({"ok": True, "file": out.name})
        except BaseException as e:
            self._json({"ok": False, "error": str(e)}, 400)


PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>视频工厂 · 产品图一键生成带货视频</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0a0c10; --card: #12151d; --card2: #0d1017;
    --line: rgba(255,255,255,.08); --line2: rgba(255,255,255,.14);
    --txt: #eef1f6; --muted: #8b93a5; --dim: #5c6475;
    --red: #fe2c55; --cyan: #25f4ee; --green: #2fd47f;
  }
  html { -webkit-text-size-adjust: 100%; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: var(--bg); color: var(--txt); line-height: 1.65; min-height: 100vh;
  }
  body::before {
    content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 0;
    background:
      radial-gradient(520px 320px at 12% -6%, rgba(254,44,85,.14), transparent 65%),
      radial-gradient(520px 320px at 92% 4%, rgba(37,244,238,.10), transparent 65%);
  }
  .wrap { position: relative; z-index: 1; max-width: min(1900px, 96vw); margin: 0 auto; padding: 36px 28px 90px; }
  /* 创作工作台：左=准备素材，右=生成台 */
  .ws { display: flex; gap: 22px; align-items: flex-start; }
  .ws-left  { flex: 1 1 40%; min-width: 320px; display: flex; flex-direction: column; gap: 14px; }
  .ws-right { flex: 1 1 60%; min-width: 340px; position: sticky; top: 18px; display: flex; flex-direction: column; gap: 12px; }
  .ws .card { margin-bottom: 0; }
  .panel-title { font-size: 12px; font-weight: 800; letter-spacing: 3px; color: var(--muted);
                 text-transform: uppercase; margin: 2px 6px; display: flex; align-items: center; gap: 8px; }
  .panel-title::after { content: ""; flex: 1; height: 1px; background: var(--line); }
  .flowarrow { text-align: center; font-size: 12px; color: var(--dim); font-weight: 700; letter-spacing: 1px; }
  .slot { border: 1.5px dashed var(--line2); border-radius: 14px; background: var(--card2);
          min-height: 120px; display: flex; flex-direction: column; align-items: center; justify-content: center;
          text-align: center; padding: 16px; margin: 12px 0; }
  .slot .ph { font-size: 12.5px; color: var(--dim); }
  @media (max-width: 920px) {
    .ws { flex-direction: column; }
    .ws-left, .ws-right { flex: 1 1 100%; min-width: 0; position: static; }
    .wrap { max-width: 680px; }
  }

  header { text-align: center; margin-bottom: 34px; }
  .logo {
    display: inline-flex; align-items: center; justify-content: center;
    width: 58px; height: 58px; border-radius: 17px; font-size: 28px; margin-bottom: 14px;
    background: linear-gradient(135deg, rgba(254,44,85,.22), rgba(37,244,238,.14));
    border: 1px solid var(--line2);
    box-shadow: 0 8px 32px rgba(254,44,85,.18);
  }
  h1 { font-size: 26px; font-weight: 800; letter-spacing: .5px; }
  h1 .grad {
    background: linear-gradient(92deg, #fff 15%, #ffb3c3 55%, #7df5f1);
    -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
  }
  .sub { color: var(--muted); font-size: 13.5px; margin-top: 6px; }

  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 18px;
    padding: 22px 22px 20px; margin-bottom: 16px;
    box-shadow: 0 1px 0 rgba(255,255,255,.03) inset, 0 10px 30px rgba(0,0,0,.25);
  }
  .step { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
  .num {
    width: 24px; height: 24px; border-radius: 8px; flex: none;
    display: flex; align-items: center; justify-content: center;
    font-size: 12.5px; font-weight: 800; color: #fff;
    background: linear-gradient(135deg, var(--red), #ff6b4a);
    box-shadow: 0 3px 10px rgba(254,44,85,.35);
  }
  .step h2 { font-size: 15.5px; font-weight: 700; }
  .step .opt { color: var(--dim); font-size: 12px; font-weight: 400; margin-left: 2px; }

  label { display: block; font-size: 12.5px; color: var(--muted); margin: 12px 0 6px; }
  input[type=password], input[type=text], textarea {
    width: 100%; padding: 11px 13px; font-size: 14px; color: var(--txt);
    background: var(--card2); border: 1px solid var(--line2); border-radius: 11px;
    outline: none; transition: border-color .15s, box-shadow .15s;
  }
  input:focus, textarea:focus {
    border-color: rgba(37,244,238,.6); box-shadow: 0 0 0 3px rgba(37,244,238,.12);
  }
  .hint { font-size: 12px; color: var(--dim); margin-top: 8px; }
  .hint b { color: var(--muted); font-weight: 600; }

  .drop {
    border: 1.5px dashed var(--line2); border-radius: 13px; background: var(--card2);
    padding: 18px 14px; text-align: center; cursor: pointer;
    transition: border-color .15s, background .15s;
  }
  .drop:hover, .drop.over { border-color: rgba(37,244,238,.55); background: rgba(37,244,238,.04); }
  .drop .ico { font-size: 22px; display: block; margin-bottom: 4px; }
  .drop .t { font-size: 13.5px; color: var(--txt); font-weight: 600; }
  .drop .d { font-size: 12px; color: var(--dim); margin-top: 2px; }
  .thumbs { display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; margin-top: 10px; }
  .thumbs img {
    width: 64px; height: 64px; object-fit: cover; border-radius: 10px;
    border: 1px solid var(--line2); box-shadow: 0 4px 12px rgba(0,0,0,.4);
  }
  input[type=file] { display: none; }

  textarea#prompt {
    min-height: 250px; font-size: 13px; line-height: 1.7; resize: vertical;
    font-family: inherit; color: #cdd4e0;
  }

  .grid { display: flex; gap: 14px; flex-wrap: wrap; }
  .grid > div { flex: 1; min-width: 180px; }
  .models { display: flex; flex-flow: row wrap; gap: 8px; }
  .mcard {
    flex: 1 1 130px; min-width: 130px;
    display: flex; align-items: center; gap: 10px; padding: 11px 13px;
    background: var(--card2); border: 1.5px solid var(--line2); border-radius: 12px;
    cursor: pointer; transition: border-color .15s, background .15s;
  }
  .mcard.sel { border-color: var(--red); background: rgba(254,44,85,.07); }
  .mcard .radio {
    width: 15px; height: 15px; border-radius: 50%; flex: none;
    border: 2px solid var(--dim); transition: all .15s;
  }
  .mcard.sel .radio { border-color: var(--red); background: var(--red); box-shadow: 0 0 0 3px rgba(254,44,85,.25); }
  .mcard .mn { font-size: 13.5px; font-weight: 700; }
  .mcard .md { font-size: 11.5px; color: var(--dim); }
  .badge {
    margin-left: auto; flex: none; font-size: 10.5px; font-weight: 700; padding: 2px 8px;
    border-radius: 20px; letter-spacing: .5px;
  }
  .badge.rec { background: rgba(47,212,127,.15); color: var(--green); }
  .badge.warn { background: rgba(255,180,60,.13); color: #ffb43c; }

  .pills { display: flex; gap: 8px; flex-wrap: wrap; }
  .pill {
    padding: 8px 15px; font-size: 13px; font-weight: 600; color: var(--muted);
    background: var(--card2); border: 1.5px solid var(--line2); border-radius: 22px;
    cursor: pointer; transition: all .15s; user-select: none;
  }
  .pill.sel { color: #041418; background: var(--cyan); border-color: var(--cyan);
              box-shadow: 0 3px 14px rgba(37,244,238,.3); }
  .pill small { font-weight: 400; opacity: .75; }
  .pill.sel small { opacity: .85; }

  .est {
    margin-top: 16px; padding: 10px 14px; border-radius: 11px; font-size: 13px;
    background: rgba(37,244,238,.06); border: 1px solid rgba(37,244,238,.18);
    color: #9ef3ef; display: flex; align-items: center; gap: 8px;
  }
  .est b { font-size: 15px; }

  #go {
    width: 100%; padding: 16px; margin-top: 6px; font-size: 16.5px; font-weight: 800;
    color: #fff; border: 0; border-radius: 14px; cursor: pointer; letter-spacing: 1px;
    background: linear-gradient(94deg, var(--red), #ff5f3d);
    box-shadow: 0 10px 30px rgba(254,44,85,.35);
    transition: transform .12s, box-shadow .12s, opacity .15s;
  }
  #go:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 14px 36px rgba(254,44,85,.45); }
  #go:active:not(:disabled) { transform: translateY(0); }
  #go:disabled { opacity: .55; cursor: not-allowed; }

  #progress { display: none; }
  .stateline { display: flex; align-items: center; gap: 10px; font-size: 14px; font-weight: 600; margin-bottom: 12px; }
  .spin {
    width: 17px; height: 17px; border-radius: 50%; flex: none;
    border: 2.5px solid rgba(255,255,255,.15); border-top-color: var(--cyan);
    animation: rot .9s linear infinite;
  }
  @keyframes rot { to { transform: rotate(360deg); } }
  #log {
    font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11.5px;
    color: #9fb0c8; white-space: pre-wrap; word-break: break-all;
    max-height: 230px; overflow-y: auto; background: #080a0e;
    border: 1px solid var(--line); border-radius: 11px; padding: 12px 14px;
  }
  #result { display: none; text-align: center; margin-top: 18px; }
  .phone {
    display: inline-block; padding: 10px; border-radius: 30px;
    background: linear-gradient(160deg, #1a1e28, #0c0e14);
    border: 1px solid var(--line2); box-shadow: 0 20px 50px rgba(0,0,0,.5), 0 0 60px rgba(37,244,238,.07);
  }
  .phone video { display: block; width: 250px; border-radius: 22px; }
  #dlink {
    display: inline-block; margin-top: 16px; padding: 12px 34px; font-size: 14.5px; font-weight: 700;
    color: #04180e; text-decoration: none; border-radius: 12px;
    background: linear-gradient(94deg, var(--green), #4ce3a0);
    box-shadow: 0 8px 24px rgba(47,212,127,.3);
    transition: transform .12s;
  }
  #dlink:hover { transform: translateY(-1px); }
  footer { text-align: center; color: var(--dim); font-size: 11.5px; margin-top: 26px; }
</style>
</head>
<body>
<div class="wrap">

<header>
  <div class="logo">🎬</div>
  <h1><span class="grad">视频工厂</span></h1>
  <div class="sub">车型 + 产品图 → 首帧图 → 15 秒 TikTok 带货视频</div>
</header>

<details class="card" style="margin-bottom:16px">
  <summary style="cursor:pointer; font-size:15px; font-weight:700">📖 使用说明 &amp; 如何开通 API Key（点开）</summary>
  <div style="margin-top:14px; font-size:13px; line-height:1.8; color:var(--muted)">
    <div style="font-size:14px; font-weight:700; color:var(--txt); margin-bottom:4px">怎么用（5 步）</div>
    1. 顶部填好 API Key（用哪个引擎就填哪个，填一次本机保存）<br>
    2. 填车型、传产品图（实拍多角度锁花纹更准）<br>
    3. 选脚本 —— 默认「🎬 组合镜头」＝复刻爆款带货片（举垫→倒咖啡防水演示→擦净→点赞）<br>
    4. 在「生成台」选生图引擎（Seedream / Nano Banana）和视频引擎（Seedance / Veo）<br>
    5. 点「🎬 一键生成组合视频」自动逐镜头生成并拼接；或手动 ⑤生成首帧图 → ⑥生成视频 出单条<br>
    <div style="font-size:14px; font-weight:700; color:var(--txt); margin:14px 0 4px">三个 Key 怎么开通</div>
    <b style="color:var(--txt)">① NoToken 令牌</b>（生视频 Seedance）：注册 <a href="https://notoken.pro/register?aff=ZN6z" target="_blank">notoken.pro</a> → 钱包管理充值 → 令牌管理 → 添加令牌，<b style="color:#ffb43c">分组必须选「SeeDance满血」</b> → 复制 <span style="font-family:monospace">sk-</span> 开头的令牌<br>
    <b style="color:var(--txt)">② 火山方舟 Key</b>（生图 Seedream）：登录 <a href="https://console.volcengine.com/ark" target="_blank">火山方舟控制台</a>（需实名）→「开通管理」开通 Doubao-Seedream → 「API Key 管理」创建 → 复制 <span style="font-family:monospace">ark-</span> 开头<br>
    <b style="color:var(--txt)">③ Google Gemini Key</b>（Nano Banana / Veo / AI 写脚本）：打开 <a href="https://aistudio.google.com/api-keys" target="_blank">aistudio.google.com/api-keys</a> → Create API key → 复制 <span style="font-family:monospace">AIza</span> 开头<br>
    <div style="margin-top:10px; color:var(--dim)">提示：只用国内引擎就填 NoToken + 火山方舟；只用 Google 引擎就填 Gemini。三个可以只填要用的。</div>
  </div>
</details>

<div class="card" style="margin-bottom:16px; border-color:var(--line2)">
  <div style="font-size:15.5px; font-weight:700; margin-bottom:2px">🔑 API 配置</div>
  <div class="hint" style="margin-bottom:12px">三个密钥各填一次即可，只保存在你自己电脑上。用哪个引擎就填哪个：国内引擎用 NoToken + 火山方舟；Google 引擎（Nano Banana / Veo / AI 写脚本）用 Gemini。</div>
  <div class="grid" style="align-items:flex-start">
    <div>
      <label>NoToken 令牌 <span style="color:var(--dim)">（Seedance 生视频）</span></label>
      <input type="password" id="key" placeholder="sk-xxxxxxxx" autocomplete="off">
      <div class="hint" style="margin:6px 0 0">notoken.pro 控制台 → 令牌管理</div>
    </div>
    <div>
      <label>火山方舟 Key <span style="color:var(--dim)">（Seedream 生图）</span></label>
      <input type="password" id="arkKey" placeholder="ark-xxxxxxxx" autocomplete="off">
      <div class="hint" style="margin:6px 0 0">console.volcengine.com/ark → API Key 管理</div>
    </div>
    <div>
      <label>Google Gemini Key <span style="color:var(--dim)">（Nano Banana / Veo / AI 脚本）</span></label>
      <input type="password" id="geminiKey" placeholder="AIza..." autocomplete="off">
      <div class="hint" style="margin:6px 0 0">aistudio.google.com/api-keys</div>
    </div>
  </div>
</div>

<div class="ws">

<div class="ws-left">
<div class="panel-title">🎬 准备素材</div>

<div class="card">
  <div class="step"><div class="num">1</div><h2>车型</h2><span class="opt">产品适配的车型</span></div>
  <input type="text" id="carModel" placeholder="例：特斯拉 Model Y 2025-2026">
  <div class="hint">填清楚车型，AI 会把它用进画面和口播（例如 "…for your Tesla Model Y!"）。</div>
</div>

<div class="card">
  <div class="step"><div class="num">2</div><h2>产品图</h2><span class="opt">实拍多角度最佳</span></div>
  <div style="display:flex; gap:12px; flex-wrap:wrap">
    <div class="drop" id="dropMain" style="flex:1 1 180px">
      <span class="ico">🖼️</span>
      <span class="t">产品主图</span>
      <div class="d">实拍/白底 · 必传</div>
      <div class="thumbs" id="prevMain"></div>
    </div>
    <div class="drop" id="dropExtra" style="flex:1 1 180px">
      <span class="ico">🔍</span>
      <span class="t">更多角度 / 细节图</span>
      <div class="d">选传 · 锁花纹更准</div>
      <div class="thumbs" id="prevExtra"></div>
    </div>
  </div>
  <input type="file" id="image" accept="image/*">
  <input type="file" id="extras" accept="image/*" multiple>
</div>

<div class="card">
  <div class="step"><div class="num">3</div><h2>选择脚本</h2><span class="opt">挑一个模板，或让 AI 按卖点写</span></div>
  <div class="models" id="scriptPick"></div>
  <div style="margin-top:14px; padding-top:14px; border-top:1px solid var(--line)">
    <div style="font-size:13.5px; font-weight:700; margin-bottom:2px">✨ 让 AI 按卖点写脚本 <span style="color:var(--dim); font-weight:400">（用顶部的 Gemini Key）</span></div>
    <label>产品卖点</label>
    <input type="text" id="selling" placeholder="填产品卖点，如：立体高边防水、加厚耐磨、易清洁">
    <button id="aiScript" style="margin-top:10px; background:linear-gradient(94deg,#8b5cf6,#25a0d4); box-shadow:0 8px 24px rgba(139,92,246,.3)">✨ AI 生成脚本</button>
    <div id="aiScriptProg" style="display:none; font-size:12.5px; color:var(--muted); margin-top:8px"></div>
  </div>
</div>

<div class="card">
  <div class="step"><div class="num">4</div><h2>确认脚本</h2><span class="opt">人物动作 + 口播台词，可直接改</span></div>
  <textarea id="prompt" spellcheck="false"></textarea>
  <div class="hint">引号里是口播台词，改成你产品的卖点即可。产品全程保持稳定、不变形。</div>
</div>

</div>

<div class="ws-right">
<div class="panel-title">✨ 生成台</div>

<div class="card" id="comboBox" style="display:none; border-color:rgba(139,92,246,.4)">
  <div style="font-size:14px; font-weight:700; margin-bottom:4px">🎬 组合镜头 · 一键出片</div>
  <div class="hint" style="margin-bottom:10px">当前选的是「组合镜头」。点下面按钮，工具会自动为每个镜头生成首帧图+视频再拼成一条完整带货片（引擎沿用下面 ⑤⑥ 的选择）。</div>
  <button id="comboGo" style="background:linear-gradient(94deg,#8b5cf6,#c026d3); box-shadow:0 10px 28px rgba(139,92,246,.35)">🎬 一键生成组合视频</button>
  <div class="hint" style="text-align:center; margin-top:8px">或者用下面的 ⑤ → ⑥ 手动逐步生成单条</div>
</div>

<div class="card">
  <div class="step"><div class="num">5</div><h2>生成首帧图</h2><span class="opt">把产品画准</span></div>
  <label>生图引擎 <span style="color:var(--dim)">（Key 在顶部填）</span></label>
  <div class="pills" id="ffEngine" style="margin-bottom:10px">
    <div class="pill sel" data-v="seedream">Seedream <small>火山方舟</small></div>
    <div class="pill" data-v="nanobanana">Nano Banana <small>Google</small></div>
  </div>
  <label>模型</label>
  <select id="ffModel"></select>
  <details style="margin-top:12px">
    <summary style="cursor:pointer; font-size:12.5px; color:var(--muted)">高级：首帧画面描述（一般不用改）</summary>
    <textarea id="ffPrompt" spellcheck="false" style="min-height:120px; margin-top:8px"></textarea>
  </details>
  <button id="ffGo" style="margin-top:12px">🎨 生成首帧图</button>
  <div id="ffProg" style="display:none; font-size:12.5px; color:var(--muted); margin-top:10px"></div>
  <div id="ffPrev" style="display:none; text-align:center; margin-top:12px">
    <img id="ffImg" style="max-width:220px; border-radius:12px; border:1px solid var(--line2)">
    <div class="hint" style="color:var(--green); margin-top:6px">✅ 首帧图已生成，去第 6 步生成视频（会用这张图）</div>
  </div>
</div>

<div class="card">
  <div class="step"><div class="num">6</div><h2>生成视频</h2><span class="opt">首帧动起来</span></div>
  <label>视频引擎 <span style="color:var(--dim)">（Key 在顶部填）</span></label>
  <div class="pills" id="vEngine" style="margin-bottom:10px">
    <div class="pill sel" data-v="seedance">Seedance <small>NoToken</small></div>
    <div class="pill" data-v="veo">Veo 3.1 <small>Google</small></div>
  </div>
  <label>模型</label>
  <div class="models" id="models"></div>
  <select id="veoModel" style="display:none"></select>
  <div class="grid" style="margin-top:10px">
    <div>
      <label>分辨率</label>
      <div class="pills" id="resolution">
        <div class="pill sel" data-v="720p">720p <small>推荐</small></div>
        <div class="pill" data-v="1080p">1080p <small>更清晰</small></div>
      </div>
    </div>
    <div>
      <label>时长</label>
      <div class="pills" id="duration"></div>
    </div>
  </div>
  <div class="est">💰 预估 <b id="estv">--</b> <span id="estUnit">创作点</span><span style="opacity:.6" id="estNote">（1 点 ≈ 1 元）</span></div>
  <button id="go" style="margin-top:14px">🚀 生成视频</button>
</div>

<div class="card" id="progress">
  <div class="stateline" id="stateline"><div class="spin"></div><span id="statetxt">正在生成…</span></div>
  <div id="log"></div>
  <div id="result">
    <div class="phone"><video id="player" controls></video></div>
    <br><a id="dlink" download>⬇️ 下载视频</a>
  </div>
</div>

</div>
</div>

<footer>本工具只在你自己的电脑上运行 · 生成期间请勿关闭启动窗口</footer>
</div>

<script>
const $ = id => document.getElementById(id);
let rates = {}, mult = 1, model = 'seedance-2.0';
let scenetpl = '', selTpl = '', generatedFF = null, comboId = null, comboPreview = '';
const comboHeader = '【组合镜头】按下面顺序，每个镜头自动生成首帧图+视频再拼成一条：\\n\\n';
let ffModels = { seedream: [], nanobanana: [] }, veoModels = [];
const carVal = () => ($('carModel').value.trim() || '该车型');
const sub = t => (t || '').replace(/\\{车型\\}/g, carVal());

function fillFfModels(engine) {
  $('ffModel').innerHTML = (ffModels[engine] || []).map(m => `<option value="${m.id}">${m.label}</option>`).join('');
}
function buildDuration(engine) {
  const opts = engine === 'veo' ? [4, 6, 8] : [5, 8, 10];
  $('duration').innerHTML = opts.map((d, i) => `<div class="pill${i === 0 ? ' sel' : ''}" data-v="${d}">${d} 秒</div>`).join('');
}

// ── 初始化配置 ──
fetch('/api/config').then(r => r.json()).then(c => {
  $('key').value = c.key || '';
  $('arkKey').value = c.ark_key || '';
  $('geminiKey').value = c.gemini_key || '';
  $('carModel').value = c.car_default || '';
  scenetpl = c.firstframe_scene || '';
  mult = c.multiplier;
  ffModels.seedream = c.seedream_models || [];
  ffModels.nanobanana = c.nanobanana_models || [];
  veoModels = c.veo_models || [];
  fillFfModels('seedream');
  $('veoModel').innerHTML = veoModels.map(m => `<option value="${m.id}">${m.label}</option>`).join('');
  // 第3步：组合镜头模板（多镜头·置顶，紫色标记）
  (c.combos || []).forEach(cb => {
    const d = document.createElement('div');
    d.className = 'mcard';
    d.style.cssText = 'flex:1 1 100%; border-color:rgba(139,92,246,.55)';
    d.innerHTML = `<div class="radio"></div><div><div class="mn">🎬 ${cb.name}</div>`
      + `<div class="md">${cb.shots.join(' → ')}</div></div>`
      + `<span class="badge" style="margin-left:auto;background:rgba(139,92,246,.18);color:#b79dff">组合</span>`;
    d.onclick = () => {
      comboId = cb.id; comboPreview = cb.script_preview || '';
      document.querySelectorAll('#scriptPick .mcard').forEach(x => x.classList.remove('sel'));
      d.classList.add('sel');
      $('prompt').value = comboHeader + sub(comboPreview);
      setComboMode(true);
    };
    $('scriptPick').appendChild(d);
  });
  // 第3步：单镜头脚本卡片
  (c.scripts || []).forEach((s, i) => {
    const d = document.createElement('div');
    d.className = 'mcard';
    d.innerHTML = `<div class="radio"></div><div><div class="mn">${s.name}</div><div class="md">${s.desc}</div></div>`;
    d.onclick = () => {
      comboId = null; selTpl = s.script;
      document.querySelectorAll('#scriptPick .mcard').forEach(x => x.classList.remove('sel'));
      d.classList.add('sel');
      $('prompt').value = sub(selTpl);
      setComboMode(false);
    };
    $('scriptPick').appendChild(d);
  });
  if (c.combos && c.combos.length) {
    document.querySelector('#scriptPick .mcard').click();  // 默认选中「组合镜头」
  } else if (c.scripts && c.scripts[0]) {
    selTpl = c.scripts[0].script; $('prompt').value = sub(selTpl);
  }
  $('ffPrompt').value = sub(scenetpl);
  // 第6步：Seedance 模型卡片
  c.models.forEach((m, i) => {
    rates[m.id] = m.rate;
    const d = document.createElement('div');
    d.className = 'mcard' + (i === 0 ? ' sel' : '');
    let badge = '';
    if (m.tag === '推荐') badge = '<span class="badge rec">推荐</span>';
    if (m.tag === '慎用') badge = '<span class="badge warn">慎用</span>';
    d.innerHTML = `<div class="radio"></div><div><div class="mn">${m.label}</div><div class="md">${m.desc}</div></div>${badge}`;
    d.onclick = () => {
      model = m.id;
      document.querySelectorAll('#models .mcard').forEach(x => x.classList.remove('sel'));
      d.classList.add('sel');
      updateEst();
    };
    $('models').appendChild(d);
  });
  buildDuration('seedance');
  updateEst();
});

// 车型变化 → 重建首帧描述 + 当前脚本
$('carModel').addEventListener('change', () => {
  $('ffPrompt').value = sub(scenetpl);
  if (comboId) $('prompt').value = comboHeader + sub(comboPreview);
  else if (selTpl) $('prompt').value = sub(selTpl);
});
// Gemini Key（顶部统一填一个，Nano Banana / Veo / AI 脚本共用）
const gemVal = () => $('geminiKey').value.trim();
$('veoModel').addEventListener('change', updateEst);

// ── AI 生成脚本（Gemini 文本）──
$('aiScript').onclick = async () => {
  const gk = gemVal();
  if (!gk) { alert('请先填 Google Gemini Key'); return; }
  if (!$('selling').value.trim()) { alert('请先填产品卖点'); return; }
  const fd = new FormData();
  fd.append('gemini_key', gk);
  fd.append('car_model', $('carModel').value.trim());
  fd.append('selling', $('selling').value.trim());
  $('aiScript').disabled = true; $('aiScript').textContent = '⏳ 生成中…';
  $('aiScriptProg').style.display = 'block'; $('aiScriptProg').textContent = 'AI 正在按卖点写脚本…';
  try {
    const j = await (await fetch('/api/gen_script', { method: 'POST', body: fd })).json();
    if (!j.ok) { $('aiScriptProg').textContent = '❌ ' + j.error; return; }
    selTpl = j.script;
    $('prompt').value = j.script;
    document.querySelectorAll('#scriptPick .mcard').forEach(x => x.classList.remove('sel'));
    $('aiScriptProg').style.display = 'none';
  } catch (err) { $('aiScriptProg').textContent = '❌ ' + err; }
  finally { $('aiScript').disabled = false; $('aiScript').textContent = '✨ AI 生成脚本'; }
};

// ── 生图引擎切换（Seedream / Nano Banana）──
$('ffEngine').addEventListener('click', e => {
  const p = e.target.closest('.pill'); if (!p) return;
  document.querySelectorAll('#ffEngine .pill').forEach(x => x.classList.remove('sel'));
  p.classList.add('sel');
  fillFfModels(p.dataset.v);
});
const ffEngineVal = () => document.querySelector('#ffEngine .sel').dataset.v;

// ── 视频引擎切换（Seedance / Veo）──
$('vEngine').addEventListener('click', e => {
  const p = e.target.closest('.pill'); if (!p) return;
  document.querySelectorAll('#vEngine .pill').forEach(x => x.classList.remove('sel'));
  p.classList.add('sel');
  const veo = p.dataset.v === 'veo';
  $('models').style.display = veo ? 'none' : 'flex';
  $('veoModel').style.display = veo ? 'block' : 'none';
  buildDuration(veo ? 'veo' : 'seedance');
  updateEst();
});
const vEngineVal = () => document.querySelector('#vEngine .sel').dataset.v;

// ── 分段选择器（事件委托，时长选项会动态重建）──
function pillVal(id) { return document.querySelector('#' + id + ' .sel').dataset.v; }
['resolution', 'duration'].forEach(id => {
  $(id).addEventListener('click', e => {
    const p = e.target.closest('.pill');
    if (!p) return;
    document.querySelectorAll('#' + id + ' .pill').forEach(x => x.classList.remove('sel'));
    p.classList.add('sel');
    updateEst();
  });
});

function updateEst() {
  const res = pillVal('resolution'), dur = +pillVal('duration');
  if (vEngineVal() === 'veo') {
    const m = veoModels.find(x => x.id === $('veoModel').value) || veoModels[0] || { usd_per_sec: 0.4 };
    let rate = m.usd_per_sec;
    if (res !== '720p') rate = m.id.includes('fast') ? 0.12 : (m.id.includes('lite') ? 0.08 : rate);
    $('estv').textContent = '$' + (rate * dur).toFixed(2);
    $('estUnit').textContent = '';
    $('estNote').textContent = '（美元 · 成功才扣费）';
  } else {
    const [w, h] = res === '720p' ? [720, 1280] : [1080, 1920];
    $('estv').textContent = (w * h * 24 * dur / 1024 / 1e6 * (rates[model] || 46) * mult).toFixed(1);
    $('estUnit').textContent = '创作点';
    $('estNote').textContent = '（1 点 ≈ 1 元）';
  }
}

// ── 上传区（点击 + 拖拽 + 预览） ──
function wireDrop(dropId, inputId, prevId, multiple) {
  const drop = $(dropId), input = $(inputId), prev = $(prevId);
  drop.onclick = () => input.click();
  drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = e => {
    e.preventDefault(); drop.classList.remove('over');
    input.files = e.dataTransfer.files;
    showPrev();
  };
  input.onchange = showPrev;
  function showPrev() {
    prev.innerHTML = '';
    [...input.files].slice(0, multiple ? 9 : 1).forEach(f => {
      const img = document.createElement('img');
      img.src = URL.createObjectURL(f);
      prev.appendChild(img);
    });
  }
}
wireDrop('dropMain', 'image', 'prevMain', false);
wireDrop('dropExtra', 'extras', 'prevExtra', true);

// ── 第5步：生成首帧图（Seedream / Nano Banana）──
$('ffGo').onclick = async () => {
  const eng = ffEngineVal();
  if (eng === 'nanobanana') {
    if (!gemVal()) { alert('请先在顶部填写 Google Gemini Key'); return; }
  } else {
    if (!$('arkKey').value.trim().startsWith('ark-')) { alert('请先在顶部填写火山方舟 Key（ark- 开头）'); return; }
  }
  if (!$('image').files[0]) { alert('请先在第 2 步上传产品图'); return; }
  const fd = new FormData();
  fd.append('engine', eng);
  fd.append('prompt', sub($('ffPrompt').value));
  fd.append('model', $('ffModel').value);
  if (eng === 'nanobanana') fd.append('gemini_key', gemVal());
  else fd.append('ark_key', $('arkKey').value.trim());
  fd.append('image', $('image').files[0]);
  for (const f of $('extras').files) fd.append('image', f);  // 多角度参考一起喂
  $('ffGo').disabled = true; $('ffGo').textContent = '⏳ 生成中（约 10-30 秒）…';
  $('ffProg').style.display = 'block'; $('ffProg').textContent = '正在生成首帧图…';
  $('ffPrev').style.display = 'none';
  try {
    const j = await (await fetch('/api/seedream', { method: 'POST', body: fd })).json();
    if (!j.ok) { $('ffProg').textContent = '❌ ' + j.error; return; }
    generatedFF = j.file;
    $('ffImg').src = '/video/' + encodeURIComponent(j.file) + '?t=' + Date.now();
    $('ffProg').style.display = 'none';
    $('ffPrev').style.display = 'block';
  } catch (err) { $('ffProg').textContent = '❌ ' + err; }
  finally { $('ffGo').disabled = false; $('ffGo').textContent = '🎨 生成首帧图'; }
};

// 组合镜头模式：只切换「一键生成组合视频」框的显隐。⑤⑥ 手动流程始终可用、不禁用。
function setComboMode(on) {
  $('comboBox').style.display = on ? 'block' : 'none';
}

function startPolling() {
  $('progress').style.display = 'block';
  $('result').style.display = 'none';
  $('stateline').style.display = 'flex';
  $('progress').scrollIntoView({ behavior: 'smooth' });
  t0 = Date.now();
  timer = setInterval(poll, 3000);
}

let timer = null, t0 = 0;

// ── 组合镜头：一键逐镜头生成 + 自动拼接 ──
$('comboGo').onclick = async () => {
  const veo = vEngineVal() === 'veo';
  const nb = ffEngineVal() === 'nanobanana';
  if (!comboId) { alert('请先在第 3 步选一个组合镜头模板'); return; }
  if ((nb || veo) && !gemVal()) { alert('用 Nano Banana / Veo 需填 Google Gemini Key'); return; }
  if (!nb && !$('arkKey').value.trim().startsWith('ark-')) { alert('Seedream 生图需填火山方舟 Key（ark- 开头）'); return; }
  if (!veo && !$('key').value.trim().startsWith('sk-')) { alert('Seedance 生视频需填 NoToken 令牌（sk- 开头）'); return; }
  if (!$('image').files[0]) { alert('请先在第 2 步上传产品图'); return; }
  const fd = new FormData();
  fd.append('combo_id', comboId);
  fd.append('car_model', $('carModel').value.trim());
  fd.append('img_engine', ffEngineVal());
  fd.append('video_engine', veo ? 'veo' : 'seedance');
  fd.append('ff_model', $('ffModel').value);
  fd.append('veo_model', $('veoModel').value);
  fd.append('resolution', pillVal('resolution'));
  if (gemVal()) fd.append('gemini_key', gemVal());
  if (!nb) fd.append('ark_key', $('arkKey').value.trim());
  if (!veo) fd.append('key', $('key').value.trim());
  fd.append('image', $('image').files[0]);
  for (const f of $('extras').files) fd.append('image', f);
  $('comboGo').disabled = true; $('comboGo').textContent = '⏳ 逐镜头生成中（较久，勿关页面）…';
  const r = await fetch('/api/generate_combo', { method: 'POST', body: fd });
  const j = await r.json();
  if (!j.ok) { alert(j.error); reset(); return; }
  startPolling();
};

// ── 第6步：手动生成单条视频（Seedance / Veo）──
$('go').onclick = async () => {
  const veo = vEngineVal() === 'veo';
  // —— 单镜头分支 ——
  if (veo) {
    if (!gemVal()) { alert('请先在顶部填写 Google Gemini Key'); return; }
  } else {
    if (!$('key').value.trim().startsWith('sk-')) { alert('请先在顶部填 NoToken 令牌（sk- 开头）'); return; }
  }
  if (!generatedFF) { alert('请先在第 5 步点「🎨 生成首帧图」'); return; }
  const fd = new FormData();
  fd.append('prompt', sub($('prompt').value));
  fd.append('resolution', pillVal('resolution'));
  fd.append('duration', pillVal('duration'));
  fd.append('mode', 'first_frame');
  fd.append('firstframe_file', generatedFF);
  fd.append('video_engine', veo ? 'veo' : 'seedance');
  if (veo) { fd.append('gemini_key', gemVal()); fd.append('veo_model', $('veoModel').value); }
  else { fd.append('key', $('key').value.trim()); fd.append('model', model); }

  $('go').disabled = true; $('go').textContent = '⏳ 生成中…';
  $('progress').style.display = 'block';
  $('result').style.display = 'none';
  $('stateline').style.display = 'flex';
  $('progress').scrollIntoView({ behavior: 'smooth' });
  t0 = Date.now();

  const r = await fetch('/api/generate', { method: 'POST', body: fd });
  const j = await r.json();
  if (!j.ok) { alert(j.error); reset(); return; }
  timer = setInterval(poll, 3000);
};

async function poll() {
  const s = await (await fetch('/api/status')).json();
  $('log').textContent = s.log.join('\\n');
  $('log').scrollTop = $('log').scrollHeight;
  const mins = Math.floor((Date.now() - t0) / 60000), secs = Math.floor((Date.now() - t0) / 1000) % 60;
  $('statetxt').textContent = `正在生成… 已用时 ${mins}:${String(secs).padStart(2, '0')}`;
  if (s.state === 'done') {
    clearInterval(timer);
    $('stateline').style.display = 'none';
    $('player').src = '/video/' + encodeURIComponent(s.video);
    $('dlink').href = '/video/' + encodeURIComponent(s.video);
    $('dlink').setAttribute('download', s.video);
    $('result').style.display = 'block';
    reset();
  } else if (s.state === 'error') {
    clearInterval(timer);
    $('statetxt').textContent = '❌ 生成失败，详情见下方日志';
    document.querySelector('.spin').style.display = 'none';
    reset();
  }
}
function reset() {
  $('go').disabled = false; $('go').textContent = '🚀 生成视频';
  if ($('comboGo')) { $('comboGo').disabled = false; $('comboGo').textContent = '🎬 一键生成组合视频'; }
  const sp = document.querySelector('.spin');
  if (sp) sp.style.display = '';
}
</script>
</body>
</html>"""


def main() -> None:
    INPUT_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    port = 8765
    for _ in range(10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        raise SystemExit("找不到可用端口")
    url = f"http://127.0.0.1:{port}"
    print(f"✅ 视频工厂已启动：{url}")
    print("   浏览器没自动打开的话，手动访问上面这个地址。")
    print("   使用期间请保持本窗口开着；关闭窗口即退出。")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
