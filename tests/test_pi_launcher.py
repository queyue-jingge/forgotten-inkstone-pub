#!/usr/bin/env python3
"""tools/pi-persona.cmd 的行为测试：真跑那个 .cmd，配一个假 pi 看它到底收到什么。

跑：python3 -m unittest discover -s tests
只测启动器本身（argv 转发、两条守卫、stdout 干净），不碰真 pi / pi-acp；
端到端的活体测试在 tests/test_pi_live.py（默认跳过）。

为什么要有这层：pi-acp 把 pi 的 stdout 当协议管道（非 JSON 行会被它当"分身说的话"
转给客户端），而我们的启动器正好夹在中间——echo、乱码、stdout 里的报错都会漏进去。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO, "tools", "pi-persona.cmd")
DRIVER = os.path.join(REPO, "driver.py")
COMSPEC = os.environ.get("COMSPEC") or "cmd.exe"

# 假 pi：把收到的 argv 与 TEACH_PERSONA_FILE 写成 JSON（UTF-8），供断言
FAKE_PI_PY = """import json, os, sys
with open(os.environ["FAKE_PI_OUT"], "w", encoding="utf-8") as f:
    json.dump({"argv": sys.argv[1:], "persona_env": os.environ.get("TEACH_PERSONA_FILE")},
              f, ensure_ascii=False)
"""
FAKE_PI_CMD = '@echo off\r\n"%s" "%s" %%*\r\n'


def make_scratch():
    """建本次测试专用的临时目录（平台 TEMP 下）。

    不用 tempfile.TemporaryDirectory：本机沙箱里 mkdtemp 建的目录带 0o700，
    受限进程自己都写不进去、也删不掉（同 tests/test_driver_acp.py 的理由）。
    """
    base = tempfile.gettempdir()
    for i in range(1000):
        path = os.path.join(base, "inkstone-launcher-%d-%d" % (os.getpid(), i))
        try:
            os.makedirs(path)
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"TEMP（{base}）下建不出临时目录")


@unittest.skipUnless(os.name == "nt", "启动器是 Windows 批处理脚本")
class PiPersonaLauncher(unittest.TestCase):
    """真跑 tools/pi-persona.cmd（cmd.exe，不 mock）。"""

    def setUp(self):
        self.scratch = make_scratch()
        self.addCleanup(lambda: shutil.rmtree(self.scratch, ignore_errors=True))
        self.bin_dir = os.path.join(self.scratch, "fakebin")
        os.makedirs(self.bin_dir)
        self.fake_py = os.path.join(self.scratch, "fake_pi.py")
        with open(self.fake_py, "w", encoding="utf-8") as f:
            f.write(FAKE_PI_PY)
        with open(os.path.join(self.bin_dir, "pi.cmd"), "w", encoding="utf-8") as f:
            f.write(FAKE_PI_CMD % (sys.executable, self.fake_py))

        # 人格路径故意带空格 + 中文：Windows 上最容易断的一条路
        self.persona_dir = os.path.join(self.scratch, "人格 测试")
        os.makedirs(self.persona_dir)
        self.persona = os.path.join(self.persona_dir, "白砚.md")
        with open(self.persona, "w", encoding="utf-8") as f:
            f.write("PERSONA-LAUNCHER-MARKER\n")
        self.out = os.path.join(self.scratch, "pi-saw.json")

    def run_launcher(self, *args, persona="__default__", set_env=True):
        env = dict(os.environ)
        env.pop("TEACH_PERSONA_FILE", None)
        env["PATH"] = self.bin_dir + os.pathsep + env.get("PATH", "")
        env["FAKE_PI_OUT"] = self.out
        if set_env:
            env["TEACH_PERSONA_FILE"] = self.persona if persona == "__default__" else persona
        return subprocess.run([COMSPEC, "/c", LAUNCHER, *args], env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)

    def pi_saw(self):
        with open(self.out, encoding="utf-8") as f:
            return json.load(f)

    def test_forwards_pi_acp_argv_and_persona_path(self):
        res = self.run_launcher("--mode", "rpc", "--no-themes")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "", "@echo off 没生效：这行会被 pi-acp 当分身说话")
        self.assertEqual(self.pi_saw(), {
            "argv": ["--append-system-prompt", self.persona,
                     "--mode", "rpc", "--no-themes"],
            "persona_env": self.persona,
        })

    def test_forwards_args_with_spaces(self):
        # pi-acp 会给会话文件路径（--session <path>）；带空格的参数不许被切开
        session = os.path.join(self.scratch, "my sessions", "s.jsonl")
        res = self.run_launcher("--mode", "rpc", "--no-themes", "--session", session)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.pi_saw()["argv"],
                         ["--append-system-prompt", self.persona,
                          "--mode", "rpc", "--no-themes", "--session", session])

    def test_missing_env_exits_2_without_starting_pi(self):
        res = self.run_launcher("--mode", "rpc", set_env=False)
        self.assertEqual(res.returncode, 2)
        self.assertEqual(res.stdout, "")
        self.assertIn("TEACH_PERSONA_FILE", res.stderr)
        self.assertFalse(os.path.exists(self.out), "守卫没拦住：pi 还是被起了")

    def test_missing_persona_file_exits_3_without_starting_pi(self):
        # 路径写错时 pi 会把「路径字符串」当人格正文静默塞进系统提示（resolvePromptInput），
        # 所以这一条必须硬拦
        gone = os.path.join(self.scratch, "没有这个文件.md")
        res = self.run_launcher("--mode", "rpc", persona=gone)
        self.assertEqual(res.returncode, 3)
        self.assertEqual(res.stdout, "")
        self.assertIn("not found", res.stderr)
        self.assertFalse(os.path.exists(self.out), "守卫没拦住：pi 还是被起了")

    def test_flag_lives_in_the_launcher_not_in_driver(self):
        # 源码级反向锁：driver.py 不许出现这个 flag（见 tests/test_driver_acp.py
        # NoLegacyProtocol：那个字面量是旧 CLI 协议的残留），它只属于启动器
        with open(LAUNCHER, encoding="utf-8") as f:
            wrapper = f.read()
        with open(DRIVER, encoding="utf-8") as f:
            driver_src = f.read()
        self.assertIn("--append-system-prompt", wrapper)
        self.assertNotIn("--append-system-prompt", driver_src)

    def test_wrapper_is_pure_ascii(self):
        # cmd.exe 按 OEM 代码页读 .cmd：里面出现中文就会乱码，所以只放 ASCII
        with open(LAUNCHER, "rb") as f:
            raw = f.read()
        bad = [(i, b) for i, b in enumerate(raw) if b > 127]
        self.assertEqual(bad, [], "启动器里有非 ASCII 字节：%r" % (bad[:5],))


if __name__ == "__main__":
    unittest.main()
