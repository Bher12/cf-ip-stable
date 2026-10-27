# -*- coding: utf-8 -*-
"""核心逻辑单元测试（2026-10-27 新增）

覆盖本次改造的四块：
  1. 死 IP 冷却表（neg_cache）
  2. IP 段多样性约束（ip_select）
  3. 保底顶替逻辑（main._pick_victim / _ensure_*_representation）
  4. 产出守卫与推送去重（main.evaluate_output_guard / _push_needed）

运行：python -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ip_select          # noqa: E402
import neg_cache          # noqa: E402


class TestNegCache(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "neg.json")
        self.c = neg_cache.NegCache(self.path, fail_threshold=2, cooldown_runs=3)

    def test_cools_after_threshold(self):
        n = "1.2.3.4:443#US"
        self.c.begin_run()
        self.c.record_fail(n)
        self.assertFalse(self.c.is_cooled(n), "失败 1 次不该冷却")
        self.c.record_fail(n)
        self.assertTrue(self.c.is_cooled(n), "连续 2 次失败应冷却")

    def test_unfreeze_after_cooldown(self):
        n = "1.2.3.4:443#US"
        self.c.begin_run()
        self.c.record_fail(n)
        self.c.record_fail(n)
        for _ in range(3):
            self.c.begin_run()
        self.assertFalse(self.c.is_cooled(n), "冷却 3 轮后应解封")

    def test_success_clears_record(self):
        n = "1.2.3.4:443#US"
        self.c.begin_run()
        self.c.record_fail(n)
        self.c.record_ok(n)
        self.assertNotIn(n, self.c.nodes)

    def test_persist_roundtrip(self):
        n = "1.2.3.4:443#US"
        self.c.begin_run()
        self.c.record_fail(n)
        self.c.record_fail(n)
        self.c.save()
        c2 = neg_cache.NegCache(self.path, fail_threshold=2, cooldown_runs=3)
        self.assertEqual(c2.run_no, self.c.run_no)
        self.assertTrue(c2.is_cooled(n))

    def test_filter_keeps_others(self):
        good, bad = "1.1.1.1:443#US", "2.2.2.2:443#JP"
        self.c.begin_run()
        self.c.record_fail(bad)
        self.c.record_fail(bad)
        kept, skipped = self.c.filter([good, bad])
        self.assertEqual(kept, [good])
        self.assertEqual(skipped, 1)


class TestDiversity(unittest.TestCase):
    def test_per_24_limit(self):
        nodes = ["1.2.3.%d:443#US" % i for i in range(1, 6)]
        kept, dropped = ip_select.apply_diversity(nodes, max_per_24=2, max_per_16=4)
        self.assertEqual(len(kept), 2)
        self.assertEqual(len(dropped), 3)

    def test_per_16_limit(self):
        # 不同 /24、同一 /16
        nodes = ["179.253.%d.1:443#US" % i for i in (225, 251, 255, 241, 100)]
        kept, dropped = ip_select.apply_diversity(nodes, max_per_24=2, max_per_16=4)
        self.assertEqual(len(kept), 4, "同一 /16 最多 4 个")
        self.assertEqual(len(dropped), 1)

    def test_ipv6_per_64(self):
        nodes = ["[2a06:98c1:58:0:a:1:2:%d]:443#US" % i for i in range(1, 5)]
        kept, _ = ip_select.apply_diversity(nodes, max_per_24=2, max_per_16=4, max_per_64=2)
        self.assertEqual(len(kept), 2)

    def test_keeps_order(self):
        nodes = ["9.9.9.1:443#US", "9.9.9.2:443#US", "9.9.9.3:443#US"]
        kept, _ = ip_select.apply_diversity(nodes, max_per_24=2, max_per_16=4)
        self.assertEqual(kept, nodes[:2], "应保留排序靠前的")


class TestMainLogic(unittest.TestCase):
    """需要导入 main（会加载配置，故屏蔽其 stdout）。"""

    @classmethod
    def setUpClass(cls):
        cls._cwd = os.getcwd()
        os.chdir(ROOT)
        buf = io.StringIO()
        with redirect_stdout(buf):
            import main
        cls.m = main

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._cwd)

    def test_victim_is_lowest_score(self):
        m = self.m
        if not hasattr(m, "_pick_victim"):
            self.skipTest("原版无 IPv6 保底，跳过")
        nodes = ["1.1.1.%d:443#US" % i for i in range(1, 6)]
        scores = {n: 100.0 - i * 10 for i, n in enumerate(nodes)}
        victim = m._pick_victim(nodes, want_v6_victim=False, score_map=scores)
        self.assertEqual(victim, nodes[-1], "应淘汰分数最低的节点")

    def test_victim_tiebreak_uses_last(self):
        m = self.m
        if not hasattr(m, "_pick_victim"):
            self.skipTest("原版无 IPv6 保底，跳过")
        nodes = ["1.1.1.1:443#US", "1.1.1.2:443#US", "1.1.1.3:443#US"]
        victim = m._pick_victim(nodes, want_v6_victim=False, score_map={})
        self.assertEqual(victim, nodes[-1], "同分时应淘汰排名靠后的")

    def test_ipv6_fill_keeps_top_ipv4(self):
        m = self.m
        if not hasattr(m, "_ensure_ipv6_representation"):
            self.skipTest("原版无 IPv6 保底，跳过")
        old_top, old_mode = m.GLOBAL_TOP_N, m.USE_GLOBAL_MODE
        try:
            m.GLOBAL_TOP_N, m.USE_GLOBAL_MODE = 5, True
            v4 = ["1.1.1.%d:443#US" % i for i in range(1, 6)]
            scores = {n: 100.0 - i * 10 for i, n in enumerate(v4)}
            v6pool = [("[2a06:98c1:58::%d]:443#IPV6" % i, 10.0 - i) for i in range(2)]
            out = m._ensure_ipv6_representation(list(v4), v6pool, 2, scores)
            self.assertEqual(len(out), 5)
            self.assertIn(v4[0], out, "最高分的 IPv4 不该被挤掉")
            self.assertNotIn(v4[-1], out, "最低分的 IPv4 应被挤掉")
        finally:
            m.GLOBAL_TOP_N, m.USE_GLOBAL_MODE = old_top, old_mode

    def test_output_guard(self):
        m = self.m
        self.assertEqual(m.evaluate_output_guard(0, 0)[0], False, "无历史基线不拦截")
        self.assertEqual(m.evaluate_output_guard(3, 27)[0], True, "崩塌应拦截")
        self.assertEqual(m.evaluate_output_guard(20, 27)[0], False, "正常波动不拦截")
        self.assertEqual(m.evaluate_output_guard(6, 6)[0], False, "小幅下降不拦截")

    def test_push_dedup(self):
        m = self.m
        d = tempfile.mkdtemp()
        ip = os.path.join(d, "ip.txt")
        with open(ip, "w", encoding="utf-8") as f:
            f.write("1.1.1.1:443#US\n")
        need, digest = m._push_needed(d, "ip.txt")
        self.assertTrue(need, "首次应推送")
        m._mark_pushed(d, digest)
        need2, _ = m._push_needed(d, "ip.txt")
        self.assertFalse(need2, "内容未变应跳过")
        with open(ip, "a", encoding="utf-8") as f:
            f.write("2.2.2.2:443#JP\n")
        need3, _ = m._push_needed(d, "ip.txt")
        self.assertTrue(need3, "内容变化应推送")


if __name__ == "__main__":
    unittest.main(verbosity=2)
