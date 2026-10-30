#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Catch 演示后端
- 静态文件服务
- 图片上传（dataURL → 文件）
- 模拟生成任务（队列 + 进度轮询）
- 作品列表

启动： python3 server.py [端口]
默认端口 8788
"""
import os, sys, json, base64, uuid, time, threading, mimetypes, re
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote

ROOT = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(ROOT, "uploads")
OUTPUT_DIR = os.path.join(ROOT, "outputs")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

TASKS = {}       # taskId -> {status, progress, createdAt, templateId, imageUrl, resultUrl}
WORKS = []       # 已完成作品
LOCK = threading.Lock()

# 模拟耗时（秒）
GEN_MIN, GEN_MAX = 16, 26

# 结果占位资源：优先用真实录制的生成结果
PLACEHOLDERS = [
    ("assets/result_video.jpg", "video"),
]


def _find_result_asset():
    for p, kind in PLACEHOLDERS:
        if os.path.exists(os.path.join(ROOT, p)):
            return p, kind
    return None, None


def _run_task(task_id):
    """后台线程：模拟生成过程"""
    t = TASKS[task_id]
    total = GEN_MIN + (hash(task_id) % (GEN_MAX - GEN_MIN + 1))
    step = 0.4
    elapsed = 0.0
    while elapsed < total:
        time.sleep(step)
        elapsed += step
        with LOCK:
            t["progress"] = min(99, int(elapsed / total * 100))
    asset, kind = _find_result_asset()
    with LOCK:
        t["status"] = "done"
        t["progress"] = 100
        t["resultUrl"] = asset or ""
        WORKS.insert(0, {
            "id": task_id,
            "title": t.get("templateTitle", "Untitled"),
            "templateId": t.get("templateId"),
            "createdAt": int(time.time()),
            "type": kind or "video",
            "duration": 12,
            "thumb": asset or "",
            "url": asset or "",
            "imageUrl": t.get("imageUrl", ""),
        })


class Handler(SimpleHTTPRequestHandler):
    server_version = "AICatchDemo/1.0"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    # ---------- 工具 ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _save_data_url(self, data_url):
        m = re.match(r"data:image/(\w+);base64,(.+)", data_url or "", re.S)
        if not m:
            return None
        ext = "jpg" if m.group(1) in ("jpeg", "jpg") else m.group(1)
        try:
            blob = base64.b64decode(m.group(2))
        except Exception:
            return None
        name = f"{uuid.uuid4().hex}.{ext}"
        with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
            f.write(blob)
        return f"/uploads/{name}"

    # ---------- 路由 ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/works":
            with LOCK:
                return self._json({"works": WORKS})
        if path.startswith("/api/task/"):
            tid = path.rsplit("/", 1)[-1]
            with LOCK:
                t = TASKS.get(tid)
                if not t:
                    return self._json({"error": "not found"}, 404)
                return self._json({
                    "id": tid,
                    "status": t["status"],
                    "progress": t["progress"],
                    "resultUrl": t.get("resultUrl", ""),
                })
        if path == "/api/health":
            return self._json({"ok": True, "tasks": len(TASKS), "works": len(WORKS)})
        if path.startswith("/api/ark/video/"):
            tid = path.rsplit("/", 1)[-1]
            try:
                d = ark_video_poll(tid)
                if d.get("status") == "succeeded":
                    return self._json({"status": "succeeded",
                                       "videoUrl": d.get("content", {}).get("video_url", "")})
                return self._json({"status": d.get("status", "unknown")})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/upload":
            data = self._read_json()
            url = self._save_data_url(data.get("dataUrl"))
            if not url:
                return self._json({"error": "invalid image"}, 400)
            return self._json({"ok": True, "url": url})
        if path == "/api/generate":
            data = self._read_json()
            tid = uuid.uuid4().hex[:12]
            with LOCK:
                TASKS[tid] = {
                    "status": "generating",
                    "progress": 0,
                    "createdAt": int(time.time()),
                    "templateId": data.get("templateId", ""),
                    "templateTitle": data.get("templateTitle", "Untitled"),
                    "imageUrl": data.get("imageUrl", ""),
                    "credits": data.get("credits", 0),
                    "resultUrl": "",
                }
            threading.Thread(target=_run_task, args=(tid,), daemon=True).start()
            return self._json({"ok": True, "taskId": tid})
        if path == "/api/ark/extract":
            data = self._read_json()
            prompt = (data.get("prompt") or "").strip()
            if not prompt:
                return self._json({"error": "prompt required"}, 400)
            try:
                return self._json({"ok": True, "data": ark_extract(prompt)})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == "/api/ark/frame":
            data = self._read_json()
            prompt = (data.get("prompt") or "").strip()
            if not prompt:
                return self._json({"error": "prompt required"}, 400)
            name = uuid.uuid4().hex[:10] + ".jpg"
            try:
                return self._json({"ok": True, "url": ark_frame(prompt, name)})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if path == "/api/ark/video":
            data = self._read_json()
            prompt = (data.get("prompt") or "").strip()
            if not prompt:
                return self._json({"error": "prompt required"}, 400)
            try:
                return self._json({"ok": True, "taskId": ark_video_create(prompt, int(data.get("duration", 5)))})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)

    def end_headers(self):
        # 演示用：禁用缓存，避免改了素材浏览器还在用旧的
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):
        msg = fmt % args
        if "/api/" in msg:
            sys.stderr.write("  %s\n" % msg)




# ================= 火山方舟（真实生成） =================
ARK_KEY_FILE = os.path.join(os.path.dirname(ROOT), "ark_test", ".ark_key")
ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
ARK_IMG_MODEL = "doubao-seedream-4-0-20260415"
ARK_VID_MODEL = "doubao-seedance-2-5-260628"
ARK_TXT_MODEL = "doubao-seed-2-1-lite-260915"

def _ark_key():
    try:
        return open(ARK_KEY_FILE).read().strip()
    except Exception:
        return os.environ.get("ARK_API_KEY", "")

def _ark_post(path, payload, timeout=180):
    import urllib.request
    key = _ark_key()
    if not key:
        raise RuntimeError("no ark key")
    req = urllib.request.Request(ARK_BASE + path, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def _ark_get(path):
    import urllib.request
    key = _ark_key()
    req = urllib.request.Request(ARK_BASE + path,
        headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())

EXTRACT_SYS = (
    "You are a prompt structuring engine. Given an AI video prompt, extract replaceable entities. "
    "For each entity output type (subject/scene/color/style/element) and exactly 4 replacement candidates. "
    "Candidates must be same grammatical slot, same length range, style-consistent, and MUST NOT overlap between entities. "
    'Output JSON only: {"entities":[{"text":"...","type":"subject","candidates":["...","...","...","..."]}]}'
)

def ark_extract(prompt):
    d = _ark_post("/chat/completions", {
        "model": ARK_TXT_MODEL,
        "messages": [{"role": "system", "content": EXTRACT_SYS},
                     {"role": "user", "content": prompt}],
        "temperature": 0.6,
        "response_format": {"type": "json_object"},
    }, timeout=240)
    return json.loads(d["choices"][0]["message"]["content"])

def ark_frame(prompt, out_name):
    import urllib.request
    d = _ark_post("/images/generations", {
        "model": ARK_IMG_MODEL, "prompt": prompt, "size": "1280x720",
        "response_format": "url", "watermark": False})
    url = d["data"][0]["url"]
    path = os.path.join(OUTPUT_DIR, out_name)
    with urllib.request.urlopen(url, timeout=120) as f:
        open(path, "wb").write(f.read())
    return "/outputs/" + out_name

def ark_video_create(prompt, duration=5):
    d = _ark_post("/contents/generations/tasks", {
        "model": ARK_VID_MODEL,
        "content": [{"type": "text", "text": prompt}],
        "duration": duration, "ratio": "16:9", "resolution": "720p",
        "watermark": False, "generate_audio": False})
    return d["id"]

def ark_video_poll(task_id):
    return _ark_get("/contents/generations/tasks/" + task_id)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8788
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"\n  AI Catch Demo 运行中")
    print(f"  本机:   http://127.0.0.1:{port}/")
    print(f"  手机:   http://<本机局域网IP>:{port}/  （需同一 WiFi）")
    print(f"  停止:   Ctrl+C\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止\n")


if __name__ == "__main__":
    main()
