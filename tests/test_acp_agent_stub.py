#!/usr/bin/env python3
"""ACP 测试用 stub agent：纯标准库、可直接执行。

被 [sys.executable, 本文件] 拉起。默认是个很乖的「讲课台 agent」：
initialize 报 v1 + 能力，session/new|load 给 sess-stub，session/prompt 先喷一串
session/update（agent_message_chunk / tool_call / tool_call_update / plan），
再按 STUB_ASK_PERMISSION 决定要不要请求一次权限，最后回 end_turn。

环境变量（全部可选）：
  STUB_PROTOCOL_VERSION       默认 1    initialize 回的版本号（测版本不符）
  STUB_LOAD_SESSION           默认 0    init 里 loadSession 能力真假
  STUB_EXIT_AFTER_INIT        默认 0    1 = 回完 initialize 立刻退出
  STUB_SILENT                 默认 0    1 = 什么都不回（测超时）
  STUB_ASK_PERMISSION         默认 1    prompt 时先发 session/request_permission
  STUB_PERMISSION_OPTIONS     默认 normal  normal | none | allow-only
                                        （后两个测「没有可选 option」的退化）
  STUB_GARBAGE_LINES          默认 0    每次响应前先往 stdout 写 N 行非 JSON
  STUB_REQUIRE_SYSTEM_PROMPT  默认 0    1 = 缺人格就报错 exit 3（反向锁）
  STUB_USAGE                  默认 0    1 = 多插一帧 usage_update
  STUB_FAIL_LOAD              默认 0    1 = session/load 回 JSON-RPC error
                                        （测「error 响应 -> AcpError」与失败后能否重来）
  STUB_UNKNOWN_REQUEST        默认 0    1 = prompt 时发一个未知方法请求，等客户端
                                        回错再收尾（测 -32601）
  STUB_ECHO_PROMPT            默认 0    1 = 在 prompt 结果里带回收到的文本
                                        （测并发 prompt 按 id 配对）
  STUB_PROMPT_BARRIER         默认 0    N>1 时攒够 N 个 session/prompt 才一起回，
                                        且倒着回（测两个 prompt 真同时在飞）
  STUB_RECORD                          文件路径：收到的每一帧原始 JSON 逐行追加
  STUB_LOG                             文件路径：调试日志

所有等待都有秒级上限（READ_TIMEOUT），测试宁可失败也不能挂死。
"""
import json
import os
import queue
import sys
import threading

READ_TIMEOUT = 10.0
SESSION_ID = "sess-stub"
PROMPT_ID_BASE = 9000          # stub 自己发请求用的 id，跟客户端的 1..N 错开
TOOL_CALL = {"toolCallId": "call-1", "title": "读一下教材", "kind": "read"}
PERMISSION_OPTIONS = {
    "normal": [
        {"optionId": "allow-once", "name": "允许一次", "kind": "allow_once"},
        {"optionId": "reject-once", "name": "拒绝一次", "kind": "reject_once"},
        {"optionId": "allow-always", "name": "总是允许", "kind": "allow_always"},
    ],
    "none": [],
    "allow-only": [
        {"optionId": "allow-once", "name": "允许一次", "kind": "allow_once"},
    ],
}
UPDATES = ("agent_message_chunk", "tool_call", "tool_call_update", "plan")


def env(name, default=""):
    """读环境变量；没设或设成空串都算没设。"""
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def env_int(name, default):
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def fix_encoding():
    """管道默认跟 Windows ANSI 代码页走，协议要求 UTF-8；顺手把换行锁成 \n。"""
    for stream, kw in ((sys.stdin, {"encoding": "utf-8"}),
                       (sys.stdout, {"encoding": "utf-8", "newline": "\n"}),
                       (sys.stderr, {"encoding": "utf-8", "errors": "replace"})):
        try:
            stream.reconfigure(**kw)
        except Exception:
            pass


def log(msg):
    path = os.environ.get("STUB_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")
    except Exception:
        pass


_RECORD_WARNED = []


def record(raw):
    """把收到的一帧原样追加下来，测试拿它断言客户端到底发了什么。"""
    path = os.environ.get("STUB_RECORD")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(raw + "\n")
    except Exception as e:
        # 记不了帧不能让 agent 死，但必须吼一声：不然测试只看到「记录是空的」。
        if not _RECORD_WARNED:
            _RECORD_WARNED.append(True)
            sys.stderr.write(f"stub: STUB_RECORD 写不了（{path}）：{e!r}\n")
            sys.stderr.flush()


class Stub:
    def __init__(self):
        self.protocol_version = env_int("STUB_PROTOCOL_VERSION", 1)
        self.load_session = env("STUB_LOAD_SESSION") == "1"
        self.exit_after_init = env("STUB_EXIT_AFTER_INIT") == "1"
        self.ask_permission = env("STUB_ASK_PERMISSION", "1") == "1"
        self.permission_options = PERMISSION_OPTIONS.get(
            env("STUB_PERMISSION_OPTIONS", "normal"), PERMISSION_OPTIONS["normal"])
        self.garbage = env_int("STUB_GARBAGE_LINES", 0)
        self.require_system_prompt = env("STUB_REQUIRE_SYSTEM_PROMPT") == "1"
        self.usage = env("STUB_USAGE") == "1"
        self.fail_load = env("STUB_FAIL_LOAD") == "1"
        self.unknown_request = env("STUB_UNKNOWN_REQUEST") == "1"
        self.echo_prompt = env("STUB_ECHO_PROMPT") == "1"
        self.barrier = env_int("STUB_PROMPT_BARRIER", 0)

        self.next_id = PROMPT_ID_BASE
        self.ready = {}          # prompt 请求 id -> 收到的文本
        self.blocked = {}        # prompt 请求 id -> 还欠几个响应
        self.pending = {}        # 我发出的请求 id -> 对应哪个 prompt

    # ---- 发 ----

    def write(self, frame):
        sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def notify(self, method, params):
        self.write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params):
        rid = self.next_id
        self.next_id += 1
        self.write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return rid

    def garbage_lines(self):
        for i in range(self.garbage):
            sys.stdout.write(f"这不是 JSON 行 #{i + 1}\n")
        sys.stdout.flush()

    def respond(self, rid, result):
        self.garbage_lines()             # 垃圾行在响应之前：测「脏 stdout 不致命」
        self.write({"jsonrpc": "2.0", "id": rid, "result": result})

    def respond_error(self, rid, code, message):
        self.garbage_lines()
        self.write({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    def update(self, session_id, name, **extra):
        # 形参别叫 kind：update 体里本来就有 kind 字段（tool_call 用），会撞名。
        body = {"sessionUpdate": name}
        body.update(extra)
        self.notify("session/update", {"sessionId": session_id, "update": body})

    # ---- 收 ----

    def handle(self, frame):
        method = frame.get("method")
        rid = frame.get("id")
        params = frame.get("params")
        if not isinstance(params, dict):
            params = {}
        if method == "initialize":
            self.on_initialize(rid, params)
        elif method in ("session/new", "session/load"):
            self.on_session(method, rid, params)
        elif method == "session/prompt":
            self.on_prompt(rid, params)
        elif method is not None:
            log(f"客户端来了没见过的请求：{method}")
            self.respond_error(rid, -32601, f"method not found: {method}")
        elif rid is not None:
            self.on_response(rid, frame)
        else:
            log(f"既无 method 也无 id 的帧，丢弃：{frame!r}")

    def on_initialize(self, rid, params):
        self.respond(rid, {
            "protocolVersion": self.protocol_version,
            "agentCapabilities": {
                "loadSession": self.load_session,
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False},
            },
            "agentInfo": {"name": "stub-acp", "version": "0.0.1"},
            "authMethods": [],
        })
        if self.exit_after_init:
            log("按 STUB_EXIT_AFTER_INIT 退出")
            sys.exit(0)

    def on_session(self, method, rid, params):
        if self.require_system_prompt:
            prompt = (params.get("_meta") or {}).get("systemPrompt")
            if not isinstance(prompt, str) or prompt == "":
                sys.stderr.write("stub: session 请求里没有非空字符串 _meta.systemPrompt，"
                                 "驱动器没把人格发出来\n")
                sys.stderr.flush()
                sys.exit(3)
        if method == "session/load" and self.fail_load:
            self.respond_error(rid, -32600, "session/load 失败（stub 故意）")
            return
        if method == "session/load":
            # 回响应之前先重放历史：客户端的 replay 窗口要盖住这几帧。
            session_id = params.get("sessionId")
            for i in (1, 2):
                self.update(session_id, "agent_message_chunk",
                            content={"type": "text", "text": f"历史重放 {i}"})
            self.respond(rid, {})
        else:
            self.respond(rid, {"sessionId": SESSION_ID})

    def on_prompt(self, rid, params):
        session_id = params.get("sessionId")
        items = params.get("prompt")
        text = ""
        if isinstance(items, list) and items and isinstance(items[0], dict):
            text = items[0].get("text") or ""
        self.update(session_id, "agent_message_chunk",
                    content={"type": "text", "text": f"收到：{text}"})
        self.update(session_id, "tool_call", toolCallId=TOOL_CALL["toolCallId"],
                    title=TOOL_CALL["title"], kind=TOOL_CALL["kind"], status="pending")
        self.update(session_id, "tool_call_update", toolCallId=TOOL_CALL["toolCallId"],
                    status="completed")
        self.update(session_id, "plan", entries=[{"content": "讲一段", "status": "completed"}])
        if self.usage:
            self.update(session_id, "usage_update", used=123, size=1000)

        self.ready[rid] = text
        self.blocked[rid] = 0
        if self.ask_permission:
            self.blocked[rid] += 1
            self.pending[self.request("session/request_permission", {
                "sessionId": session_id,
                "toolCall": dict(TOOL_CALL),
                "options": [dict(o) for o in self.permission_options],
            })] = rid
        if self.unknown_request:
            self.blocked[rid] += 1
            self.pending[self.request("session/unknown_thing", {"sessionId": session_id})] = rid
        self.maybe_finish()

    def on_response(self, rid, frame):
        prompt_id = self.pending.pop(rid, None)
        if prompt_id is None:
            log(f"收到没登记的响应 id={rid}")
            return
        log(f"响应 id={rid} -> prompt {prompt_id}：{frame.get('result') or frame.get('error')}")
        self.blocked[prompt_id] = max(0, self.blocked.get(prompt_id, 0) - 1)
        self.maybe_finish()

    def maybe_finish(self):
        """所有欠账都清了才收尾；barrier>1 时先攒够 N 个 prompt（测并发）。"""
        if self.barrier > 1 and len(self.ready) < self.barrier:
            return
        done = sorted([rid for rid, n in self.blocked.items() if n == 0], reverse=True)
        for rid in done:
            result = {"stopReason": "end_turn"}
            if self.echo_prompt:
                result["promptText"] = self.ready.pop(rid)
            else:
                self.ready.pop(rid, None)
            self.blocked.pop(rid, None)
            self.respond(rid, result)


def pump_stdin(q):
    try:
        for line in sys.stdin:
            q.put(line)
    except Exception:
        pass
    q.put(None)          # EOF：主循环收到就退


def main():
    fix_encoding()
    if os.environ.get("STUB_SILENT") == "1":
        # 一个字都不回：客户端靠超时收场，测试关 stdin 后我们退。
        for _ in sys.stdin:
            pass
        return 0
    log("stub 起来")
    q = queue.Queue()
    threading.Thread(target=pump_stdin, args=(q,), daemon=True).start()
    stub = Stub()
    while True:
        try:
            line = q.get(timeout=READ_TIMEOUT)
        except queue.Empty:
            log(f"等客户端帧超过 {READ_TIMEOUT}s，退出")
            return 0
        if line is None:
            log("stdin EOF，退出")
            return 0
        line = line.strip()
        if not line:
            continue
        record(line)
        try:
            frame = json.loads(line)
        except Exception:
            log(f"收到非 JSON 帧，跳过：{line[:120]}")
            continue
        if isinstance(frame, dict):
            stub.handle(frame)
        else:
            log(f"收到非对象帧，跳过：{line[:120]}")


if __name__ == "__main__":
    sys.exit(main())
