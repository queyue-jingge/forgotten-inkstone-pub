#!/usr/bin/env python3
"""TEACH_AGENT 一键预设 + first 人格通道的单测：只用标准库、不起子进程。

跑：python -m unittest discover -s tests
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
PERSONA = "你是白砚，讲课时慢一点。"
MARKER = "【以下是你的固定人设"


def make_scratch():
    """建本次测试专用的临时目录（平台 TEMP 下），返回路径。

    不用 tempfile.TemporaryDirectory：本机沙箱里 mkdtemp 建的目录带 0o700，
    受限进程自己都写不进去、也删不掉。os.makedirs 用默认权限建，读写删都正常。
    """
    base = tempfile.gettempdir()
    for i in range(1000):
        path = os.path.join(base, "inkstone-presets-%d-%d" % (os.getpid(), i))
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


class PresetDefaults(TempLog):
    """TEACH_AGENT=<名> 填 BIN/ARGS/SYSTEM_PROMPT 默认；显式环境变量逐项优先。"""

    def test_each_preset_fills_bin_args_channel(self):
        # TEACH_DRY=1：预设指的 bin 多半没装，空跑才是不装 agent 也能验配置的口径
        for name, want in driver.AGENT_PRESETS.items():
            cfg = driver.load_config({"TEACH_AGENT": name, "TEACH_DRY": "1"})
            self.assertEqual(cfg.bin, want["bin"], name)
            self.assertEqual(cfg.args_raw, want["args"], name)
            self.assertEqual(cfg.arg_tokens, want["args"].split(), name)
            self.assertEqual(cfg.system_prompt_mode, want["system_prompt"], name)
            self.assertEqual(cfg.preset, name)

    def test_preset_name_case_insensitive_and_stripped(self):
        cfg = driver.load_config({"TEACH_AGENT": " CoDeX ", "TEACH_DRY": "1"})
        self.assertEqual(cfg.preset, "codex")
        self.assertEqual(cfg.bin, "codex-acp")

    def test_unknown_preset_rejected_with_legal_names(self):
        with self.assertRaises(driver.ConfigError) as cm:
            driver.load_config({"TEACH_AGENT": "gemini", "TEACH_DRY": "1"})
        self.assertIn("claude/codex/opencode/dsh", str(cm.exception))

    def test_empty_preset_means_unset(self):
        cfg = driver.load_config({"TEACH_AGENT": "", "TEACH_AGENT_BIN": BIN})
        self.assertIsNone(cfg.preset)
        self.assertEqual(cfg.system_prompt_mode, "meta")   # 完全回到旧默认

    def test_no_preset_bin_still_required(self):
        with self.assertRaises(driver.ConfigError) as cm:
            driver.load_config({})
        self.assertIn("TEACH_AGENT", str(cm.exception))    # 报错里要指得出路
        self.assertIn("claude/codex/opencode/dsh", str(cm.exception))

    def test_explicit_bin_beats_preset_args_still_preset(self):
        cfg = driver.load_config({"TEACH_AGENT": "opencode", "TEACH_AGENT_BIN": BIN})
        self.assertEqual(cfg.bin, BIN)
        self.assertEqual(cfg.bin_path, BIN)
        self.assertEqual(cfg.args_raw, "acp")
        self.assertEqual(cfg.preset, "opencode")

    def test_explicit_args_beat_preset(self):
        cfg = driver.load_config({"TEACH_AGENT": "dsh",
                                  "TEACH_AGENT_ARGS": "--profile acp2",
                                  "TEACH_DRY": "1"})
        self.assertEqual(cfg.args_raw, "--profile acp2")
        self.assertEqual(cfg.arg_tokens, ["--profile", "acp2"])

    def test_explicit_empty_args_beat_preset(self):
        # 设成空串也是「显式设了」：一个 flag 都不传（与旧语义一致）
        cfg = driver.load_config({"TEACH_AGENT": "opencode",
                                  "TEACH_AGENT_ARGS": "", "TEACH_DRY": "1"})
        self.assertEqual(cfg.args_raw, "")
        self.assertEqual(cfg.arg_tokens, [])

    def test_explicit_system_prompt_beats_preset(self):
        cfg = driver.load_config({"TEACH_AGENT": "codex",
                                  "TEACH_AGENT_SYSTEM_PROMPT": "meta",
                                  "TEACH_DRY": "1"})
        self.assertEqual(cfg.system_prompt_mode, "meta")

    def test_preset_leaves_permission_log_protocol_alone(self):
        cfg = driver.load_config({"TEACH_AGENT": "dsh", "TEACH_DRY": "1"})
        self.assertEqual(cfg.permission, "allow")
        self.assertFalse(cfg.log_updates)
        self.assertEqual(cfg.protocol, "1")


class PresetResolution(TempLog):
    """预设的 bin 解析与启动日志。"""

    def test_missing_preset_bin_fails_when_not_dry(self):
        # 打桩 resolve_bin：稳定模拟「PATH 上没有」，不受本机装没装 codex-acp 影响
        before = driver.resolve_bin
        driver.resolve_bin = lambda name: None
        self.addCleanup(lambda: setattr(driver, "resolve_bin", before))
        with self.assertRaises(driver.ConfigError) as cm:
            driver.load_config({"TEACH_AGENT": "codex"})
        self.assertIn("找不到", str(cm.exception))

    def test_describe_agent_marks_preset(self):
        cfg = driver.load_config({"TEACH_AGENT": "codex", "TEACH_AGENT_BIN": BIN})
        self.assertIn("preset=codex", driver.describe_agent(cfg))

    def test_describe_agent_without_preset_has_no_marker(self):
        cfg = driver.load_config({"TEACH_AGENT_BIN": BIN})
        self.assertNotIn("preset=", driver.describe_agent(cfg))


class FirstChannel(TempLog):
    """first 通道：人格只拼在新会话的第一句用户消息前，续会话与别的通道不受影响。"""

    def setUp(self):
        super().setUp()
        before = driver.chapter_meta
        driver.chapter_meta = lambda course, chapter: {}    # _seed 不该碰网
        self.addCleanup(lambda: setattr(driver, "chapter_meta", before))

    def sess(self, mode=driver.FIRST_MODE, persona=PERSONA):
        cfg = driver.Config(bin=BIN, system_prompt_mode=mode, dry=True)
        cfg.bin_path = BIN
        cfg.arg_tokens = []
        s = driver.Session("semi", "1", cfg)
        s.prompt_text = persona
        return s

    def test_new_session_seed_carries_persona_first(self):
        seed = self.sess()._seed(None)
        self.assertIn(MARKER, seed)
        self.assertIn(PERSONA, seed)
        self.assertIn("【人设结束】", seed)
        self.assertLess(seed.index(MARKER), seed.index("唤醒了"))   # 人格在最前

    def test_resume_seed_has_no_persona(self):
        seed = self.sess()._seed("old-sid")
        self.assertNotIn(MARKER, seed)
        self.assertIn("回到讲课台", seed)

    def test_empty_persona_no_marker(self):
        seed = self.sess(persona="")._seed(None)
        self.assertNotIn(MARKER, seed)

    def test_meta_mode_seed_has_no_persona(self):
        # meta 通道的人格仍只走 _meta，绝不再拼进第一句（同一份人格不两条路各灌一遍）
        s = self.sess(mode="meta")
        self.assertNotIn(MARKER, s._seed(None))
        self.assertEqual(s._system_prompt(), PERSONA)

    def test_first_mode_meta_stays_empty(self):
        self.assertIsNone(self.sess()._system_prompt())

    def test_first_mode_argv_and_launcher_stay_empty(self):
        s = self.sess()
        self.assertEqual(s._prompt_via_args(), "")
        self.assertEqual(s._prompt_file_via_args(), "")
        self.assertEqual(s._launcher_env(), {})

    def test_dry_report_names_first_channel_and_shows_persona(self):
        logs = []
        before = driver.log
        driver.log = logs.append
        self.addCleanup(lambda: setattr(driver, "log", before))
        s = self.sess()
        s._dry_report(None, s._seed(None))
        text = "\n".join(logs)
        self.assertIn("persona=first", text)
        self.assertIn("第一句 prompt 全文", text)
        self.assertIn(MARKER, text)          # 空跑把第一句全文打出来，人格自动可见

    def test_dry_report_meta_mode_has_no_first_line(self):
        logs = []
        before = driver.log
        driver.log = logs.append
        self.addCleanup(lambda: setattr(driver, "log", before))
        s = self.sess(mode="meta")
        s._dry_report(None, s._seed(None))
        self.assertNotIn("persona=first", "\n".join(logs))


if __name__ == "__main__":
    unittest.main()
