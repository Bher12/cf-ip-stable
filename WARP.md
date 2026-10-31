# Cloudflare WARP 免费节点订阅

基于 Cloudflare WARP 免费账号（wgcf 注册）+ 本机实测优选端点生成，可直接导入 Clash / mihomo 系客户端或 WireGuard 客户端使用。

## 订阅地址

**Clash / mihomo 系（推荐）**

```
https://raw.githubusercontent.com/Bher12/cf-ip-stable/main/warp-clash.yaml
```

支持客户端：Clash Verge / Clash Meta (ClashMetaForAndroid) / FlClash / Karing / Stash / Mihomo Party / sing-box(需转换)。

**WireGuard 原生配置**

```
https://raw.githubusercontent.com/Bher12/cf-ip-stable/main/warp-wg.conf
```

可直接导入 WireGuard 官方客户端、Shadowrocket（WireGuard 类型节点）等。

## 节点说明

- 共 10 个节点，全部指向 Cloudflare WARP 官方 Anycast 端点（162.159.192.0/24、188.114.96.0/24 等），端口 2408
- 节点名后的数字是本机实测延迟（HTTP 请求完成时间，含隧道开销）
- `WARP-自动` 组每 120 秒自动测速选最优节点，晚高峰端点抖动时自动切换
- 所有节点共用同一个 WARP 账号（同一出口 IP）

## 分流规则

默认只把 AI / 海外站点走 WARP，国内站点直连：

- AI 类：OpenAI、Claude、Gemini、Grok、Perplexity、Poe、Midjourney、HuggingFace → WARP
- 国内域名（.cn / qq / baidu / taobao / bilibili / zhihu 等）→ 直连
- 其余 → WARP

## 实测结果（2026-10-31 晚，中国移动宽带）

| 项目 | 结果 |
|---|---|
| WARP 握手 | 成功，`warp=on` |
| 出口机房 | 洛杉矶 LAX（`104.28.208.136`） |
| 端点延迟 | 712 ~ 770 ms（HTTP 完成时间） |
| 下载速度 | 0.9 ~ 7 Mbps（晚高峰波动大） |
| Google / YouTube | 200 正常 |
| Gemini | 200 正常 |
| Claude | 302 正常（可登录） |
| ChatGPT / api.openai.com | **403 被 OpenAI 拦截** |

> WARP 的出口是 Cloudflare 数据中心 IP，OpenAI 对这类 IP 段有封禁，ChatGPT 走 WARP 会返回 403。WARP 适合 Google 系、YouTube、Claude、Gemini 以及需要海外 IP 的普通站点，不能用来解锁 ChatGPT。

## 重新生成 / 更新端点

本目录下的脚本可随时重扫端点并重新生成配置（需要同目录放一份 `mihomo.exe` 内核）：

```bash
python scan_warp2.py     # 扫描 100 个 WARP 端点，输出 scan2_result.json
python gen_final.py      # 读取 endpoints.json，生成 out/warp.yaml 与 out/warp.conf
python push_warp.py      # 推送到 GitHub
```

## 注意

- `warp.yaml` / `warp.conf` 里包含本 WARP 账号的私钥，仓库为公开仓库，任何人拿到链接都能使用该账号。如不希望被他人使用，可执行 `wgcf register` 重新注册一个账号替换密钥。
- WARP 免费版不限流量，但晚高峰速度波动明显，不适合作为唯一主力线路。
