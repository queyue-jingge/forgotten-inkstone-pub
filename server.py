#!/usr/bin/env python3
"""讲课台 —— 轻量后端。

职责：
  * 发静态前端（web/）
  * 把教材 PDF 的一页按需渲成 jpg（poppler pdftoppm），缓存复用
  * 暴露一个极小的 JSON 接口，让"讲稿"和"翻页"能由任意进程驱动

约定（白砚驱动侧就用这几个口）：
  GET  /api/state?course=<c>              取当前卷/页/讲稿
  POST /api/append  {course, markdown}    往讲稿尾部追加（一句句冒）
  POST /api/replace {course, markdown}    整体替换讲稿
  POST /api/page    {course, page}        翻到某 PDF 页
  POST /api/volume  {course, volume}      切换卷（信号与系统上/下）
  GET  /api/inbox?course=<c>&since=<n>    读学生从网页发来的话
  POST /api/say     {course, text}        网页端投递一句话（前端用）
  POST /api/board   {course, open?, image?, x?, y?, w?, clear?}  白板：开板/放题图/摆位/清板（白砚用）
  GET  /api/courses                       课程清单
"""
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
BOOKS = os.environ.get("TEACH_BOOKS", "/path/to/books")
CACHE = os.path.join(ROOT, "cache")
STATE = os.path.join(ROOT, "state")
LESSONS = os.path.join(ROOT, "lessons")
DRAW = os.path.join(ROOT, "draw")
MEDIA = os.path.join(ROOT, "media")
INBOX = os.path.join(ROOT, "inbox.jsonl")
SESSIONS = os.path.join(STATE, "sessions.json")           # 哪些章"开过"（有会话 id）
TEACH_QUEUE = os.environ.get("TEACH_QUEUE", os.path.join(ROOT, "teach-queue.json"))  # 开讲请求 → 控制端 读
PORT = int(os.environ.get("TEACH_PORT", "8000"))
HOST = os.environ.get("TEACH_HOST", "127.0.0.1")   # 默认只听本机；对外要显式设 TEACH_HOST=0.0.0.0

# ---- 访问口令（本地 127.0.0.1 直接放行，外网要口令） ----
PASSWORD = os.environ.get("TEACH_PASS", "changeme")
AUTH_COOKIE = "teach_auth"
AUTH_TOKEN = hashlib.sha256(("teach-ui:" + PASSWORD).encode()).hexdigest()
AUTH_MAX_AGE = 60 * 60 * 24 * 30     # cookie 记 30 天

# 安全：绑非回环地址时必须有口令，否则拒绝启动（防"clone 下来直接跑成对外弱口令服务"）
if HOST not in ("127.0.0.1", "::1", "localhost") and PASSWORD == "changeme":
    raise SystemExit(
        "TEACH_PASS 未设置。绑对外地址（TEACH_HOST=%s）时请先设口令，例如："
        " TEACH_PASS=你的口令 TEACH_HOST=0.0.0.0 python3 server.py" % HOST)

LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>讲课台 · 进入</title>
<style>
:root{--bg:#14161c;--bg2:#1b1e26;--bg3:#222634;--line:#2c3040;--fg:#e6e8f0;--dim:#8b90a2;--accent:#8aa2ff;--bad:#f87171}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--fg);display:flex;align-items:center;justify-content:center;
 font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.card{background:var(--bg2);border:1px solid var(--line);border-radius:.9rem;padding:1.6rem 1.8rem;
 width:min(92vw,22rem);box-shadow:0 18px 50px rgba(0,0,0,.45)}
h1{font-size:1.05rem;letter-spacing:.14em;margin:0 0 1.1rem;color:#cbd2e6;font-weight:600}
input[type=password]{width:100%;background:var(--bg3);border:1px solid var(--line);border-radius:.55rem;
 color:var(--fg);padding:.62rem .8rem;font:inherit;font-size:1rem}
input[type=password]:focus{outline:none;border-color:var(--accent)}
button{width:100%;margin-top:.85rem;background:var(--accent);color:#0d1020;border:none;
 border-radius:.55rem;padding:.62rem;font:inherit;font-weight:600;cursor:pointer}
.err{color:var(--bad);font-size:.85rem;margin-top:.7rem}
</style></head><body>
<form class="card" method="post" action="/login">
 <h1>讲课台</h1>
 <input type="password" name="password" placeholder="口令" autofocus autocomplete="current-password">
 <input type="hidden" name="next" value="{{NEXT}}">
 <button type="submit">进入</button>
 {{ERR}}
</form>
</body></html>
"""

for d in (CACHE, STATE, LESSONS, DRAW, MEDIA):
    os.makedirs(d, exist_ok=True)

IMG_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
           "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp"}

# ────────────────────────────────────────────────────────────────────────
# 示例配置：下面三张表（VOLUMES / COURSES / CHAPTERS）只是演示用的两门课本，
# 换成你自己的——PDF 放进 TEACH_BOOKS 指的目录，再改这里指向它即可。
# ────────────────────────────────────────────────────────────────────────
# 卷：一份 PDF。offset = PDF页 − 书页（书页 = PDF页 − offset）
VOLUMES = {
    "semi": {
        "title": "半导体物理（刘恩科 第8版）",
        "pdf": "半导体物理学第8版-刘恩科.pdf",
        "pages": 401,
        "offset": 13,
    },
    "sig1": {
        "title": "信号与系统（郑君里 第4版·上册）",
        "pdf": "信号与系统第4版-上册.pdf",
        "pages": 469,
        "offset": 19,
    },
    "sig2": {
        "title": "信号与系统（郑君里 第4版·下册）",
        "pdf": "信号与系统第4版-下册.pdf",
        "pages": 436,
        "offset": 22,
    },
}

COURSES = {
    "semi": {"title": "半导体物理", "volumes": ["semi"], "default": "semi", "page": 14},
    "signals": {"title": "信号与系统", "volumes": ["sig1", "sig2"], "default": "sig1", "page": 20},
}

# 章：id 稳定（复习/会话按它挂钩）；vol+page = 该章起始（PDF 页）
CHAPTERS = {
    "semi": [
        {"id": "1", "title": "半导体中的电子状态", "vol": "semi", "page": 14},
        {"id": "2", "title": "半导体中杂质和缺陷能级", "vol": "semi", "page": 50},
        {"id": "3", "title": "半导体中载流子的统计分布", "vol": "semi", "page": 69},
        {"id": "4", "title": "半导体的导电性", "vol": "semi", "page": 105},
        {"id": "5", "title": "非平衡载流子", "vol": "semi", "page": 139},
        {"id": "6", "title": "pn结", "vol": "semi", "page": 169},
        {"id": "7", "title": "金属和半导体的接触", "vol": "semi", "page": 200},
        {"id": "8", "title": "半导体表面与MIS结构", "vol": "semi", "page": 217},
        {"id": "9", "title": "半导体异质结构", "vol": "semi", "page": 250},
        {"id": "10", "title": "半导体的光学性质和光电与发光现象", "vol": "semi", "page": 286},
        {"id": "11", "title": "半导体的热电性质", "vol": "semi", "page": 322},
        {"id": "12", "title": "半导体磁和压阻效应", "vol": "semi", "page": 336},
        {"id": "13", "title": "非晶态半导体", "vol": "semi", "page": 366},
    ],
    "signals": [
        {"id": "1", "title": "绪论", "vol": "sig1", "page": 20},
        {"id": "2", "title": "连续时间系统的时域分析", "vol": "sig1", "page": 66},
        {"id": "3", "title": "傅里叶变换", "vol": "sig1", "page": 116},
        {"id": "4", "title": "拉普拉斯变换·连续时间系统的s域分析", "vol": "sig1", "page": 214},
        {"id": "5", "title": "傅里叶变换应用于通信系统", "vol": "sig1", "page": 317},
        {"id": "6", "title": "信号的矢量空间分析", "vol": "sig1", "page": 376},
        {"id": "7", "title": "离散时间系统的时域分析", "vol": "sig2", "page": 23},
        {"id": "8", "title": "z变换·离散时间系统的z域分析", "vol": "sig2", "page": 67},
        {"id": "9", "title": "离散傅里叶变换及其他离散正交变换", "vol": "sig2", "page": 143},
        {"id": "10", "title": "模拟与数字滤波器", "vol": "sig2", "page": 201},
        {"id": "11", "title": "反馈系统", "vol": "sig2", "page": 298},
        {"id": "12", "title": "系统的状态变量分析", "vol": "sig2", "page": 353},
    ],
}

# 有文字层的卷 → OCR 全文 txt（pdftotext 输出，\f 分页；PDF 第 N 页 = 段 [N-1]）
TEXTS = {
    "semi": {"txt": "半导体物理学第8版-刘恩科.txt"},
}


def page_text(volume, page):
    """取该卷该 PDF 页的 OCR 文本；无文字层返回 None。"""
    t = TEXTS.get(volume)
    if not t:
        return None
    p = os.path.join(BOOKS, t["txt"])
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            pages = f.read().split("\f")
        i = int(page) - 1
        if 0 <= i < len(pages):
            return pages[i].strip()
    except Exception:
        return None
    return None

_render_locks = {}
_locks_guard = threading.Lock()


def _lock_for(key):
    with _locks_guard:
        if key not in _render_locks:
            _render_locks[key] = threading.Lock()
        return _render_locks[key]


def state_path(course):
    return os.path.join(STATE, f"{course}.json")


def load_state(course):
    c = COURSES.get(course)
    if not c:
        return None
    chs = CHAPTERS.get(course, [])
    base = {"course": course, "volume": c["default"], "page": c["page"],
            "chapter": (chs[0]["id"] if chs else None), "live": None,
            "board": {"open": False, "image": None, "ts": 0, "clear_rev": 0,
                      "x": 0.06, "y": 0.06, "w": 0.88},
            "rev": 0, "updated": 0}
    p = state_path(course)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                base.update(json.load(f))
        except Exception:
            pass
    return base


def save_state(course, st):
    st["updated"] = int(time.time())
    st["rev"] = int(st.get("rev", 0)) + 1
    tmp = state_path(course) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, state_path(course))
    return st


def lesson_path(course, chapter):
    d = os.path.join(LESSONS, course)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{chapter}.md")


def read_lesson(course, chapter):
    if chapter is None:
        return ""
    p = lesson_path(course, chapter)
    if not os.path.exists(p):
        return ""
    with open(p, encoding="utf-8") as f:
        return f.read()


def write_lesson(course, chapter, text):
    with open(lesson_path(course, chapter), "w", encoding="utf-8") as f:
        f.write(text)


def append_lesson(course, chapter, text):
    if chapter is None:
        return
    with open(lesson_path(course, chapter), "a", encoding="utf-8") as f:
        if text and not text.startswith("\n"):
            f.write("\n")
        f.write(text)


# ---- 会话登记：哪些章"开过" ----
# 约定（2026-10-12）：一章一个会话 id，存过 id 才算"开过"。没开过的章点唤醒不落 live，
# 改为往 控制端 队列丢一条"开讲请求"。
def load_sessions():
    try:
        with open(SESSIONS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_sessions(s):
    tmp = SESSIONS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SESSIONS)


def opened_chapters(course):
    s = load_sessions().get(course)
    return sorted(s.keys()) if isinstance(s, dict) else []


def is_opened(course, chapter):
    s = load_sessions().get(course)
    return isinstance(s, dict) and str(chapter) in s


def register_open(course, chapter):
    """记一笔"这章开过了"。sid 先留空，等驱动器接上再回填。"""
    s = load_sessions()
    s.setdefault(course, {})[str(chapter)] = {"opened": int(time.time())}
    save_sessions(s)


def enqueue_open(course, chapter):
    """把一条"开讲请求"写进队列文件，供外部控制端轮询（讲课台 → 控制端）。"""
    ch = next((c for c in CHAPTERS.get(course, []) if c["id"] == str(chapter)), None)
    q = []
    try:
        with open(TEACH_QUEUE, encoding="utf-8") as f:
            q = json.load(f)
        if not isinstance(q, list):
            q = []
    except Exception:
        q = []
    q.append({
        "id": secrets.token_hex(8),
        "course": course,
        "course_title": COURSES.get(course, {}).get("title"),
        "chapter": str(chapter),
        "chapter_title": (ch or {}).get("title"),
        "page": (ch or {}).get("page"),
        "ts": int(time.time() * 1000),   # 毫秒：控制端 侧 fmtTime 按 ms 解
    })
    os.makedirs(os.path.dirname(TEACH_QUEUE), exist_ok=True)
    tmp = TEACH_QUEUE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(q, f, ensure_ascii=False)
    os.replace(tmp, TEACH_QUEUE)


def render_page(volume, page):
    """渲 PDF 第 page 页（1-based）到缓存 jpg，返回路径。"""
    v = VOLUMES.get(volume)
    if not v:
        return None, "unknown volume"
    try:
        page = int(page)
    except Exception:
        return None, "bad page"
    if page < 1 or page > v["pages"]:
        return None, f"page out of range 1..{v['pages']}"
    outdir = os.path.join(CACHE, volume)
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"{page}.jpg")
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out, None
    lock = _lock_for((volume, page))
    with lock:
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out, None
        pdf = os.path.join(BOOKS, v["pdf"])
        base = os.path.join(outdir, str(page))
        r = subprocess.run(
            ["pdftoppm", "-f", str(page), "-l", str(page), "-jpeg",
             "-r", "150", "-singlefile", pdf, base],
            capture_output=True, timeout=60)
        if r.returncode != 0 or not os.path.exists(base + ".jpg"):
            return None, (r.stderr.decode("utf-8", "replace") or "render failed")
        return base + ".jpg", None


class Handler(BaseHTTPRequestHandler):
    server_version = "teach-ui/0.1"

    def log_message(self, fmt, *args):
        pass  # 安静

    # ---------- helpers ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------- 口令 ----------
    def _is_local(self):
        ip = self.client_address[0]
        return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _authed(self):
        if self._is_local():
            return True
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == AUTH_COOKIE and v == AUTH_TOKEN:
                return True
        return False

    def _gate(self, path):
        """未登录：/api/* 回 401；页面类跳登录页。返回 True 表示已拦下。"""
        if self._authed():
            return False
        if path.startswith("/api/"):
            self._json({"error": "unauthorized"}, 401)
        else:
            nxt = urllib.parse.quote(self.path, safe="")
            self.send_response(303)
            self.send_header("Location", "/login?next=" + nxt)
            self.send_header("Content-Length", "0")
            self.end_headers()
        return True

    def _login_page(self, error=False, nxt="/", code=200):
        err = '<div class="err">口令不对</div>' if error else ''
        safe = nxt.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
        body = LOGIN_HTML.replace("{{NEXT}}", safe).replace("{{ERR}}", err).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _set_cookie(self, value, age):
        return (f"{AUTH_COOKIE}={value}; Path=/; Max-Age={age}; "
                "HttpOnly; SameSite=Lax")

    def _do_login(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
        pw = (form.get("password") or [""])[0]
        nxt = (form.get("next") or ["/"])[0] or "/"
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        if pw != PASSWORD:
            return self._login_page(error=True, nxt=nxt, code=401)
        self.send_response(303)
        self.send_header("Location", nxt)
        self.send_header("Set-Cookie", self._set_cookie(AUTH_TOKEN, AUTH_MAX_AGE))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_file(self, path, ctype, cache=True):
        if not os.path.isfile(path):
            self.send_error(404, "not found")
            return
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control",
                         "public, max-age=86400" if cache else "no-store")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------- GET ----------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path

        if path == "/login":
            return self._login_page(nxt=(q.get("next") or ["/"])[0])
        if path == "/logout":
            self.send_response(303)
            self.send_header("Set-Cookie", self._set_cookie("", 0))
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self._gate(path):
            return

        if path == "/" or path == "/index.html":
            return self._serve_file(os.path.join(WEB, "index.html"),
                                    "text/html; charset=utf-8", cache=False)

        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            if ".." in rel:
                return self.send_error(400, "bad path")
            ctype = {
                ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".html": "text/html; charset=utf-8",
                ".woff2": "font/woff2",
                ".json": "application/json; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(os.path.splitext(rel)[1], "application/octet-stream")
            # 字体大、几乎不变 → 长缓存；css/js 会改 → 不缓存
            cache = os.path.splitext(rel)[1] == ".woff2"
            return self._serve_file(os.path.join(WEB, rel), ctype, cache=cache)

        if path.startswith("/draw/"):
            rel = path[len("/draw/"):]
            if ".." in rel or not rel:
                return self.send_error(400, "bad path")
            ctype = {
                ".svg": "image/svg+xml",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }.get(os.path.splitext(rel)[1].lower(), "application/octet-stream")
            return self._serve_file(os.path.join(DRAW, rel), ctype)

        if path.startswith("/media/"):
            rel = path[len("/media/"):]
            if ".." in rel or not rel:
                return self.send_error(400, "bad path")
            ctype = {
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
            }.get(os.path.splitext(rel)[1].lower(), "application/octet-stream")
            return self._serve_file(os.path.join(MEDIA, rel), ctype)

        if path.startswith("/page/"):
            m = re.match(r"^/page/([A-Za-z0-9_]+)/(\d+)\.jpg$", path)
            if not m:
                return self.send_error(404, "bad page url")
            p, err = render_page(m.group(1), m.group(2))
            if err:
                return self._json({"error": err}, 404)
            return self._serve_file(p, "image/jpeg")

        if path == "/api/courses":
            return self._json({
                c: {"title": v["title"], "volumes": v["volumes"]}
                for c, v in COURSES.items()})

        if path == "/api/state":
            course = (q.get("course") or ["semi"])[0]
            st = load_state(course)
            if not st:
                return self._json({"error": "unknown course"}, 404)
            v = VOLUMES[st["volume"]]
            return self._json({
                "course": course,
                "title": COURSES[course]["title"],
                "volumes": COURSES[course]["volumes"],
                "volume": st["volume"],
                "volume_title": v["title"],
                "offset": v["offset"],
                "page": st["page"],
                "book_page": st["page"] - v["offset"],
                "total_pages": v["pages"],
                "rev": st["rev"],
                "updated": st["updated"],
                "chapters": CHAPTERS.get(course, []),
                "chapter": st.get("chapter"),
                "live": st.get("live"),
                "board": st.get("board") or {"open": False, "image": None,
                                             "ts": 0, "clear_rev": 0,
                                             "x": 0.06, "y": 0.06, "w": 0.88},
                "opened": opened_chapters(course),
                "markdown": read_lesson(course, st.get("chapter")),
            })

        if path == "/api/pageinfo":
            course = (q.get("course") or ["semi"])[0]
            st = load_state(course)
            if not st:
                return self._json({"error": "unknown course"}, 404)
            vol = st["volume"]
            try:
                page = int((q.get("page") or [st["page"]])[0])
            except Exception:
                page = st["page"]
            v = VOLUMES[vol]
            if page < 1 or page > v["pages"]:
                return self._json({"error": f"page out of range 1..{v['pages']}"}, 400)
            txt = page_text(vol, page)
            return self._json({
                "course": course, "volume": vol, "volume_title": v["title"],
                "page": page, "book_page": page - v["offset"],
                "total_pages": v["pages"],
                "image": f"/page/{vol}/{page}.jpg",
                "has_text": txt is not None,
                "text": txt,
            })

        if path == "/api/inbox":
            course = (q.get("course") or ["semi"])[0]
            since = int((q.get("since") or ["0"])[0])
            items = []
            if os.path.exists(INBOX):
                with open(INBOX, encoding="utf-8") as f:
                    lines = f.readlines()
                for i, line in enumerate(lines):
                    if i < since:
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("course") == course:
                        items.append({"n": i + 1, "ts": rec.get("ts"),
                                      "text": rec.get("text"),
                                      "images": rec.get("images", [])})
            return self._json({"since": since, "next": len(lines) if os.path.exists(INBOX) else 0,
                               "items": items})

        return self.send_error(404, "not found")

    # ---------- POST ----------
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        if path == "/login":
            return self._do_login()
        if self._gate(path):
            return
        body = self._read_json()

        if path == "/api/upload":
            data = body.get("data", "")
            mime = (body.get("mime") or "image/png").lower()
            if not data:
                return self._json({"error": "empty"}, 400)
            if "," in data and data.strip().startswith("data:"):
                data = data.split(",", 1)[1]
            ext = IMG_EXT.get(mime, "png")
            try:
                raw = base64.b64decode(data)
            except Exception:
                return self._json({"error": "bad base64"}, 400)
            name = f"{int(time.time() * 1000)}-{secrets.token_hex(3)}.{ext}"
            with open(os.path.join(MEDIA, name), "wb") as f:
                f.write(raw)
            return self._json({"url": f"/media/{name}",
                               "path": os.path.join(MEDIA, name),
                               "bytes": len(raw)})

        if path == "/api/say":
            course = body.get("course", "semi")
            st = load_state(course)
            if not st:
                return self._json({"error": "unknown course"}, 404)
            text = (body.get("text") or "").strip()
            images = []
            for u in (body.get("images") or []):
                nm = os.path.basename(str(u))
                if not nm or ".." in nm:
                    continue
                images.append({"url": f"/media/{nm}",
                               "path": os.path.join(MEDIA, nm)})
            if not text and not images:
                return self._json({"error": "empty"}, 400)
            rec = {"ts": int(time.time()), "course": course,
                   "chapter": st.get("chapter"), "src": "web",
                   "text": text, "images": images}
            with open(INBOX, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            # 同步落进讲稿流：网页把他的话显示成一句，轮次轨也据此落点
            lines = []
            if text:
                lines.append(text)
            for im in images:
                lines.append(f"![]({im['url']})")
            append_lesson(course, st.get("chapter"),
                          "\n\n<!--me-->\n" + "\n".join(lines) + "\n<!--/me-->\n")
            return self._json({"ok": True, "images": images})

        if path == "/api/append":
            course = body.get("course", "semi")
            st = load_state(course)
            append_lesson(course, st.get("chapter") if st else None,
                          body.get("markdown", ""))
            return self._json(save_state(course, st))

        if path == "/api/replace":
            course = body.get("course", "semi")
            st = load_state(course)
            write_lesson(course, st.get("chapter") if st else None,
                         body.get("markdown", ""))
            return self._json(save_state(course, st))

        if path == "/api/page":
            course = body.get("course", "semi")
            st = load_state(course)
            try:
                st["page"] = int(body.get("page", st["page"]))
            except Exception:
                return self._json({"error": "bad page"}, 400)
            return self._json(save_state(course, st))

        if path == "/api/volume":
            course = body.get("course", "semi")
            vol = body.get("volume")
            if vol not in COURSES[course]["volumes"]:
                return self._json({"error": "bad volume"}, 400)
            st = load_state(course)
            st["volume"] = vol
            return self._json(save_state(course, st))

        # 切章：换当前章 + 左侧 PDF 跳到该章起始（vol+page）
        if path == "/api/chapter":
            course = body.get("course", "semi")
            cid = str(body.get("chapter", ""))
            ch = next((c for c in CHAPTERS.get(course, []) if c["id"] == cid), None)
            if not ch:
                return self._json({"error": "bad chapter"}, 400)
            st = load_state(course)
            st["chapter"] = cid
            st["volume"] = ch["vol"]
            st["page"] = ch["page"]
            return self._json(save_state(course, st))

        # 驱动器登记某章的会话 id（开过的章续上次用它）；同时确保该章标成"开过"
        if path == "/api/session":
            course = body.get("course", "semi")
            cid = str(body.get("chapter", ""))
            sid = body.get("sid")
            if not any(c["id"] == cid for c in CHAPTERS.get(course, [])):
                return self._json({"error": "bad chapter"}, 400)
            s = load_sessions()
            entry = s.setdefault(course, {}).setdefault(cid, {})
            entry.setdefault("opened", int(time.time()))
            if sid:
                entry["sid"] = str(sid)
            save_sessions(s)
            return self._json({"ok": True, "course": course,
                               "chapter": cid, "sid": entry.get("sid")})

        # 白板：白砚开一块板（可带题图）/ 清板。学生端据 state.board 弹"选项"、贴底图。
        # x/y/w：题图在板上的位置/大小（0~1 的小数，相对板宽/板高；可省，前端有默认）。
        if path == "/api/board":
            course = body.get("course", "semi")
            st = load_state(course)
            if not st:
                return self._json({"error": "unknown course"}, 404)
            b = st.setdefault("board", {"open": False, "image": None,
                                        "ts": 0, "clear_rev": 0,
                                        "x": 0.06, "y": 0.06, "w": 0.88})
            now = int(time.time() * 1000)
            if "image" in body:
                img = body.get("image")            # none/"" = 撤掉题图，板变纯白
                b["image"] = str(img) if img else None
                b["open"] = bool(b["image"])
                b["ts"] = now
                for k in ("x", "y", "w"):        # 题图摆位：白砚自己定的，夹一到 0~1
                    if k in body and body.get(k) is not None:
                        try:
                            b[k] = max(0.0, min(1.0, float(body.get(k))))
                        except (TypeError, ValueError):
                            pass
            if "open" in body:
                b["open"] = bool(body.get("open"))
                b["ts"] = now
            if body.get("clear"):
                b["clear_rev"] = int(b.get("clear_rev", 0) or 0) + 1
                # 清板只动 clear_rev，不碰 ts——免得清一下却在学生端弹出一个"开板选项"
            return self._json(save_state(course, st))

        # 唤醒开关（示例：只落状态，真实会话由驱动器拉起）；全局只允许一个 live
        if path == "/api/live":
            course = body.get("course", "semi")
            val = body.get("value")
            st = load_state(course)
            if not st:
                return self._json({"error": "unknown course"}, 404)
            if val in (None, "", False):
                st["live"] = None
            else:
                cid = str(val)
                if not any(c["id"] == cid for c in CHAPTERS.get(course, [])):
                    return self._json({"error": "bad chapter"}, 400)
                if not is_opened(course, cid):
                    # 没开过的章：不落 live，改发一单"开讲请求"给控制端，等他确认
                    enqueue_open(course, cid)
                    return self._json({"ok": True, "queued": True,
                                       "course": course, "chapter": cid,
                                       "live": st.get("live")})
                st["live"] = cid
                for oc in COURSES:
                    if oc == course:
                        continue
                    ost = load_state(oc)
                    if ost and ost.get("live"):
                        ost["live"] = None
                        save_state(oc, ost)
            return self._json(save_state(course, st))

        return self.send_error(404, "not found")


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"teach-ui on http://{HOST}:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
