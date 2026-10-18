# cf-ip-stable — 稳定性增强版（独立仓库）

这是 [Tll-ll/cf-ip](https://github.com/Tll-ll/cf-ip) 的一个 **fork / 改进版**，专为「选出来的 IP 不够稳、不持久」而做。
它与你正在用的原版**完全独立**：独立文件夹、独立 git 远程、独立 GitHub 仓库、独立订阅链接，可与原版**并行运行、互不干扰**。

---

## 改了什么（核心：测频繁，发谨慎）

原版每 5 分钟全量重排，且只用「瞬时」分数排序，所以轻微网络抖动就会让第一名换人。
本版在原有测量（TCP / 可用性 / HTTP / 带宽）**之上**叠加一层**稳定性选择**：

| 机制 | 作用 |
|---|---|
| 跨运行历史评分 | 每个 IP 维护 EMA 分数 + 近期样本，用「变异系数倒数」算一致性（越稳越高） |
| 稳定分排序 | 换人/上榜只依据 `稳定分 = EMA × 一致性`；尖峰 IP 离散度大 → 一致性低 → 稳定分被压低，顶不掉现任 |
| 滞回（hysteresis） | 现任未被新候选明显超越（默认 >15%）则保留，避免微幅波动换人 |
| 确认闸门 | 全新 IP 必须连续出现 `STABILITY_CONFIRM_RUNS` 次**且**一致性达标，才允许替换现任，滤掉单次尖峰 |
| 缺勤宽限 | 已发布 IP 短暂没测到（网络抖一下）时保留，超过 `STABILITY_MAX_ABSENT_RUNS` 次才淘汰 |

效果：**测量仍每次进行（频繁观测），但「发布集合」只在被证实时才变更（谨慎生效）**，
发布到 `ip.txt` / GitHub 的列表会明显更稳、更持久。

---

## 新增配置项（config.json，均可不填，有内置默认值）

| 配置 | 默认 | 说明 |
|---|---|---|
| `STABILITY_ENABLED` | `true` | 是否启用稳定性层（设 `false` 即回到原版行为） |
| `STABILITY_STATE_FILE` | `stability_state.json` | 跨运行历史状态文件（持久化记忆） |
| `STABILITY_EMA_ALPHA` | `0.35` | EMA 平滑系数，越小越看重历史 |
| `STABILITY_RECENT_N` | `8` | 用于算一致性的近期样本数 |
| `STABILITY_MIN_SAMPLES` | `2` | 样本达到该数量才开始用一致性 |
| `STABILITY_HYSTERESIS_RATIO` | `0.15` | 替换现任所需的最小超越比例（越大越「恋旧」） |
| `STABILITY_CONFIRM_RUNS` | `2` | 新 IP 需连续出现几次才允许上榜 |
| `STABILITY_MAX_ABSENT_RUNS` | `3` | 已发布 IP 连续几次没测到才淘汰 |
| `STABILITY_MIN_CONSISTENCY` | `0.55` | 新候选一致性门槛，低于此值视为尖峰、禁止顶替 |

> 想更稳：调大 `STABILITY_HYSTERESIS_RATIO` / `STABILITY_CONFIRM_RUNS` / `STABILITY_MAX_ABSENT_RUNS`。
> 想更快接纳真正更好的 IP：调小上述值。`STABILITY_MIN_CONSISTENCY` 是防尖峰的关键闸。

---

## 部署到「另一个 GitHub 仓库」（与原版平行）

1. **GitHub 新建仓库**：如 `cf-ip-stable`（Private/Public 均可，不要勾选初始化文件）。
2. **初始化本地仓库并关联新远程**（在本文件夹内执行）：
   ```bash
   git init
   git remote add origin https://github.com/你的用户名/cf-ip-stable.git
   git add -A && git commit -m "init: cf-ip-stable"
   git push -u origin main
   ```
3. **准备推送脚本**（从模板复制，填入你的 token / 用户名 / 仓库名 / 分支）：
   ```bash
   cp git_sync.sh.example git_sync.sh        # Linux
   # Windows: 复制 git_sync.ps1.example 为 git_sync.ps1
   # 然后编辑 git_sync.sh 填写四个变量
   ```
4. **（可选）调参**：若只想把它当作「第二个订阅源」而不想重复更新 Cloudflare DNS / 微信，
   在文件夹内新建 `config.json` 并写入：
   ```json
   { "CF_ENABLED": false, "ENABLE_WXPUSHER": false }
   ```
   （其余配置沿用内置默认值；稳定性参数也可在此覆盖。）
5. **安装依赖 + 定时任务**：Linux 运行 `bash setup.sh`；Windows 用原版 `setup.ps1`。
6. **手动验证一次**：`python main.py`，确认生成 `ip.txt` 与 `stability_state.json`、
   并成功推送到新仓库；之后每 5 分钟自动优选 + 推送。

---

## 与原版并行使用

- 两套放在**不同文件夹**，各自有独立 `.git` 与 `origin` → 推到**不同仓库**。
- 各自生成独立的 `ip.txt` 订阅链接（Raw 链接不同），你的代理客户端可同时订阅两者做对比。
- 本版的 `stability_state.json` 会被一起提交，换机/重部署时历史记忆不丢失。
- 首跑会「冷启动」直接采用瞬时 Top-N；随后几轮内发布集合会自然稳定下来，无需人工干预。

---

## 文件说明（相对原版新增/改动）

- `stability_layer.py` —— **新增**稳定性层模块（可单独 `python stability_layer.py` 自测）
- `main.py` —— 在最终选择处接入稳定性层（原测量/过滤逻辑不动）
- `git_sync.sh.example` / `git_sync.ps1.example` —— **新增**推送脚本模板（额外提交 `stability_state.json`）
- `config.stability.example.json` —— **新增**稳定性参数参考
