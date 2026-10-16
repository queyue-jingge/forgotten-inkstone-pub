#!/usr/bin/env python3
"""讲课台 · 后端驱动器（ACP 版）。

一个常驻进程管所有章，同一刻只醒一章（跟各课 state 的 live 走）：
  · 盯 state/<course>.json 的 live：变到某章 → 拉起白砚；变空 → 收掉
  · 开过的章（sessions.json 有 sid）→ ACP session/load 续上；没开过 → session/new 并塞一句交代
  · 网页新话落进 inbox.jsonl → 喂给当前白砚
  · 白砚自己用讲课台接口（/api/append 等）写回讲稿，本驱动器不管它的输出内容

白砚 = 一个由 TEACH_AGENT_BIN / TEACH_AGENT_ARGS 指定的、说 ACP 的常驻子进程
（stdio 上的 JSON-RPC 2.0，v1；协议细节都在 acp.py 里）。

人格 prompts/白砚.md 默认走**系统提示通道**：acp.py 把全文挂在 session/new 与
session/load 的 _meta.systemPrompt 上，绝不塞进第一句用户消息。通道由
TEACH_AGENT_SYSTEM_PROMPT 选（meta 默认 / meta_append / args / both / none）。

跑：   TEACH_AGENT_BIN=<你的 ACP agent> TEACH_BASE=http://127.0.0.1:8000 python3 driver.py
空跑： TEACH_DRY=1 TEACH_AGENT_BIN=x python3 driver.py    # 不 spawn，只打命令/通道/三帧
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request

try:
    import acp
except ImportError as _acp_err:      # acp.py 没落盘/没放对地方：先记下来，别在 import 就炸
    acp, _ACP_ERROR = None, _acp_err
else:
    _ACP_ERROR = None

# AcpClient 引进本模块命名空间：测试要换成假客户端时换 driver.AcpClient 就行，不用碰 acp.py。
AcpClient = getattr(acp, "AcpClient", None)
# 下面几个常量正常从 acp.py 取；它不在时给一份兜底，只为空跑预览还能把三帧打出来。
PROTOCOL_VERSION = getattr(acp, "PROTOCOL_VERSION", 1)
CLIENT_INFO = getattr(AcpClient, "CLIENT_INFO",
                      {"name": "teach-platform", "title": "讲课台", "version": "0.2.0"})

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(ROOT, "state")
SESSIONS = os.path.join(STATE, "sessions.json")
INBOX = os.path.join(ROOT, "inbox.jsonl")
PROMPT_FILE = os.environ.get("TEACH_PROMPT") or os.path.join(ROOT, "prompts", "白砚.md")
if not os.path.isabs(PROMPT_FILE):
    PROMPT_FILE = os.path.join(ROOT, PROMPT_FILE)
LOG = os.path.join(ROOT, "driver.log")
BASE = os.environ.get("TEACH_BASE", "http://127.0.0.1:8000")

AGENT_INIT_TIMEOUT = 60.0   # 冷启动一个 agent 可能要装插件、拉配置，给足

# clientCapabilities：acp.py 里有同名常量就优先用它，免得这里抄一份慢慢漂移。
CLIENT_CAPABILITIES = getattr(acp, "CLIENT_CAPABILITIES", {
    "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False})


class ConfigError(Exception):
    """启动阶段的配置错误：报错后以 exit code 2 退出，不进主循环。"""


# ---- agent 配置：起哪个 agent、带哪些 flag 全由环境变量决定 ----

DEFAULT_AGENT_ARGS = ""
PLACEHOLDERS = ("prompt", "prompt_file", "session")
SYSTEM_PROMPT_MODES = ("meta", "meta_append", "args", "both", "none")
PERMISSION_MODES = ("allow", "deny", "cancel")
# 人格走 _meta 的三种取值（meta_append 的差别只在本侧日志里：acp.py 只收一个字符串）
META_MODES = ("meta", "meta_append", "both")
_KNOWN = "、".join("{" + p + "}" for p in PLACEHOLDERS)
_BRACED = re.compile(r"\{([^{}]*)\}")


class Config:
    """一份算好的 agent 配置。

    load_config() 造它、check_config() 校验它；字段都是死的，测试可以手搓一份坏的
    直接喂 check_config，不必去动环境变量。
    """

    def __init__(self, bin="", args_raw=DEFAULT_AGENT_ARGS, system_prompt_mode="meta",
                 permission="allow", log_updates=False, protocol="1", dry=False):
        self.bin = bin                    # 环境变量里的原文（未解析）
        self.bin_path = None              # resolve_bin 出来的路径；PATH 上找不到则 None
        self.args_raw = args_raw          # 原始串（报错/日志要打原文）
        self.arg_tokens = []              # split_args 切好的模板 token
        self.system_prompt_mode = system_prompt_mode
        self.permission = permission
        self.log_updates = log_updates
        self.protocol = protocol
        # dry 放进 Config 是为了 check_config：空跑的意义就是不装 agent 也能验配置，
        # 所以空跑时放行「PATH 上找不到程序」。
        self.dry = dry


def split_args(s):
    """把 TEACH_AGENT_ARGS 切成一串 token。

    按空白切分；成对的 " 把含空格的片段括成一项且引号本身去掉；
    反斜杠不作转义（Windows 路径里的反斜杠原样保留，也不给空格转义）；
    引号不配对 → ConfigError。
    """
    tokens = []
    cur = []
    started = False
    in_quote = False
    for ch in s:
        if ch == '"':
            in_quote = not in_quote
            started = True
        elif ch.isspace() and not in_quote:
            if started:
                tokens.append("".join(cur))
                cur = []
                started = False
        else:
            cur.append(ch)
            started = True
    if in_quote:
        raise ConfigError(f"TEACH_AGENT_ARGS 里的引号不配对：{s}")
    if started:
        tokens.append("".join(cur))
    return tokens


def expand_args(tokens, *, prompt, prompt_file, session):
    """展开 3 个占位符；其余 {xxx} 原样保留（启动时统一告警一次）。

    {prompt} 展开成**一个** token：人格全文带空白也整块传，不按空白再切。
    """
    out = []
    for tok in tokens:
        if tok == "{prompt}":
            out.append(prompt)
        elif tok == "{prompt_file}":
            out.append(prompt_file)
        elif tok == "{session}":
            out.append(session or "")
        else:
            out.append(tok)
    return out


def resolve_bin(name):
    """PATH 上找可执行文件（Windows 认 PATHEXT：npm 装的 CLI 是 .CMD）；找不到给 None。"""
    if not name:
        return None
    try:
        return shutil.which(name)
    except Exception:
        return None


def check_config(cfg):
    """校验一份 Config：不合法 → ConfigError（main 捕获后打中文说明、exit 2）。

    跟 load_config 分开，是为了能拿手搓的 Config 一条条单测失败分支。
    """
    if not cfg.bin:
        raise ConfigError("TEACH_AGENT_BIN 没设：它必填，填 PATH 上的名字或绝对路径，"
                          "例如 TEACH_AGENT_BIN=claude-code-acp（见 README「接一个 ACP 分身」）")
    if any(c.isspace() for c in cfg.bin):
        raise ConfigError(f"TEACH_AGENT_BIN={cfg.bin!r} 里有空白：BIN 只放程序本身"
                          f"（PATH 上的名字或绝对路径），flag 一律放 TEACH_AGENT_ARGS")
    if cfg.system_prompt_mode not in SYSTEM_PROMPT_MODES:
        raise ConfigError(f"TEACH_AGENT_SYSTEM_PROMPT={cfg.system_prompt_mode!r} 不认："
                          f"只能取 {'/'.join(SYSTEM_PROMPT_MODES)}")
    if cfg.permission not in PERMISSION_MODES:
        raise ConfigError(f"TEACH_AGENT_PERMISSION={cfg.permission!r} 不认："
                          f"只能取 {'/'.join(PERMISSION_MODES)}")
    if cfg.protocol != "1":
        raise ConfigError(f"TEACH_AGENT_PROTOCOL={cfg.protocol!r} 不认：这一版只实现了 "
                          f"ACP v1（TEACH_AGENT_PROTOCOL=1），v2 还没做")
    if not cfg.bin_path and not cfg.dry:
        raise ConfigError(f"PATH 上找不到 {cfg.bin!r}：确认装好了、名字没写错，或直接给绝对路径"
                          f"（Windows 上 .cmd / .exe 都认）")
    if AcpClient is None and not cfg.dry:
        raise ConfigError(f"缺 acp.py（{_ACP_ERROR}）：它得跟 driver.py 放在同一个目录")
    for tok in cfg.arg_tokens:
        for name in _BRACED.findall(tok):
            if name not in PLACEHOLDERS:
                _warn_once(f"TEACH_AGENT_ARGS 里的 {{{name}}} 不认识，会原样传给 agent"
                           f"（只有 {_KNOWN} 这 3 个会被展开）")


def load_config(environ=os.environ):
    """把环境变量读成一份 Config（纯函数：只读传入的字典，绝不碰 os.environ）。

    驱动器启动时读一次，运行中改环境变量不生效，重启才生效。
    """
    cfg = Config(
        bin=environ.get("TEACH_AGENT_BIN") or "",
        args_raw=environ.get("TEACH_AGENT_ARGS", DEFAULT_AGENT_ARGS),
        system_prompt_mode=(environ.get("TEACH_AGENT_SYSTEM_PROMPT") or "meta").strip(),
        permission=(environ.get("TEACH_AGENT_PERMISSION") or "allow").strip(),
        log_updates=(environ.get("TEACH_AGENT_LOG_UPDATES") or "0").strip() == "1",
        protocol=(environ.get("TEACH_AGENT_PROTOCOL") or "1").strip(),
        dry=read_dry(environ),
    )
    cfg.bin_path = resolve_bin(cfg.bin)
    cfg.arg_tokens = split_args(cfg.args_raw)     # 引号不配对在这里就炸
    check_config(cfg)
    return cfg


def describe_agent(cfg):
    """启动时打一行配置，报错时方便对照。"""
    bin_txt = cfg.bin_path or f"{cfg.bin}（PATH 上没找到）"
    return (f"agent: bin={bin_txt} args={subprocess.list2cmdline(cfg.arg_tokens)} "
            f"system_prompt={cfg.system_prompt_mode} permission={cfg.permission} "
            f"log_updates={1 if cfg.log_updates else 0} protocol={cfg.protocol}")


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


_WARNED = set()


def _warn_once(msg):
    if msg in _WARNED:
        return
    _WARNED.add(msg)
    log(msg)


def read_dry(environ=os.environ):
    """TEACH_DRY=1 空跑；旧名字 DRIVER_DRY 兜底并告警一次（别让老脚本一升级就哑掉）。"""
    raw = environ.get("TEACH_DRY")
    if raw is None:
        raw = environ.get("DRIVER_DRY")
        if raw is not None:
            _warn_once("DRIVER_DRY 改名叫 TEACH_DRY 了：这次仍按 DRIVER_DRY 生效，记得换过来")
    return raw == "1"


def read_poll(environ=os.environ):
    """TEACH_POLL 秒数（默认 2）；旧名字 DRIVER_POLL 兜底并告警一次。写错退回 2。"""
    raw = environ.get("TEACH_POLL")
    if raw is None:
        raw = environ.get("DRIVER_POLL")
        if raw is not None:
            _warn_once("DRIVER_POLL 改名叫 TEACH_POLL 了：这次仍按 DRIVER_POLL 生效，记得换过来")
    if raw is None:
        return 2.0
    try:
        poll = float(raw)
    except ValueError:
        _warn_once(f"TEACH_POLL={raw!r} 不是数字，按默认 2 秒走")
        return 2.0
    if poll <= 0:
        _warn_once(f"TEACH_POLL={raw!r} 得是正数，按默认 2 秒走")
        return 2.0
    return poll


DRY = read_dry()
POLL = read_poll()


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def current_live():
    """扫 state/*.json，返回 (course, chapter)；都空则 None（同一刻只认一章 live）。"""
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


def _chunk_text(update):
    """agent_message_chunk 的正文：content 是一个 ContentBlock {"type":"text","text":...}。"""
    content = update.get("content")
    if isinstance(content, dict):
        return (content.get("text") or "").strip()
    if isinstance(content, str):
        return content.strip()
    return ""


def dry_frames(cfg, sid, system_prompt, seed):
    """空跑要打的三帧：initialize / session/new|load / session/prompt。

    id 只是预览（真发时由 acp.py 自增）；目的是不装 agent 也能对着它的文档核对参数。
    """
    first = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": PROTOCOL_VERSION,
                        "clientCapabilities": CLIENT_CAPABILITIES,
                        "clientInfo": CLIENT_INFO}}
    second_params = {"cwd": ROOT, "mcpServers": []}
    if sid:
        second_params = {"sessionId": sid, **second_params}
    if system_prompt:
        second_params["_meta"] = {"systemPrompt": system_prompt}
    method = "session/load" if sid else "session/new"
    second = {"jsonrpc": "2.0", "id": 2, "method": method, "params": second_params}
    third = {"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
             "params": {"sessionId": sid or "<session/new 回的 id>",
                        "prompt": [{"type": "text", "text": seed}]}}
    return ((f"1 initialize", first), (f"2 {method}", second), ("3 session/prompt", third))


class Session:
    """一章一个白砚：起停由驱动器管，说话走 acp.AcpClient。"""

    def __init__(self, course, chapter, cfg):
        self.course = course
        self.chapter = chapter
        self.cfg = cfg
        self.client = None
        self.argv = None            # 展开后的完整命令（起点失败时要打出来）
        self.prompt_text = ""       # 人格全文
        self.sid = None
        self.fed = 0

    # ---- 人格：一个来源，三条通道 ----
    def _system_prompt(self):
        """给 acp.py 的人格文本（它挂到 _meta.systemPrompt）；不走 meta 通道时给 None。"""
        if self.cfg.system_prompt_mode in META_MODES:
            return self.prompt_text
        return None

    def _prompt_via_args(self):
        """{prompt} 展开成什么：走 argv 通道给全文，否则给空串——同一份人格不能两条路各灌一遍。"""
        return self.prompt_text if self.cfg.system_prompt_mode in ("args", "both") else ""

    def _prompt_file_via_args(self):
        """{prompt_file} 同理：不走 argv 通道时给空串（文件路径也算人格那条路）。"""
        return PROMPT_FILE if self.cfg.system_prompt_mode in ("args", "both") else ""

    def _argv(self, sid):
        """展开后的完整命令：真起和空跑共用一份，免得两边对不上。"""
        return [self.cfg.bin_path or self.cfg.bin] + expand_args(
            self.cfg.arg_tokens, prompt=self._prompt_via_args(),
            prompt_file=self._prompt_file_via_args(), session=str(sid) if sid else "")

    # ---- 起 ----
    def start(self):
        """起白砚：读人格 → 算开场交代 → 开/续 ACP 会话（空跑只打日志）。"""
        old_sid = session_of(self.course, self.chapter)
        try:
            with open(PROMPT_FILE, encoding="utf-8") as f:
                self.prompt_text = f.read()
        except Exception as e:
            log(f"读白砚提示词失败：{e}")
            self.prompt_text = ""

        if self.cfg.dry:
            self._dry_report(old_sid, self._seed(old_sid))
            self.sid = old_sid or "(dry)"
            self.fed = self._inbox_len()    # 空跑也别把开讲前的旧 inbox 重喂
            return

        info = self._open(old_sid)
        self.sid = info.get("sessionId") or ""
        # 交代按**真正建立起来的**会话算：旧会话没续上就落成新会话，话术也得跟着换，
        # 否则新会话里的白砚会以为上下文还在。
        resumed = bool(old_sid) and bool(info.get("resumed"))
        log(f"白砚 session={self.sid}（{self.course} 第{self.chapter}章"
            f"{'，续上旧会话' if resumed else ''}）")
        if self.sid:
            register_sid(self.course, self.chapter, self.sid)
        self.feed(self._seed(old_sid if resumed else None))
        self.fed = self._inbox_len()        # 别把开讲前的旧 inbox 重喂

    def _spawn(self, sid, *, load):
        """起一个 ACP 客户端：sid 非空且 load 走 session/load，否则 session/new。"""
        argv = self._argv(sid)
        self.argv = argv
        client = AcpClient(argv[0], argv[1:], cwd=ROOT,
                           env=dict(os.environ, TEACH_DRIVER="1"),
                           system_prompt=self._system_prompt(),
                           on_permission=self._on_permission,
                           on_update=self._on_update,
                           on_log=self._agent_log,
                           init_timeout=AGENT_INIT_TIMEOUT)
        self.client = client
        return client.start(session_id=sid if load else None, load_session=load)

    def _open(self, old_sid):
        """开/续会话。续不上不算启动失败——丢掉旧 sid、重开一个新会话接着讲。

        acp.py 的约定：session/load 被 agent 拒了（多半是它不支持 session/load）
        就抛 AcpError，所以这一支同时盖住「agent 不支持」和「旧会话在对面没了」。
        换个进程起新会话（而不是在同一个 client 上再 start 一次）：argv 要按新
        session 重新展开，旧 client 的命令行里还留着旧 sid。
        """
        if old_sid:
            try:
                return self._spawn(old_sid, load=True)
            except Exception as e:
                log(f"旧会话 {old_sid} 无法续（{_exc_text(e)}；agent 可能不支持 "
                    f"session/load），另开新会话")
                self._close_client()
        return self._spawn(None, load=False)

    def _close_client(self):
        client, self.client = self.client, None
        if client is None:
            return
        try:
            client.close()
        except Exception as e:
            log(f"收白砚出错：{_exc_text(e)}")

    def _seed(self, old_sid):
        """开场交代：续会话/新会话两套话术，都是**普通用户消息**（人格不走这里）。"""
        meta = chapter_meta(self.course, self.chapter)
        title = meta.get("chapter_title") or "（未命名）"
        course_title = meta.get("course_title") or "（未命名课程）"
        if old_sid:
            return (f"学生回到讲课台了（还是《{course_title}》"
                    f"第{self.chapter}章「{title}」）。接着上次讲——先 `GET {BASE}/api/inbox"
                    f"?course={self.course}` 看看他最后说了什么，再决定开口说什么。")
        where = (f"从书页 {meta.get('book_page')}（PDF {meta.get('page')}）起"
                 if meta.get("page") else "从本章起始页起（讲课台没回应，页号自己查）")
        return (f"学生刚在讲课台唤醒了《{course_title}》第{self.chapter}章「{title}」，{where}。"
                f"你是白砚，去开讲：先 `GET {BASE}/api/state?course={self.course}` 看眼当前讲稿"
                f"（markdown 字段）——已有内容就接着往下讲、别重来；还空着就写一段开场白。"
                f"动笔前可跑 `python3 teach.py view {self.course}` 看当前页"
                f"（它会给出那页的图和文字，图直接打开看）。"
                f"输出一律走 POST /api/append 追加。别一次讲太多，讲完停下等他。")

    def _dry_report(self, old_sid, seed):
        """空跑：把会执行的命令、人格走的通道、将要发的三帧全打出来（不 spawn）。"""
        argv = self._argv(old_sid)
        self.argv = argv
        log(f"[DRY] 会起白砚：{self.course} 第{self.chapter}章 "
            f"{'续 ' + old_sid if old_sid else '新开'}（不 spawn）")
        log(f"[DRY] 会执行：{subprocess.list2cmdline(argv)}")
        log(f"[DRY] cwd={ROOT}")
        log(f"[DRY] system_prompt channel={self.cfg.system_prompt_mode} "
            f"meta_bytes={len(self._system_prompt() or '')} "
            f"argv_prompt={'是' if self._prompt_via_args() else '否'}")
        log(f"[DRY] clientCapabilities={json.dumps(CLIENT_CAPABILITIES, ensure_ascii=False)}")
        for label, frame in dry_frames(self.cfg, old_sid, self._system_prompt(), seed):
            log(f"[DRY] 帧·{label}：{json.dumps(frame, ensure_ascii=False)}")
        log("[DRY] 第一句 prompt 全文：\n" + seed)

    # ---- 白砚那边来的消息 ----
    def _on_permission(self, sid, tool_call, options):
        """权限一律按配置回（默认 allow）：白砚开白板、翻页时没人守在终端前点确认。"""
        tc = tool_call or {}
        title = tc.get("title") or tc.get("toolCallId") or "工具"
        log(f"[白砚权限] {title} → {self.cfg.permission}")
        return self.cfg.permission

    def _on_update(self, sid, update, replay):
        """只记日志，绝不往讲稿写东西——讲稿是白砚自己 POST /api/append 的事。"""
        up = update or {}
        kind = up.get("sessionUpdate")
        if kind == "tool_call":
            title = up.get("title") or up.get("toolCallId") or "?"
            log(f"[白砚工具] {title} ({up.get('kind')}/{up.get('status')})")
        elif kind == "agent_message_chunk" and self.cfg.log_updates:
            text = _chunk_text(up)
            if text:
                log(f"[白砚说] {text[:200]}")
        # 其余变体（plan / usage_update / tool_call_update…）忽略：驱动不替白砚记讲稿

    def _agent_log(self, line):
        log(f"[白砚日志] {line}")

    # ---- 喂 ----
    def feed(self, text):
        """发一句 session/prompt：一条 daemon 线程，一轮可能讲很久，不能卡住 2 秒的 poll 循环。"""
        if self.cfg.dry or not self.client:
            log(f"[DRY] 喂白砚：{text[:100]}")
            return
        log(f"喂白砚：{text[:100]}")
        threading.Thread(target=self._prompt_once, args=(text,), daemon=True).start()

    def _prompt_once(self, text):
        client = self.client
        try:
            res = client.prompt(text)
            log(f"白砚一轮结束（stopReason={res.get('stopReason')}）")
        except Exception as e:
            log(f"喂白砚失败：{_exc_text(e)}")

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
                msg += "\n（他附了图：" + "、".join(imgs) + "，打开那张图看）"
            self.feed(msg)
        self.fed = len(lines)

    # ---- 收 ----
    def alive(self):
        if self.cfg.dry:
            return True       # 空跑没有真进程：当作活着，别让主循环把空跑的会话收掉
        return bool(self.client) and self.client.alive()

    def exit_code(self):
        """进程退出码：acp.py 有 returncode 就用它，没有就退回 proc.poll()（都没有给 None）。"""
        client = self.client
        code = getattr(client, "returncode", None)
        if code is None:
            code = getattr(getattr(client, "proc", None), "returncode", None)
        return code

    def stop(self):
        if self.cfg.dry:
            log("[DRY] 收白砚")
            return
        if self.client is None:
            return
        log(f"收白砚：{self.course} 第{self.chapter}章")
        self._close_client()


G = {"target": None, "sess": None, "dead": None}


def _on_signal(sig, _frm):
    log(f"收到信号 {sig}，退出")
    if G["sess"]:
        G["sess"].stop()
    os._exit(0)


def _exc_text(e):
    code = getattr(e, "winerror", None)
    txt = str(e)
    if code and f"WinError {code}" not in txt:
        txt = f"{txt}（WinError {code}）"
    return f"{type(e).__name__}: {txt}"


def _start_hint(e):
    """按异常类型补一句能照做的提示。"""
    code = getattr(e, "winerror", None)
    if code == 193:
        return "提示：WinError 193 = 这份文件不能直接执行——别指 .ps1，用 .cmd / .exe。"
    if code == 2 or isinstance(e, FileNotFoundError):
        return ("提示：BIN 只放程序本身（PATH 上的名字或绝对路径），全部 flag 放 "
                "TEACH_AGENT_ARGS；填了整条带空格的命令就会这样。")
    return ("提示：用 TEACH_DRY=1 打一份会执行的命令与三帧 JSON 核对；"
            "agent 说什么协议由它自己负责。")


def tick(cfg):
    """主循环的一拍：看 live 起停白砚、把新话喂进去。抽成函数是为了能单测。"""
    live = current_live()
    if live != G["target"]:
        log(f"live 变：{G['target']} → {live}")
        if G["sess"]:
            G["sess"].stop()
            G["sess"] = None
        G["target"] = live
        if not live:
            G["dead"] = None     # 台子空了：清掉「不再重试」的记号，下次唤醒是全新一轮
        elif live == G["dead"]:
            log(f"这一章这一轮已经停过（{live[0]} 第{live[1]}章）：不自动重试，"
                f"改好配置后重新唤醒本章")
        else:
            sess = Session(live[0], live[1], cfg)
            try:
                sess.start()
                G["sess"] = sess
            except Exception as e:
                G["sess"] = None
                log(f"起白砚失败（{live[0]} 第{live[1]}章）：{_exc_text(e)}")
                if sess.argv:
                    log(f"展开后命令：{subprocess.list2cmdline(sess.argv)}")
                log(_start_hint(e))
                log("这一章先不动了——改好配置后重新唤醒本章（不会每 2 秒重试）")
                try:
                    sess.stop()
                except Exception:
                    pass
                G["dead"] = live
    if G["sess"]:
        if not G["sess"].alive():
            code = G["sess"].exit_code()
            log(f"白砚进程已退出（exit code {code if code is not None else '未知'}），本章先停")
            try:
                G["sess"].stop()
            except Exception:
                pass
            G["dead"] = G["target"]     # 记住这一章：同一 live 不重试风暴
            G["sess"] = None
            G["target"] = None          # 状态机回 idle，等下一次 live 变化
        else:
            G["sess"].poll_inbox()


def main(environ=os.environ):
    try:
        cfg = load_config(environ)
    except ConfigError as e:
        log(f"配置错误：{e}")
        log("驱动器不启动。改好环境变量后重跑 python3 driver.py")
        sys.exit(2)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    log(f"驱动器启动 | base={BASE} dry={DRY} poll={POLL}s")
    log(describe_agent(cfg))
    if cfg.dry and current_live() is None:
        # 空跑的意义是「不装 agent 也能把配置看全」：还没唤醒任何章时用示例章节把同一条
        # start() 路径走一遍（DRY 分支只打日志），免得空跑什么都不显示。
        log("[DRY] 现在没有 live 章节：下面用示例章节「(dry) 第1章」预览；"
            "唤醒某章后，空跑看的就是那一章")
        try:
            Session("(dry)", "1", cfg).start()
        except Exception as e:
            log(f"[DRY] 预览失败：{_exc_text(e)}")
    while True:
        tick(cfg)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
