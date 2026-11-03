# -*- coding: utf-8 -*-
"""
longconn_monitor.py — 长连接稳定性监测模块（cf-ip-stable）

功能：对带宽测速筛出的头部节点做一段时间的持续探测（模拟“看一段视频”），
统计每个节点的连通率、平均延迟与抖动（标准差）。main.py 据此剔除监测期
断流节点、合并抖动惩罚、并把连通率乘进综合评分。

对外接口（与 main.py 的调用约定保持一致）：
    run_longconn_monitor(nodes, duration_sec, interval_sec, probe_timeout,
                         node_workers) -> dict

    nodes          : ["1.2.3.4:443#US", ...]
    duration_sec   : 监测总时长（秒）
    interval_sec   : 探测间隔（秒）
    probe_timeout  : 单次探测超时（秒）
    node_workers   : 同时监测的节点数（线程数）

返回：
    {
      node_str: {
        "success_rate": float,   # 连通率 0.0~1.0
        "avg_ms":       float,   # 成功探测的平均延迟（毫秒）
        "jitter_ms":    float,   # 成功探测延迟的标准差（毫秒）
        "probes":       int,     # 总探测次数
        "ok":           int,     # 成功次数
      }, ...
    }

探测语义与 main.py 的 HTTP 检测一致：
对 http://ip:port/cdn-cgi/trace 发 HEAD 请求，要求返回 400 且
Server 头以 cloudflare 开头，否则视为该次探测失败。

可单独自测：python3 longconn_monitor.py "1.2.3.4:443#US" [duration] [interval]
"""

import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

warnings.filterwarnings("ignore", category=InsecureRequestWarning)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA}
_NO_PROXY = {"http": None, "https": None}


def _probe_once(node_str, timeout):
    """对单个节点做一次 HTTP 探测。

    返回 (ok, latency_ms)。ok 表示该节点本轮探测是否为有效 Cloudflare 响应。
    """
    ip_port = node_str.split("#", 1)[0].strip()
    url = f"http://{ip_port}/cdn-cgi/trace"
    start = time.time()
    try:
        resp = requests.head(
            url,
            timeout=(timeout, timeout),
            verify=False,
            allow_redirects=False,
            headers=_HEADERS,
            proxies=_NO_PROXY,
        )
        lat = (time.time() - start) * 1000
        if resp.status_code != 400:
            return False, lat
        server = resp.headers.get("server", "")
        if not server.lower().startswith("cloudflare"):
            return False, lat
        return True, lat
    except Exception:
        return False, (time.time() - start) * 1000


def _monitor_node(node_str, duration_sec, interval_sec, probe_timeout,
                  stop_event):
    """单节点监测循环：每隔 interval_sec 探测一次，直到 duration_sec 结束。"""
    latencies = []
    probes = 0
    ok_count = 0
    deadline = time.time() + max(1, duration_sec)

    while not stop_event.is_set() and time.time() < deadline:
        ok, lat = _probe_once(node_str, probe_timeout)
        probes += 1
        if ok:
            ok_count += 1
            latencies.append(lat)
        # 等待下一轮；剩余时间不足一个间隔则提前结束
        remaining = deadline - time.time()
        stop_event.wait(min(interval_sec, max(0.0, remaining)))
        if time.time() >= deadline:
            break

    if probes <= 0:
        return node_str, None

    if latencies:
        n = len(latencies)
        avg = sum(latencies) / n
        var = sum((x - avg) ** 2 for x in latencies) / n
        jitter = var ** 0.5
    else:
        avg = 0.0
        jitter = 9999.0  # 一次都没成功：抖动记为极大值，让评分自然垫底

    metrics = {
        "success_rate": ok_count / probes,
        "avg_ms": round(avg, 2),
        "jitter_ms": round(jitter, 2),
        "probes": probes,
        "ok": ok_count,
    }
    return node_str, metrics


def run_longconn_monitor(nodes, duration_sec=60, interval_sec=5,
                         probe_timeout=3, node_workers=12):
    """对一批节点并行做长连接监测，返回 {node: metrics} 映射。

    任一异常都不会向外抛出（返回已完成的节点结果），保证主流程不因
    监测模块故障而中断。
    """
    if not nodes:
        return {}

    results = {}
    results_lock = threading.Lock()

    def _worker(node):
        stop_event = threading.Event()
        try:
            _, metrics = _monitor_node(node, duration_sec, interval_sec,
                                       probe_timeout, stop_event)
        except Exception:
            metrics = None
        if metrics is not None:
            with results_lock:
                results[node] = metrics

    workers = max(1, min(int(node_workers or 12), len(nodes)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_worker, n) for n in nodes]
        for _ in as_completed(futures):
            pass  # 结果在 _worker 内加锁写入

    return results


# ---------------- 单独自测 ----------------
if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print('用法: python3 longconn_monitor.py "1.2.3.4:443#US" [duration] [interval]')
        sys.exit(1)

    _nodes = sys.argv[1:]
    _duration = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    _interval = int(sys.argv[3]) if len(sys.argv) > 3 else 3

    out = run_longconn_monitor(_nodes, duration_sec=_duration,
                               interval_sec=_interval)
    print(json.dumps(out, ensure_ascii=False, indent=2))
