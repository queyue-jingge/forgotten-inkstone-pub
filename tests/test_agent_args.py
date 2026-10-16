#!/usr/bin/env python3
"""driver.py 的 agent 配置层单测：只用标准库。

跑：python3 -m unittest discover -s tests
    （Windows 上 python 是 Store stub，用绝对路径解释器，别用裸 python）
"""
import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import driver

# 一个**一定存在**的程序：配置层要 resolve_bin 通过才能验后面的分支。
BIN = sys.executable or "python"


def make_scratch():
    """建本次测试专用的临时目录（平台 TEMP 下），返回路径。

    不用 tempfile.TemporaryDirectory：本机沙箱里 mkdtemp 建的目录带 0o700，
    受限进程自己都写不进去、也删不掉（测试全瞎还留一堆删不掉的残留）。
    os.makedirs 用默认权限建，读写删在沙箱里都正常。
    """
    base = tempfile.gettempdir()
    for i in range(1000):
        path = os.path.join(base, "inkstone-args-%d-%d" % (os.getpid(), i))
        try:
            os.makedirs(path)
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"TEMP（{base}）下建不出临时目录")


class TempLog(unittest.TestCase):
    """把 driver.LOG 指到临时目录：测试里触发的告警别写进仓库根的 driver.log。"""

    def setUp(self):
        self._tmp = make_scratch()
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        self._log_before = driver.LOG
        driver.LOG = os.path.join(self._tmp, "driver.log")
        driver._WARNED.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        driver.LOG = self._log_before
        driver._WARNED.clear()


class SplitArgs(unittest.TestCase):
    def test_whitespace_and_newlines(self):
        self.assertEqual(driver.split_args("  -a\t-b\n -c  "), ["-a", "-b", "-c"])

    def test_quoted_group_keeps_spaces(self):
        self.assertEqual(driver.split_args('-x "a b" -y'), ["-x", "a b", "-y"])

    def test_quotes_stripped_mid_token(self):
        self.assertEqual(driver.split_args('--p="a b"'), ["--p=a b"])

    def test_backslash_not_an_escape(self):
        bs = chr(92)
        raw = "C:" + bs + "Users" + bs + "x" + bs + "y.json"
        self.assertEqual(driver.split_args(raw), [raw])
        self.assertEqual(driver.split_args('"a ' + bs + ' b"'), ["a " + bs + " b"])

    def test_empty(self):
        self.assertEqual(driver.split_args(""), [])
        self.assertEqual(driver.split_args("   \n\t "), [])

    def test_empty_quoted_token(self):
        self.assertEqual(driver.split_args('-x ""'), ["-x", ""])

    def test_unbalanced_quote_raises(self):
        with self.assertRaises(driver.ConfigError):
            driver.split_args('--x "unclosed')
        with self.assertRaises(driver.ConfigError):
            driver.split_args('--x "unclosed ')


class ResolveBin(unittest.TestCase):
    def test_missing_name_gives_none(self):
        self.assertIsNone(driver.resolve_bin("definitely-not-a-real-agent-xyz"))

    def test_empty_gives_none(self):
        self.assertIsNone(driver.resolve_bin(""))

    def test_absolute_existing_path_resolves(self):
        self.assertEqual(driver.resolve_bin(BIN), BIN)


class ExpandArgs(unittest.TestCase):
    def _expand(self, tokens, **kw):
        kw.setdefault("prompt", "PROMPT")
        kw.setdefault("prompt_file", os.path.join("abs", "prompts", "白砚.md"))
        kw.setdefault("session", "")
        return driver.expand_args(tokens, **kw)

    def test_prompt_and_prompt_file(self):
        self.assertEqual(self._expand(["{prompt}", "{prompt_file}"]),
                         ["PROMPT", os.path.join("abs", "prompts", "白砚.md")])

    def test_prompt_with_spaces_stays_one_token(self):
        # 人格全文带换行/空白：必须整块作为一个 argv token，不能按空白再切
        full = "你是白砚。\n讲课时慢一点，别一次讲太多。"
        self.assertEqual(self._expand(["--sys", "{prompt}"], prompt=full), ["--sys", full])

    def test_session_placeholder(self):
        self.assertEqual(self._expand(["--id", "{session}"], session="s1"), ["--id", "s1"])
        self.assertEqual(self._expand(["--id", "{session}"]), ["--id", ""])

    def test_unknown_placeholder_kept_verbatim(self):
        self.assertEqual(self._expand(["--x", "{foo}", "{prompt}bar", "{ }"]),
                         ["--x", "{foo}", "{prompt}bar", "{ }"])

    def test_no_resume_placeholder_left(self):
        # {resume} 已经删掉：现在会原样传给 agent（属于"不认识"那一类）
        self.assertEqual(self._expand(["{resume}"]), ["{resume}"])

class ConfigDefaults(TempLog):
    """不设任何 TEACH_AGENT_* 时的默认值（BIN 是唯一必填项）。"""

    def test_defaults(self):
        cfg = driver.load_config({"TEACH_AGENT_BIN": BIN})
        self.assertEqual(cfg.bin, BIN)
        self.assertEqual(cfg.bin_path, BIN)
        self.assertEqual(cfg.args_raw, "")
        self.assertEqual(cfg.arg_tokens, [])
        self.assertEqual(cfg.system_prompt_mode, "meta")
        self.assertEqual(cfg.permission, "allow")
        self.assertFalse(cfg.log_updates)
        self.assertEqual(cfg.protocol, "1")
        self.assertFalse(cfg.dry)

    def test_default_args_is_empty(self):
        self.assertEqual(driver.DEFAULT_AGENT_ARGS, "")

    def test_placeholder_set(self):
        # {resume} 已删；留下的三个正好是 expand_args 认的那三个
        self.assertEqual(tuple(driver.PLACEHOLDERS), ("prompt", "prompt_file", "session"))

    def test_modes(self):
        self.assertEqual(driver.SYSTEM_PROMPT_MODES, ("meta", "meta_append", "args", "both", "none"))
        self.assertEqual(driver.PERMISSION_MODES, ("allow", "deny", "cancel"))


class ConfigOverrides(TempLog):
    def test_everything_overridden(self):
        cfg = driver.load_config({
            "TEACH_AGENT_BIN": BIN,
            "TEACH_AGENT_ARGS": '--sys "a b" {prompt} {prompt_file} {session}',
            "TEACH_AGENT_SYSTEM_PROMPT": "both",
            "TEACH_AGENT_PERMISSION": "deny",
            "TEACH_AGENT_LOG_UPDATES": "1",
            "TEACH_AGENT_PROTOCOL": "1",
            "TEACH_DRY": "1",
        })
        self.assertEqual(cfg.arg_tokens,
                         ["--sys", "a b", "{prompt}", "{prompt_file}", "{session}"])
        self.assertEqual(cfg.system_prompt_mode, "both")
        self.assertEqual(cfg.permission, "deny")
        self.assertTrue(cfg.log_updates)
        self.assertTrue(cfg.dry)

    def test_each_system_prompt_mode_accepted(self):
        for mode in driver.SYSTEM_PROMPT_MODES:
            cfg = driver.load_config({"TEACH_AGENT_BIN": BIN,
                                      "TEACH_AGENT_SYSTEM_PROMPT": mode})
            self.assertEqual(cfg.system_prompt_mode, mode)

    def test_each_permission_mode_accepted(self):
        for mode in driver.PERMISSION_MODES:
            cfg = driver.load_config({"TEACH_AGENT_BIN": BIN, "TEACH_AGENT_PERMISSION": mode})
            self.assertEqual(cfg.permission, mode)

    def test_log_updates_only_one_is_true(self):
        for raw, want in (("0", False), ("1", True)):
            cfg = driver.load_config({"TEACH_AGENT_BIN": BIN, "TEACH_AGENT_LOG_UPDATES": raw})
            self.assertIs(cfg.log_updates, want)

    def test_env_values_are_stripped(self):
        cfg = driver.load_config({"TEACH_AGENT_BIN": BIN,
                                  "TEACH_AGENT_SYSTEM_PROMPT": " args ",
                                  "TEACH_AGENT_PERMISSION": " cancel "})
        self.assertEqual(cfg.system_prompt_mode, "args")
        self.assertEqual(cfg.permission, "cancel")

    def test_empty_agent_args_means_no_template_tokens(self):
        cfg = driver.load_config({"TEACH_AGENT_BIN": BIN, "TEACH_AGENT_ARGS": ""})
        self.assertEqual(cfg.arg_tokens, [])

    def test_old_dry_name_still_works(self):
        cfg = driver.load_config({"TEACH_AGENT_BIN": BIN, "DRIVER_DRY": "1"})
        self.assertTrue(cfg.dry)


class EnvHelpers(TempLog):
    """TEACH_DRY / TEACH_POLL 的读法（含旧名字兜底）。"""

    def test_dry_default_and_value(self):
        self.assertFalse(driver.read_dry({}))
        self.assertTrue(driver.read_dry({"TEACH_DRY": "1"}))
        self.assertFalse(driver.read_dry({"TEACH_DRY": "0"}))

    def test_dry_old_name_fallback(self):
        self.assertTrue(driver.read_dry({"DRIVER_DRY": "1"}))

    def test_dry_new_name_wins(self):
        self.assertTrue(driver.read_dry({"TEACH_DRY": "1", "DRIVER_DRY": "0"}))

    def test_poll_default_and_value(self):
        self.assertEqual(driver.read_poll({}), 2.0)
        self.assertEqual(driver.read_poll({"TEACH_POLL": "0.5"}), 0.5)

    def test_poll_old_name_fallback(self):
        self.assertEqual(driver.read_poll({"DRIVER_POLL": "3"}), 3.0)

    def test_poll_garbage_falls_back_to_default(self):
        self.assertEqual(driver.read_poll({"TEACH_POLL": "abc"}), 2.0)
        self.assertEqual(driver.read_poll({"TEACH_POLL": "0"}), 2.0)
        self.assertEqual(driver.read_poll({"TEACH_POLL": "-1"}), 2.0)


class LoadConfigPurity(TempLog):
    """load_config 是纯函数：只读传进来的字典。"""

    def test_does_not_read_os_environ(self):
        keep = dict(os.environ)
        try:
            os.environ["TEACH_AGENT_BIN"] = "poisoned-bin"
            os.environ["TEACH_AGENT_SYSTEM_PROMPT"] = "yolo"
            os.environ["TEACH_AGENT_PERMISSION"] = "yolo"
            os.environ["TEACH_AGENT_ARGS"] = "--poisoned"
            cfg = driver.load_config({"TEACH_AGENT_BIN": BIN})
        finally:
            os.environ.clear()
            os.environ.update(keep)
        self.assertEqual(cfg.bin, BIN)
        self.assertEqual(cfg.arg_tokens, [])
        self.assertEqual(cfg.system_prompt_mode, "meta")
        self.assertEqual(cfg.permission, "allow")

    def test_does_not_write_os_environ(self):
        before = dict(os.environ)
        driver.load_config({"TEACH_AGENT_BIN": BIN, "TEACH_DRY": "1",
                            "TEACH_AGENT_ARGS": "--x {foo}"})
        self.assertEqual(dict(os.environ), before)

    def test_does_not_change_module_constants(self):
        base, prompt, sessions = driver.BASE, driver.PROMPT_FILE, driver.SESSIONS
        driver.load_config({"TEACH_AGENT_BIN": BIN, "TEACH_BASE": "http://poisoned"})
        self.assertEqual(driver.BASE, base)
        self.assertEqual(driver.PROMPT_FILE, prompt)
        self.assertEqual(driver.SESSIONS, sessions)


if __name__ == "__main__":
    unittest.main()
