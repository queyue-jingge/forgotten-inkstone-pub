#!/usr/bin/env python3
"""讲课台 · 后端驱动器。

一个常驻进程管所有章，同一刻只醒一章（跟各课 state 的 live 走）：
  · 盯 state/<course>.json 的 live：变到某章 → 拉起白砚；变空 → 收掉
  · 开过的章（sessions.json 有 sid）→ --resume 续上；没开过 → 起新的并塞一句交代
  · 网页新话落进 inbox.jsonl → 喂给当前白砚
  · 白砚自己用讲课台接口（/api/append 等）写回讲稿，本驱动器不管它的输出内容

白砚 = 一个 claude -p 常驻子进程，stdin 走 stream-json 喂 user 消息，
        stdout 只看 system/init 拿 session_id 记档。

跑：   TEACH_BASE=http://127.0.0.1:54862 python3 driver.py
空跑： DRIVER_DRY=1 python3 driver.py        # 不动真进程，只打日志
"""
import json
import os
import signal
import subprocess
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "state")
SESSIONS = os.path.join(STATE, "sessions.json")
INBOX = os.path.join(ROOT, "inbox.jsonl")
PROMPT_FILE = os.environ.get("TEACH_PROMPT", os.path.join(ROOT, "prompts", "白砚.md"))
LOG = os.path.join(ROOT, "driver.log")
BASE = os.environ.get("TEACH_BASE", "http://127.0.0.1:54862")
CLAUDE_BIN = os.environ.get("TEACH_CLAUDE_BIN", "claude")
DRY = os.environ.get("DRIVER_DRY") == "1"
POLL = float(os.environ.get("DRIVER_POLL", "2"))


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        os.write(2, line.encode("utf-8", "replace"))
    except Exception:
        pass
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def current_live():
    """扫 state/*.json，返回 (course, chapter)；都空则 None。"""
    try:
        names = [n for n in os.listdir(STATE) if n.endswith(".json")]
    except Exception:
        return None
    for n in names:
        if n == "sessions.json":
            continue
        st = load_json(os.path.join(STATE, n), {})
        live = st.get("live")
        if live:
            return (st.get("course") or n[:-5], str(live))
    return None


def chapter_meta(course, chapter):
    """问讲课台要该章标题 / 起始页 / 书页。失败给空。"""
    try:
        with urllib.request.urlopen(f"{BASE}/api/state?course={course}", timeout=5) as r:
            st = json.load(r)
    except Exception:
        return {}
    ch = next((c for c in st.get("chapters", []) if c["id"] == chapter), None)
    off = st.get("offset", 0)
    m = {"course_title": st.get("title"), "chapter": chapter}
    if ch:
        m["chapter_title"] = ch.get("title")
        m["page"] = ch.get("page")
        m["book_page"] = (ch.get("page") or 0) - off
    return m


def session_of(course, chapter):
    s = load_json(SESSIONS, {})
    return ((s.get(course) or {}).get(str(chapter)) or {}).get("sid")


def register_sid(course, chapter, sid):
    try:
        data = json.dumps({"course": course, "chapter": chapter, "sid": sid}).encode()
        req = urllib.request.Request(f"{BASE}/api/session", data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        log(f"登记 sid 失败：{e}")


class Session:
    """一章一个白砚；completely 由驱动器起停。"""

    def __init__(self, course, chapter):
        self.course = course
        self.chapter = chapter
        self.proc = None
        self.sid = None
        self.fed = 0
        self.lock = threading.Lock()

    # ---- 起 ----
    def start(self):
        sid = session_of(self.course, self.chapter)
        try:
            with open(PROMPT_FILE, encoding="utf-8") as f:
                prompt = f.read()
        except Exception as e:
            log(f"读白砚提示词失败：{e}")
            prompt = ""

        args = [CLAUDE_BIN, "-p",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--verbose", "--dangerously-skip-permissions",
                "--append-system-prompt", prompt]
        if sid:
            args += ["--resume", sid]

        if DRY:
            log(f"[DRY] 会起白砚：{self.course} 第{self.chapter}章 "
                f"{'续 ' + sid if sid else '新开'}")
            self.sid = sid or "(dry)"
            self.fed = self._inbox_len()
            return

        log(f"起白砚：{self.course} 第{self.chapter}章 {'续 ' + sid if sid else '新开'}")
        env = dict(os.environ, TEACH_DRIVER="1")
        self.proc = subprocess.Popen(
            args, cwd=ROOT, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

        meta = chapter_meta(self.course, self.chapter)
        title = meta.get("chapter_title", "")
        if sid:
            seed = (f"学生回到讲课台了（还是《{meta.get('course_title')}》"
                    f"第{self.chapter}章「{title}」）。接着上次讲——先 `GET {BASE}/api/inbox"
                    f"?course={self.course}` 看看他最后说了什么，再决定开口说什么。")
        else:
            seed = (f"学生刚在讲课台唤醒了《{meta.get('course_title')}》第{self.chapter}章"
                    f"「{title}」，从书页 {meta.get('book_page')}（PDF {meta.get('page')}）起。"
                    f"你是白砚，去开讲：先 `GET {BASE}/api/state?course={self.course}` 看眼当前讲稿"
                    f"（markdown 字段）——已有内容就接着往下讲、别重来；还空着就写一段开场白。"
                    f"动笔前可跑 `python3 teach.py view {self.course}` 看当前页（图用 Read 打开）。"
                    f"输出一律走 POST /api/append 追加。别一次讲太多，讲完停下等他。")
        self.feed(seed)
        self.fed = self._inbox_len()   # 别把开讲前的旧 inbox 重喂

    # ---- 读 ----
    def _read_stdout(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type")
                if t == "system" and obj.get("subtype") == "init" and obj.get("session_id"):
                    self.sid = obj["session_id"]
                    register_sid(self.course, self.chapter, self.sid)
                    log(f"白砚 session={self.sid}")
                elif t == "result":
                    log("白砚一轮结束")
        except Exception:
            pass

    def _drain_stderr(self):
        try:
            for line in self.proc.stderr:
                log(f"[白砚] {line.rstrip()}")
        except Exception:
            pass

    # ---- 喂 ----
    def feed(self, text):
        with self.lock:
            if DRY or not self.proc or not self.proc.stdin:
                log(f"[DRY] 喂白砚：{text[:100]}")
                return
            line = json.dumps({"type": "user",
                               "message": {"role": "user",
                                           "content": [{"type": "text", "text": text}]}})
            try:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
                log(f"喂白砚：{text[:100]}")
            except Exception as e:
                log(f"喂白砚失败：{e}")

    def _inbox_len(self):
        try:
            with open(INBOX, encoding="utf-8") as f:
                return sum(1 for _ in f)
        except Exception:
            return 0

    def poll_inbox(self):
        try:
            with open(INBOX, encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            lines = []
        for i in range(self.fed, len(lines)):
            raw = lines[i].strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            if rec.get("course") != self.course or str(rec.get("chapter")) != self.chapter:
                continue
            text = rec.get("text") or ""
            imgs = [im.get("path") for im in (rec.get("images") or []) if im.get("path")]
            msg = f"学生：{text}" if text else "学生：（只发了图）"
            if imgs:
                msg += "\n（他附了图：" + "、".join(imgs) + "，用 Read 打开看）"
            self.feed(msg)
        self.fed = len(lines)

    # ---- 收 ----
    def stop(self):
        p = self.proc
        if DRY:
            log("[DRY] 收白砚")
            return
        if not p:
            return
        log(f"收白砚：{self.course} 第{self.chapter}章")
        try:
            p.terminate()
            try:
                p.wait(timeout=6)
            except subprocess.TimeoutExpired:
                p.kill()
        except Exception:
            pass
        self.proc = None


G = {"target": None, "sess": None}


def _on_signal(sig, _frm):
    log(f"收到信号 {sig}，退出")
    if G["sess"]:
        G["sess"].stop()
    os._exit(0)


def main():
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    log(f"驱动器启动 | base={BASE} dry={DRY} poll={POLL}s")
    while True:
        live = current_live()
        if live != G["target"]:
            log(f"live 变：{G['target']} → {live}")
            if G["sess"]:
                G["sess"].stop()
                G["sess"] = None
            G["target"] = live
            if live:
                G["sess"] = Session(*live)
                G["sess"].start()
        if G["sess"]:
            G["sess"].poll_inbox()
        time.sleep(POLL)


if __name__ == "__main__":
    main()
