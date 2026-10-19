#!/usr/bin/env python3
"""
Cloudflare IP 优选工具 (TCP筛选 + IP可用性二次筛选 + HTTP检测 + curl带宽测速 + WxPusher通知)
依赖：requests, curl, aiohttp
配置文件：同目录下的 config.json
结果保存到 ip.txt，并自动推送到 GitHub，同时批量更新到 Cloudflare DNS
支持 Windows / Linux
"""

import requests
import socket
import time
import sys
import re
import os
import subprocess
import shutil
import json
import random
import asyncio
import aiohttp
import ipaddress
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib3.exceptions import InsecureRequestWarning

# 修复 Windows 下 ProactorEventLoop 残留任务报警
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 禁用 SSL 警告 (仅用于对 IP 直连的探测请求)
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# 稳定性增强层（独立模块，与脚本同目录，可被单独自测）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stability_layer

# ===== SSL 校验策略 =====
# 探测类请求（直连任意 IP、节点证书不可信）显式传 verify=False；
# 涉及凭证的请求（Cloudflare API / WxPusher / GitHub）保持默认校验，
# 避免 token 在中间人场景下被截获。
# 此前这里用猴补丁把 requests.get/post 全局设成 verify=False，
# 会让 Cloudflare API 的 Bearer token 走未校验证书的连接。
# ===========================================

# ==================== 预编译正则 ====================
# 同时支持 IPv4 与带方括号的 IPv6（[ipv6]:port[#country]）。
_IPV4 = r"\d+\.\d+\.\d+\.\d+"
_IPV6 = r"\[[0-9A-Fa-f:]+\]"
NODE_PATTERN = re.compile(r"^(" + _IPV4 + r"|" + _IPV6 + r"):(\d+)#(.+)$")
IP_PORT_PATTERN = re.compile(r"^(" + _IPV4 + r"|" + _IPV6 + r"):(\d+)#")


# ----------------------------------------------------------------------------
# IPv6 候选生成（Cloudflare IPv6 段随机采样）
# IPv6 地址空间巨大无法全扫，按配置比例从 CF IPv6 段随机抽取，
# 后续由 TCP/HTTP/带宽测试自动筛掉未宣告/不可达的地址。
# ----------------------------------------------------------------------------
def _tcp_open_v6(addr, port, timeout):
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((str(addr), int(port)))
        return True
    except Exception:
        return False


def _discover_ipv6_seeds():
    """解析 Cloudflare 主机名 AAAA 记录拿到『确定可达』的 IPv6 种子地址；
    若解析失败（如环境无 IPv6 DNS）则退回内置种子。返回 IPv6Address 列表。"""
    hosts = cfg.get("IPV6_SEED_HOSTS") or ["speed.cloudflare.com", "www.cloudflare.com", "cloudflare.com"]
    seeds = []
    seen = set()
    for h in hosts:
        try:
            infos = socket.getaddrinfo(h, 443, socket.AF_INET6, socket.SOCK_STREAM)
            for info in infos:
                ip = info[4][0]
                if ":" in ip and not ip.startswith("::ffff:"):
                    a = ipaddress.ip_address(ip)
                    if a not in seen:
                        seen.add(a)
                        seeds.append(a)
        except Exception:
            continue
    if not seeds:
        for s in (cfg.get("IPV6_FALLBACK_SEEDS") or []):
            try:
                a = ipaddress.ip_address(s)
                if a not in seen:
                    seen.add(a)
                    seeds.append(a)
            except Exception:
                continue
    return seeds


def _discover_live_prefixes(seeds):
    """对每个种子所在 /48，扫描其 256 个 /64，返回『可达』的 /64 前缀列表。"""
    if not seeds:
        return []
    workers = int(IPV6_SCAN_WORKERS)
    timeout = float(IPV6_SCAN_TIMEOUT)
    tgt48 = []
    seen48 = set()
    for s in seeds:
        try:
            # strict=False 掩掉主机位，使种子地址归一到其 /48 网络地址
            net48 = ipaddress.ip_network(f"{s}/48", strict=False)
        except Exception:
            continue
        if net48 not in seen48:
            seen48.add(net48)
            tgt48.append(net48)
    if not tgt48:
        return []
    tasks = []
    for net48 in tgt48:
        for x in range(0, 256):
            tasks.append((x, net48))

    def probe(item):
        x, net48 = item
        # 在 /48 内枚举 256 个不同的 /64 子网：/64 索引位于第 4 段（bits 48-63）
        addr = ipaddress.IPv6Address(int(net48.network_address) + (x << 48))
        return net48, x, _tcp_open_v6(addr, 443, timeout)

    live = []
    try:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(tasks)))) as ex:
            for net48, x, ok in ex.map(probe, tasks):
                if ok:
                    addr64 = ipaddress.IPv6Address(int(net48.network_address) + (x << 48))
                    # strict=False 掩掉接口ID，得到对齐的 /64 网络地址
                    live.append(ipaddress.ip_network(f"{addr64}/64", strict=False))
    except Exception:
        pass
    return live


def generate_ipv6_candidates():
    """发现可达的 Cloudflare IPv6 /64 前缀，并在其中随机变换接口ID采样，
    返回 [ipv6]:port#IPV6 节点列表。只采样确认可达的前缀，避免整段 /32 随机
    抽不到（CF 仅在特定已宣告前缀响应）。"""
    count = int(IPV6_SAMPLE_COUNT)
    port = int(IPV6_PORT)
    placeholder = IPV6_PLACEHOLDER_COUNTRY
    if count <= 0:
        return []
    seeds = _discover_ipv6_seeds()
    if not seeds:
        return []
    live64 = _discover_live_prefixes(seeds)
    if not live64:
        # 退化：直接用种子所在 /64 采样
        for s in seeds:
            try:
                live64.append(ipaddress.ip_network(f"{s}/64", strict=False))
            except Exception:
                pass
    if not live64:
        return []
    random.shuffle(live64)
    per = max(1, count // len(live64))
    nodes = []
    seen = set()
    for net64 in live64:
        base = int(net64.network_address)
        got = 0
        attempts = 0
        while got < per and attempts < per * 4:
            attempts += 1
            addr = ipaddress.IPv6Address(base + random.getrandbits(64))
            node = "[%s]:%d#%s" % (addr, port, placeholder)
            if node not in seen:
                seen.add(node)
                nodes.append(node)
                got += 1
    return nodes


def _is_ipv6_node(node_str):
    return node_str.startswith("[")


def _node_port(node_str):
    """返回节点端口（兼容 IPv4 / IPv6），解析失败返回 None。"""
    if node_str.startswith("["):
        m = re.match(r"^\[[0-9A-Fa-f:]+\]:(\d+)", node_str)
    else:
        m = re.match(r"^\d+\.\d+\.\d+\.\d+:(\d+)", node_str)
    return m.group(1) if m else None


def _ensure_ipv6_representation(final_selected, v6_pool):
    """保证合并输出的 ip.txt 中始终含至少 IPV6_MIN_IN_OUTPUT 个 IPv6 节点。

    v6_pool: IPv6 候选池，元素为 (node, score)。优先用带带宽得分的节点；
    若带宽探测失败导致 scored_nodes 为空，则退回「已通过 TCP + HTTP /cdn-cgi/trace
    校验」的 IPv6 节点（已确属真实 Cloudflare 边缘），保证不丢 IPv6。
    若数量仍不足，用得分最高的 IPv6 顶替得分最低的 IPv4，腾出名额。"""
    min_v6 = int(IPV6_MIN_IN_OUTPUT)
    if min_v6 <= 0:
        return final_selected
    v6_in = [n for n in final_selected if _is_ipv6_node(n)]
    if len(v6_in) >= min_v6:
        return final_selected
    # 同一节点取最高得分（带宽得分 > HTTP 校验保底分）
    best = {}
    for n, s in v6_pool:
        if _is_ipv6_node(n) and (n not in best or s > best[n]):
            best[n] = s
    pool = sorted(best.items(), key=lambda x: x[1], reverse=True)
    selected = set(final_selected)
    pool = [it for it in pool if it[0] not in selected]
    need = min_v6 - len(v6_in)
    if not pool:
        return final_selected
    removable = sorted([n for n in final_selected if not _is_ipv6_node(n)],
                       key=lambda n: best.get(n, 0.0))
    result = list(final_selected)
    added = 0
    for node, _ in pool:
        if added >= need:
            break
        if removable:
            result.remove(removable.pop(0))
        result.append(node)
        added += 1
    return result


# ----------------------------------------------------------------------------
# IPv6 地区解析（仅用于展示）：免费、无 token 的 GeoIP API，带本地缓存。
# 在「全部过滤 + 稳定性筛选」之后才解析，因此不会因真实地区命中黑名单而误删
# IPv6 节点（保活优先）；解析失败或缺网则回退占位 "IPV6"。
# ----------------------------------------------------------------------------
_IPV6_GEO_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ipv6_geo_cache.json")


def _load_ipv6_geo_cache():
    try:
        with open(_IPV6_GEO_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_ipv6_geo_cache(cache):
    try:
        with open(_IPV6_GEO_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _geo_lookup(ip):
    """免费无 key 的 GeoIP 查询，返回两位大写国家码或空串。"""
    apis = [
        f"https://ipwho.is/{ip}",
        f"https://ipapi.co/{ip}/json/",
    ]
    for url in apis:
        try:
            r = requests.get(
                url, timeout=4, verify=False,
                headers={"User-Agent": "Mozilla/5.0 (compatible; cf-ip-stable/1.0)"})
            if r.status_code != 200:
                continue
            d = r.json()
            cc = (d.get("country_code") or d.get("countryCode") or "").upper()
            if len(cc) == 2 and cc.isalpha():
                return cc
        except Exception:
            continue
    return ""


def resolve_ipv6_countries(nodes):
    """为最终输出的 IPv6 节点解析真实国家，返回 {node: 'XX'}（失败保留 'IPV6'）。"""
    cache = _load_ipv6_geo_cache()  # {ip: cc}
    result = {}
    changed = False
    for n in nodes:
        if not _is_ipv6_node(n):
            continue
        ip = n.split(']:')[0].lstrip('[')
        cc = cache.get(ip)
        if not cc:
            cc = _geo_lookup(ip) or "IPV6"
            if cc != "IPV6":
                cache[ip] = cc
                changed = True
        result[n] = cc
    if changed:
        _save_ipv6_geo_cache(cache)
    return result


# ==================== 国家代码映射表（全球覆盖）====================
CN_TO_CODE = {
    "阿富汗": "AF", "奥兰群岛": "AX", "阿尔巴尼亚": "AL", "阿尔及利亚": "DZ",
    "美属萨摩亚": "AS", "安道尔": "AD", "安哥拉": "AO", "安圭拉": "AI",
    "南极洲": "AQ", "安提瓜和巴布达": "AG", "阿根廷": "AR", "亚美尼亚": "AM",
    "阿鲁巴": "AW", "澳大利亚": "AU", "奥地利": "AT", "阿塞拜疆": "AZ",
    "巴哈马": "BS", "巴林": "BH", "孟加拉国": "BD", "孟加拉": "BD",
    "巴巴多斯": "BB", "白俄罗斯": "BY", "比利时": "BE", "伯利兹": "BZ",
    "贝宁": "BJ", "百慕大": "BM", "不丹": "BT", "玻利维亚": "BO",
    "波黑": "BA", "波斯尼亚和黑塞哥维那": "BA", "博茨瓦纳": "BW",
    "布维岛": "BV", "巴西": "BR", "英属印度洋领地": "IO",
    "文莱": "BN", "保加利亚": "BG", "布基纳法索": "BF", "布隆迪": "BI",
    "柬埔寨": "KH", "喀麦隆": "CM", "加拿大": "CA", "佛得角": "CV",
    "开曼群岛": "KY", "中非": "CF", "乍得": "TD", "智利": "CL",
    "中国": "CN", "圣诞岛": "CX", "科科斯(基林)群岛": "CC",
    "哥伦比亚": "CO", "科摩罗": "KM", "刚果(布)": "CG", "刚果（布）": "CG",
    "刚果(金)": "CD", "刚果（金）": "CD", "库克群岛": "CK",
    "哥斯达黎加": "CR", "科特迪瓦": "CI", "克罗地亚": "HR", "古巴": "CU",
    "塞浦路斯": "CY", "捷克": "CZ", "丹麦": "DK", "吉布提": "DJ",
    "多米尼克": "DM", "多米尼加": "DO", "厄瓜多尔": "EC", "埃及": "EG",
    "萨尔瓦多": "SV", "赤道几内亚": "GQ", "厄立特里亚": "ER",
    "爱沙尼亚": "EE", "埃塞俄比亚": "ET", "福克兰群岛(马尔维纳斯)": "FK",
    "法罗群岛": "FO", "斐济": "FJ", "芬兰": "FI", "法国": "FR",
    "法属圭亚那": "GF", "法属波利尼西亚": "PF", "法属南部领地": "TF",
    "加蓬": "GA", "冈比亚": "GM", "格鲁吉亚": "GE", "德国": "DE",
    "加纳": "GH", "直布罗陀": "GI", "希腊": "GR", "格陵兰": "GL",
    "格林纳达": "GD", "瓜德罗普": "GP", "关岛": "GU", "危地马拉": "GT",
    "根西岛": "GG", "几内亚": "GN", "几内亚比绍": "GW", "圭亚那": "GY",
    "海地": "HT", "赫德岛和麦克唐纳群岛": "HM", "梵蒂冈": "VA",
    "洪都拉斯": "HN", "香港": "HK", "匈牙利": "HU", "冰岛": "IS",
    "印度": "IN", "印度尼西亚": "ID", "伊朗": "IR", "伊拉克": "IQ",
    "爱尔兰": "IE", "马恩岛": "IM", "以色列": "IL", "意大利": "IT",
    "牙买加": "JM", "日本": "JP", "泽西岛": "JE", "约旦": "JO",
    "哈萨克斯坦": "KZ", "肯尼亚": "KE", "基里巴斯": "KI", "朝鲜": "KP",
    "韩国": "KR", "科威特": "KW", "吉尔吉斯斯坦": "KG", "老挝": "LA",
    "拉脱维亚": "LV", "黎巴嫩": "LB", "莱索托": "LS", "利比里亚": "LR",
    "利比亚": "LY", "列支敦士登": "LI", "立陶宛": "LT", "卢森堡": "LU",
    "澳门": "MO", "北马其顿": "MK", "马其顿": "MK", "马达加斯加": "MG",
    "马拉维": "MW", "马来西亚": "MY", "马尔代夫": "MV", "马里": "ML",
    "马耳他": "MT", "马绍尔群岛": "MH", "马提尼克": "MQ",
    "毛里塔尼亚": "MR", "毛里求斯": "MU", "马约特": "YT", "墨西哥": "MX",
    "密克罗尼西亚": "FM", "摩尔多瓦": "MD", "摩纳哥": "MC", "蒙古": "MN",
    "黑山": "ME", "蒙特塞拉特": "MS", "摩洛哥": "MA", "莫桑比克": "MZ",
    "缅甸": "MM", "纳米比亚": "NA", "瑙鲁": "NR", "尼泊尔": "NP",
    "荷兰": "NL", "新喀里多尼亚": "NC", "新西兰": "NZ", "尼加拉瓜": "NI",
    "尼日尔": "NE", "尼日利亚": "NG", "纽埃": "NU", "诺福克岛": "NF",
    "北马里亚纳群岛": "MP", "挪威": "NO", "阿曼": "OM", "巴基斯坦": "PK",
    "帕劳": "PW", "巴勒斯坦": "PS", "巴拿马": "PA", "巴布亚新几内亚": "PG",
    "巴拉圭": "PY", "秘鲁": "PE", "菲律宾": "PH", "皮特凯恩": "PN",
    "波兰": "PL", "葡萄牙": "PT", "波多黎各": "PR", "卡塔尔": "QA",
    "留尼汪": "RE", "罗马尼亚": "RO", "俄罗斯": "RU", "卢旺达": "RW",
    "圣巴泰勒米": "BL", "圣赫勒拿": "SH", "圣基茨和尼维斯": "KN",
    "圣卢西亚": "LC", "圣马丁": "MF", "圣皮埃尔和密克隆": "PM",
    "圣文森特和格林纳丁斯": "VC", "萨摩亚": "WS", "圣马力诺": "SM",
    "圣多美和普林西比": "ST", "沙特阿拉伯": "SA", "沙特": "SA",
    "塞内加尔": "SN", "塞尔维亚": "RS", "塞舌尔": "SC", "塞拉利昂": "SL",
    "新加坡": "SG", "圣马丁(荷兰)": "SX", "斯洛伐克": "SK",
    "斯洛文尼亚": "SI", "所罗门群岛": "SB", "索马里": "SO", "南非": "ZA",
    "南乔治亚和南桑威奇群岛": "GS", "南苏丹": "SS", "西班牙": "ES",
    "斯里兰卡": "LK", "苏丹": "SD", "苏里南": "SR", "斯瓦尔巴和扬马延": "SJ",
    "斯威士兰": "SZ", "瑞典": "SE", "瑞士": "CH", "叙利亚": "SY",
    "台湾": "TW", "塔吉克斯坦": "TJ", "坦桑尼亚": "TZ", "泰国": "TH",
    "东帝汶": "TL", "多哥": "TG", "托克劳": "TK", "汤加": "TO",
    "特立尼达和多巴哥": "TT", "突尼斯": "TN", "土耳其": "TR",
    "土库曼斯坦": "TM", "特克斯和凯科斯群岛": "TC", "图瓦卢": "TV",
    "乌干达": "UG", "乌克兰": "UA", "阿联酋": "AE", "英国": "GB",
    "美国": "US", "美国本土外小岛屿": "UM", "乌拉圭": "UY",
    "乌兹别克斯坦": "UZ", "瓦努阿图": "VU", "委内瑞拉": "VE",
    "越南": "VN", "英属维尔京群岛": "VG", "美属维尔京群岛": "VI",
    "瓦利斯和富图纳": "WF", "西撒哈拉": "EH", "也门": "YE",
    "赞比亚": "ZM", "津巴布韦": "ZW",
}

# 三位字母国家代码 → 两位字母国家代码（ISO 3166-1 alpha-3 → alpha-2）
ALPHA3_TO_ALPHA2 = {
    "AFG": "AF", "ALA": "AX", "ALB": "AL", "DZA": "DZ", "ASM": "AS",
    "AND": "AD", "AGO": "AO", "AIA": "AI", "ATA": "AQ", "ATG": "AG",
    "ARG": "AR", "ARM": "AM", "ABW": "AW", "AUS": "AU", "AUT": "AT",
    "AZE": "AZ", "BHS": "BS", "BHR": "BH", "BGD": "BD", "BRB": "BB",
    "BLR": "BY", "BEL": "BE", "BLZ": "BZ", "BEN": "BJ", "BMU": "BM",
    "BTN": "BT", "BOL": "BO", "BIH": "BA", "BWA": "BW", "BVT": "BV",
    "BRA": "BR", "IOT": "IO", "BRN": "BN", "BGR": "BG", "BFA": "BF",
    "BDI": "BI", "KHM": "KH", "CMR": "CM", "CAN": "CA", "CPV": "CV",
    "CYM": "KY", "CAF": "CF", "TCD": "TD", "CHL": "CL", "CHN": "CN",
    "CXR": "CX", "CCK": "CC", "COL": "CO", "COM": "KM", "COG": "CG",
    "COD": "CD", "COK": "CK", "CRI": "CR", "CIV": "CI", "HRV": "HR",
    "CUB": "CU", "CYP": "CY", "CZE": "CZ", "DNK": "DK", "DJI": "DJ",
    "DMA": "DM", "DOM": "DO", "ECU": "EC", "EGY": "EG", "SLV": "SV",
    "GNQ": "GQ", "ERI": "ER", "EST": "EE", "ETH": "ET", "FLK": "FK",
    "FRO": "FO", "FJI": "FJ", "FIN": "FI", "FRA": "FR", "GUF": "GF",
    "PYF": "PF", "ATF": "TF", "GAB": "GA", "GMB": "GM", "GEO": "GE",
    "DEU": "DE", "GHA": "GH", "GIB": "GI", "GRC": "GR", "GRL": "GL",
    "GRD": "GD", "GLP": "GP", "GUM": "GU", "GTM": "GT", "GGY": "GG",
    "GIN": "GN", "GNB": "GW", "GUY": "GY", "HTI": "HT", "HMD": "HM",
    "VAT": "VA", "HND": "HN", "HKG": "HK", "HUN": "HU", "ISL": "IS",
    "IND": "IN", "IDN": "ID", "IRN": "IR", "IRQ": "IQ", "IRL": "IE",
    "IMN": "IM", "ISR": "IL", "ITA": "IT", "JAM": "JM", "JPN": "JP",
    "JEY": "JE", "JOR": "JO", "KAZ": "KZ", "KEN": "KE", "KIR": "KI",
    "PRK": "KP", "KOR": "KR", "KWT": "KW", "KGZ": "KG", "LAO": "LA",
    "LVA": "LV", "LBN": "LB", "LSO": "LS", "LBR": "LR", "LBY": "LY",
    "LIE": "LI", "LTU": "LT", "LUX": "LU", "MAC": "MO", "MKD": "MK",
    "MDG": "MG", "MWI": "MW", "MYS": "MY", "MDV": "MV", "MLI": "ML",
    "MLT": "MT", "MHL": "MH", "MTQ": "MQ", "MRT": "MR", "MUS": "MU",
    "MYT": "YT", "MEX": "MX", "FSM": "FM", "MDA": "MD", "MCO": "MC",
    "MNG": "MN", "MNE": "ME", "MSR": "MS", "MAR": "MA", "MOZ": "MZ",
    "MMR": "MM", "NAM": "NA", "NRU": "NR", "NPL": "NP", "NLD": "NL",
    "NCL": "NC", "NZL": "NZ", "NIC": "NI", "NER": "NE", "NGA": "NG",
    "NIU": "NU", "NFK": "NF", "MNP": "MP", "NOR": "NO", "OMN": "OM",
    "PAK": "PK", "PLW": "PW", "PSE": "PS", "PAN": "PA", "PNG": "PG",
    "PRY": "PY", "PER": "PE", "PHL": "PH", "PCN": "PN", "POL": "PL",
    "PRT": "PT", "PRI": "PR", "QAT": "QA", "REU": "RE", "ROU": "RO",
    "RUS": "RU", "RWA": "RW", "BLM": "BL", "SHN": "SH", "KNA": "KN",
    "LCA": "LC", "MAF": "MF", "SPM": "PM", "VCT": "VC", "WSM": "WS",
    "SMR": "SM", "STP": "ST", "SAU": "SA", "SEN": "SN", "SRB": "RS",
    "SYC": "SC", "SLE": "SL", "SGP": "SG", "SXM": "SX", "SVK": "SK",
    "SVN": "SI", "SLB": "SB", "SOM": "SO", "ZAF": "ZA", "SGS": "GS",
    "SSD": "SS", "ESP": "ES", "LKA": "LK", "SDN": "SD", "SUR": "SR",
    "SJM": "SJ", "SWZ": "SZ", "SWE": "SE", "CHE": "CH", "SYR": "SY",
    "TWN": "TW", "TJK": "TJ", "TZA": "TZ", "THA": "TH", "TLS": "TL",
    "TGO": "TG", "TKL": "TK", "TON": "TO", "TTO": "TT", "TUN": "TN",
    "TUR": "TR", "TKM": "TM", "TCA": "TC", "TUV": "TV", "UGA": "UG",
    "UKR": "UA", "ARE": "AE", "GBR": "GB", "USA": "US", "UMI": "UM",
    "URY": "UY", "UZB": "UZ", "VUT": "VU", "VEN": "VE", "VNM": "VN",
    "VGB": "VG", "VIR": "VI", "WLF": "WF", "ESH": "EH", "YEM": "YE",
    "ZMB": "ZM", "ZWE": "ZW",
}

# 构建两位有效代码集合，用于快速校验
CODE_SET = set(CN_TO_CODE.values())


# ==================== 加载配置文件 ====================
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def load_config():
    """加载 config.json 配置文件，缺失必填字段时抛出异常"""
    defaults = {
        "USE_GLOBAL_MODE": True,
        "GLOBAL_TOP_N": 15,
        "PER_COUNTRY_TOP_N": 1,
        "BANDWIDTH_CANDIDATES": 150,
        "TCP_PROBES": 1,
        "MIN_SUCCESS_RATE": 1.0,
        "TCP_LATENCY_WEIGHT": 0.0,
        "TIMEOUT": 2.0,
        "SOCKET_DEFAULT_TIMEOUT": 3,
        "PROGRESS_PRINT_INTERVAL": 1,
        "FILTER_COUNTRIES_ENABLED": False,
        "ALLOWED_COUNTRIES": ["US"],
        "PRE_FILTER_BLOCKED_ENABLED": True,
        "PRE_FILTER_BLOCKED_COUNTRIES": ["CN"],
        "PRE_FILTER_PORT_ENABLED": True,
        "PRE_FILTER_PORTS": [443],
        "ENABLE_WXPUSHER": True,
        "WXPUSHER_APP_TOKEN": "your_app_token_here",
        "WXPUSHER_UIDS": ["your_uid_here"],
        "WXPUSHER_API_URL": "https://wxpusher.zjiecode.com/api/send/message",
        "NOTIFY_TIMEOUT": 3,
        "NOTIFY_CONNECT_TIMEOUT": 3,
        "CF_ENABLED": True,
        "CF_API_TOKEN": "your_CF_API_TOKEN",
        "CF_ZONE_ID": "your_CF_ZONE_ID",
        "CF_DNS_RECORD_NAME": "your_CF_DNS_RECORD_NAME",
        "CF_TTL": 60,
        "CF_PROXIED": False,
        "CF_DNS_CONNECT_TIMEOUT": 3,
        "CF_DNS_READ_TIMEOUT": 3,
        "DNS_RECORD_TYPE": "TXT",
        "ADDITIONAL_SOURCES": [
    {
        "url": "https://zip.cm.edu.kg/all.txt",
        "enabled": True
    },
    {
        "url": "https://countrymerge.pages.dev/all.txt",
        "enabled": True
    }
],
        "FETCH_MAX_RETRIES": 3,
        "FETCH_RETRY_DELAY": 3,
        "FETCH_TIMEOUT": 3,
        "FETCH_CONNECT_TIMEOUT": 3,
        "IP_CALIBRATION_ENABLED": False,
        "TOKEN_FAILURE_THRESHOLD": 3,
        "IP_CALIBRATION_MIN_INTERVAL": 0.1,
        "IP_CALIBRATION_TOKEN_FILE": "valid_tokens.txt",
        "IP_CALIBRATION_CACHE_FILE": "ipinfo_cache.txt",
        "OUTPUT_FILE": "ip.txt",
        "MIN_OUTPUT_NODES": 8,
        "MIN_KEEP_RATIO": 0.6,
        "ENABLE_LOGGING": False,
        "LOG_FILE": "cfnb.log",
        "FORCE_DIRECT": False,
        "TEST_AVAILABILITY": True,
        "AVAILABILITY_CHECK_API": "https://api.090227.xyz/check",
        "AVAILABILITY_TIMEOUT": 3,
        "AVAILABILITY_CONNECT_TIMEOUT": 3,
        "AVAILABILITY_RETRY_MAX": 2,
        "AVAILABILITY_RETRY_DELAY": 3,
        "AVAILABILITY_INNER_RETRY_ENABLED": True,
        "AVAILABILITY_INNER_RETRY_MAX": 2,
        "AVAILABILITY_INNER_RETRY_DELAY": 3,
        "HTTP_TEST_ENABLED": True,
        "HTTP_TEST_TIMEOUT": 3,
        "HTTP_TEST_CONNECT_TIMEOUT": 3,
        "HTTP_TEST_MAX_ROUNDS": 2,
        "HTTP_TEST_ROUND_DELAY": 3,
        "HTTP_TEST_INNER_RETRY_ENABLED": True,
        "HTTP_TEST_MAX_RETRIES": 2,
        "HTTP_TEST_RETRY_DELAY": 3,
        "HTTP_TEST_METHOD": "HEAD",
        "HTTP_LATENCY_WEIGHT": 3.0,
        "JITTER_WEIGHT": 3.0,
        "HTTP_JITTER_SAMPLES": 3,
        "FILTER_IPV6_AVAILABILITY": True,
        "FILTER_BLOCKED_COUNTRIES_ENABLED": True,
        "BLOCKED_COUNTRIES": [
            "BD", "BI", "BY", "CD", "CF", "CN", "CU", "DE", "ET", "HK",
            "IR", "KP", "LY", "MO", "NG", "NL", "PK", "RU", "SD", "SO",
            "SY", "TH", "TW", "UA", "VE", "VN", "YE", "ZW"
        ],
        "DNS_IP_RISK_FILTER_ENABLED": False,
        "DNS_IP_RISK_MAX_LEVEL": "高风险",
        "DNS_UPDATE_TARGET_COUNT": 15,
        "BANDWIDTH_SIZE_MB": 1.0,
        "BANDWIDTH_TIMEOUT": 3,
        "BANDWIDTH_RETRY_MAX": 2,
        "BANDWIDTH_RETRY_DELAY": 3,
        "BANDWIDTH_URL_TEMPLATE": "{scheme}://speed.cloudflare.com:{port}/__down?bytes={bytes}",
        "BANDWIDTH_PROCESS_BUFFER": 2,
        "BANDWIDTH_CONNECT_TIMEOUT": 3,
        "BANDWIDTH_SAMPLES": 2,
        "SPEED_WEIGHT": 3.0,
        "IP_CALIBRATION_CONCURRENCY": 300,
        "MAX_WORKERS": 300,
        "AVAILABILITY_WORKERS": 32,
        "FALLBACK_WORKERS": 32,
        "BANDWIDTH_WORKERS": 3,
        "HTTP_TEST_WORKERS": 32,
        "DNS_UPDATE_MAX_RETRIES": 3,
        "DNS_UPDATE_RETRY_DELAY": 3,
        "GITHUB_SYNC_MAX_RETRIES": 3,
        "GITHUB_SYNC_RETRY_DELAY": 3,
        "GIT_SYNC_PROCESS_TIMEOUT": 180,
        "AD_HEADER_ENABLED": False,
        "AD_HEADER_LINES": [],
        "AD_FOOTER_ENABLED": False,
        "AD_FOOTER_LINES": [],
        "AD_PERLINE_ENABLED": False,
        "AD_PERLINE_TEXT": "",
        "IP_TXT_SHOW_BANDWIDTH": False,
        "IP_TXT_SHOW_HTTP_LATENCY": False,
        "IP_TXT_SHOW_HTTP_JITTER": False,
        "IP_TXT_SHOW_LATENCY": False,
        # ===== 稳定性增强层（解决选中 IP 反复跳动）=====
        "STABILITY_ENABLED": True,
        "STABILITY_STATE_FILE": "stability_state.json",
        "STABILITY_EMA_ALPHA": 0.35,
        "STABILITY_RECENT_N": 8,
        "STABILITY_MIN_SAMPLES": 2,
        "STABILITY_HISTORY_WEIGHT": 0.6,
        "STABILITY_HYSTERESIS_RATIO": 0.15,
        "STABILITY_CONFIRM_RUNS": 2,
        "STABILITY_MAX_ABSENT_RUNS": 3,
        "STABILITY_MIN_CONSISTENCY": 0.55,
        # ===== IPv6 支持（合并进 ip.txt）=====
        "IPV6_ENABLED": True,
        "IPV6_SEED_HOSTS": ["speed.cloudflare.com", "www.cloudflare.com", "cloudflare.com"],
        "IPV6_FALLBACK_SEEDS": ["2400:cb00:2049:1::a29f:2020", "2606:4700:4700::1111"],
        "IPV6_SCAN_TIMEOUT": 1.0,
        "IPV6_SCAN_WORKERS": 256,
        "IPV6_SAMPLE_COUNT": 300,
        "IPV6_PORT": 443,
        "IPV6_PLACEHOLDER_COUNTRY": "IPV6",
        "IPV6_CANDIDATE_RESERVE": 30,
        "IPV6_MIN_IN_OUTPUT": 5,
    }

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        print(f"未找到配置文件 {CONFIG_FILE}，将使用内置默认配置运行。")
        print(f"你可根据需要创建 config.json 文件（参考文档），程序会自动识别。")
        return defaults
    except json.JSONDecodeError as e:
        print(f"错误：配置文件格式不正确 - {e}")
        sys.exit(1)

    for key, value in defaults.items():
        if key not in config:
            config[key] = value
            print(f"配置项 {key} 未设置，使用默认值：{value}")

    return config

cfg = load_config()
USE_GLOBAL_MODE = cfg["USE_GLOBAL_MODE"]
GLOBAL_TOP_N = cfg["GLOBAL_TOP_N"]
PER_COUNTRY_TOP_N = cfg["PER_COUNTRY_TOP_N"]
BANDWIDTH_CANDIDATES = cfg["BANDWIDTH_CANDIDATES"]
TCP_PROBES = cfg["TCP_PROBES"]
MIN_SUCCESS_RATE = cfg["MIN_SUCCESS_RATE"]
TCP_LATENCY_WEIGHT = cfg["TCP_LATENCY_WEIGHT"]
TIMEOUT = cfg["TIMEOUT"]
SOCKET_DEFAULT_TIMEOUT = cfg["SOCKET_DEFAULT_TIMEOUT"]
PROGRESS_PRINT_INTERVAL = cfg["PROGRESS_PRINT_INTERVAL"]
FILTER_COUNTRIES_ENABLED = cfg["FILTER_COUNTRIES_ENABLED"]
ALLOWED_COUNTRIES = cfg["ALLOWED_COUNTRIES"]
PRE_FILTER_BLOCKED_ENABLED = cfg["PRE_FILTER_BLOCKED_ENABLED"]
PRE_FILTER_BLOCKED_COUNTRIES = [c.upper() for c in cfg["PRE_FILTER_BLOCKED_COUNTRIES"]]
PRE_FILTER_PORT_ENABLED = cfg["PRE_FILTER_PORT_ENABLED"]
PRE_FILTER_PORTS = [str(p) for p in cfg["PRE_FILTER_PORTS"]]
ENABLE_WXPUSHER = cfg["ENABLE_WXPUSHER"]
WXPUSHER_APP_TOKEN = cfg["WXPUSHER_APP_TOKEN"]
WXPUSHER_UIDS = cfg["WXPUSHER_UIDS"]
WXPUSHER_API_URL = cfg["WXPUSHER_API_URL"]
NOTIFY_TIMEOUT = cfg["NOTIFY_TIMEOUT"]
NOTIFY_CONNECT_TIMEOUT = cfg["NOTIFY_CONNECT_TIMEOUT"]
CF_ENABLED = cfg["CF_ENABLED"]
CF_API_TOKEN = cfg["CF_API_TOKEN"]
CF_ZONE_ID = cfg["CF_ZONE_ID"]
CF_DNS_RECORD_NAME = cfg["CF_DNS_RECORD_NAME"]
CF_TTL = cfg["CF_TTL"]
CF_PROXIED = cfg["CF_PROXIED"]
CF_DNS_CONNECT_TIMEOUT = cfg["CF_DNS_CONNECT_TIMEOUT"]
CF_DNS_READ_TIMEOUT = cfg["CF_DNS_READ_TIMEOUT"]
DNS_RECORD_TYPE = cfg["DNS_RECORD_TYPE"]
ADDITIONAL_SOURCES = cfg["ADDITIONAL_SOURCES"]
FETCH_MAX_RETRIES = cfg["FETCH_MAX_RETRIES"]
FETCH_RETRY_DELAY = cfg["FETCH_RETRY_DELAY"]
FETCH_TIMEOUT = cfg["FETCH_TIMEOUT"]
FETCH_CONNECT_TIMEOUT = cfg["FETCH_CONNECT_TIMEOUT"]
IP_CALIBRATION_ENABLED = cfg["IP_CALIBRATION_ENABLED"]
TOKEN_FAILURE_THRESHOLD = cfg["TOKEN_FAILURE_THRESHOLD"]
IP_CALIBRATION_MIN_INTERVAL = cfg["IP_CALIBRATION_MIN_INTERVAL"]
IP_CALIBRATION_TOKEN_FILE = cfg["IP_CALIBRATION_TOKEN_FILE"]
IP_CALIBRATION_CACHE_FILE = cfg["IP_CALIBRATION_CACHE_FILE"]
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg["OUTPUT_FILE"])
MIN_OUTPUT_NODES = cfg["MIN_OUTPUT_NODES"]
MIN_KEEP_RATIO = cfg["MIN_KEEP_RATIO"]
ENABLE_LOGGING = cfg["ENABLE_LOGGING"]
LOG_FILE = cfg["LOG_FILE"]
FORCE_DIRECT = cfg["FORCE_DIRECT"]
TEST_AVAILABILITY = cfg["TEST_AVAILABILITY"]
AVAILABILITY_CHECK_API = cfg["AVAILABILITY_CHECK_API"]
AVAILABILITY_TIMEOUT = cfg["AVAILABILITY_TIMEOUT"]
AVAILABILITY_CONNECT_TIMEOUT = cfg["AVAILABILITY_CONNECT_TIMEOUT"]
AVAILABILITY_RETRY_MAX = cfg["AVAILABILITY_RETRY_MAX"]
AVAILABILITY_RETRY_DELAY = cfg["AVAILABILITY_RETRY_DELAY"]
AVAILABILITY_INNER_RETRY_ENABLED = cfg["AVAILABILITY_INNER_RETRY_ENABLED"]
AVAILABILITY_INNER_RETRY_MAX = cfg["AVAILABILITY_INNER_RETRY_MAX"]
AVAILABILITY_INNER_RETRY_DELAY = cfg["AVAILABILITY_INNER_RETRY_DELAY"]
HTTP_TEST_ENABLED = cfg["HTTP_TEST_ENABLED"]
HTTP_TEST_TIMEOUT = cfg["HTTP_TEST_TIMEOUT"]
HTTP_TEST_CONNECT_TIMEOUT = cfg["HTTP_TEST_CONNECT_TIMEOUT"]
HTTP_TEST_MAX_ROUNDS = cfg["HTTP_TEST_MAX_ROUNDS"]
HTTP_TEST_ROUND_DELAY = cfg["HTTP_TEST_ROUND_DELAY"]
HTTP_TEST_INNER_RETRY_ENABLED = cfg["HTTP_TEST_INNER_RETRY_ENABLED"]
HTTP_TEST_MAX_RETRIES = cfg["HTTP_TEST_MAX_RETRIES"]
HTTP_TEST_RETRY_DELAY = cfg["HTTP_TEST_RETRY_DELAY"]
HTTP_TEST_METHOD = cfg["HTTP_TEST_METHOD"]
HTTP_LATENCY_WEIGHT = cfg["HTTP_LATENCY_WEIGHT"]
JITTER_WEIGHT = cfg["JITTER_WEIGHT"]
HTTP_JITTER_SAMPLES = cfg["HTTP_JITTER_SAMPLES"]
FILTER_IPV6_AVAILABILITY = cfg["FILTER_IPV6_AVAILABILITY"]
FILTER_BLOCKED_COUNTRIES_ENABLED = cfg["FILTER_BLOCKED_COUNTRIES_ENABLED"]
BLOCKED_COUNTRIES = cfg["BLOCKED_COUNTRIES"]
DNS_IP_RISK_FILTER_ENABLED = cfg["DNS_IP_RISK_FILTER_ENABLED"]
DNS_IP_RISK_MAX_LEVEL = cfg["DNS_IP_RISK_MAX_LEVEL"]
DNS_UPDATE_TARGET_COUNT = cfg["DNS_UPDATE_TARGET_COUNT"]
BANDWIDTH_SIZE_MB = cfg["BANDWIDTH_SIZE_MB"]
BANDWIDTH_TIMEOUT = cfg["BANDWIDTH_TIMEOUT"]
BANDWIDTH_RETRY_MAX = cfg["BANDWIDTH_RETRY_MAX"]
BANDWIDTH_RETRY_DELAY = cfg["BANDWIDTH_RETRY_DELAY"]
BANDWIDTH_URL_TEMPLATE = cfg["BANDWIDTH_URL_TEMPLATE"]
BANDWIDTH_PROCESS_BUFFER = cfg["BANDWIDTH_PROCESS_BUFFER"]
BANDWIDTH_CONNECT_TIMEOUT = cfg["BANDWIDTH_CONNECT_TIMEOUT"]
BANDWIDTH_SAMPLES = cfg.get("BANDWIDTH_SAMPLES", 2)
SPEED_WEIGHT = cfg["SPEED_WEIGHT"]
IP_CALIBRATION_CONCURRENCY = cfg["IP_CALIBRATION_CONCURRENCY"]
MAX_WORKERS = cfg["MAX_WORKERS"]
AVAILABILITY_WORKERS = cfg["AVAILABILITY_WORKERS"]
FALLBACK_WORKERS = cfg["FALLBACK_WORKERS"]
BANDWIDTH_WORKERS = cfg["BANDWIDTH_WORKERS"]
HTTP_TEST_WORKERS = cfg["HTTP_TEST_WORKERS"]
DNS_UPDATE_MAX_RETRIES = cfg["DNS_UPDATE_MAX_RETRIES"]
DNS_UPDATE_RETRY_DELAY = cfg["DNS_UPDATE_RETRY_DELAY"]
GITHUB_SYNC_MAX_RETRIES = cfg["GITHUB_SYNC_MAX_RETRIES"]
GITHUB_SYNC_RETRY_DELAY = cfg["GITHUB_SYNC_RETRY_DELAY"]
GIT_SYNC_PROCESS_TIMEOUT = cfg["GIT_SYNC_PROCESS_TIMEOUT"]
AD_HEADER_ENABLED = cfg["AD_HEADER_ENABLED"]
AD_HEADER_LINES = cfg["AD_HEADER_LINES"]
AD_FOOTER_ENABLED = cfg["AD_FOOTER_ENABLED"]
AD_FOOTER_LINES = cfg["AD_FOOTER_LINES"]
AD_PERLINE_ENABLED = cfg["AD_PERLINE_ENABLED"]
AD_PERLINE_TEXT = cfg["AD_PERLINE_TEXT"]
IP_TXT_SHOW_BANDWIDTH = cfg["IP_TXT_SHOW_BANDWIDTH"]
IP_TXT_SHOW_HTTP_LATENCY = cfg["IP_TXT_SHOW_HTTP_LATENCY"]
IP_TXT_SHOW_HTTP_JITTER = cfg["IP_TXT_SHOW_HTTP_JITTER"]
IP_TXT_SHOW_LATENCY = cfg["IP_TXT_SHOW_LATENCY"]
STABILITY_ENABLED = cfg["STABILITY_ENABLED"]
STABILITY_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg["STABILITY_STATE_FILE"])
STABILITY_EMA_ALPHA = cfg["STABILITY_EMA_ALPHA"]
STABILITY_RECENT_N = cfg["STABILITY_RECENT_N"]
STABILITY_MIN_SAMPLES = cfg["STABILITY_MIN_SAMPLES"]
STABILITY_HISTORY_WEIGHT = cfg["STABILITY_HISTORY_WEIGHT"]
STABILITY_HYSTERESIS_RATIO = cfg["STABILITY_HYSTERESIS_RATIO"]
STABILITY_CONFIRM_RUNS = cfg["STABILITY_CONFIRM_RUNS"]
STABILITY_MAX_ABSENT_RUNS = cfg["STABILITY_MAX_ABSENT_RUNS"]
STABILITY_MIN_CONSISTENCY = cfg["STABILITY_MIN_CONSISTENCY"]
# IPv6 支持（合并进 ip.txt）
IPV6_ENABLED = cfg["IPV6_ENABLED"]
IPV6_SEED_HOSTS = cfg["IPV6_SEED_HOSTS"]
IPV6_FALLBACK_SEEDS = cfg["IPV6_FALLBACK_SEEDS"]
IPV6_SCAN_TIMEOUT = cfg["IPV6_SCAN_TIMEOUT"]
IPV6_SCAN_WORKERS = cfg["IPV6_SCAN_WORKERS"]
IPV6_SAMPLE_COUNT = cfg["IPV6_SAMPLE_COUNT"]
IPV6_PORT = cfg["IPV6_PORT"]
IPV6_PLACEHOLDER_COUNTRY = cfg["IPV6_PLACEHOLDER_COUNTRY"]
IPV6_CANDIDATE_RESERVE = cfg["IPV6_CANDIDATE_RESERVE"]
IPV6_MIN_IN_OUTPUT = cfg["IPV6_MIN_IN_OUTPUT"]

# 记录本次运行中发生硬降级的环节（该层完全失效、退化为上一层的原始结果）。
# 只要出现过降级，写 ip.txt 时就要求产出不低于上次，否则保留旧结果。
DEGRADED_STAGES = []

# 各阶段漏斗计数，运行结束写入 run_summary.json，便于事后定位是哪一层塌的
RUN_STATS = {}

if FORCE_DIRECT:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"

socket.setdefaulttimeout(SOCKET_DEFAULT_TIMEOUT)

# ==================== 单实例锁（文件锁，跨平台） ====================
_LOCK_FILE_HANDLE = None
_LOCK_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".run.lock")

def acquire_single_instance():
    global _LOCK_FILE_HANDLE
    f = None
    try:
        f = open(_LOCK_FILE_PATH, "a+")
        f.seek(0)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
        return False
    _LOCK_FILE_HANDLE = f
    return True

def release_single_instance():
    global _LOCK_FILE_HANDLE
    if _LOCK_FILE_HANDLE is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt
            _LOCK_FILE_HANDLE.seek(0)
            msvcrt.locking(_LOCK_FILE_HANDLE.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(_LOCK_FILE_HANDLE.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        _LOCK_FILE_HANDLE.close()
    except Exception:
        pass
    _LOCK_FILE_HANDLE = None
    # 尝试删除锁文件，失败忽略（Windows 上可能被别的实例占用）
    try:
        os.remove(_LOCK_FILE_PATH)
    except Exception:
        pass

# ====================================================

def send_wxpusher_notification(content, summary):
    if not ENABLE_WXPUSHER:
        return
    try:
        payload = {
            "appToken": WXPUSHER_APP_TOKEN,
            "content": content,
            "summary": summary,
            "uids": WXPUSHER_UIDS
        }
        headers = {"Content-Type": "application/json; charset=utf-8"}
        resp = requests.post(
            WXPUSHER_API_URL,
            data=json.dumps(payload),
            headers=headers,
            timeout=(NOTIFY_CONNECT_TIMEOUT, NOTIFY_TIMEOUT)
        )
        if resp.status_code == 200:
            print("微信通知已发送")
        else:
            print(f"微信通知发送失败: {resp.status_code}")
    except Exception as e:
        print(f"微信通知异常: {e}")

# ==================== IP 风险等级查询 ====================
RISK_LEVEL_ORDER = {
    "极度纯净": 0,
    "纯净": 1,
    "轻微风险": 2,
    "高风险": 3,
    "极度危险": 4,
}

def get_ip_risk_level(ip):
    """查询单个 IP 的风险等级字符串，失败返回 '未知'"""
    url = f"https://api.ipapi.is/?q={ip}"
    try:
        resp = requests.get(url, timeout=10, verify=False)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return "未知"

    company_score = data.get("company", {}).get("abuser_score")
    asn_score = data.get("asn", {}).get("abuser_score")
    security_flags = {
        "is_crawler": data.get("is_crawler", False),
        "is_proxy": data.get("is_proxy", False),
        "is_vpn": data.get("is_vpn", False),
        "is_tor": data.get("is_tor", False),
        "is_abuser": data.get("is_abuser", False),
        "is_bogon": data.get("is_bogon", False),
    }

    def extract_score(score_str):
        if not score_str:
            return 0.0
        match = re.match(r"([\d.]+)\s*\(([^)]+)\)", str(score_str).strip())
        if match:
            return float(match.group(1))
        try:
            return float(score_str)
        except (ValueError, TypeError):
            return 0.0

    company = extract_score(company_score)
    asn = extract_score(asn_score)
    base_score = ((company + asn) / 2) * 5

    risk_count = sum(1 for key in ["is_crawler", "is_proxy", "is_vpn", "is_tor", "is_abuser"]
                     if security_flags.get(key, False))
    final_score = base_score + risk_count * 0.15
    if security_flags.get("is_bogon", False):
        final_score += 1.0

    percentage = final_score * 100
    if percentage >= 100:
        return "极度危险"
    elif percentage >= 20:
        return "高风险"
    elif percentage >= 5:
        return "轻微风险"
    elif percentage >= 0.25:
        return "纯净"
    else:
        return "极度纯净"

# ==================== 自适应多数据源解析引擎 ====================
def extract_country_code(label):
    """从任意标签中提取标准两位国家代码（支持两位代码、三位代码映射、中文名、emoji国旗、混合无关文字）"""
    label = label.strip()
    if not label:
        return None

    tokens = re.split(r'[\s,;|/]+', label)

    # 1. 检查两位/三位代码
    for token in tokens:
        token_cleaned = re.sub(r'^[\d\s\-_.|#]+', '', token.strip())
        m3 = re.match(r'^([A-Z]{3})(?![A-Za-z])', token_cleaned)
        if m3 and m3.group(1) in ALPHA3_TO_ALPHA2:
            return ALPHA3_TO_ALPHA2[m3.group(1)]
        m2 = re.match(r'^([A-Z]{2})(?![A-Za-z])', token_cleaned)
        if m2 and m2.group(1) in CODE_SET:
            return m2.group(1)

    # 2. 中文子串提取（改进）
    for token in tokens:
        token_cleaned = re.sub(r'^[\d\s\-_.|#]+', '', token)
        cn_matches = re.findall(r'[\u4e00-\u9fff（）()]+', token_cleaned)
        for cn in cn_matches:
            code = CN_TO_CODE.get(cn)
            if code:
                return code

    # 3. 国旗 emoji
    emoji_chars = [c for c in label if '\U0001F1E6' <= c <= '\U0001F1FF']
    if len(emoji_chars) >= 2 and len(emoji_chars) % 2 == 0:
        first = ord(emoji_chars[0]) - 0x1F1E6
        second = ord(emoji_chars[1]) - 0x1F1E6
        if 0 <= first <= 25 and 0 <= second <= 25:
            return chr(first + ord('A')) + chr(second + ord('A'))

    return None


def _parse_json_nodes(data):
    nodes = []
    if isinstance(data, list):
        for item in data:
            nodes.extend(_parse_json_nodes(item))
    elif isinstance(data, dict):
        for key in ('nodes', 'data', 'result', 'list'):
            if key in data and isinstance(data[key], list):
                nodes.extend(_parse_json_nodes(data[key]))
                break
        ip = data.get('ip') or data.get('host')
        port = data.get('port')
        code = data.get('country') or data.get('cc')
        if ip and port and code:
            nodes.append(f"{ip}:{port}#{code.upper()}")
    elif isinstance(data, str):
        nodes.extend(_parse_text_nodes(data))
    return nodes


def _query_country(ip, port):
    try:
        resp = requests.get(
            AVAILABILITY_CHECK_API,
            params={"proxyip": f"{ip}:{port}"},
            timeout=(AVAILABILITY_CONNECT_TIMEOUT, AVAILABILITY_TIMEOUT),
            verify=False
        )
        if resp.status_code == 200:
            data = resp.json()
            country = data.get("probe_results", {}).get("ipv4", {}).get("exit", {}).get("country", "")
            if country and len(country) == 2:
                return country.upper()
    except Exception:
        pass
    return None


def _resolve_countries_batch(ipports):
    results = {}
    total = len(ipports)
    completed = 0
    last_print = time.time()

    def worker(ipport):
        ip, port = ipport.rsplit(':', 1)
        return ipport, _query_country(ip, port)

    with ThreadPoolExecutor(max_workers=FALLBACK_WORKERS) as executor:
        futures = {executor.submit(worker, ipp): ipp for ipp in ipports}
        for future in as_completed(futures):
            try:
                ipport, code = future.result()
                results[ipport] = code
            except Exception:
                results[futures[future]] = None
            completed += 1
            now = time.time()
            if now - last_print >= PROGRESS_PRINT_INTERVAL or completed == total:
                print(f"\r[备用API查询] 进度：{completed}/{total} ({(completed/total)*100:.1f}%)", end="", flush=True)
                last_print = now

    if total > 0:
        print()
    return results


def _parse_text_nodes(text):
    nodes = []
    pending = []

    tokens = text.split()
    for token in tokens:
        pure_match = re.match(r'^(\d+\.\d+\.\d+\.\d+:\d+)$', token)
        if pure_match:
            pending.append(pure_match.group(1))
            continue

        if '#' not in token:
            continue
        try:
            ipport, label = token.split('#', 1)
        except ValueError:
            continue
        ipport = ipport.strip()
        label = label.strip()

        if ipport.startswith('['):
            continue
        if not re.match(r'^\d+\.\d+\.\d+\.\d+:\d+$', ipport):
            continue

        code = extract_country_code(label)
        if code:
            nodes.append(f"{ipport}#{code}")
        else:
            pending.append(ipport)

    if pending:
        print(f"{len(pending)} 个节点未能识别或缺少国家，通过可用性检测 API 查询国家...")
        resolved = _resolve_countries_batch(pending)
        for ipport, code in resolved.items():
            if code:
                nodes.append(f"{ipport}#{code}")

    return nodes


def parse_adaptive(text):
    text = text.strip()
    if not text:
        return []

    if text.startswith('{') or text.startswith('['):
        try:
            data = json.loads(text)
            return _parse_json_nodes(data)
        except (json.JSONDecodeError, Exception):
            pass

    return _parse_text_nodes(text)


def fetch_additional_source(url):
    if not url:
        return []

    for attempt in range(1, FETCH_MAX_RETRIES + 1):
        try:
            print(f"正在请求数据源 {url} (尝试 {attempt}/{FETCH_MAX_RETRIES}) ...")
            # 只声明本地一定能解码的编码：环境里没有 zstandard 时声明 zstd
            # 会让 urllib3 在 CDN 选 zstd 时抛 ContentDecodingError。
            headers = {"Accept-Encoding": "gzip, deflate, br"}
            try:
                resp = requests.get(url, timeout=(FETCH_CONNECT_TIMEOUT, FETCH_TIMEOUT),
                                    headers=headers, verify=False)
                resp.raise_for_status()
                _src_text = resp.text
            except Exception as _e:
                print("requests failed, try curl fallback: %s" % _e)
                curl_bin = shutil.which("curl")
                if not curl_bin:
                    raise
                _r = subprocess.run([curl_bin, '-sS', '-m', '15', '-k', '-A', 'curl/7.79.1', url],
                                    capture_output=True, timeout=25)
                if _r.returncode != 0 or not _r.stdout:
                    raise
                _src_text = _r.stdout.decode('utf-8', 'ignore')
            nodes = parse_adaptive(_src_text)
            print(f"从 {url} 解析出 {len(nodes)} 个节点。")
            return nodes
        except Exception as e:
            print(f"请求或解析失败 ({url}): {e}")
            if attempt < FETCH_MAX_RETRIES:
                print(f"等待 {FETCH_RETRY_DELAY} 秒后重试...")
                time.sleep(FETCH_RETRY_DELAY)
            else:
                print(f"已尝试 {FETCH_MAX_RETRIES} 次，放弃该数据源。")
                return []

# =========================== IP 地区校准模块 ===========================
class IpInfoAsync:
    def __init__(self, token_list, concurrency, min_interval, trust_env, failure_threshold):
        self.token_list = token_list
        self.n_tokens = len(token_list)
        self.current_token_index = 0
        self.token_lock = asyncio.Lock()
        self.semaphore = asyncio.Semaphore(concurrency)
        self.min_interval = min_interval
        self.session = None
        self.trust_env = trust_env
        self.failure_threshold = max(2, failure_threshold)   # 强制 ≥2
        self.token_failures = [0] * self.n_tokens
        self.cooldown_until = [0.0] * self.n_tokens
        self.last_request_time = [0.0] * self.n_tokens
        self.rate_locks = [asyncio.Lock() for _ in range(self.n_tokens)]

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(trust_env=self.trust_env)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _acquire_token(self):
        """环形轮询：跳过已超阈值/冷却中的，全部不可用时选最不坏的"""
        async with self.token_lock:
            now = asyncio.get_event_loop().time()
            n = self.n_tokens
            for _ in range(n):
                self.current_token_index = (self.current_token_index + 1) % n
                idx = self.current_token_index
                if (self.token_failures[idx] < self.failure_threshold
                        and self.cooldown_until[idx] <= now):
                    return idx
            # 全部不可用 → 选失败最少的硬上，不重置计数
            idx = min(range(n), key=lambda i: self.token_failures[i])
            self.current_token_index = idx
            return idx

    async def _rate_limit(self, idx):
        async with self.rate_locks[idx]:
            now = asyncio.get_event_loop().time()
            wait = self.last_request_time[idx] + self.min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_request_time[idx] = asyncio.get_event_loop().time()

    async def get_ip_details(self, ip_address):
        max_attempts = max(3, min(self.n_tokens, 10))
        for _ in range(max_attempts):
            idx = await self._acquire_token()
            token = self.token_list[idx]
            url = (f"https://ipinfo.io/{ip_address}/json?token={token}"
                   if ip_address else
                   f"https://ipinfo.io/json?token={token}")

            await self._rate_limit(idx)
            async with self.semaphore:
                try:
                    async with self.session.get(
                        url, timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        if resp.status == 429:
                            # 限流：短冷却，不计 token 死亡
                            self.cooldown_until[idx] = asyncio.get_event_loop().time() + 1.0
                            continue
                        if resp.status in (401, 403):
                            # 真·token 失效
                            self.token_failures[idx] += 1
                            continue
                        resp.raise_for_status()
                        data = await resp.json()
                        self.token_failures[idx] = 0
                        country = data.get("country", "Unknown")
                        if country == "Unknown":
                            continue
                        return {
                            "CountryCode": country,
                            "Region": data.get("region", "Unknown"),
                            "City": data.get("city", "Unknown"),
                            "ASN": data.get("org", "").split(" ")[0] if data.get("org", "").startswith("AS") else "Unknown",
                            "ISP": data.get("org", "").split(" ", 1)[-1] if " " in data.get("org", "") else data.get("org", "Unknown"),
                        }
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    # 网络抖动：短冷却，不计 token 死亡
                    self.cooldown_until[idx] = asyncio.get_event_loop().time() + 0.3
                    continue
        return None

def load_tokens(filepath):
    if not os.path.exists(filepath):
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

def load_ipinfo_cache(cache_file):
    if not os.path.exists(cache_file):
        return {}
    cache = {}
    with open(cache_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or '#' not in line:
                continue
            ipport, tag = line.split('#', 1)
            cache[ipport.strip()] = tag.strip()
    return cache

def save_ipinfo_cache(cache_file, new_records):
    with open(cache_file, "a", encoding="utf-8") as f:
        for ipport, tag in new_records:
            f.write(f"{ipport}#{tag}\n")

def sort_cache_file(cache_file):
    if not os.path.exists(cache_file):
        return
    with open(cache_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    parsed = []
    for line in lines:
        line = line.strip()
        if not line or '#' not in line:
            continue
        ipport, tag = line.split('#', 1)
        ip_str = ipport.split(':')[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        parsed.append((ip_obj, line))

    parsed.sort(key=lambda x: x[0])

    with open(cache_file, "w", encoding="utf-8") as f:
        for _, line in parsed:
            f.write(line + "\n")

async def validate_tokens(token_list, concurrency, min_interval, trust_env):
    valid = []
    sem = asyncio.Semaphore(concurrency)
    async with aiohttp.ClientSession(trust_env=trust_env) as session:
        async def check(token):
            url = f"https://ipinfo.io/json?token={token}"
            try:
                async with sem:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status != 200:
                            return token, False
                        data = await resp.json()
                        return token, bool(data.get("country"))
            except Exception:
                return token, False

        tasks = [asyncio.ensure_future(check(t)) for t in token_list]
        total = len(tasks)
        completed = 0
        for coro in asyncio.as_completed(tasks):
            token, ok = await coro
            if ok:
                valid.append(token)
            completed += 1
            print(f"\rToken 校验进度：{completed}/{total}", end="", flush=True)
        print()
    return valid

async def query_new_ips(new_ips, token_list, concurrency, min_interval, trust_env,
                        ipport_map=None, cache_file=None):
    result = {}
    if not new_ips or not token_list:
        return result

    print(f"需要查询 {len(new_ips)} 个新 IP...")
    async with IpInfoAsync(token_list, concurrency, min_interval, trust_env, TOKEN_FAILURE_THRESHOLD) as handler:
        tasks = []
        for ip in new_ips:
            task = asyncio.ensure_future(handler.get_ip_details(ip))
            task.my_ip = ip
            tasks.append(task)
        total = len(tasks)
        completed = 0
        failed = 0

        f = None
        if cache_file:
            f = open(cache_file, "a", encoding="utf-8")

        try:
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    ip = task.my_ip
                    try:
                        info = task.result()
                    except Exception:
                        info = None
                    completed += 1
                    if info and info.get("CountryCode") != "Unknown":
                        tag_parts = [info["CountryCode"]]
                        if info.get("Region") and info["Region"] != "Unknown":
                            tag_parts.append(info["Region"])
                        if info.get("City") and info["City"] != "Unknown":
                            tag_parts.append(info["City"])
                        if info.get("ASN") and info["ASN"] != "Unknown":
                            tag_parts.append(info["ASN"])
                        if info.get("ISP") and info["ISP"] != "Unknown":
                            tag_parts.append(info["ISP"])
                        tag = " ".join(tag_parts)
                        result[ip] = tag
                        if f and ipport_map and ip in ipport_map:
                            for ipport in ipport_map[ip]:
                                f.write(f"{ipport}#{tag}\n")
                                f.flush()
                    else:
                        failed += 1
                    print(f"\r[{completed}/{total}] 地区校准...", end="", flush=True)
        finally:
            if f:
                f.close()
    print()
    if total > 0 and failed == total:
        send_wxpusher_notification("IP地区校准：所有IP查询均失败，可能所有token均已失效。", "IP校准全失败")
        print("警告：所有 IP 地区校准失败，可能所有 token 已失效。")
    return result

def calibrate_regions(nodes, token_file, cache_file):
    if not IP_CALIBRATION_ENABLED:
        print("IP 地区校准已禁用，跳过。")
        return

    # ★ 先应用缓存
    cache = load_ipinfo_cache(cache_file)
    for i, node in enumerate(nodes):
        ipport = node.split('#')[0]
        tag = cache.get(ipport)
        if tag:
            nodes[i] = f"{ipport}#{tag.split()[0]}"

    token_list = load_tokens(token_file)
    if not token_list:
        print("valid_tokens.txt 为空，IP 地区校准跳过。")
        return

    trust_env = not FORCE_DIRECT

    print("正在进行 token 有效性校验...")
    valid_tokens = asyncio.run(validate_tokens(token_list, IP_CALIBRATION_CONCURRENCY, IP_CALIBRATION_MIN_INTERVAL, trust_env))
    if not valid_tokens:
        print("所有 token 均已失效，地区校准跳过。")
        send_wxpusher_notification("IP地区校准：所有token均已失效，本次校准跳过。", "IP校准 Token 耗尽")
        return
    print(f"有效 token 数量: {len(valid_tokens)}")

    ipport_set = set()
    for node in nodes:
        ipport = node.split('#')[0]
        ipport_set.add(ipport)

    cached_ipports = set(cache.keys())
    new_ipports = ipport_set - cached_ipports

    if not new_ipports:
        print("所有 IP 已在缓存中，无需查询。")
    else:
        new_ips_set = set()
        ip_to_ipports = defaultdict(list)
        for ipport in new_ipports:
            ip = ipport.split(':')[0]
            new_ips_set.add(ip)
            ip_to_ipports[ip].append(ipport)

        print(f"检测到 {len(new_ipports)} 个新 IP:端口，涉及 {len(new_ips_set)} 个唯一 IP，开始查询...")
        ip_info = asyncio.run(query_new_ips(
            list(new_ips_set),
            valid_tokens,
            IP_CALIBRATION_CONCURRENCY,
            IP_CALIBRATION_MIN_INTERVAL,
            trust_env,
            ipport_map=ip_to_ipports,
            cache_file=cache_file
        ))

        for ip, tag in ip_info.items():
            for ipport in ip_to_ipports.get(ip, []):
                cache[ipport] = tag

    for i, node in enumerate(nodes):
        ipport = node.split('#')[0]
        tag = cache.get(ipport)
        if tag:
            country_code = tag.split()[0]
            nodes[i] = f"{ipport}#{country_code}"

    sort_cache_file(cache_file)

# =========================== 核心测试、筛选、测速及更新函数 ===========================

def test_tcp_latency(ip, port, timeout=TIMEOUT, probes=TCP_PROBES):
    is_v6 = ip.startswith("[")
    family = socket.AF_INET6 if is_v6 else socket.AF_INET
    host = ip[1:-1] if is_v6 else ip
    max_latency = 0.0
    success = 0
    for _ in range(probes):
        try:
            start = time.time()
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect((host, int(port)))
            latency = time.time() - start
            if latency > max_latency:
                max_latency = latency
            success += 1
        except Exception:
            continue
    if success == 0:
        return float("inf"), 0
    return max_latency, success

def test_node(node_str):
    m = NODE_PATTERN.match(node_str)
    if not m:
        return None
    ip, port, country = m.groups()
    max_lat, success = test_tcp_latency(ip, port)
    if success == 0 or (success / TCP_PROBES) < MIN_SUCCESS_RATE:
        return None
    return (node_str, max_lat, country, success)

def check_availability(node_str):
    # IPv6 节点走的是 Cloudflare 原生 IPv6 边缘，外置可用性 API 仅校验 IPv4 代理，
    # 对 IPv6 直接放行；是否真为可用 CF 节点由后续的 HTTP /cdn-cgi/trace 检测把关。
    if _is_ipv6_node(node_str):
        return (node_str, True, "ipv6-bypass", {})
    m = IP_PORT_PATTERN.match(node_str)
    if not m:
        return (node_str, False, "unknown", {})
    ip, port = m.group(1), m.group(2)
    proxyip = f"{ip}:{port}"

    best_stack = "unknown"
    best_exit_info = {}
    success = False

    max_attempts = AVAILABILITY_INNER_RETRY_MAX + 1 if AVAILABILITY_INNER_RETRY_ENABLED else 1
    retry_delay = AVAILABILITY_INNER_RETRY_DELAY if AVAILABILITY_INNER_RETRY_ENABLED else 0

    for attempt in range(max_attempts):
        try:
            resp = requests.get(
                AVAILABILITY_CHECK_API,
                params={"proxyip": proxyip},
                timeout=(AVAILABILITY_CONNECT_TIMEOUT, AVAILABILITY_TIMEOUT),
                verify=False
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") is True:
                    success = True
                    best_stack = data.get("inferred_stack", "unknown")
                    probe = data.get("probe_results", {}).get("ipv6") or data.get("probe_results", {}).get("ipv4") or {}
                    best_exit_info = probe.get("exit", {})
                    break
        except Exception:
            pass
        if attempt < max_attempts - 1 and retry_delay > 0:
            time.sleep(retry_delay)

    return (node_str, success, best_stack, best_exit_info)

def check_http_server(node_str, timeout, max_retries, retry_delay, method, connect_timeout, inner_retry_enabled):
    m = IP_PORT_PATTERN.match(node_str)
    if not m:
        return (node_str, False, "parse_error", 0.0, 0.0)
    ip, port = m.group(1), m.group(2)
    url = f"http://{ip}:{port}/cdn-cgi/trace"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    }

    test_rounds = max(3, HTTP_JITTER_SAMPLES)
    # 允许的失败轮数：3 轮允许 1 轮抖动。此前任何一轮异常即判死节点，
    # 网络瞬时拥塞会把整批节点连锁淘汰（实测同一批节点通过率可在 84% 与 0.7% 间跳变）。
    allowed_fail = max(1, test_rounds // 3)
    latencies = []
    failed_rounds = 0
    max_attempts = (max_retries + 1) if inner_retry_enabled else 1
    for _ in range(test_rounds):
        last_error = "connection_error"
        # 连接类错误在单轮内重试（此前该参数未实现，一次抖动即判死节点）
        for attempt in range(max_attempts):
            try:
                start = time.time()
                request_kwargs = {
                    "timeout": (connect_timeout, timeout),
                    "verify": False,
                    "allow_redirects": False,
                    "headers": headers,
                    "proxies": {"http": None, "https": None}
                }

                if method.upper() == "HEAD":
                    resp = requests.head(url, **request_kwargs)
                else:
                    resp = requests.get(url, **request_kwargs)

                lat = (time.time() - start) * 1000
                if resp.status_code != 400:
                    return (node_str, False, f"status_{resp.status_code}", 0.0, 0.0)
                server = resp.headers.get("server", "")
                if not server.lower().startswith("cloudflare"):
                    return (node_str, False, server, 0.0, 0.0)
                latencies.append(lat)
                break
            except Exception:
                last_error = "connection_error"
                if attempt < max_attempts - 1 and retry_delay > 0:
                    time.sleep(retry_delay)
        else:
            # 该轮重试耗尽仍失败：未超过容忍额度则继续下一轮
            failed_rounds += 1
            if failed_rounds > allowed_fail:
                return (node_str, False, last_error, 0.0, 0.0)

    if len(latencies) < test_rounds - allowed_fail:
        return (node_str, False, "not_enough_samples", 0.0, 0.0)

    # 延迟取中位数、抖动取标准差，两者基于同一组样本，口径一致。
    # （此前延迟取 max、抖动取全样本标准差，排序会系统性偏向"某一次特别慢"的节点）
    latencies.sort()
    n = len(latencies)
    if n % 2 == 1:
        median_lat = latencies[n // 2]
    else:
        median_lat = (latencies[n // 2 - 1] + latencies[n // 2]) / 2.0

    mean_lat = sum(latencies) / n
    variance = sum((l - mean_lat) ** 2 for l in latencies) / n
    jitter = variance ** 0.5

    return (node_str, True, "cloudflare", median_lat, jitter)

def availability_filter_candidates(candidates):
    if not TEST_AVAILABILITY or not candidates:
        return candidates, {}, {}

    print(f"\n对 {len(candidates)} 个候选节点进行可用性二次筛选...")
    passed = []
    ip_info = {}
    exit_details = {}
    completed = 0
    total = len(candidates)
    last_print = time.time()

    with ThreadPoolExecutor(max_workers=AVAILABILITY_WORKERS) as executor:
        futures = {executor.submit(check_availability, node): node for node in candidates}
        for future in as_completed(futures):
            completed += 1
            node_str, ok, stack, exit_info = future.result()
            if ok:
                passed.append(node_str)
                ip_info[node_str] = stack
                exit_details[node_str] = exit_info
            now = time.time()
            if now - last_print >= PROGRESS_PRINT_INTERVAL or completed == total:
                print(f"\r[可用性检测] 进度：{completed}/{total} ({(completed/total)*100:.1f}%) 通过数量：{len(passed)}", end="", flush=True)
                last_print = now
    print()
    return passed, ip_info, exit_details

def availability_filter_with_retry(candidates):
    if not TEST_AVAILABILITY or not candidates:
        return candidates, {}, {}

    passed = []
    ip_info = {}
    exit_details = {}
    for attempt in range(1, AVAILABILITY_RETRY_MAX + 1):
        print(f"\n[可用性检测] 第 {attempt} 轮检测...")
        passed, ip_info, exit_details = availability_filter_candidates(candidates)
        if passed:
            print(f"可用性检测通过 {len(passed)} 个节点")
            return passed, ip_info, exit_details
        if attempt < AVAILABILITY_RETRY_MAX:
            print(f"本轮可用性检测通过率为 0%，等待 {AVAILABILITY_RETRY_DELAY} 秒后重试...")
            time.sleep(AVAILABILITY_RETRY_DELAY)

    print(f"可用性检测经 {AVAILABILITY_RETRY_MAX} 轮重试后仍无节点通过。")
    DEGRADED_STAGES.append("可用性检测全部失败")
    send_wxpusher_notification(
        content=f"IP 可用性检测经 {AVAILABILITY_RETRY_MAX} 轮重试后仍无节点通过，已跳过过滤，使用原候选列表继续。",
        summary="可用性检测全部失败"
    )
    return candidates, {}, {}

def http_server_filter(candidates, config):
    if not config.get("HTTP_TEST_ENABLED", False) or not candidates:
        return candidates, {}, {}

    timeout = HTTP_TEST_TIMEOUT
    connect_timeout = HTTP_TEST_CONNECT_TIMEOUT
    max_retries = HTTP_TEST_MAX_RETRIES
    retry_delay = HTTP_TEST_RETRY_DELAY
    inner_retry_enabled = HTTP_TEST_INNER_RETRY_ENABLED
    workers = HTTP_TEST_WORKERS
    method = HTTP_TEST_METHOD
    max_rounds = HTTP_TEST_MAX_ROUNDS
    round_delay = HTTP_TEST_ROUND_DELAY

    # 通过率低于该比例视为"疑似拥塞/限速"而非节点质量差，降并发后重试
    ANOMALY_RATE = 0.30
    best = (None, {}, {})      # 记录各轮中最优的一次结果

    for round_num in range(1, max_rounds + 1):
        print(f"\n[HTTP检测] 第 {round_num} 轮检测（并发 {workers}）...")
        print(f"\n对 {len(candidates)} 个候选节点进行 HTTP 二次筛选...")

        passed = []
        http_latency_map = {}
        http_jitter_map = {}
        total = len(candidates)
        completed = 0
        last_print = time.time()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(check_http_server, node, timeout, max_retries, retry_delay, method, connect_timeout, inner_retry_enabled): node
                for node in candidates
            }
            for future in as_completed(future_map):
                node_str, valid, server, http_lat, http_jitter = future.result()
                completed += 1
                if valid:
                    passed.append(node_str)
                    http_latency_map[node_str] = http_lat
                    http_jitter_map[node_str] = http_jitter
                now = time.time()
                if now - last_print >= PROGRESS_PRINT_INTERVAL or completed == total:
                    print(f"\r[HTTP检测] 进度：{completed}/{total} ({(completed/total)*100:.1f}%) 通过数量：{len(passed)}", end="", flush=True)
                    last_print = now

        print()
        if best[0] is None or len(passed) > len(best[0]):
            best = (passed, http_latency_map, http_jitter_map)

        rate = len(passed) / total if total else 0.0
        if passed and rate >= ANOMALY_RATE:
            print(f"HTTP检测通过 {len(passed)} 个节点")
            return passed, http_latency_map, http_jitter_map

        if round_num < max_rounds:
            if rate < ANOMALY_RATE:
                new_workers = max(4, workers // 2)
                print(f"本轮通过率仅 {rate*100:.1f}%（低于 {ANOMALY_RATE*100:.0f}%），"
                      f"疑似网络拥塞或限速，并发 {workers} → {new_workers} 后重试...")
                workers = new_workers
            else:
                print(f"本轮 HTTP 检测通过率为 0%，等待 {round_delay} 秒后重试...")
            time.sleep(round_delay)

    # 重试结束仍未达到正常通过率：优先返回已验证过的节点，而不是放行未验证的候选
    if best[0]:
        print(f"HTTP检测通过率持续偏低，已返回 {len(best[0])} 个验证通过的节点（未放行未验证节点）。")
        DEGRADED_STAGES.append("HTTP检测通过率异常")
        send_wxpusher_notification(
            content=f"HTTP检测通过率持续低于 {ANOMALY_RATE*100:.0f}%，"
                    f"已保留 {len(best[0])}/{len(candidates)} 个验证通过的节点，请检查网络状况。",
            summary="HTTP检测通过率异常"
        )
        return best

    DEGRADED_STAGES.append("HTTP检测全部失败")
    send_wxpusher_notification(
        content=f"HTTP检测经 {max_rounds} 轮重试后仍无节点通过，已降级使用过滤前列表。",
        summary="HTTP检测全部失败"
    )
    print(f"HTTP检测经 {max_rounds} 轮重试后仍无节点通过，降级使用过滤前候选列表。")
    return candidates, {}, {}

def measure_bandwidth_curl(node_str):
    m = IP_PORT_PATTERN.match(node_str)
    if not m:
        return (node_str, 0)
    ip, port = m.group(1), m.group(2)
    port_int = int(port)

    # ---------- 端口与协议的映射 ----------
    HTTP_PORTS = {80, 8080, 8880, 2052, 2082, 2086, 2095}
    HTTPS_PORTS = {443, 2053, 2083, 2087, 2096, 8443}

    if port_int in HTTP_PORTS:
        scheme = "http"
        insecure_flag = []          # HTTP 不需要 --insecure
    elif port_int in HTTPS_PORTS:
        scheme = "https"
        insecure_flag = ["--insecure"]
    else:
        # 未知端口，默认尝试 https（保险）
        scheme = "https"
        insecure_flag = ["--insecure"]
    # ---------------------------------------

    null_device = "NUL" if sys.platform == "win32" else "/dev/null"

    # 用模板生成最终 URL，替换 {scheme}、{port}、{bytes}
    url = BANDWIDTH_URL_TEMPLATE.format(
        scheme=scheme,
        port=port,
        bytes=int(BANDWIDTH_SIZE_MB * 1024 * 1024)
    )

    # ---------- 多次测量取中位数，消除突发(burst)抖动 ----------
    # 单节点测速次数（可从配置读取，默认 2）
    samples_cfg = globals().get("BANDWIDTH_SAMPLES", 2)
    # 至少下载这么多字节才算一次有效样本（避免刚连上就断导致虚高）
    min_valid_bytes = max(256 * 1024, int(BANDWIDTH_SIZE_MB * 1024 * 1024 * 0.25))
    samples = []

    for _ in range(max(1, samples_cfg)):
        curl_cmd = [
            "curl", "-s", "-o", null_device,
            # 输出：下载字节数 / 首字节时间 / 总时间
            "-w", "%{size_download} %{time_starttransfer} %{time_total}",
            "-L",
            "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "--noproxy", "*",
            "--resolve", f"speed.cloudflare.com:{port}:{ip}",
            "--connect-timeout", str(BANDWIDTH_CONNECT_TIMEOUT),
            "--max-time", str(BANDWIDTH_TIMEOUT),
        ] + insecure_flag + [url]   # 动态添加 --insecure（如果是 https）

        try:
            result = subprocess.run(curl_cmd, capture_output=True, text=True,
                                    timeout=BANDWIDTH_TIMEOUT + BANDWIDTH_PROCESS_BUFFER)
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split()
                if len(parts) >= 3:
                    size_bytes = float(parts[0])
                    ttfb = float(parts[1])
                    time_total = float(parts[2])
                    # 纯传输时间 = 总时间 - 首字节时间（扣除 TCP/TLS 握手与排队等待）
                    transfer = time_total - ttfb
                    if size_bytes >= min_valid_bytes and transfer > 0.05:
                        speed_mbps = (size_bytes * 8) / (transfer * 1000 * 1000)
                        samples.append(speed_mbps)
        except Exception:
            pass
    # ----------------------------------------------------------

    if not samples:
        return (node_str, 0)
    samples.sort()
    # 取中位数：比平均值更能抵抗单次突发/卡顿
    # 偶数个样本时取中间两个的平均，避免 BANDWIDTH_SAMPLES=2 时退化成"取最大值"
    n = len(samples)
    if n % 2 == 1:
        median = samples[n // 2]
    else:
        median = (samples[n // 2 - 1] + samples[n // 2]) / 2.0
    return (node_str, median)

def bandwidth_filter(candidates):
    if not candidates:
        return []

    if not shutil.which("curl"):
        print("未检测到 curl 命令，带宽测速将跳过。")
        return []

    print(f"\n开始带宽测速（对前 {len(candidates)} 个节点，并发 {BANDWIDTH_WORKERS}，超时 {BANDWIDTH_TIMEOUT}s）...")
    results = []
    completed = 0
    total = len(candidates)
    last_print = time.time()

    with ThreadPoolExecutor(max_workers=BANDWIDTH_WORKERS) as executor:
        futures = {executor.submit(measure_bandwidth_curl, node): node for node in candidates}
        for future in as_completed(futures):
            completed += 1
            node, speed = future.result()
            if speed > 0:
                results.append((node, speed))
            now = time.time()
            if now - last_print >= PROGRESS_PRINT_INTERVAL or completed == total:
                print(f"\r[带宽测速] 进度：{completed}/{total} ({(completed/total)*100:.1f}%)", end="", flush=True)
                last_print = now

    print()
    results.sort(key=lambda x: x[1], reverse=True)
    return results

def batch_update_cloudflare_dns(ip_list, ip_info=None, full_bw_results=None, target_count=None, latency_map=None, http_latency_map=None, http_jitter_map=None):
    if not CF_ENABLED:
        print("Cloudflare DNS 批量更新未启用。")
        return

    if target_count is None:
        target_count = DNS_UPDATE_TARGET_COUNT

    dns_content_list = []
    dns_node_list = []
    filtered_by_port = 0
    filtered_by_ipv6 = 0
    filtered_by_country = 0
    filtered_by_risk = 0
    risk_fallback_ip_list = []
    risk_fallback_node_list = []

    record_type = DNS_RECORD_TYPE.upper()
    if record_type not in ("A", "TXT"):
        print(f"不支持的 DNS_RECORD_TYPE: {record_type}，已跳过 DNS 更新。")
        return

    risk_map = {}
    if DNS_IP_RISK_FILTER_ENABLED and full_bw_results:
        ip_set = set()
        for node_str, _ in full_bw_results:
            if ':' in node_str:
                ip_set.add(node_str.split(':')[0])
        if ip_set:
            workers = min(FALLBACK_WORKERS, len(ip_set))
            print(f"正在并发查询 {len(ip_set)} 个 IP 的风险等级（并发 {workers}）...")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(get_ip_risk_level, ip): ip for ip in ip_set}
                for future in as_completed(futures):
                    ip = futures[future]
                    try:
                        risk_map[ip] = future.result()
                    except Exception:
                        risk_map[ip] = "未知"
            print("风险等级查询完成。")

    if full_bw_results and ip_info:
        blocked_set = set()
        if FILTER_BLOCKED_COUNTRIES_ENABLED:
            blocked_set = {c.upper() for c in BLOCKED_COUNTRIES}

        for node_str, speed in full_bw_results:
            if ':' not in node_str:
                continue
            parts = node_str.split(':')
            if len(parts) < 2:
                continue
            pure_ip = parts[0]
            port = parts[1].split('#')[0]

            if record_type == "A" and port != '443':
                filtered_by_port += 1
                continue

            if FILTER_IPV6_AVAILABILITY:
                stack = ip_info.get(node_str, "unknown")
                if stack == "ipv6_only":
                    filtered_by_ipv6 += 1
                    continue

            if blocked_set and '#' in node_str:
                country = node_str.split('#')[-1].split()[0].upper()
                if country in blocked_set:
                    filtered_by_country += 1
                    continue

            if DNS_IP_RISK_FILTER_ENABLED:
                # 先登记回退候选（万一所有节点都因风险等级被剔除，用这批兜底）
                risk_fallback_ip_list.append(pure_ip)
                risk_fallback_node_list.append(node_str)

                risk_level = risk_map.get(pure_ip, "未知")
                max_level = DNS_IP_RISK_MAX_LEVEL
                if risk_level == "未知" or RISK_LEVEL_ORDER.get(risk_level, 99) > RISK_LEVEL_ORDER.get(max_level, 2):
                    filtered_by_risk += 1
                    continue

            if record_type == "A":
                dns_content_list.append(pure_ip)
            else:
                dns_content_list.append(f"{pure_ip}:{port}")
            dns_node_list.append(node_str)

            if len(dns_content_list) >= target_count:
                break

        if DNS_IP_RISK_FILTER_ENABLED and not dns_content_list and filtered_by_risk > 0:
            send_wxpusher_notification(
                content="风险等级检测全部失败：所有候选节点均因风险等级过高或 API 查询失败被过滤，已回退到无风险等级过滤的候选列表。",
                summary="风险等级检测全部失败"
            )
            fallback_content = []
            fallback_nodes = []
            for i, (ip, node) in enumerate(zip(risk_fallback_ip_list, risk_fallback_node_list)):
                if record_type == "A":
                    fallback_content.append(ip)
                else:
                    ip_port = node.split('#')[0]
                    fallback_content.append(ip_port)
                fallback_nodes.append(node)
                if len(fallback_content) >= target_count:
                    break
            dns_content_list = fallback_content
            dns_node_list = fallback_nodes

        filter_parts = []
        if filtered_by_port > 0:
            filter_parts.append(f"非443端口过滤({filtered_by_port}个)")
        if FILTER_IPV6_AVAILABILITY:
            filter_parts.append(f"IPv6落地过滤({filtered_by_ipv6}个)")
        if FILTER_BLOCKED_COUNTRIES_ENABLED:
            filter_parts.append(f"DNS黑名单过滤({filtered_by_country}个)")
        if DNS_IP_RISK_FILTER_ENABLED and filtered_by_risk > 0:
            filter_parts.append(f"风险等级过滤({filtered_by_risk}个)")
        filter_str = " + ".join(filter_parts) if filter_parts else "无过滤"
        print(f"从 {len(full_bw_results)} 个测速节点中筛选出 {len(dns_content_list)} 个{'IP' if record_type=='A' else 'IP:端口'} 用于 DNS 更新（{filter_str}）。")

    if not dns_content_list:
        if ip_list:
            print("未能从完整测速结果构建 DNS 列表，降级使用 ip.txt 中的 IP。")
            if record_type == "A":
                dns_content_list = ip_list
                dns_node_list = ip_list
            else:
                print("TXT 模式需要端口信息，但降级数据中无端口，DNS 更新跳过。")
                return
        else:
            msg = "没有可用的 IP 用于 DNS 更新，跳过。"
            print(msg)
            send_wxpusher_notification(content=msg, summary="DNS 更新跳过")
            return

    seen = set()
    unique_content = []
    unique_nodes = []
    for content, node in zip(dns_content_list, dns_node_list):
        if content not in seen:
            seen.add(content)
            unique_content.append(content)
            unique_nodes.append(node)
    dns_content_list = unique_content
    dns_node_list = unique_nodes

    print(f"\n准备将以下 {len(dns_content_list)} 个{'IP' if record_type=='A' else 'IP:端口'} 更新到 Cloudflare DNS（记录类型 {record_type}）:")
    speed_map = {}
    if full_bw_results:
        speed_map = {node: speed for node, speed in full_bw_results}
    for i, (content, node) in enumerate(zip(dns_content_list, dns_node_list), 1):
        speed = speed_map.get(node, 0)
        lat_ms = float('inf')
        http_lat_ms = None
        http_jitter_ms = None
        if latency_map and node in latency_map:
            lat_ms = latency_map[node] * 1000
        if http_latency_map and node in http_latency_map:
            http_lat_ms = http_latency_map[node]
        if http_jitter_map and node in http_jitter_map:
            http_jitter_ms = http_jitter_map[node]
        
        # 让显示标签带上国家代码
        display_label = node if '#' in node else content
        line = f"{i}. {display_label} 速度 {speed:.2f} Mbps"
        if http_lat_ms is not None:
            line += f" 延迟 {http_lat_ms:.2f} ms"
        if http_jitter_ms is not None:
            line += f" 抖动 {http_jitter_ms:.2f} ms"
        if lat_ms != float('inf'):
            line += f" 延迟 {lat_ms:.2f} ms"
        print(line)

    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json"
    }

    if record_type == "A":
        for attempt in range(1, DNS_UPDATE_MAX_RETRIES + 1):
            print(f"\n[DNS 更新] 尝试 {attempt}/{DNS_UPDATE_MAX_RETRIES}...")
            try:
                list_url = f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records?type=A&name={CF_DNS_RECORD_NAME}"
                response = requests.get(list_url, headers=headers, timeout=(CF_DNS_CONNECT_TIMEOUT, CF_DNS_READ_TIMEOUT))
                response.raise_for_status()
                result = response.json()
                if not result.get('success'):
                    raise Exception(f"查询 DNS 记录失败: {result.get('errors')}")

                existing_records = result.get('result', [])
                deletes = [{"id": rec["id"]} for rec in existing_records]
                posts = [
                    {
                        "name": CF_DNS_RECORD_NAME,
                        "type": "A",
                        "content": ip,
                        "ttl": CF_TTL,
                        "proxied": CF_PROXIED
                    }
                    for ip in dns_content_list
                ]

                batch_url = f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records/batch"
                payload = {"deletes": deletes, "posts": posts}
                response = requests.post(batch_url, headers=headers, json=payload,
                                        timeout=(CF_DNS_CONNECT_TIMEOUT, CF_DNS_READ_TIMEOUT))
                response.raise_for_status()
                result = response.json()
                if not result.get('success'):
                    raise Exception(f"批量更新失败: {result.get('errors')}")

                success_msg = f"Cloudflare DNS 批量更新成功！已将 {CF_DNS_RECORD_NAME} 指向 {len(dns_content_list)} 个 IP。"
                print(success_msg)
                return

            except Exception as e:
                error_msg = f"[尝试 {attempt}/{DNS_UPDATE_MAX_RETRIES}] DNS 更新出错: {e}"
                print(error_msg)
                if attempt < DNS_UPDATE_MAX_RETRIES:
                    time.sleep(DNS_UPDATE_RETRY_DELAY)
                else:
                    final_error = f"Cloudflare DNS 更新失败，已重试 {DNS_UPDATE_MAX_RETRIES} 次，错误：{e}"
                    print(final_error)
                    send_wxpusher_notification(content=final_error, summary="DNS 更新失败")

    else:
        for attempt in range(1, DNS_UPDATE_MAX_RETRIES + 1):
            print(f"\n[TXT 记录更新] 尝试 {attempt}/{DNS_UPDATE_MAX_RETRIES}...")
            try:
                list_url = f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records?type=TXT&name={CF_DNS_RECORD_NAME}"
                resp = requests.get(list_url, headers=headers, timeout=(CF_DNS_CONNECT_TIMEOUT, CF_DNS_READ_TIMEOUT))
                resp.raise_for_status()
                existing = resp.json().get('result', [])
                deletes = [{"id": rec["id"]} for rec in existing]

                posts = [
                    {
                        "name": CF_DNS_RECORD_NAME,
                        "type": "TXT",
                        "content": content,
                        "ttl": CF_TTL
                    }
                    for content in dns_content_list
                ]

                batch_url = f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records/batch"
                payload = {"deletes": deletes, "posts": posts}
                batch_resp = requests.post(batch_url, headers=headers, json=payload,
                                           timeout=(CF_DNS_CONNECT_TIMEOUT, CF_DNS_READ_TIMEOUT))
                batch_resp.raise_for_status()
                result = batch_resp.json()
                if not result.get('success'):
                    raise Exception(f"批量更新失败: {result.get('errors')}")

                print(f"Cloudflare TXT 记录批量更新成功！共 {len(dns_content_list)} 条记录，每条内容为一个 IP:端口。")
                return

            except Exception as e:
                error_msg = f"[尝试 {attempt}/{DNS_UPDATE_MAX_RETRIES}] TXT 更新出错: {e}"
                print(error_msg)
                if attempt < DNS_UPDATE_MAX_RETRIES:
                    time.sleep(DNS_UPDATE_RETRY_DELAY)
                else:
                    final_error = f"Cloudflare TXT 记录更新失败，已重试 {DNS_UPDATE_MAX_RETRIES} 次，错误：{e}"
                    print(final_error)
                    send_wxpusher_notification(content=final_error, summary="DNS 更新失败")

def sync_to_github():
    script_dir = os.path.dirname(os.path.abspath(__file__))

    if sys.platform == "win32":
        script_name = "git_sync.ps1"
        interpreter = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]
        creationflags = subprocess.CREATE_NO_WINDOW
    else:
        script_name = "git_sync.sh"
        interpreter = ["bash"]
        creationflags = 0

    script_path = os.path.join(script_dir, script_name)
    if not os.path.exists(script_path):
        print(f"未找到 {script_name}，跳过 GitHub 同步。")
        return

    if sys.platform != "win32":
        try:
            os.chmod(script_path, 0o755)
        except Exception:
            pass

    for attempt in range(1, GITHUB_SYNC_MAX_RETRIES + 1):
        print(f"\n正在同步到 GitHub (尝试 {attempt}/{GITHUB_SYNC_MAX_RETRIES})...")
        try:
            cmd = interpreter + [script_path]
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags
            )

            try:
                stdout, stderr = process.communicate(timeout=GIT_SYNC_PROCESS_TIMEOUT)
                if process.returncode == 0:
                    print("已自动推送到 GitHub。")
                    return
                else:
                    print(f"推送失败 (退出码 {process.returncode})")
                    if stderr:
                        print(f"错误信息: {stderr.strip()}")
            except subprocess.TimeoutExpired:
                process.kill()
                print(f"推送超时（超过 {GIT_SYNC_PROCESS_TIMEOUT} 秒）")
        except Exception as e:
            print(f"推送过程异常: {e}")

        if attempt < GITHUB_SYNC_MAX_RETRIES:
            time.sleep(GITHUB_SYNC_RETRY_DELAY)

    send_wxpusher_notification(
        content=f"GitHub 推送失败，已重试 {GITHUB_SYNC_MAX_RETRIES} 次，请检查网络或仓库状态。",
        summary="GitHub 推送失败"
    )
    print(f"已尝试 {GITHUB_SYNC_MAX_RETRIES} 次推送，均失败，请检查网络或 GitHub 仓库状态。")

def evaluate_output_guard(final_count, prev_count):
    """最小产出保护判定。

    返回 (是否拦截, 本次要求的阈值)。
      - 阈值 = max(MIN_OUTPUT_NODES, 上次数量 × MIN_KEEP_RATIO)
      - 若本次有环节硬降级（结果来自上一层未验证节点），阈值提高到不低于上次
    """
    if prev_count > 0:
        need = max(MIN_OUTPUT_NODES, int(prev_count * MIN_KEEP_RATIO))
    else:
        need = MIN_OUTPUT_NODES
    if DEGRADED_STAGES and final_count < prev_count:
        need = max(need, prev_count)
    return final_count < need, need


def count_output_nodes(path):
    """统计 ip.txt 中有效节点行数（忽略空行、广告行和注释行）"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return sum(1 for line in f
                       if line.strip() and '#' in line and not line.lstrip().startswith('#'))
    except FileNotFoundError:
        return 0
    except Exception:
        return 0


def write_run_summary(final_count, prev_count, blocked):
    """把本次运行的漏斗计数写成 JSON，供通知脚本和事后排查使用"""
    path = os.path.join(os.path.dirname(os.path.abspath(OUTPUT_FILE)), "run_summary.json")
    payload = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stages": RUN_STATS,
        "final": final_count,
        "prev": prev_count,
        "blocked": blocked,
        "degraded": list(DEGRADED_STAGES),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"写入 run_summary.json 失败: {e}")


def write_ip_txt(final_nodes, output_file,
                 header_enabled, header_lines,
                 footer_enabled, footer_lines,
                 perline_enabled, perline_text,
                 speed_map=None, latency_map=None,
                 http_latency_map=None, http_jitter_map=None,
                 display_overrides=None):
    display_overrides = display_overrides or {}
    with open(output_file, "w", encoding="utf-8") as f:
        if header_enabled:
            for line in header_lines:
                f.write(line + "\n")
        for node in final_nodes:
            # 展示用节点（IPv6 已替换为真实地区），但指标查询用原始节点
            out_node = display_overrides.get(node, node)
            extras = []
            if IP_TXT_SHOW_LATENCY and latency_map and node in latency_map:
                extras.append(f"{latency_map[node]*1000:.0f}ms")
            if IP_TXT_SHOW_HTTP_LATENCY and http_latency_map and node in http_latency_map:
                extras.append(f"{http_latency_map[node]:.0f}ms")
            if IP_TXT_SHOW_BANDWIDTH and speed_map and node in speed_map:
                extras.append(f"{speed_map[node]:.2f}Mbps")
            if IP_TXT_SHOW_HTTP_JITTER and http_jitter_map and node in http_jitter_map:
                extras.append(f"{http_jitter_map[node]:.0f}ms")
            line = out_node
            if extras:
                line += " " + " ".join(extras)
            if perline_enabled and perline_text:
                line += perline_text
            f.write(line + "\n")
        if footer_enabled:
            for line in footer_lines:
                f.write(line + "\n")

def main():
    mode_str = f"全局最优{GLOBAL_TOP_N}个" if USE_GLOBAL_MODE else f"每个国家最优{PER_COUNTRY_TOP_N}个"
    print(f"当前模式：{mode_str}，每个节点测试 {TCP_PROBES} 次 TCP 连接")
    print(f"最低成功率要求：{MIN_SUCCESS_RATE*100:.0f}%")
    print(f"IP 可用性二次筛选：{'启用' if TEST_AVAILABILITY else '禁用'}（仅对候选节点）")
    print(f"HTTP检测：{'启用' if HTTP_TEST_ENABLED else '禁用'}（仅对候选节点）")
    print(f"IPv6 客户端 IP 过滤（仅作用于DNS更新环节）：{'启用' if FILTER_IPV6_AVAILABILITY else '禁用'}")
    print(f"DNS黑名单过滤：{'启用' if FILTER_BLOCKED_COUNTRIES_ENABLED else '禁用'}，黑名单国家：{', '.join(BLOCKED_COUNTRIES)}")
    print(f"IP 风险等级过滤：{'启用' if DNS_IP_RISK_FILTER_ENABLED else '禁用'}（最高允许：{DNS_IP_RISK_MAX_LEVEL}）")
    print(f"带宽测速候选数：{BANDWIDTH_CANDIDATES}，测速文件大小：{BANDWIDTH_SIZE_MB} MB，超时：{BANDWIDTH_TIMEOUT}s")
    if FILTER_COUNTRIES_ENABLED:
        print(f"前置白名单过滤：启用，仅保留：{', '.join(ALLOWED_COUNTRIES)}")

    nodes = []
    for source in ADDITIONAL_SOURCES:
        if not source.get("enabled", True):
            continue
        url = source.get("url")
        if not url:
            continue
        v2_nodes = fetch_additional_source(url)
        if v2_nodes:
            seen = set()
            for n in nodes:
                seen.add(n.split('#')[0])
            for n in v2_nodes:
                key = n.split('#')[0]
                if key not in seen:
                    seen.add(key)
                    nodes.append(n)
    RUN_STATS["数据源合并"] = len(nodes)
    print(f"合并后总计 {len(nodes)} 个节点。")

    if IPV6_ENABLED:
        v6 = generate_ipv6_candidates()
        if v6:
            nodes.extend(v6)
            print(f"已注入 {len(v6)} 个 IPv6 候选节点（Cloudflare IPv6 段随机采样）。")

    token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), IP_CALIBRATION_TOKEN_FILE)
    cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), IP_CALIBRATION_CACHE_FILE)
    calibrate_regions(nodes, token_file, cache_file)

    if PRE_FILTER_PORT_ENABLED:
        before = len(nodes)
        nodes = [n for n in nodes if _node_port(n) in PRE_FILTER_PORTS]
        after = len(nodes)
        ports_display = ', '.join(PRE_FILTER_PORTS)
        print(f"前置端口过滤（仅保留端口 {ports_display}）：{before} -> {after} 个节点")
        if not nodes:
            print("前置端口过滤后无任何节点，退出程序。")
            sys.exit(0)

    if PRE_FILTER_BLOCKED_ENABLED and PRE_FILTER_BLOCKED_COUNTRIES:
        before = len(nodes)
        blocked_set = set(PRE_FILTER_BLOCKED_COUNTRIES)
        nodes = [n for n in nodes if n.split('#')[-1].split()[0].upper() not in blocked_set]
        after = len(nodes)
        print(f"前置黑名单过滤：{before} -> {after} 个节点（已屏蔽：{', '.join(sorted(blocked_set))}）")
        if not nodes:
            print("前置黑名单过滤后无任何节点，退出程序。")
            sys.exit(0)

    if not nodes:
        print("没有获取到任何有效节点，退出。")
        sys.exit(1)

    if FILTER_COUNTRIES_ENABLED and ALLOWED_COUNTRIES:
        before = len(nodes)
        allowed_set = {c.upper() for c in ALLOWED_COUNTRIES}
        filtered_nodes = []
        for node in nodes:
            parts = node.split('#')
            if len(parts) == 2 and parts[1].split()[0].upper() in allowed_set:
                filtered_nodes.append(node)
        nodes = filtered_nodes
        after = len(nodes)
        print(f"\n国家过滤（测试前）：{before} -> {after} 个节点（允许国家：{', '.join(allowed_set)}）")
        if not nodes:
            print("过滤后无任何节点，退出程序。")
            sys.exit(0)

    total = len(nodes)
    print(f"开始 TCP 连接测试（超时 {TIMEOUT}s，并发 {MAX_WORKERS}）...")

    results = []
    completed = 0
    last_print = time.time()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(test_node, node): node for node in nodes}
        for future in as_completed(futures):
            completed += 1
            res = future.result()
            if res:
                results.append(res)
            now = time.time()
            if now - last_print >= PROGRESS_PRINT_INTERVAL or completed == total:
                print(f"\r进度：{completed}/{total} ({(completed/total)*100:.1f}%)", end="", flush=True)
                last_print = now

    RUN_STATS["TCP通过"] = len(results)
    print("\nTCP 测试完成！")
    if not results:
        print("没有通过成功率筛选的节点，请检查网络或降低 MIN_SUCCESS_RATE。")
        sys.exit(0)

    results.sort(key=lambda x: (-x[3], x[1]))
    latency_map = {node: lat for node, lat, _, _ in results}

    if USE_GLOBAL_MODE:
        ipv4_results = [r for r in results if not _is_ipv6_node(r[0])]
        ipv6_results = [r for r in results if _is_ipv6_node(r[0])]
        reserve = int(IPV6_CANDIDATE_RESERVE)
        reserve = min(reserve, len(ipv6_results))
        ipv4_take = max(0, BANDWIDTH_CANDIDATES - reserve)
        candidates = [n for n, _, _, _ in ipv4_results[:ipv4_take]]
        candidates += [n for n, _, _, _ in ipv6_results[:reserve]]
        print(f"\nTCP 最优前 {len(candidates)} 个节点进入候选池"
              f"（其中 IPv6 预留 {reserve} 个）。")
    else:
        country_nodes = defaultdict(list)
        for node_str, lat, country, succ in results:
            country_nodes[country].append((node_str, lat, succ))

        total_countries = len(country_nodes)
        base_limit = max(1, BANDWIDTH_CANDIDATES // total_countries)
        candidates = []
        for country, nodes in country_nodes.items():
            nodes_sorted = sorted(nodes, key=lambda x: (-x[2], x[1]))
            limit = min(len(nodes_sorted), base_limit)
            for node_str, lat, succ in nodes_sorted[:limit]:
                candidates.append(node_str)
        print(f"\n各国家候选池分配：共 {total_countries} 个国家，每国最多 {base_limit} 个候选，总计 {len(candidates)} 个节点进入候选池。")

    if not candidates:
        print("没有候选节点，退出。")
        sys.exit(0)

    RUN_STATS["候选池"] = len(candidates)
    candidates_after_availability, avail_ip_info, avail_exit_details = availability_filter_with_retry(candidates)
    RUN_STATS["可用性通过"] = len(candidates_after_availability)
    candidates_after_http, http_latency_map, http_jitter_map = http_server_filter(candidates_after_availability, cfg)
    RUN_STATS["HTTP通过"] = len(candidates_after_http)

    bw_results = []
    for attempt in range(1, BANDWIDTH_RETRY_MAX + 1):
        print(f"\n[带宽测速] 第 {attempt} 轮测试...")
        bw_results = bandwidth_filter(candidates_after_http)
        RUN_STATS["带宽有效"] = len(bw_results)
        if bw_results:
            break
        if attempt < BANDWIDTH_RETRY_MAX:
            print(f"本轮测速无有效结果，等待 {BANDWIDTH_RETRY_DELAY} 秒后重试...")
            time.sleep(BANDWIDTH_RETRY_DELAY)

    if not bw_results:
        print("\n带宽测速多次重试仍无有效结果，将使用 TCP 筛选结果作为最终节点。")
        DEGRADED_STAGES.append("带宽测速全部失败")
        send_wxpusher_notification(
            content=f"带宽测速经 {BANDWIDTH_RETRY_MAX} 轮尝试后仍无有效结果，已降级使用 TCP 排序节点。",
            summary="带宽测速全部失败"
        )
        speed_map = {}
        if USE_GLOBAL_MODE:
            final_selected = [node for node, _, _, _ in results[:GLOBAL_TOP_N]]
        else:
            final_selected = []
            for country, nodes in country_nodes.items():
                nodes_sorted = sorted(nodes, key=lambda x: (-x[2], x[1]))
                for node_str, _, _ in nodes_sorted[:PER_COUNTRY_TOP_N]:
                    final_selected.append(node_str)
    else:
        speed_map = {node: speed for node, speed in bw_results}
        scored_nodes = []
        for node, speed in bw_results:
            tcp_lat = latency_map.get(node, 999.0)
            http_lat = http_latency_map.get(node, 999999.0)
            http_jitter = http_jitter_map.get(node, 999999.0)
            http_lat_sec = http_lat / 1000.0
            http_jitter_sec = http_jitter / 1000.0
            penalty = 1.0 + TCP_LATENCY_WEIGHT * tcp_lat + HTTP_LATENCY_WEIGHT * http_lat_sec + JITTER_WEIGHT * http_jitter_sec
            score = (SPEED_WEIGHT * speed) / penalty
            scored_nodes.append((node, score, speed, tcp_lat, http_lat))

        scored_nodes.sort(key=lambda x: x[1], reverse=True)

        if STABILITY_ENABLED:
            # 稳定性增强层：跨运行历史评分 + 滞回 + 确认闸门。
            # 测量仍每次进行，但「发布集合」只在被证实时变更，避免 IP 反复跳动。
            _stab_state = stability_layer.load_state(STABILITY_STATE_FILE)
            final_selected, _stab_state = stability_layer.apply_stability(
                _stab_state, scored_nodes, cfg,
                USE_GLOBAL_MODE, GLOBAL_TOP_N, PER_COUNTRY_TOP_N)
            stability_layer.save_state(STABILITY_STATE_FILE, _stab_state)
            print(f"\n[稳定性层] 已发布 {len(final_selected)} 个节点"
                  f"（滞回/确认已生效，发布集合仅在被证实时变更）")
        elif USE_GLOBAL_MODE:
            final_selected = [item[0] for item in scored_nodes[:GLOBAL_TOP_N]]
        else:
            country_scored = defaultdict(list)
            for item in scored_nodes:
                node, score, speed, tcp_lat, http_lat = item
                country = node.split('#')[-1] if '#' in node else ''
                if country:
                    country_scored[country].append(item)
            final_selected = []
            for country, items in country_scored.items():
                items.sort(key=lambda x: x[1], reverse=True)
                for item in items[:PER_COUNTRY_TOP_N]:
                    final_selected.append(item[0])
            score_dict = {item[0]: item[1] for item in scored_nodes}
            final_selected.sort(key=lambda n: score_dict.get(n, 0), reverse=True)

    # IPv6 保底：保证合并输出中始终含一定数量的 IPv6 节点
    # 候选池优先取带带宽得分的 scored_nodes，退回已由 HTTP /cdn-cgi/trace 校验的 IPv6
    _v6_pool = []
    if 'scored_nodes' in locals():
        _v6_pool += [(n, s) for n, s, *_ in scored_nodes]
    _v6_pool += [(n, 0.0) for n in candidates_after_http if _is_ipv6_node(n)]
    final_selected = _ensure_ipv6_representation(final_selected, _v6_pool)

    # IPv6 节点解析真实地区（仅展示用，放在全部筛选之后，不影响保活/过滤）
    _ipv6_geo = resolve_ipv6_countries(final_selected)
    display_overrides = {
        n: n.replace("#IPV6", "#" + _ipv6_geo.get(n, "IPV6"))
        for n in final_selected if _is_ipv6_node(n)
    }

    print("\n================ 最终优选节点 ================")
    for i, node in enumerate(final_selected, 1):
        disp = display_overrides.get(node, node)
        speed = speed_map.get(node, 0)
        tcp_lat = latency_map.get(node)
        if tcp_lat is not None:
            line = f"{i}. {disp} {tcp_lat*1000:.0f}ms {speed:.2f}Mbps"
        else:
            line = f"{i}. {disp} {speed:.2f}Mbps"
        print(line)

    # ============ 最小产出保护 ============
    # 产出明显低于历史水平时，保留旧 ip.txt、不更新 DNS、不推送 GitHub。
    # 历史事故：2026-10-17 20:05 那次只产出 1 个节点（正常 30~36），
    # 却照常覆盖并 force push，导致公开订阅降级了 12 小时。
    prev_count = count_output_nodes(OUTPUT_FILE)
    blocked, need = evaluate_output_guard(len(final_selected), prev_count)
    RUN_STATS["最终产出"] = len(final_selected)
    write_run_summary(len(final_selected), prev_count, blocked)
    if blocked:
        reason = f"，且降级环节：{'、'.join(DEGRADED_STAGES)}" if DEGRADED_STAGES else ""
        msg = (f"本次仅产出 {len(final_selected)} 个节点，低于安全阈值 {need}"
               f"（上次 {prev_count}）{reason}。已保留旧 {OUTPUT_FILE}，本次不覆盖、不更新 DNS、不推送。")
        print(f"\n[保护] {msg}")
        send_wxpusher_notification(content=msg, summary="产出异常，已拦截发布")
        return
    # =====================================

    write_ip_txt(final_selected, OUTPUT_FILE,
                 AD_HEADER_ENABLED, AD_HEADER_LINES,
                 AD_FOOTER_ENABLED, AD_FOOTER_LINES,
                 AD_PERLINE_ENABLED, AD_PERLINE_TEXT,
                 speed_map=speed_map,
                 latency_map=latency_map,
                 http_latency_map=http_latency_map,
                 http_jitter_map=http_jitter_map,
                 display_overrides=display_overrides)
    print(f"\n结果已保存到 {OUTPUT_FILE}（共 {len(final_selected)} 个节点）")

    ip_list = [node.split(':')[0] for node in final_selected]

    batch_update_cloudflare_dns(
        ip_list,
        ip_info=avail_ip_info,
        full_bw_results=bw_results,
        target_count=None,
        latency_map=latency_map,
        http_latency_map=http_latency_map,
        http_jitter_map=http_jitter_map
    )

    sync_to_github()

if __name__ == "__main__":
    import atexit

    # ---- 单实例检查：已有实例在跑就直接退出 ----
    if not acquire_single_instance():
        print("检测到本程序已在运行，本次启动自动退出。")
        sys.exit(0)
    atexit.register(release_single_instance)
    # -------------------------------------------

    enable_log = ENABLE_LOGGING
    log_filename = LOG_FILE

    if enable_log:
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            log_path = os.path.join(script_dir, log_filename)
            # 追加写入 + 超过 2MB 轮转（此前用 "w" 每次覆盖，出问题后无法回溯）
            try:
                if os.path.exists(log_path) and os.path.getsize(log_path) > 2 * 1024 * 1024:
                    os.replace(log_path, log_path + ".1")
            except Exception:
                pass
            log_f = open(log_path, "a", encoding="utf-8")
            log_f.write("\n" + "=" * 64 + "\n")
            log_f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  运行开始\n")
            print("日志已启用，输出将追加保存到 " + log_path)
        except Exception as e:
            print(f"无法打开日志文件 {log_path}: {e}")
            log_f = None
        else:
            class _Tee:
                def __init__(self, *files):
                    self.files = files
                def write(self, obj):
                    for f in self.files:
                        f.write(obj)
                        f.flush()
                def flush(self):
                    for f in self.files:
                        f.flush()
            sys.stdout = _Tee(sys.stdout, log_f)

            def _close_log():
                try:
                    sys.stdout = sys.__stdout__
                    log_f.close()
                except Exception:
                    pass
            atexit.register(_close_log)

    main()