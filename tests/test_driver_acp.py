#!/usr/bin/env python3
"""driver.py 的集成测试：真起 stub agent（tests/test_acp_agent_stub.py）+ 一个只实现
/api/state 与 /api/session 的 stdlib HTTP stub，把驱动器整条路走一遍。

跑：python3 -m unittest discover -s tests
断言分三层：
  · 帧级：stub 把收到的每一帧原样记进 STUB_RECORD，直接核对驱动器到底发了什么；
  · 副作用级：POST /api/session 的 body、log 文案、inbox 有没有被写；
  · 源码级：driver.py 里不许再出现旧协议的字面量。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import driver  # noqa: E402  （sys.path 补好再 import）

STUB = os.path.join(REPO, "tests", "test_acp_agent_stub.py")
STUB_SID = "sess-stub"                       # stub 的 session/new 固定回这个
PYEXE = sys.executable                       # stub 用同一个解释器起
STUB_CALL = '"%s"' % STUB
# 一个**一定存在**的程序：非空跑时 check_config 要 resolve_bin 过得去
BIN_OK = PYEXE

# 人格正文：多行 + 唯一标记 + emoji。标记绝不能出现在用户消息里。
PERSONA = ("你是白砚，讲课台的讲义分身。\n"
           "PERSONA-MARKER-7f3a9c：这段只许走系统提示通道，不许出现在用户消息里。\n"
           "带中文引号「」和 emoji 🖌 也要原样过管道。")
MARKER = "PERSONA-MARKER-7f3a9c"

# stub 的行为开关：全部经 os.environ 传给子进程（acp.py 把驱动器进程的环境整个交给 agent）
STUB_ENV_KEYS = ("STUB_PROTOCOL_VERSION", "STUB_LOAD_SESSION", "STUB_EXIT_AFTER_INIT",
                 "STUB_SILENT", "STUB_ASK_PERMISSION", "STUB_PERMISSION_OPTIONS",
                 "STUB_GARBAGE_LINES", "STUB_REQUIRE_SYSTEM_PROMPT", "STUB_USAGE",
                 "STUB_UNKNOWN_REQUEST", "STUB_ECHO_PROMPT", "STUB_PROMPT_BARRIER",
                 "STUB_FAIL_LOAD", "STUB_RECORD", "STUB_LOG")


def make_scratch():
    """建本次测试专用的临时目录（平台 TEMP 下），返回路径。

    不用 tempfile.TemporaryDirectory：本机沙箱里 mkdtemp 建的目录带 0o700，
    受限进程自己都写不进去、也删不掉（stub 记帧会直接 PermissionError，测试全瞎
    还留一地删不掉的残留）。os.makedirs 用默认权限建，读写删在沙箱里都正常。
    """
    base = tempfile.gettempdir()
    for i in range(1000):
        path = os.path.join(base, "inkstone-drv-%d-%d" % (os.getpid(), i))
        try:
            os.makedirs(path)
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"TEMP（{base}）下建不出临时目录")


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def read_frames(path):
    """把 stub 记下的每帧读回来（文件可能正在被追加，坏行直接跳）。"""
    out = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def find_frame(frames, method):
    return next((f for f in frames if f.get("method") == method), None)


def count_frames(path, method):
    return len([f for f in read_frames(path) if f.get("method") == method])


def wait_for(pred, timeout=6.0):
    """等某个条件成立；喂话/回话都是后台线程干的，测试只能轮询。"""
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


# ---- 讲课台 HTTP stub：只实现驱动器要用的两个接口 ----

STATE_PAYLOAD = {
    "course": "semi",
    "title": "半导体物理",
    "offset": 10,
    "chapters": [{"id": "1", "title": "第一章 能带", "page": 13},
                 {"id": "2", "title": "第二章 载流子", "page": 40}],
}


class ApiStub:
    """记下驱动器打过来的请求，够断言就行。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = []      # POST /api/session 的 body，按顺序
        self.state_paths = []   # GET /api/state 的 path

    def record_session(self, body):
        with self.lock:
            self.sessions.append(body)

    def record_state(self, path):
        with self.lock:
            self.state_paths.append(path)


class _ApiHandler(BaseHTTPRequestHandler):
    api = None      # 每个测试 setUp 时挂上

    def log_message(self, *args):       # 别往 stderr 刷访问日志
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            self.api.record_state(self.path)
            return self._json(STATE_PAYLOAD)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        if self.path == "/api/session":
            self.api.record_session(body)
            return self._json({"ok": True, **body})
        self._json({"error": "not found"}, 404)


def start_api(api):
    handler = type("_BoundApiHandler", (_ApiHandler,), {"api": api})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server

class ScratchCase(unittest.TestCase):
    """轻量脚手架：一个临时目录 + 把 driver.LOG 指进去（别写仓库根的 driver.log）。"""

    def setUp(self):
        self.scratch = make_scratch()
        self.addCleanup(lambda: shutil.rmtree(self.scratch, ignore_errors=True))
        self.log_path = os.path.join(self.scratch, "driver.log")
        self._log_before = driver.LOG
        driver.LOG = self.log_path
        driver._WARNED.clear()
        self.addCleanup(self._restore_log)

    def _restore_log(self):
        driver.LOG = self._log_before
        driver._WARNED.clear()

    def log_text(self):
        return read_text(self.log_path)


class DriverCase(ScratchCase):
    """集成脚手架：临时目录 + 讲课台 HTTP stub + stub agent 的开关。"""

    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.scratch, "state")
        os.makedirs(self.state)
        self.inbox = os.path.join(self.scratch, "inbox.jsonl")
        self.persona = os.path.join(self.scratch, "persona.md")
        self.record = os.path.join(self.scratch, "record.jsonl")
        with open(self.persona, "w", encoding="utf-8") as f:
            f.write(PERSONA)

        # 把 driver 的路径常量全指到临时目录：仓库根不许留下 state/、driver.log、inbox.jsonl
        self._saved = {}
        for name, value in (("STATE", self.state),
                            ("SESSIONS", os.path.join(self.state, "sessions.json")),
                            ("INBOX", self.inbox),
                            ("PROMPT_FILE", self.persona),
                            ("LOG", self.log_path)):
            self._saved[name] = getattr(driver, name)
            setattr(driver, name, value)
        self.addCleanup(self._restore_modules)

        self.api = ApiStub()
        self.server = start_api(self.api)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self._saved["BASE"] = driver.BASE
        driver.BASE = "http://127.0.0.1:%d" % self.server.server_address[1]

        self._env_saved = {k: os.environ.get(k) for k in STUB_ENV_KEYS}
        for key in STUB_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["STUB_RECORD"] = self.record
        os.environ["STUB_LOG"] = os.path.join(self.scratch, "stub.log")
        self.addCleanup(self._restore_env)

        driver.G.update({"target": None, "sess": None, "dead": None})
        self.sessions = []
        self.addCleanup(self._stop_sessions)

    # ---- 收尾 ----
    def _stop_sessions(self):
        for sess in self.sessions:
            try:
                sess.stop()
            except Exception:
                pass
        if driver.G["sess"]:
            try:
                driver.G["sess"].stop()
            except Exception:
                pass
        driver.G.update({"target": None, "sess": None, "dead": None})

    def _restore_modules(self):
        for name, value in self._saved.items():
            setattr(driver, name, value)

    def _restore_env(self):
        for key, value in self._env_saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ---- 造配置 / 造会话 ----
    def cfg(self, *, args=None, system_prompt="meta", permission="allow",
            log_updates=False, dry=False, bin_name=None):
        return driver.load_config({
            "TEACH_AGENT_BIN": bin_name or BIN_OK,
            "TEACH_AGENT_ARGS": STUB_CALL if args is None else args,
            "TEACH_AGENT_SYSTEM_PROMPT": system_prompt,
            "TEACH_AGENT_PERMISSION": permission,
            "TEACH_AGENT_LOG_UPDATES": "1" if log_updates else "0",
            "TEACH_DRY": "1" if dry else "0",
        })

    def sess(self, course="semi", chapter="1", **kw):
        sess = driver.Session(course, chapter, self.cfg(**kw))
        self.sessions.append(sess)
        return sess

    def patch_live(self, live):
        """把 current_live 钉成固定值（主循环单测用）；用完自动还原。"""
        before = driver.current_live
        driver.current_live = lambda: live
        self.addCleanup(lambda: setattr(driver, "current_live", before))

    # ---- 读 record / log ----
    def frames(self):
        return read_frames(self.record)

    def prompts(self):
        return [f["params"]["prompt"][0]["text"] for f in self.frames()
                if f.get("method") == "session/prompt"]

    def wait_prompts(self, n, timeout=8.0):
        ok = wait_for(lambda: len(self.prompts()) >= n, timeout)
        self.assertTrue(ok, "等 session/prompt×%d 超时；record=%r" % (n, self.frames()))
        return self.prompts()

    def write_sessions(self, sid, course="semi", chapter="1"):
        path = os.path.join(self.state, "sessions.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({course: {chapter: {"sid": sid}}}, f)

    def append_inbox(self, *records):
        with open(self.inbox, "a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class NewSession(DriverCase):
    """新会话：人格走 _meta 系统提示通道，用户消息里绝不带人格正文。"""

    def test_persona_goes_to_meta_not_user_message(self):
        os.environ["STUB_REQUIRE_SYSTEM_PROMPT"] = "1"   # 人格没发出来 stub 就 exit 3
        sess = self.sess()
        sess.start()
        self.wait_prompts(1)

        frm = self.frames()
        new = find_frame(frm, "session/new")
        self.assertIsNotNone(new, "没发 session/new：%r" % (frm,))
        self.assertEqual(new["params"]["_meta"]["systemPrompt"], PERSONA)
        self.assertEqual(new["params"]["mcpServers"], [])
        self.assertTrue(os.path.isabs(new["params"]["cwd"]))
        self.assertEqual(new["params"]["cwd"], REPO)

        prompt = find_frame(frm, "session/prompt")
        self.assertNotIn("_meta", prompt["params"])
        text = prompt["params"]["prompt"][0]["text"]
        self.assertNotIn(MARKER, text)                  # 硬验收：人格不进用户消息
        self.assertNotIn("只许走系统提示通道", text)
        self.assertIn("/api/state", text)               # 旧版三段交代语义还在
        self.assertIn("POST /api/append", text)
        self.assertIn("teach.py view", text)
        self.assertNotIn("Read", text)                  # Claude 专有工具名不再出现
        self.assertEqual(prompt["params"]["prompt"][0]["type"], "text")

        # 拿到 sessionId 后照旧登记：body 与 ACP sessionId 一致
        self.assertEqual(sess.sid, STUB_SID)
        self.assertEqual(self.api.sessions[-1],
                         {"course": "semi", "chapter": "1", "sid": STUB_SID})

    def test_tool_call_becomes_one_log_line(self):
        sess = self.sess()
        sess.start()
        self.wait_prompts(1)
        self.assertTrue(wait_for(lambda: "[白砚工具]" in self.log_text()),
                        "没记工具日志：%s" % self.log_text())
        self.assertIn("[白砚工具] 读一下教材 (read/pending)", self.log_text())
        # 默认不记 agent_message_chunk：讲稿是白砚自己 POST /api/append 的事
        self.assertNotIn("[白砚说]", self.log_text())

    def test_message_chunk_logged_only_when_enabled(self):
        sess = self.sess(log_updates=True)
        sess.start()
        self.wait_prompts(1)
        self.assertTrue(wait_for(lambda: "[白砚说]" in self.log_text()),
                        "开了 LOG_UPDATES 却没记 chunk：%s" % self.log_text())
        self.assertIn("收到：", self.log_text())

    def test_seed_uses_state_api_for_chapter_meta(self):
        sess = self.sess()
        sess.start()
        self.wait_prompts(1)
        self.assertTrue(any("course=semi" in p for p in self.api.state_paths))
        self.assertIn("第一章 能带", self.prompts()[0])
        self.assertIn("半导体物理", self.prompts()[0])


class SystemPromptChannels(DriverCase):
    """人格的三条通道：meta（默认）/ args / both / none。"""

    def args_template(self):
        return '"%s" --persona {prompt} {prompt_file}' % STUB

    def test_args_channel_persona_is_single_argv_token(self):
        sess = self.sess(system_prompt="args", args=self.args_template())
        sess.start()
        self.wait_prompts(1)
        # 人格全文带换行/空格，但必须是一个 argv token
        self.assertEqual(sess.argv, [BIN_OK, STUB, "--persona", PERSONA, self.persona])
        new = find_frame(self.frames(), "session/new")
        self.assertNotIn("_meta", new["params"])        # 只走 argv，不再挂 _meta
        self.assertNotIn(MARKER, self.prompts()[0])

    def test_none_channel_has_neither(self):
        sess = self.sess(system_prompt="none", args=self.args_template())
        sess.start()
        self.wait_prompts(1)
        self.assertEqual(sess.argv, [BIN_OK, STUB, "--persona", "", ""])
        new = find_frame(self.frames(), "session/new")
        self.assertNotIn("_meta", new["params"])
        self.assertNotIn(MARKER, self.prompts()[0])

    def test_both_channel_gives_both(self):
        os.environ["STUB_REQUIRE_SYSTEM_PROMPT"] = "1"
        sess = self.sess(system_prompt="both", args=self.args_template())
        sess.start()
        self.wait_prompts(1)
        self.assertEqual(sess.argv, [BIN_OK, STUB, "--persona", PERSONA, self.persona])
        new = find_frame(self.frames(), "session/new")
        self.assertEqual(new["params"]["_meta"]["systemPrompt"], PERSONA)
        self.assertNotIn(MARKER, self.prompts()[0])

    def test_meta_append_also_uses_meta(self):
        os.environ["STUB_REQUIRE_SYSTEM_PROMPT"] = "1"
        sess = self.sess(system_prompt="meta_append")
        sess.start()
        self.wait_prompts(1)
        new = find_frame(self.frames(), "session/new")
        self.assertEqual(new["params"]["_meta"]["systemPrompt"], PERSONA)
        # 通道名只有空跑日志里会点（真起进程时走哪条路由 acp.py 决定，不重复记）
        self.sess(dry=True, bin_name="x", system_prompt="meta_append").start()
        self.assertIn("channel=meta_append", self.log_text())

class ResumeSession(DriverCase):
    """续会话：有 sid 走 session/load；续不上就丢掉旧 sid、另开新会话。"""

    def test_existing_sid_uses_session_load(self):
        self.write_sessions("old-sid-123")
        os.environ["STUB_LOAD_SESSION"] = "1"
        sess = self.sess()
        sess.start()
        self.wait_prompts(1)

        load = find_frame(self.frames(), "session/load")
        self.assertIsNotNone(load, "有 sid 却没走 session/load：%r" % (self.frames(),))
        self.assertEqual(load["params"]["sessionId"], "old-sid-123")
        self.assertEqual(load["params"]["_meta"]["systemPrompt"], PERSONA)
        self.assertIsNone(find_frame(self.frames(), "session/new"))
        self.assertEqual(sess.sid, "old-sid-123")
        self.assertEqual(self.api.sessions[-1]["sid"], "old-sid-123")
        self.assertIn("接着上次讲", self.prompts()[0])   # 续会话那套交代
        self.assertNotIn(MARKER, self.prompts()[0])
        self.assertTrue(sess.alive())                    # 重放的历史不致命

    def test_load_error_falls_back_to_new_session(self):
        self.write_sessions("gone-sid")
        os.environ["STUB_FAIL_LOAD"] = "1"               # stub 故意让 session/load 回 error
        sess = self.sess()
        sess.start()
        self.wait_prompts(1)

        frm = self.frames()
        self.assertIsNotNone(find_frame(frm, "session/load"))
        self.assertIsNotNone(find_frame(frm, "session/new"),
                             "load 失败后没回退 session/new：%r" % (frm,))
        self.assertEqual(sess.sid, STUB_SID)             # 落了新 sid
        self.assertEqual(self.api.sessions[-1]["sid"], STUB_SID)
        log = self.log_text()
        self.assertIn("旧会话 gone-sid 无法续", log)
        self.assertIn("另开新会话", log)
        self.assertTrue(sess.alive())                    # 回退不算启动失败
        self.assertIn("学生刚在讲课台唤醒", self.prompts()[0])   # 换成新开那套交代
        self.assertNotIn("接着上次讲", self.prompts()[0])

    def test_no_sid_means_session_new(self):
        os.environ["STUB_LOAD_SESSION"] = "1"            # agent 支持续，但我们没 sid
        sess = self.sess()
        sess.start()
        self.wait_prompts(1)
        self.assertIsNone(find_frame(self.frames(), "session/load"))
        self.assertIsNotNone(find_frame(self.frames(), "session/new"))


class InboxFeed(DriverCase):
    """收件箱：跨课/跨章过滤，本课本章每条只喂一次。"""

    def test_filters_and_feeds_once(self):
        sess = self.sess()
        sess.start()
        self.wait_prompts(1)                             # 开场白
        self.append_inbox(
            {"course": "semi", "chapter": "1", "text": "老师好"},
            {"course": "semi", "chapter": "2", "text": "别的章"},
            {"course": "signals", "chapter": "1", "text": "别的课"},
            {"course": "semi", "chapter": 1, "text": "章号是数字"},
            {"course": "semi", "chapter": "1", "text": "",
             "images": [{"path": "board-1.png"}]},
        )
        sess.poll_inbox()
        self.wait_prompts(4)

        joined = "\n".join(self.prompts())
        self.assertIn("学生：老师好", joined)
        self.assertIn("学生：章号是数字", joined)          # 数字章号也算本章
        self.assertIn("board-1.png", joined)             # 图片仍走路径别名
        self.assertIn("打开那张图看", joined)
        self.assertNotIn("Read", joined)
        self.assertIn("学生：（只发了图）", joined)
        self.assertNotIn("别的章", joined)
        self.assertNotIn("别的课", joined)

        sess.poll_inbox()                                # 同一份 inbox 不重喂
        time.sleep(0.4)
        self.assertEqual(len(self.prompts()), 4)
        self.assertEqual(sess.fed, 5)

    def test_start_swallows_old_inbox(self):
        self.append_inbox({"course": "semi", "chapter": "1", "text": "开讲前的老话"})
        sess = self.sess()
        sess.start()
        self.wait_prompts(1)
        sess.poll_inbox()
        time.sleep(0.3)
        self.assertEqual(len(self.prompts()), 1)         # 老话不重喂
        self.assertNotIn("开讲前的老话", "\n".join(self.prompts()))


class MainLoop(DriverCase):
    """主循环的一拍：起停、进程死后不重试、换章收旧。"""

    def test_dead_process_stops_chapter_without_retry(self):
        self.patch_live(("semi", "1"))
        cfg = self.cfg()
        driver.tick(cfg)
        sess = driver.G["sess"]
        self.assertIsNotNone(sess, "第一拍没起白砚")
        self.sessions.append(sess)
        self.wait_prompts(1)
        inits = count_frames(self.record, "initialize")

        sess.client.close()                              # 等价于白砚进程退出
        self.assertFalse(sess.alive())
        driver.tick(cfg)                                 # 发现死了：日志 + 清 sess/target
        self.assertIsNone(driver.G["sess"])
        self.assertIsNone(driver.G["target"])
        self.assertIn("白砚进程已退出", self.log_text())
        self.assertIn("本章先停", self.log_text())

        driver.tick(cfg)                                 # 同一 live 不自动重试
        driver.tick(cfg)
        self.assertEqual(count_frames(self.record, "initialize"), inits)
        self.assertIsNone(driver.G["sess"])
        self.assertIn("不自动重试", self.log_text())

    def test_switch_chapter_stops_old_session(self):
        live = {"v": ("semi", "1")}
        before = driver.current_live
        driver.current_live = lambda: live["v"]
        self.addCleanup(lambda: setattr(driver, "current_live", before))
        cfg = self.cfg()

        driver.tick(cfg)
        first = driver.G["sess"]
        self.sessions.append(first)
        self.wait_prompts(1)

        live["v"] = ("semi", "2")                        # 同一刻只许一章 live
        driver.tick(cfg)
        second = driver.G["sess"]
        self.sessions.append(second)
        self.assertIsNot(first, second)
        self.assertEqual((second.course, second.chapter), ("semi", "2"))
        self.assertFalse(first.alive())                  # 旧的收掉了

        live["v"] = None                                 # 台子空了：全收掉
        driver.tick(cfg)
        self.assertIsNone(driver.G["sess"])
        self.assertFalse(second.alive())
        self.assertTrue(wait_for(lambda: "收白砚" in self.log_text()))

    def test_start_failure_logs_command_and_does_not_storm(self):
        self.patch_live(("semi", "1"))
        calls = []

        def boom(*args, **kw):
            calls.append(args)
            raise OSError(2, "系统找不到指定的文件")

        before = driver.AcpClient
        driver.AcpClient = boom
        self.addCleanup(lambda: setattr(driver, "AcpClient", before))

        cfg = self.cfg()
        driver.tick(cfg)                                 # 起失败：记 dead、本章先不动
        self.assertIsNone(driver.G["sess"])
        driver.tick(cfg)
        driver.tick(cfg)
        self.assertEqual(len(calls), 1, "同一 live 起了不止一次：%r" % (calls,))
        log = self.log_text()
        self.assertIn("起白砚失败", log)
        self.assertIn("展开后命令", log)
        self.assertIn("BIN 只放程序本身", log)            # _start_hint 的提示
        self.assertIn("不会每 2 秒重试", log)


class DryRun(DriverCase):
    """空跑：不 spawn、不登记 sid、不写 inbox，但命令/通道/三帧要打全。"""

    def test_dry_does_not_spawn_or_touch_state(self):
        sess = self.sess(dry=True, bin_name="x")          # PATH 上没有 x：空跑照样放行
        sess.start()
        self.assertFalse(os.path.exists(self.record))     # stub 没被起过
        self.assertFalse(os.path.exists(self.inbox))      # 没写 inbox
        self.assertEqual(self.api.sessions, [])           # 空跑不登记 sid
        self.assertEqual(sess.sid, "(dry)")
        self.assertEqual(sess.fed, 0)
        self.assertTrue(sess.alive())                     # 空跑会话别被主循环收掉

        sess.feed("学生：在吗")                            # feed 只打日志
        self.assertFalse(os.path.exists(self.record))

        log = self.log_text()
        self.assertIn("会起白砚", log)
        self.assertIn("会执行", log)
        self.assertIn("system_prompt channel=meta", log)
        self.assertIn("clientCapabilities", log)
        self.assertIn("第一句 prompt 全文", log)
        self.assertIn("[DRY] 喂白砚：学生：在吗", log)
        frames = [json.loads(line.split("：", 1)[1]) for line in log.splitlines()
                  if "帧·" in line]
        self.assertEqual(len(frames), 3, "三帧没打全：%r" % (frames,))
        self.assertEqual(frames[0]["method"], "initialize")
        self.assertEqual(frames[0]["params"]["protocolVersion"], 1)
        self.assertEqual(frames[0]["params"]["clientCapabilities"],
                         {"fs": {"readTextFile": False, "writeTextFile": False},
                          "terminal": False})
        self.assertEqual(frames[1]["method"], "session/new")
        self.assertEqual(frames[1]["params"]["_meta"]["systemPrompt"], PERSONA)
        self.assertEqual(frames[2]["method"], "session/prompt")
        self.assertEqual(frames[2]["params"]["prompt"][0]["text"][:2], "学生")
        self.assertNotIn(MARKER, frames[2]["params"]["prompt"][0]["text"])

    def test_dry_preview_shows_load_when_sid_known(self):
        self.write_sessions("old-sid-9")
        sess = self.sess(dry=True, bin_name="x")
        sess.start()
        log = self.log_text()
        self.assertIn("帧·2 session/load", log)
        self.assertIn("续 old-sid-9", log)
        self.assertEqual(sess.sid, "old-sid-9")
        self.assertFalse(os.path.exists(self.record))

class ConfigErrors(ScratchCase):
    """check_config 的每条失败分支 + 空跑放行 + 未知占位符只告警一次。"""

    def test_load_config_rejects_bad_env(self):
        cases = [
            ({}, "没设"),
            ({"TEACH_AGENT_BIN": "python -m agent"}, "空白"),
            ({"TEACH_AGENT_BIN": BIN_OK, "TEACH_AGENT_ARGS": '--x "unclosed'}, "引号"),
            ({"TEACH_AGENT_BIN": BIN_OK, "TEACH_AGENT_SYSTEM_PROMPT": "yolo"},
             "SYSTEM_PROMPT"),
            ({"TEACH_AGENT_BIN": BIN_OK, "TEACH_AGENT_PERMISSION": "yolo"}, "PERMISSION"),
            ({"TEACH_AGENT_BIN": BIN_OK, "TEACH_AGENT_PROTOCOL": "2"}, "v2"),
            ({"TEACH_AGENT_BIN": "definitely-not-a-real-agent-xyz"}, "找不到"),
        ]
        for env, needle in cases:
            with self.assertRaises(driver.ConfigError, msg="env=%r" % (env,)) as cm:
                driver.load_config(env)
            self.assertIn(needle, str(cm.exception), "env=%r" % (env,))

    def test_check_config_branches_on_handmade_config(self):
        bad = [driver.Config(bin=""),
               driver.Config(bin="a b"),
               driver.Config(bin=BIN_OK, system_prompt_mode="yolo"),
               driver.Config(bin=BIN_OK, permission="yolo"),
               driver.Config(bin=BIN_OK, protocol="2"),
               driver.Config(bin="definitely-not-a-real-agent-xyz")]
        for cfg in bad:
            with self.assertRaises(driver.ConfigError, msg=repr(vars(cfg))):
                driver.check_config(cfg)

    def test_check_config_returns_none_for_good_config(self):
        cfg = driver.load_config({"TEACH_AGENT_BIN": BIN_OK})
        self.assertIsNone(driver.check_config(cfg))

    def test_dry_exempts_missing_bin(self):
        # 空跑的意义就是不装 agent 也能验配置，所以 PATH 上找不到也放行
        cfg = driver.load_config({"TEACH_AGENT_BIN": "x", "TEACH_DRY": "1"})
        self.assertIsNone(cfg.bin_path)
        self.assertTrue(cfg.dry)

    def test_unknown_placeholder_warns_once_and_keeps_token(self):
        env = {"TEACH_AGENT_BIN": BIN_OK, "TEACH_AGENT_ARGS": "--x {foo}"}
        cfg = driver.load_config(env)
        self.assertEqual(cfg.arg_tokens, ["--x", "{foo}"])
        self.assertIn("{foo}", self.log_text())
        driver.load_config(env)
        self.assertEqual(self.log_text().count("{foo}"), 1)

    def test_legacy_dry_name_falls_back(self):
        self.assertTrue(driver.read_dry({"DRIVER_DRY": "1"}))
        self.assertIn("TEACH_DRY", self.log_text())


class MainExit(ScratchCase):
    """main() 配置错就中文报错 + exit 2，不进主循环。"""

    def run_main(self, environ):
        logs = []
        before = driver.log
        driver.log = logs.append
        try:
            with self.assertRaises(SystemExit) as cm:
                driver.main(environ)
        finally:
            driver.log = before
        return cm.exception.code, "\n".join(str(x) for x in logs)

    def test_missing_bin(self):
        code, text = self.run_main({})
        self.assertEqual(code, 2)
        self.assertIn("配置错误", text)
        self.assertIn("TEACH_AGENT_BIN", text)
        self.assertIn("驱动器不启动", text)

    def test_bad_permission(self):
        code, text = self.run_main({"TEACH_AGENT_BIN": BIN_OK,
                                    "TEACH_AGENT_PERMISSION": "yolo"})
        self.assertEqual(code, 2)
        self.assertIn("TEACH_AGENT_PERMISSION", text)

    def test_bad_protocol(self):
        code, text = self.run_main({"TEACH_AGENT_BIN": BIN_OK, "TEACH_AGENT_PROTOCOL": "2"})
        self.assertEqual(code, 2)
        self.assertIn("v2", text)


class RecordingClient:
    """假 ACP 客户端：只记 __init__ 收到的参数，不起任何进程。

    用来断言驱动器**交给客户端的东西**（人格文本 / 子进程环境），不比 stub 起进程慢。
    """

    def __init__(self, *args, **kwargs):
        self.argv = args
        self.kwargs = kwargs
        self.starts = []

    def start(self, *, session_id=None, load_session=False):
        self.starts.append((session_id, load_session))
        return {"sessionId": "sess-fake", "resumed": False}

    def prompt(self, text, *, timeout=None):
        return {"stopReason": "end_turn"}

    def close(self):
        pass


class LauncherChannel(DriverCase):
    """launcher 通道：人格不进 _meta、不占 argv，只以 TEACH_PERSONA_FILE 交给启动器。"""

    def recording_sess(self, **kw):
        """起一个用假客户端顶包的会话，返回 (sess, 客户端列表)。"""
        made = []
        before = driver.AcpClient

        def factory(*args, **kwargs):
            client = RecordingClient(*args, **kwargs)
            made.append(client)
            return client

        driver.AcpClient = factory
        self.addCleanup(lambda: setattr(driver, "AcpClient", before))
        sess = self.sess(**kw)
        sess.start()
        return sess, made

    def test_launcher_sets_persona_file_env_and_skips_meta(self):
        sess, made = self.recording_sess(system_prompt="launcher")
        self.assertEqual(len(made), 1, "假客户端没被起过")
        kwargs = made[0].kwargs

        self.assertIsNone(kwargs["system_prompt"])      # 不挂 _meta
        path = kwargs["env"][driver.PERSONA_FILE_ENV]
        self.assertEqual(path, driver.PROMPT_FILE)
        self.assertTrue(os.path.isabs(path), "启动器只认绝对路径：%r" % (path,))
        with open(path, encoding="utf-8") as f:
            self.assertIn(MARKER, f.read())             # 指的就是这一份人格
        self.assertEqual(kwargs["env"]["TEACH_DRIVER"], "1")
        self.assertEqual(sess.sid, "sess-fake")

    def test_meta_default_never_sets_persona_env(self):
        # 反向锁：默认通道不许平白多出一个环境变量（其它 agent 的干净环境）
        _sess, made = self.recording_sess()
        self.assertNotIn(driver.PERSONA_FILE_ENV, made[0].kwargs["env"])
        self.assertEqual(made[0].kwargs["system_prompt"], PERSONA)

    def test_args_mode_never_sets_persona_env(self):
        _sess, made = self.recording_sess(system_prompt="args")
        self.assertNotIn(driver.PERSONA_FILE_ENV, made[0].kwargs["env"])
        self.assertIsNone(made[0].kwargs["system_prompt"])

    def test_dry_launcher_preview(self):
        sess = self.sess(system_prompt="launcher", dry=True, bin_name="x")
        sess.start()
        log = self.log_text()
        self.assertIn("%s=%s" % (driver.PERSONA_FILE_ENV, driver.PROMPT_FILE), log)
        self.assertIn("persona=launcher", log)
        self.assertIn("system_prompt channel=launcher meta_bytes=0", log)
        frames = [json.loads(line.split("：", 1)[1]) for line in log.splitlines()
                  if "帧·" in line]
        self.assertEqual(len(frames), 3, "三帧没打全：%r" % (frames,))
        self.assertNotIn("_meta", frames[1]["params"])   # launcher 下不挂 _meta
        self.assertEqual(sess.sid, "(dry)")


class NoLegacyProtocol(unittest.TestCase):
    """driver.py 源码扫描：旧协议/Claude 专有的字面量一个都不许留。"""

    LITERALS = ("--input-format", "--output-format", "stream-json",
                "--dangerously-skip-permissions", "--append-system-prompt")
    LEGACY_NAMES = ("TEACH_CLAUDE_BIN", "TEACH_CLAUDE_ARGS", "TEACH_CLAUDE_RESUME_FLAG",
                    "{resume}")

    def setUp(self):
        self.src = read_text(os.path.join(REPO, "driver.py"))

    def test_old_protocol_literals_count_is_zero(self):
        for literal in self.LITERALS:
            self.assertEqual(self.src.count(literal), 0,
                             "%s 在 driver.py 里出现 %d 次"
                             % (literal, self.src.count(literal)))

    def test_legacy_env_names_count_is_zero(self):
        for name in self.LEGACY_NAMES:
            self.assertEqual(self.src.count(name), 0, "%s 还在 driver.py 里" % name)

    def test_default_agent_args_is_empty(self):
        # 默认不带任何 flag：起什么 agent、带什么参数全由环境变量说了算
        self.assertEqual(driver.DEFAULT_AGENT_ARGS, "")
        self.assertEqual(driver.load_config({"TEACH_AGENT_BIN": BIN_OK}).arg_tokens, [])


if __name__ == "__main__":
    unittest.main()
