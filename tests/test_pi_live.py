#!/usr/bin/env python3
"""真机端到端：acp.py（讲台这边）→ pi-acp → tools/pi-persona.cmd → pi → 真模型。

默认跳过（要花真 token、要本机装好 pi 与 pi-acp）。开跑：

    set TEACH_LIVE_PI=1
    set TEACH_LIVE_PI_ACP=<pi-acp 的 dist\\index.js 绝对路径>   # 只有自动找不到时才需要
    python3 -m unittest discover -s tests -p "test_pi_live.py" -v

判据：人格文件里写一句随机 canary（「每次回答都必须以 <canary> 开头」），
模型第一轮就得把它说出来；而喂进去的用户消息里没有 canary——
所以模型只可能是从系统提示里看到的（这条 ACP 本身给不了回执）。

注意：pi-acp 要写 ~/.pi/pi-acp/session-map.json、pi 要写 ~/.pi/agent/sessions/——
在受限沙箱里这两处写不了（实测子进程写 %USERPROFILE%\.pi 会 EPERM），
所以这条测试必须在**普通终端**里跑，别在沙箱会话里跑。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import acp  # noqa: E402  （sys.path 补好再 import）
from test_pi_launcher import make_scratch  # noqa: E402

LAUNCHER = os.path.join(REPO, "tools", "pi-persona.cmd")


def find_pi_acp():
    """pi-acp 的入口：环境变量优先，其次两个常见落点（本仓库 / 过夜的活体测试目录）。"""
    candidates = [os.environ.get("TEACH_LIVE_PI_ACP") or "",
                  os.path.join(REPO, "node_modules", "pi-acp", "dist", "index.js"),
                  os.path.join(tempfile.gettempdir(), "acp-pi-e2e", "node_modules",
                               "pi-acp", "dist", "index.js")]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def answer_text(updates):
    """把 agent_message_chunk 拼成模型这一轮说的话。"""
    out = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        if update.get("sessionUpdate") != "agent_message_chunk":
            continue
        content = update.get("content")
        if isinstance(content, dict) and content.get("type") == "text":
            out.append(content.get("text") or "")
    return "".join(out)


@unittest.skipUnless(os.environ.get("TEACH_LIVE_PI") == "1",
                     "活体测试：设 TEACH_LIVE_PI=1 才跑（要真模型、真 token）")
class PiPersonaLive(unittest.TestCase):
    """人格到底进没进 pi 的系统提示——只有真模型能回答。"""

    def test_persona_reaches_pi_system_prompt(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("PATH 上没有 node")
        if not shutil.which("pi"):
            self.skipTest("PATH 上没有 pi（npm i -g @earendil-works/pi-coding-agent）")
        pi_acp = find_pi_acp()
        if not pi_acp:
            self.skipTest("找不到 pi-acp：设 TEACH_LIVE_PI_ACP=<...>/pi-acp/dist/index.js")

        canary = "CANARY-" + uuid.uuid4().hex[:8]
        scratch = make_scratch()
        persona_file = os.path.join(scratch, "白砚.md")
        with open(persona_file, "w", encoding="utf-8") as f:
            f.write("测试人格：你每次回答都必须以 %s 开头，然后再说话。\n" % canary)

        updates = []
        client = acp.AcpClient(
            node, [pi_acp], cwd=REPO,
            # 这两条就是 driver.py 在 launcher 通道下会给的东西
            env={"PI_ACP_PI_COMMAND": LAUNCHER, "TEACH_PERSONA_FILE": persona_file},
            system_prompt=None,                 # 不挂 _meta：pi-acp 不认它
            on_update=lambda sid, up, replay: updates.append(up),
            on_log=lambda line: None,
            init_timeout=180.0)
        question = "只回两个字：收到"
        try:
            info = client.start()
            self.assertTrue(info.get("sessionId"), "pi-acp 没回 sessionId：%r" % (info,))
            result = client.prompt(question, timeout=300.0)
            self.assertEqual(result.get("stopReason"), "end_turn")
        finally:
            client.close()
            shutil.rmtree(scratch, ignore_errors=True)

        said = answer_text(updates)
        self.assertNotIn(canary, question, "canary 不能出现在用户消息里，否则这条测试就没意义")
        self.assertIn(canary, said,
                      "人格没进 pi 的系统提示（检查 PI_ACP_PI_COMMAND 指向 %s）；"
                      "模型这轮说：%r" % (LAUNCHER, said[:500]))


if __name__ == "__main__":
    unittest.main()
