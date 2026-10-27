#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""neg_cache.py —— 死 IP 冷却表（负缓存）

解决「每轮把几千个已知死 IP 从头重测一遍」的浪费：
同一节点连续失败 N 轮后，冷却 M 轮不再参与探测；期间只要被测到一次成功就立即复活。

设计要点：
  * 只按「TCP 连不上」记失败 —— HTTP 抖动、带宽不足都不记，
    避免把「能连但不快」的节点误判成死节点。
  * 冷却到期后自动放回候选池；成功一次即清除记录。
  * 文件原子写入（tmp + os.replace），进程被杀不会写坏。
  * 条目上限保护，避免长期运行后文件膨胀。

单独自测：python neg_cache.py
"""

import json
import os
import time


class NegCache:
    def __init__(self, path, fail_threshold=3, cooldown_runs=6,
                 max_entries=20000, prune_after_runs=30):
        self.path = path
        self.fail_threshold = max(1, int(fail_threshold))
        self.cooldown_runs = max(1, int(cooldown_runs))
        self.max_entries = max(100, int(max_entries))
        self.prune_after_runs = max(1, int(prune_after_runs))
        self.run_no = 0
        self.nodes = {}          # node -> {"fails": int, "skip_until": int, "last": str}
        self.dirty = False
        self.load()

    # ---------------- 持久化 ----------------
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.run_no = int(data.get("run", 0))
            raw = data.get("nodes", {})
            if isinstance(raw, dict):
                self.nodes = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            self.run_no = 0
            self.nodes = {}

    def save(self):
        if not self.dirty:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"run": self.run_no, "nodes": self.nodes}, f,
                          ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError as e:
            print(f"[冷却表] 保存失败: {e}")

    # ---------------- 运行期 ----------------
    def begin_run(self):
        """每轮开始时调用，轮次 +1。"""
        self.run_no += 1
        self.dirty = True

    def is_cooled(self, node):
        rec = self.nodes.get(node)
        if not rec:
            return False
        return int(rec.get("skip_until", 0)) > self.run_no

    def filter(self, nodes):
        """返回 (保留的节点列表, 被跳过数量)。"""
        if not self.nodes:
            return nodes, 0
        kept = [n for n in nodes if not self.is_cooled(n)]
        return kept, len(nodes) - len(kept)

    def record_ok(self, node):
        if self.nodes.pop(node, None) is not None:
            self.dirty = True

    def record_fail(self, node):
        rec = self.nodes.get(node)
        if rec is None:
            rec = {"fails": 0, "skip_until": 0, "last": "", "last_run": self.run_no}
            self.nodes[node] = rec
        rec["fails"] = int(rec.get("fails", 0)) + 1
        rec["last"] = time.strftime("%Y-%m-%d %H:%M")
        rec["last_run"] = self.run_no
        if rec["fails"] >= self.fail_threshold:
            rec["skip_until"] = self.run_no + self.cooldown_runs
            rec["fails"] = 0          # 进入冷却后重新计数，冷却结束仍有 N 次机会
        self.dirty = True

    def prune(self):
        """清理「长期没再出现」的条目，并做容量上限保护。

        注意：判据必须用 last_run（最后一次出现），不能用 skip_until。
        早期版本用 skip_until < run - N 判断，会把「刚失败 1 次、还没攒够
        阈值」的活跃记录一起删掉，导致冷却永远攒不满 —— 实测踩过这个坑。
        """
        cutoff = self.run_no - self.prune_after_runs
        dead = [n for n, r in self.nodes.items()
                if int(r.get("last_run", 0)) < cutoff]
        for n in dead:
            del self.nodes[n]
        if len(self.nodes) > self.max_entries:
            # 按「最后一次出现轮次」升序淘汰，保留最新的
            ordered = sorted(self.nodes.items(),
                             key=lambda kv: int(kv[1].get("last_run", 0)))
            for n, _ in ordered[:len(self.nodes) - self.max_entries]:
                del self.nodes[n]
        if dead:
            self.dirty = True
        return len(dead)

    def stats(self):
        cooled = sum(1 for r in self.nodes.values()
                     if int(r.get("skip_until", 0)) > self.run_no)
        return {"run": self.run_no, "tracked": len(self.nodes), "cooled": cooled}


if __name__ == "__main__":
    import tempfile

    p = os.path.join(tempfile.gettempdir(), "_negcache_selftest.json")
    if os.path.exists(p):
        os.remove(p)

    c = NegCache(p, fail_threshold=2, cooldown_runs=3)
    node = "1.2.3.4:443#US"

    print("== 连续失败 2 轮后应进入冷却 ==")
    for r in range(1, 4):
        c.begin_run()
        print(f"run {r}: cooled={c.is_cooled(node)}")
        c.record_fail(node)
    c.save()

    c2 = NegCache(p, fail_threshold=2, cooldown_runs=3)
    print("重载后 run =", c2.run_no, " cooled =", c2.is_cooled(node))
    for _ in range(3):
        c2.begin_run()
    print("冷却 3 轮后应解封:", not c2.is_cooled(node))
    c2.record_ok(node)
    print("成功后记录被清除:", node not in c2.nodes)
    print("filter 用法:", c2.filter(["1.2.3.4:443#US", "5.6.7.8:443#JP"]))
