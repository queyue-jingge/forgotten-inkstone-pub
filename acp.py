#!/usr/bin/env python3
"""讲课台 · ACP（Agent Client Protocol）客户端：纯标准库。

一个 AcpClient 管一个说 ACP v1 的 agent 子进程：

    c = AcpClient(TEACH_AGENT_BIN, args, cwd=ROOT, system_prompt=人格)
    c.start(session_id=sid, load_session=True)   # 握手 + 顺手开/续一个会话
    c.prompt("学生：……")                          # 一轮话，等它讲完
    c.close()

线协议：JSON-RPC 2.0、一行一帧（换行符结尾）、UTF-8。agent 的 stdout 只走 ACP
消息，日志一律 stderr（我照样收下来转给 on_log）。

为什么单开一个 reader 线程：session/prompt 是「一轮讲完才回」的长请求，读和等必须
分开；而且 agent 会反过来请求我们（session/request_permission），读到那一行就得当
场答，否则两头一起僵住。
"""
import json
import os
import subprocess
import sys
import threading

PROTOCOL_VERSION = 1

# 不广告 fs / terminal 能力：白砚要动文件自己走 shell，少一个要维护的权限面。
# 这个字面量会（拷贝一份）进 initialize.params.clientCapabilities；driver 空跑
# 时也可以直接引用它来打印，免得两边各抄一份慢慢漂移。
CLIENT_CAPABILITIES = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
}


class AcpError(Exception):
    """ACP 层错误：版本不符、agent 报错、连接断了、用法不对。"""


class AcpTimeout(AcpError):
    """等 agent 的响应超时。"""


def _log_stderr(line):
    """默认日志出口：stderr。stdout 是协议通道，日志绝不能混进去。"""
    try:
        sys.stderr.write(str(line).rstrip() + "\n")
        sys.stderr.flush()
    except Exception:
        pass


class _Pending:
    """一个在飞请求的槽位：reader 线程填好 result/error 再 set，等待线程 wait。"""

    __slots__ = ("method", "event", "result", "error")

    def __init__(self, method):
        self.method = method
        self.event = threading.Event()
        self.result = None
        self.error = None


class AcpClient:
    """一个 ACP v1 agent 子进程的同步客户端。

    线程安全：多个 session/prompt 可以同时在飞，响应按 JSON-RPC id 各回各家
    （决不 FIFO 配对，否则两轮并发就串线）。
    """

    # 客户端自我介绍，原样进 initialize.params.clientInfo。
    CLIENT_INFO = {"name": "teach-platform", "title": "讲课台", "version": "0.2.0"}

    def __init__(self, bin_name, argv, *, cwd, env=None, system_prompt=None,
                 on_permission=None, on_update=None, on_log=None,
                 init_timeout=60.0):
        """bin_name + argv 拼成完整命令行（argv 里不放程序名）。

        on_permission(session_id, tool_call, options) -> "allow" | "deny" | "cancel"
            异常一律按 deny（拿不准就别放行）。
        on_update(session_id, update, replay)
            replay=True 表示这帧来自 session/load 的历史重放窗口。
        on_log(line)  默认写 stderr。
        init_timeout  握手 / 开会话的等待上限（秒）；prompt 自己带 timeout。
        """
        self.bin_name = bin_name
        self.argv = list(argv or [])
        self.cwd = cwd
        self.env = dict(env or {})
        self.system_prompt = system_prompt
        self.on_permission = on_permission
        self.on_update = on_update
        self.on_log = on_log if on_log is not None else _log_stderr
        self.init_timeout = float(init_timeout)

        self.proc = None
        self.session_id = None
        self.protocol_version = None
        self.agent_info = None
        self.agent_capabilities = {}
        self.load_session = False          # 最近一次 start() 是不是走的续会话

        self._initialized = False
        self._replay = False               # session/load 重放窗口开着？
        self._closed = False
        self._dead = None                  # 管道断掉的原因；置上后新请求直接失败
        self._close_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._next_id = 1                  # 我方请求 id：从 1 开始自增
        self._pending = {}                 # id -> _Pending
        self._pending_lock = threading.Lock()

    # ---------------------------------------------------------------- 对外

    def start(self, *, session_id=None, load_session=False):
        """握一次手，然后开新会话或续旧会话；返回会话信息 dict。

        session_id 给了但 load_session=False 就照样开新的：要不要「续」由调用方
        明确说，别因为顺手带了个 id 就把旧上下文捞回来。

        返回 {"sessionId", "resumed", "protocolVersion", "agentInfo", "loadSession"}。
        """
        if self._closed:
            raise AcpError("连接已关闭，要重来请新建 AcpClient")

        if self.proc is None or self.proc.poll() is not None:
            self._spawn()                  # 进程没了（或还没起）就重开
        if not self._initialized:
            self._initialize()

        meta = self._meta()
        cwd = os.path.abspath(self.cwd if self.cwd else os.getcwd())
        resumed = bool(session_id and load_session)
        if resumed:
            params = {"sessionId": session_id, "cwd": cwd, "mcpServers": []}
        else:
            params = {"cwd": cwd, "mcpServers": []}
        if meta is not None:
            params["_meta"] = meta

        if resumed:
            # 重放窗口：agent 会在回响应之前先灌一串 session/update（历史）。
            # 响应一到就关窗——之后再有 update 就是本轮的新内容了。
            self._replay = True
            try:
                self._request("session/load", params, timeout=self.init_timeout)
            finally:
                self._replay = False
            new_sid = session_id
        else:
            res = self._request("session/new", params, timeout=self.init_timeout)
            new_sid = res.get("sessionId") if isinstance(res, dict) else None
            if not new_sid:
                raise AcpError(f"session/new 没给 sessionId：{res!r}")

        self.session_id = str(new_sid)
        self.load_session = resumed
        return {
            "sessionId": self.session_id,
            "resumed": resumed,
            "protocolVersion": self.protocol_version,
            "agentInfo": self.agent_info,
            "loadSession": bool(self.agent_capabilities.get("loadSession")),
        }

    def prompt(self, text, *, timeout=None):
        """喂一轮话，等这一轮讲完；返回 {"stopReason": ...}。

        timeout=None（默认）就一直等：一轮可能讲很久，进程一断 reader 线程会立刻
        把等待者叫醒，不会真挂死。
        """
        if self.session_id is None:
            raise AcpError("还没 start()，没有 sessionId")
        params = {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": text}],
        }
        res = self._request("session/prompt", params, timeout=timeout)
        if not isinstance(res, dict) or "stopReason" not in res:
            raise AcpError(f"session/prompt 响应不合法（缺 stopReason）：{res!r}")
        return res

    def alive(self):
        """进程还活着、且没 close() 过。"""
        return (not self._closed) and self.proc is not None and self.proc.poll() is None

    @property
    def returncode(self):
        """agent 进程的退出码：还在跑 / 没起过都给 None（打日志用）。"""
        return None if self.proc is None else self.proc.poll()

    def close(self):
        """收掉 agent：关 stdin → terminate → 最多等 6s → kill。幂等。"""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._fail_all(AcpError("连接已关闭"))
            proc = self.proc
        if proc is None:
            return

        try:
            if proc.stdin is not None:
                proc.stdin.close()          # 先给个 EOF，让它自己体面地退
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        pass
        except Exception as e:
            self._log(f"收 agent 进程时出错（忽略）：{e!r}")
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    # ---------------------------------------------------------------- 内部

    def _log(self, msg):
        try:
            self.on_log(msg)
        except Exception:
            pass

    def _meta(self):
        """人格正文挂在 params._meta.systemPrompt 上（字符串形态）；空就不挂。"""
        if isinstance(self.system_prompt, str) and self.system_prompt != "":
            return {"systemPrompt": self.system_prompt}
        return None

    def _spawn(self):
        cmd = [self.bin_name] + list(self.argv)
        env = dict(os.environ)
        env.update(self.env)
        try:
            proc = subprocess.Popen(
                cmd, cwd=self.cwd, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
        except Exception as e:
            raise AcpError(f"起不了 agent（{self.bin_name}）：{e}") from e
        self.proc = proc
        self._initialized = False
        self._replay = False
        self._dead = None
        self.session_id = None
        self.protocol_version = None
        self.agent_info = None
        self.agent_capabilities = {}
        threading.Thread(target=self._reader, args=(proc,), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True).start()

    def _initialize(self):
        res = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            # 现拷一份：调用方改了常量也不该影响已经发出去的帧。
            "clientCapabilities": {
                "fs": dict(CLIENT_CAPABILITIES["fs"]),
                "terminal": CLIENT_CAPABILITIES["terminal"],
            },
            "clientInfo": dict(self.CLIENT_INFO),
        }, timeout=self.init_timeout)
        if not isinstance(res, dict):
            raise AcpError(f"initialize 响应不是对象：{res!r}")
        got = res.get("protocolVersion")
        if got != PROTOCOL_VERSION:
            raise AcpError(f"该 agent 说的是 ACP v{got}，本客户端只认 "
                           f"ACP v{PROTOCOL_VERSION}")
        self.protocol_version = got
        info = res.get("agentInfo")
        self.agent_info = info if isinstance(info, dict) else None
        caps = res.get("agentCapabilities")
        self.agent_capabilities = caps if isinstance(caps, dict) else {}
        self._initialized = True

    def _alloc_id(self):
        with self._id_lock:
            rid = self._next_id
            self._next_id += 1
        return rid

    def _request(self, method, params, timeout):
        """发一个请求并等它的响应；超时 -> AcpTimeout，连接断/报错 -> AcpError。"""
        if self._closed:
            raise AcpError("连接已关闭")
        rid = self._alloc_id()
        slot = _Pending(method)
        with self._pending_lock:
            # 登记和「reader 是不是已经收摊」在同一把锁里判：reader 断线时也是拿着
            # 这把锁扫在飞表，所以不会出现「登记完没人管、干等到超时」的空档。
            if self._dead is not None:
                raise AcpError(str(self._dead))
            self._pending[rid] = slot      # 先登记再发，免得响应比登记还快
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except Exception:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise
        if not slot.event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise AcpTimeout(f"等 {method} 响应超时（{timeout}s）")
        if slot.error is not None:
            raise slot.error
        return slot.result

    def _send(self, frame):
        """写一帧。JSON 里不会有裸换行（换行都被转义了），所以必然是「一行一帧」。"""
        line = json.dumps(frame, ensure_ascii=False)
        with self._write_lock:
            proc = self.proc
            if self._closed or proc is None or proc.stdin is None:
                raise AcpError("连接已关闭")
            try:
                proc.stdin.write(line + "\n")
                proc.stdin.flush()
            except Exception as e:
                raise AcpError(f"写 agent stdin 失败：{e}") from e

    def _fail_all(self, err, *, dead=False):
        """把所有等待者叫醒（各自一份异常对象，别共享 traceback）。

        dead=True 表示管道断了：之后新发的请求一律直接失败，不许干等超时。
        """
        with self._pending_lock:
            if dead:
                self._dead = err
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot.error = AcpError(str(err))
            slot.event.set()

    # ---- 读侧：一条线程管 stdout ----

    def _reader(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    # 垃圾行只记一笔：agent 的日志串到 stdout 是常事，不能因此断线。
                    self._log(f"agent stdout 有非 JSON 行，跳过：{line[:200]}")
                    continue
                if not isinstance(obj, dict):
                    self._log(f"agent stdout 有非对象帧，跳过：{line[:200]}")
                    continue
                try:
                    self._dispatch(obj)
                except Exception as e:
                    self._log(f"处理 agent 帧出错（跳过）：{e!r}")
        except Exception as e:
            self._log(f"读 agent stdout 中断：{e!r}")
        finally:
            # 管道断了：在飞的请求全判失败，之后的新请求也直接失败，
            # 别让调用方干等到超时才发现 agent 早没了。
            self._fail_all(AcpError("agent 连接断了"), dead=True)

    def _dispatch(self, obj):
        method = obj.get("method")
        rid = obj.get("id")
        params = obj.get("params")
        if not isinstance(params, dict):
            params = {}
        if method is not None:
            if rid is not None:
                self._on_agent_request(rid, method, params)     # agent -> 我们，要回
            else:
                self._on_notification(method, params)
        elif rid is not None:
            self._on_response(rid, obj)
        else:
            self._log(f"收到既无 method 也无 id 的帧，丢弃：{obj!r}")

    def _on_response(self, rid, obj):
        with self._pending_lock:
            slot = self._pending.pop(rid, None)
        if slot is None:
            self._log(f"收到没有对应请求的响应，丢弃：id={rid}")
            return
        err = obj.get("error")
        if err is not None:
            code = err.get("code") if isinstance(err, dict) else None
            msg = err.get("message") if isinstance(err, dict) else str(err)
            slot.error = AcpError(f"{slot.method} 被 agent 拒了：code={code} message={msg}")
        else:
            slot.result = obj.get("result")
        slot.event.set()

    def _on_notification(self, method, params):
        if method == "session/update":
            update = params.get("update")
            if not isinstance(update, dict):
                self._log(f"session/update 没有 update 对象，跳过：{params!r}")
                return
            if self.on_update is not None:
                try:
                    self.on_update(params.get("sessionId"), update, self._replay)
                except Exception as e:
                    self._log(f"on_update 抛错（忽略）：{e!r}")
        else:
            self._log(f"没见过的通知，忽略：{method}")

    def _on_agent_request(self, rid, method, params):
        if method != "session/request_permission":
            self._send({"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32601, "message": f"method not found: {method}"}})
            self._log(f"agent 请求了不认识的方法，已回 -32601：{method}")
            return
        tool_call = params.get("toolCall")
        if not isinstance(tool_call, dict):
            tool_call = {}
        options = params.get("options")
        if not isinstance(options, list):
            options = []
        outcome = self._decide(params.get("sessionId"), tool_call, options)
        self._send({"jsonrpc": "2.0", "id": rid, "result": {"outcome": outcome}})

    def _decide(self, session_id, tool_call, options):
        """把 on_permission 的三态翻成 ACP 的 outcome。

        allow/deny 都要挑一个 optionId 回给 agent；挑不到（没有对应 kind 的选项）
        就退化成 cancelled——报「选了」但 id 是空的，agent 那边只会更糊涂。
        """
        if self.on_permission is None:
            choice = "deny"                 # 没人接这个球：保守拒绝，别默认放行
            self._log("没有 on_permission，按 deny 处理")
        else:
            try:
                choice = self.on_permission(session_id, tool_call, options)
            except Exception as e:
                choice = "deny"
                self._log(f"on_permission 抛错，按 deny 处理：{e!r}")
        if choice not in ("allow", "deny", "cancel"):
            self._log(f"on_permission 回了 {choice!r}，按 deny 处理")
            choice = "deny"
        if choice == "cancel":
            return {"outcome": "cancelled"}
        want = "allow" if choice == "allow" else "reject"
        for opt in options:
            if not isinstance(opt, dict):
                continue
            kind = opt.get("kind")
            option_id = opt.get("optionId")
            if isinstance(kind, str) and kind.startswith(want) and option_id:
                return {"outcome": "selected", "optionId": option_id}
        return {"outcome": "cancelled"}

    def _drain_stderr(self, proc):
        """agent 的日志（stderr）转到 on_log；不读会把管道塞满卡死对方。"""
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    self._log(f"[agent] {line}")
        except Exception:
            pass
