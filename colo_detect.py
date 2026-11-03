# -*- coding: utf-8 -*-
"""
colo_detect.py — 节点落地机场（Cloudflare colo）识别模块

借鉴 GFW Radar（776771.xyz）的"真实落地信息"思路：
通过 Cloudflare 内置接口 /cdn-cgi/trace 的返回体中的 colo=XXX 字段，
识别每个优选节点实际落到的 Cloudflare 数据中心（如 HKG/NRT/LAX/SJC），
并把机场代码写进节点备注（#US -> #US-LAX），方便客户端一眼看出落地。

说明：
- colo = 该节点把你带到 Cloudflare 哪个数据中心，是节点真实质量的
  关键信号（同标"US"的节点，落 SJC 和落 MIA 体验完全不同）。
- trace 返回体在 200/400 状态下都是纯文本，直接正则解析即可。
- 本模块任何异常都不应影响主流程，调用方需自行 try/except。

对外接口（与 main.py 的调用约定保持一致）：
    detect_colos(nodes, timeout=4, workers=16) -> {node: {"colo": str|None, "status": int|None}}
    retag_nodes(nodes, colo_map, speed_map=None, latency_map=None,
                http_latency_map=None, http_jitter_map=None)
        -> (new_nodes, new_speed_map, new_latency_map, new_http_lat, new_http_jit)
    node_colo(node_str) -> str   # 从节点备注提取机场代码，无则 ""
    rerank_by_colo(nodes, score_map, colo_map, preferred, boost=1.15)

可单独自测：python3 colo_detect.py "1.2.3.4:443#US" ...
"""

import json
import re
import shutil
import subprocess
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

warnings.filterwarnings("ignore", category=InsecureRequestWarning)

# 用于 SNI/Host 的 Cloudflare 官方域名（trace 接口在任意 CF 站点可用）
_SNI_HOST = "speed.cloudflare.com"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
}
_NO_PROXY = {"http": None, "https": None}
_TRACE_LINE_RE = re.compile(r"^([A-Za-z0-9_]+)=(.*)$", re.M)


def _parse_trace(text):
    """把 trace 纯文本解析成 dict（fl=... h=... colo=...）。"""
    try:
        return dict(_TRACE_LINE_RE.findall(text or ""))
    except Exception:
        return {}


def _detect_one(node_str, timeout):
    """单节点落地识别：优先 HTTPS+SNI（curl --resolve，对官方段和合作段都有效），
    其次退化到明文 HTTP 带 Host 头。"""
    ip_port = node_str.split("#", 1)[0].strip()
    if ":" in ip_port:
        ip, port = ip_port.rsplit(":", 1)
    else:
        ip, port = ip_port, "443"
    host = _SNI_HOST
    curl_path = shutil.which("curl") or "curl"
    is_v6 = ":" in ip  # IPv6 地址本身含冒号

    # 方式一：curl --resolve 强制 SNI+Host 指向节点 IP（推荐，覆盖最广）
    try:
        resolve_ip = f"[{ip}]" if is_v6 else ip
        r = subprocess.run(
            [curl_path, "-sk", "--max-time", str(int(timeout) + 2),
             "--resolve", f"{host}:{port}:{resolve_ip}",
             f"https://{host}:{port}/cdn-cgi/trace"],
            capture_output=True, timeout=int(timeout) + 4, text=True)
        fields = _parse_trace(r.stdout or "")
        colo = (fields.get("colo") or "").strip().upper() or None
        if colo:
            return node_str, {"colo": colo, "status": 200}
    except Exception:
        pass

    # 方式二：明文 HTTP 直连节点端口，Host 指向 CF 域名（部分节点 80 等端口可用）
    try:
        resp = requests.get(
            f"http://{ip}:{port}/cdn-cgi/trace",
            timeout=(timeout, timeout),
            verify=False,
            allow_redirects=False,
            headers={**_HEADERS, "Host": host},
            proxies=_NO_PROXY,
        )
        fields = _parse_trace(resp.text)
        colo = (fields.get("colo") or "").strip().upper() or None
        if colo:
            return node_str, {"colo": colo, "status": resp.status_code}
    except Exception:
        pass

    return node_str, {"colo": None, "status": None}


def detect_colos(nodes, timeout=4, workers=16):
    """并发识别一批节点的落地机场，返回 {node: {"colo": ..., "status": ...}}。

    失败的节点 colo 为 None，调用方应容忍并保持原备注。
    """
    result = {}
    if not nodes:
        return result
    workers = max(1, min(int(workers), len(nodes)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_detect_one, n, timeout) for n in nodes]
        for fut in as_completed(futures):
            try:
                node, info = fut.result()
            except Exception:
                continue
            result[node] = info
    return result


def node_colo(node_str):
    """从节点备注末尾提取机场代码（如 'US-LAX' -> 'LAX'），无则返回 ''。"""
    tag = node_str.split("#", 1)[1] if "#" in node_str else ""
    m = re.search(r"([A-Z]{3})$", tag.strip().upper())
    return m.group(1) if m else ""


def retag_nodes(nodes, colo_map, speed_map=None, latency_map=None,
                http_latency_map=None, http_jitter_map=None):
    """把机场代码写进节点备注（#US -> #US-LAX），并同步平移各统计字典的键。

    - 备注里已有三字机场代码的节点保持不变（防重复叠加）。
    - 识别失败的节点保持原样。
    返回 (new_nodes, new_speed_map, new_latency_map, new_http_lat, new_http_jit)。
    """
    rename = {}
    new_nodes = []
    for n in nodes:
        info = colo_map.get(n) or {}
        colo = info.get("colo")
        if not colo or node_colo(n):
            new_nodes.append(n)
            rename[n] = n
            continue
        if "#" in n:
            head, tag = n.split("#", 1)
            nn = f"{head}#{tag}-{colo}"
        else:
            nn = f"{n}#{colo}"
        new_nodes.append(nn)
        rename[n] = nn

    def _shift(m):
        if m is None:
            return None
        return {rename.get(k, k): v for k, v in m.items()}

    return (new_nodes, _shift(speed_map), _shift(latency_map),
            _shift(http_latency_map), _shift(http_jitter_map))


def colo_distribution(nodes):
    """统计一组节点的机场分布，返回 {colo: count}（无备注的归入"?"）。"""
    dist = {}
    for n in nodes:
        c = node_colo(n) or "?"
        dist[c] = dist.get(c, 0) + 1
    return dist


def format_distribution(nodes):
    """把机场分布格式化成 'HKG×3, LAX×2' 形式（按数量降序）。"""
    dist = colo_distribution(nodes)
    return ", ".join(f"{k}×{v}" for k, v in sorted(dist.items(), key=lambda x: -x[1]))


def rerank_by_colo(nodes, score_map, colo_map, preferred, boost=1.15):
    """按综合分 × 机场加成原地重排（优选落点在偏好列表里的节点）。

    仅轻微加成（默认 ×1.15），不会推翻速度/延迟为主的排序。
    """
    def _key(n):
        base = score_map.get(n, 0.0) or 0.0
        info = colo_map.get(n) or {}
        if info.get("colo") and info["colo"] in preferred:
            base *= boost
        return base

    nodes[:] = sorted(nodes, key=_key, reverse=True)


# ---------------- 单独自测 ----------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('用法: python3 colo_detect.py "1.2.3.4:443#US" [更多节点...]')
        sys.exit(1)
    out = detect_colos(sys.argv[1:])
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print("分布：" + format_distribution(sys.argv[1:]))
