#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ip_select.py —— 发布集合的多样性约束

解决的问题：优选结果经常在同一个 /24（甚至同一个 /16）里扎堆。
实测历史产出里 179.253.* 一段曾占据 12 个 IPv4 中的 5 个 —— 这段被墙或
机房下线时，整份订阅会一起失效，稳定性层再稳也救不回来。

做法：按当前排序（分数降序）贪心保留，限制每个 /24、每个 /16 的入选数量；
IPv6 按 /64 计数（同一 /64 里的地址实际是同一台机器，意义相同）。

单独自测：python ip_select.py
"""


def _v4_parts(host):
    return [int(x) for x in host.split(".")]


def prefix_key(node, bits=24):
    """取节点的网段键。IPv4 取前 bits 位（24 或 16），IPv6 取 /64。

    node 形如 "1.2.3.4:443#US" 或 "[2a06:98c1:58:0:1:2:3:4]:443#JP"。
    """
    host = node.split("#")[0]
    if host.startswith("["):                     # IPv6
        host = host[1:].split("]")[0]
        groups = host.split(":")
        return "v6:" + ":".join(groups[:4])      # /64
    host = host.rsplit(":", 1)[0]                # 去掉端口
    if bits == 24:
        return "v4:24:" + ".".join(host.split(".")[:3])
    if bits == 16:
        return "v4:16:" + ".".join(host.split(".")[:2])
    return "v4:" + host


def apply_diversity(nodes, max_per_24=2, max_per_16=4, max_per_64=2):
    """按传入顺序贪心保留，返回 (保留列表, 被剔除列表)。

    传入顺序通常已是分数降序，所以「先到先得」等价于「优先保留高分」。
    """
    kept, dropped = [], []
    c24, c16, c64 = {}, {}, {}
    for n in nodes:
        if n.split("#")[0].startswith("["):
            k = prefix_key(n)
            if c64.get(k, 0) >= max_per_64:
                dropped.append(n)
                continue
            c64[k] = c64.get(k, 0) + 1
        else:
            k24 = prefix_key(n, 24)
            k16 = prefix_key(n, 16)
            if c24.get(k24, 0) >= max_per_24 or c16.get(k16, 0) >= max_per_16:
                dropped.append(n)
                continue
            c24[k24] = c24.get(k24, 0) + 1
            c16[k16] = c16.get(k16, 0) + 1
        kept.append(n)
    return kept, dropped


if __name__ == "__main__":
    demo = [
        "179.253.225.34:443#US", "179.253.251.146:443#US", "179.253.255.159:443#US",
        "179.253.241.115:443#US", "131.143.180.53:443#US",
        "45.66.129.247:443#JP", "140.235.37.72:443#JP",
        "[2a06:98c1:58:0:a:1:2:3]:443#US", "[2a06:98c1:58:0:a:1:2:4]:443#US",
        "[2a06:98c1:58:0:a:1:2:5]:443#US",
    ]
    kept, dropped = apply_diversity(demo, max_per_24=2, max_per_16=4, max_per_64=2)
    print("保留:", *kept, sep="\n  ")
    print("剔除:", *dropped, sep="\n  ")
    print(f"\n{demo.__len__()} -> {len(kept)}（剔除 {len(dropped)}）")
