#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stability_layer.py — cf-ip 稳定性增强层（独立于 main.py，可单独自测）

解决原工具「每 5 分钟全量重排、因测量噪声导致选中 IP 反复跳动」的痛点：

  1) 跨运行历史评分：每个 IP 维护 EMA 分数 + 近期样本，
     用「变异系数倒数」衡量一致性（越稳越接近 1）。
  2) 排序/换人只依据「稳定分 = EMA × 一致性」：
     尖峰 IP 的样本离散度大 → 一致性低 → 稳定分被压低，无法顶掉现任。
  3) 滞回（hysteresis）：现任未被新候选明显超越（>HYS 比例）则保留，
     避免微幅波动造成换人。
  4) 确认闸门：全新 IP 必须连续 CONFIRM_RUNS 次进入候选，
     且一致性达到 MIN_CONSISTENCY 才允许替换现任，滤掉单次尖峰。
  5) 缺勤宽限：已发布 IP 短暂未测到（网络抖动）时保留，
     超过 MAX_ABSENT_RUNS 次才淘汰。

核心思想：测量仍每次进行（频繁观测），但「发布集合」只在被证实时才变更（谨慎生效）。
"""

import json
import math
import os
import time
import tempfile


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def _cov(values):
    """变异系数（标准差 / 均值），衡量一组样本的离散程度。"""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(var) / abs(mean)


def _country_of(node):
    return node.split("#")[-1] if "#" in node else ""


def _consistency(node, state, cfg):
    rec = state["ips"].get(node)
    recent = rec.get("recent", []) if rec else []
    if len(recent) >= int(cfg["STABILITY_MIN_SAMPLES"]):
        return 1.0 / (1.0 + _cov(recent))
    return 1.0


# ----------------------------------------------------------------------------
# 状态持久化
# ----------------------------------------------------------------------------
def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        s = {}
    s.setdefault("ips", {})
    s.setdefault("published", [])
    s.setdefault("published_by_country", {})
    s.setdefault("run_count", 0)
    return s


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
# 状态更新（每个运行调用一次）
# ----------------------------------------------------------------------------
def _update_state(state, scored_nodes, cfg):
    alpha = float(cfg["STABILITY_EMA_ALPHA"])
    recent_n = int(cfg["STABILITY_RECENT_N"])
    seen = set()

    for item in scored_nodes:
        node = item[0]
        raw = float(item[1])
        seen.add(node)
        rec = state["ips"].get(node)
        if rec is None:
            rec = {
                "ema": raw,
                "recent": [raw],
                "consecutive_top": 0,
                "absent": 0,
                "last_seen": "",
            }
            state["ips"][node] = rec
        else:
            rec["ema"] = alpha * raw + (1.0 - alpha) * rec["ema"]
            rec["recent"].append(raw)
            if len(rec["recent"]) > recent_n:
                rec["recent"] = rec["recent"][-recent_n:]
            rec["absent"] = 0
        rec["consecutive_top"] = rec.get("consecutive_top", 0) + 1
        rec["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")

    # 未在本轮出现的已追踪 IP：缺勤 +1，连续计数清零
    for node, rec in state["ips"].items():
        if node not in seen:
            rec["absent"] = rec.get("absent", 0) + 1
            rec["consecutive_top"] = 0

    state["run_count"] = state.get("run_count", 0) + 1


def _stable_score(node, raw, state, cfg):
    """稳定分 = EMA × 一致性。冷启动（样本不足）时退化为瞬时分。

    尖峰 IP 的样本离散度大 → 一致性低 → 稳定分被压低，无法顶掉现任。
    只有「持续」更好的 IP（EMA 高且一致性高）才会拿到高稳定分。
    """
    rec = state["ips"].get(node)
    ema = rec["ema"] if (rec and "ema" in rec) else raw
    return ema * _consistency(node, state, cfg)


# ----------------------------------------------------------------------------
# 单组（全局模式 = 一组；分国家模式 = 每个国家一组）的粘性选择
# ----------------------------------------------------------------------------
def _select_group(items, incumbents, n, cfg, state, published_set, bootstrap):
    """
    items:        [(node, stable_score), ...]
    incumbents:   当前该组已发布 IP 列表（按优先级排序）
    n:            该组要发布的数量
    返回选择后的列表（长度 <= n）。
    """
    HYS = float(cfg["STABILITY_HYSTERESIS_RATIO"])
    MAXABS = int(cfg["STABILITY_MAX_ABSENT_RUNS"])
    CONFIRM = int(cfg["STABILITY_CONFIRM_RUNS"])
    MIN_CONS = float(cfg["STABILITY_MIN_CONSISTENCY"])

    comp = {nd: c for nd, c in items}
    present = set(comp.keys())

    def confirmed(nd):
        rec = state["ips"].get(nd)
        return bool(rec) and rec.get("consecutive_top", 0) >= CONFIRM

    def allowed(nd):
        # 冷启动（还没有任何已发布 IP）：全部放行，先 bootstrap 出初始集合
        if bootstrap:
            return True
        # 已发布 IP 永远允许；全新 IP 需先被确认（连续多次出现）
        return (nd in published_set) or confirmed(nd)

    def can_displace(nd):
        """新晋候选要替换现任，还必须足够「稳」（一致性达标），否则视为尖峰。"""
        if nd in published_set:
            return True
        return _consistency(nd, state, cfg) >= MIN_CONS

    ranked = sorted(items, key=lambda x: -x[1])

    def best_allowed(exclude):
        for nd, _ in ranked:
            if nd in exclude:
                continue
            if allowed(nd):
                return nd
        return None

    chosen = []
    used = set()
    for slot in range(n):
        inc = incumbents[slot] if slot < len(incumbents) else None
        best = best_allowed(used)
        pick = None

        # 该现任若已被本轮回填过（多槽去重），视为空位
        if inc is not None and inc in used:
            inc = None

        if inc is not None:
            inc_present = inc in present
            if inc_present:
                inc_c = comp[inc]
                if best is not None and can_displace(best):
                    best_c = comp[best]
                    # 新候选必须明显更好（超过滞回比例）才替换现任
                    pick = best if best_c > inc_c * (1.0 + HYS) else inc
                else:
                    pick = inc
            else:
                inc_absent = state["ips"].get(inc, {}).get("absent", 0)
                if inc in published_set and inc_absent <= MAXABS:
                    pick = inc  # 短暂缺勤 → 宽限保留
                else:
                    pick = best  # 长期缺勤 → 放行新候选
        else:
            pick = best

        if pick is None:
            break
        chosen.append(pick)
        used.add(pick)

    return chosen


# ----------------------------------------------------------------------------
# 对外主入口
# ----------------------------------------------------------------------------
def apply_stability(state, scored_nodes, cfg, use_global, n_global, n_per_country):
    """
    scored_nodes: [(node, raw_score, ...), ...]（顺序任意）
    返回 (final_selected_flat_list, state)。
    """
    _update_state(state, scored_nodes, cfg)

    comp_items = [
        (item[0], _stable_score(item[0], float(item[1]), state, cfg))
        for item in scored_nodes
    ]

    published_set = set(state.get("published", []))
    for v in state.get("published_by_country", {}).values():
        published_set.update(v)
    bootstrap = len(published_set) == 0

    if use_global:
        incumbents = list(state.get("published", []))
        chosen = _select_group(
            comp_items, incumbents, int(n_global), cfg, state, published_set, bootstrap
        )
        state["published"] = chosen
        final = list(chosen)
    else:
        by_country = {}
        for node, c in comp_items:
            by_country.setdefault(_country_of(node), []).append((node, c))
        new_pub = {}
        final = []
        for country, items in by_country.items():
            inc = list(state.get("published_by_country", {}).get(country, []))
            ch = _select_group(
                items, inc, int(n_per_country), cfg, state, published_set, bootstrap
            )
            new_pub[country] = ch
            final.extend(ch)
        state["published_by_country"] = new_pub

    # 安全阀：绝不允许返回空集合（否则会触发最小产出保护、整体停摆）
    if not final and scored_nodes:
        top_n = max(1, int(n_global))
        final = [
            item[0]
            for item, _ in sorted(
                zip(scored_nodes, [c for _, c in comp_items]),
                key=lambda x: -x[1],
            )[:top_n]
        ]

    return final, state


# ----------------------------------------------------------------------------
# 自测：模拟「噪声导致原始排序抖动，但稳定性层应稳住现任」
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    cfg = {
        "STABILITY_EMA_ALPHA": 0.35,
        "STABILITY_RECENT_N": 8,
        "STABILITY_MIN_SAMPLES": 2,
        "STABILITY_HISTORY_WEIGHT": 0.6,  # 保留字段（兼容），本层实际用稳定分
        "STABILITY_HYSTERESIS_RATIO": 0.15,
        "STABILITY_CONFIRM_RUNS": 2,
        "STABILITY_MAX_ABSENT_RUNS": 3,
        "STABILITY_MIN_CONSISTENCY": 0.55,
    }

    # A/B 稳定（真实分 ~10），C 爱尖峰（有时 20 有时 3），叠加随机噪声
    import random
    random.seed(7)
    _tp = os.path.join(tempfile.gettempdir(), "cf_stab_selftest.json")
    if os.path.exists(_tp):
        os.remove(_tp)
    state = load_state(_tp)
    print("== 模拟 8 次运行（A/B 稳定，C 尖峰，叠加随机噪声）==")
    for r in range(1, 9):
        a = 10 + random.uniform(-0.4, 0.4)
        b = 9.5 + random.uniform(-0.4, 0.4)
        c = 20 if r in (2, 5) else 3 + random.uniform(-0.3, 0.3)
        scored = [
            ("1.1.1.1:443#US", a),
            ("2.2.2.2:443#US", b),
            ("3.3.3.3:443#US", c),
        ]
        final, state = apply_stability(
            state, scored, cfg, use_global=True, n_global=2, n_per_country=1
        )
        print(f"run {r}: C_raw={c:5.1f} -> published={final}")
    print("\n期望：A/B 长期占据发布位，C 的尖峰不应顶掉它们。")

    # 反例：若 C 持续稳定在 20（不再尖峰），应当替换 A/B
    print("\n== 反例：C 此后持续稳定在 ~20 ==")
    for r in range(9, 15):
        c = 20 + random.uniform(-0.3, 0.3)
        scored = [
            ("1.1.1.1:443#US", 10),
            ("2.2.2.2:443#US", 9.5),
            ("3.3.3.3:443#US", c),
        ]
        final, state = apply_stability(
            state, scored, cfg, use_global=True, n_global=2, n_per_country=1
        )
        print(f"run {r}: C_raw={c:5.1f} -> published={final}")
    print("期望：C 稳定后应逐渐进入发布位。")
