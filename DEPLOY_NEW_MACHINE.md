# 换机部署指南（Linux / Windows 通用）

本项目可以**完全依赖 GitHub 仓库完成换机部署**——代码、配置模板、推送脚本模板都在仓库里，
新电脑拉下来填几个密钥就能跑。

---

## 一、GitHub 上都有什么

两个项目各自独立仓库：

| 项目 | 仓库 | 订阅地址 |
|---|---|---|
| 原版 | `Tll-ll/cf-ip` | `https://raw.githubusercontent.com/Tll-ll/cf-ip/refs/heads/main/ip.txt` |
| 稳定版 | `Bher12/cf-ip-stable` | `https://raw.githubusercontent.com/Bher12/cf-ip-stable/refs/heads/main/ip.txt` |

仓库内容：

| 文件 | 原版 | 稳定版 | 说明 |
|---|---|---|---|
| `main.py` | ✅ | ✅ | 主程序（跨平台，已内置 Windows 分支） |
| `stability_layer.py` | — | ✅ | 稳定性层（仅稳定版） |
| `setup.sh` / `setup.ps1` | ✅ | ✅ | 一键部署脚本 |
| `git_sync.sh.example` / `git_sync.ps1.example` | ✅ | ✅ | 推送脚本模板（**REST API 版，无需 git**） |
| `config.json.example` | ✅ | ✅ | 完整配置模板（无密钥） |
| `ip.txt` | ✅ | ✅ | 优选结果（订阅内容） |
| `run_summary.json` | ✅ | ✅ | 运行漏斗统计 |
| `stability_state.json` | — | ✅ | 跨运行稳定性记忆 |
| `README.md` / `README_STABLE.md` | ✅ | ✅ | 说明文档 |
| `notify_wrapper.ps1` / `update_fork.ps1` | ✅ | ✅ | 通知与上游同步脚本 |

**仓库里没有的（必须在新机器上补）**：

| 文件 | 原因 | 怎么办 |
|---|---|---|
| `config.json` | 含密钥 | 复制 `config.json.example`，填入密钥 |
| `git_sync.sh` / `git_sync.ps1` | 含 Token | 复制对应 `.example`，填入 Token |
| `valid_tokens.txt` | 含 ipinfo.io token | 手动创建，每行一个 token |
| `ipinfo_cache.txt` | 运行时缓存 | 会自动重建，无需管 |

---

## 二、Linux 部署

### 1. 拉代码

```bash
# 用 REST API 下载（无需安装 git）
mkdir -p ~/cf-ip-deploy/cf-ip-stable && cd ~/cf-ip-deploy/cf-ip-stable
TOKEN=你的GitHub_TOKEN
REPO=Bher12/cf-ip-stable
for f in main.py stability_layer.py setup.sh git_sync.sh.example git_sync.ps1.example \
         config.json.example README_STABLE.md README.md .gitignore; do
  curl -sL -H "Authorization: token $TOKEN" \
    "https://raw.githubusercontent.com/$REPO/refs/heads/main/$f" -o "$f"
done
```

也可以直接装 git 后 `git clone`：
```bash
git clone https://github.com/Bher12/cf-ip-stable.git
```

### 2. 装依赖

```bash
sudo apt install -y curl python3 python3-pip
python3 -m pip install --user requests aiohttp brotlicffi
```

> `brotlicffi` 是必需的：系统自带的 `brotli` 与 `aiohttp` 不兼容，会导致 ipinfo 响应静默失败。

### 3. 准备配置

```bash
cd ~/cf-ip-deploy/cf-ip-stable
cp config.json.example config.json
vi config.json          # 填 WXPUSHER_APP_TOKEN / WXPUSHER_UIDS（通知功能）
cp git_sync.sh.example git_sync.sh
vi git_sync.sh          # 填 github_token / github_username / repo_name
chmod 700 git_sync.sh
chmod 600 config.json
```

### 4. GitHub Token 存放（可选，推荐）

不想把 token 写进脚本时，用环境变量：

```bash
echo 'export GITHUB_TOKEN=ghp_xxxxx' >> ~/.bashrc
mkdir -p ~/.config/cf-ip && echo 'GITHUB_TOKEN=ghp_xxxxx' > ~/.config/cf-ip/tokens
chmod 600 ~/.config/cf-ip/tokens
```

### 5. 跑一次验证

```bash
python3 main.py
```

确认日志出现「结果已保存到 ... ip.txt」和「已自动推送到 GitHub」，且远端订阅能访问：
```bash
curl -s https://raw.githubusercontent.com/Bher12/cf-ip-stable/refs/heads/main/ip.txt | head
```

### 6. 设定时任务

```bash
crontab -e
```

加入（每 2 小时跑一次）：
```cron
0 */2 * * * . ~/.cf-ip-tokens 2>/dev/null; cd /home/你的用户名/cf-ip-deploy/cf-ip-stable && GITHUB_TOKEN=$CF_IP_STABLE_TOKEN /usr/bin/python3 main.py >> cron.log 2>&1
```

> `main.py` 自带单实例锁，重复触发会自动退出，不会互相干扰。

---

## 三、Windows 部署

### 1. 拉代码

**方式 A：浏览器下载**
访问 `https://github.com/Bher12/cf-ip-stable`，点 `Code` → `Download ZIP`，解压。

**方式 B：PowerShell 用 REST API 下载**
```powershell
$dir = "$HOME\cf-ip-deploy\cf-ip-stable"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$repo = "Bher12/cf-ip-stable"
$files = @("main.py","stability_layer.py","setup.ps1","git_sync.sh.example",
           "git_sync.ps1.example","config.json.example","README_STABLE.md")
foreach ($f in $files) {
    Invoke-WebRequest "https://raw.githubusercontent.com/$repo/refs/heads/main/$f" -OutFile "$dir\$f"
}
```

### 2. 一键部署（推荐）

```powershell
cd $HOME\cf-ip-deploy\cf-ip-stable
.\setup.ps1
```

`setup.ps1` 会自动完成：检查/安装 Python → 装依赖（requests / aiohttp / brotlicffi）
→ 检查 curl → 创建计划任务（每 2 小时一次）→ 生成配置文件。

> 脚本需要管理员权限创建计划任务，会自动提示提权。

### 3. 手动准备配置

```powershell
Copy-Item config.json.example config.json
notepad config.json          # 填 WXPUSHER_APP_TOKEN / WXPUSHER_UIDS

Copy-Item git_sync.ps1.example git_sync.ps1
notepad git_sync.ps1         # 填 github_token / github_username / repo_name
```

> Windows 版推送脚本同样是 REST API 实现，**不需要装 git**。

### 4. 验证

```powershell
python main.py
```

然后浏览器打开订阅地址确认能拉到内容：
`https://raw.githubusercontent.com/Bher12/cf-ip-stable/refs/heads/main/ip.txt`

---

## 四、跨平台差异对照

| 项目 | Linux | Windows |
|---|---|---|
| 推送脚本 | `git_sync.sh`（bash + curl） | `git_sync.ps1`（PowerShell + Invoke-RestMethod） |
| 是否需要 git | **不需要** | **不需要** |
| 单实例锁 | `fcntl.flock` 文件锁 | `CreateMutexW` 命名互斥体 + `msvcrt.locking` |
| 日志空设备 | `/dev/null` | `NUL`（代码已自动判断） |
| 定时方式 | crontab | 计划任务（setup.ps1 自动创建） |
| 路径分隔符 | `/` | `\`（代码用 `os.path` 自动适配） |
| 依赖安装 | `pip install requests aiohttp brotlicffi` | 同左（setup.ps1 自动装） |

`main.py` 内已针对 Windows 做过适配（`sys.platform == "win32"` 分支共 7 处），
**代码本身完全跨平台**，无需修改。

---

## 五、换机时必须重新配置的东西

这些是**机器相关或私密**的，GitHub 上不会有：

1. **WxPusher Token**（`config.json`）
   登录 <https://wxpusher.zjiecode.com> 获取 APP_TOKEN 与 UID
2. **GitHub Token**（`git_sync.sh` / `git_sync.ps1`）
   <https://github.com/settings/tokens> 创建，勾选 `repo` 权限
3. **ipinfo.io Token**（`valid_tokens.txt`）
   <https://ipinfo.io/signup> 获取，每行一个
4. **Cloudflare API Token**（可选，仅在 `CF_ENABLED=true` 时需要）
   需要 Zone 的 DNS 编辑权限

---

## 六、常见问题

**Q: 推送报 `401 Bad credentials`**
A: Token 填错或已过期。注意占位符检测——脚本里如果还是
`your_github_token_here`，会给出友好提示而不是 401。
验证 Token：`curl -s -o /dev/null -w "%{http_code}" -H "Authorization: token $TOKEN" https://api.github.com/user`，
返回 200 即有效。

**Q: 推送报 `404 Not Found`**
A: 仓库名或用户名写错。脚本会自动尝试创建仓库（私有），
若 Token 没有 `repo` 权限则创建失败。

**Q: `ipinfo` 查询全部失败**
A: 缺 `brotlicffi`。系统自带的 `brotli` 与 `aiohttp` 不兼容，
必须装 `brotlicffi`。

**Q: 产出里没有 IPv6 节点**
A: 检查 `config.json` 的 `IPV6_PREFIX_ALLOWLIST`。
Cloudflare 的 `2606:4700::` 段 TCP 能连但测速端点无响应，
必须限定在 `2a06:98c1:58` 段。

**Q: 产出里有 0.00Mbps 的节点**
A: 检查 `BANDWIDTH_MIN_SPEED_MBPS`（IPv4 门槛）与
`BANDWIDTH_MIN_SPEED_MBPS_IPV6`（IPv6 门槛）是否配置且生效。

**Q: 两台机器同时跑会冲突吗**
A: 不会。`main.py` 有单实例锁，同一目录内重复启动会自动退出。
但**不建议**两台机器往同一个仓库推送，会互相覆盖 `ip.txt`。
