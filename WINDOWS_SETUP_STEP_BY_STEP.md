# Windows 部署步骤（照着做就行）

> 这份文档假设你**完全没用过命令行**。每一步都写清楚「在哪点、复制什么、粘贴什么」。
> 全程大约 30~40 分钟，其中大部分时间在等下载和跑测试。

---

## 先看这里：你需要准备什么

只要三样东西，其余的部署脚本会帮你搞定：

| 要准备的 | 在哪拿 | 大概要多久 |
|---|---|---|
| **GitHub Token** | <https://github.com/settings/tokens> | 2 分钟 |
| **WxPusher Token**（可选，不想要推送通知就不要） | <https://wxpusher.zjiecode.com> | 1 分钟 |

> **不用装 Git！** 很多人一看到 GitHub 就以为要装 Git，其实不需要。
> 本项目的上传走 GitHub 官方接口，装了反而多余。

---

## 第 1 步：下载代码（5 分钟）

### 1.1 建文件夹

在「此电脑」里找个地方建个文件夹，比如 `D:\cf-ip`。

### 1.2 浏览器下载

1. 打开 <https://github.com/Bher12/cf-ip-stable>
2. 右上角绿色按钮 **`< > Code`**
3. 点 **Download ZIP**
4. 下载完解压，把里面的文件夹改名成 `cf-ip-stable`，放进 `D:\` 里

**最终应该是这个结构：**
```
D:\cf-ip-stable\
    main.py               ← 主程序
    stability_layer.py    ← 稳定性模块
    setup.ps1             ← 一键部署脚本
    config.json.example   ← 配置模板
    git_sync.ps1.example  ← 上传脚本模板
    README_STABLE.md
```

### 1.3 检查一下文件齐不齐

在 `D:\cf-ip-stable` 文件夹里**空白处点右键 → 在终端中打开**（或地址栏输入 `cmd` 回车），然后输入：

```cmd
dir
```

应该能看到 `main.py`。如果只有 3-4 个文件，说明解压错了层级，重新解压。

---

## 第 2 步：装 Python（10 分钟）

### 2.1 下载安装

1. 打开 <https://www.python.org/downloads/>
2. 点那个大大的 **Download Python 3.x.x** 按钮
3. 运行安装包

### 2.2 ⚠️ 关键：安装时一定要勾上「Add to PATH」

安装界面第一页有个复选框：

```
☑ Add python.exe to PATH     ← 这个必须勾上！
```

**勾错的后果**：后面所有命令都会报「python 不是内部或外部命令」，能折腾一下午。

### 2.3 验证装好了

在刚才打开的终端里（或者新开一个 cmd 窗口），输入：

```cmd
python --version
```

看到类似 `Python 3.12.7` 就成了。

> 如果提示找不到命令，说明第 2.2 步没勾。重新运行安装包 → 选 Modify → 勾上 Add to PATH。

---

## 第 3 步：跑一键部署脚本（10 分钟）

### 3.1 右键管理员运行

在 `D:\cf-ip-stable` 文件夹里：

1. 空白处**右键**
2. 选 **「在终端中以管理员身份打开」**
   （如果没有这一项，去开始菜单搜「PowerShell」，右键选「以管理员身份运行」）

### 3.2 运行脚本

在弹出的窗口里粘贴这行，回车：

```powershell
cd D:\cf-ip-stable
.\setup.ps1
```

### 3.3 如果提示「禁止运行脚本」

PowerShell 默认禁止运行本地脚本，会报错：

```
无法加载文件 xxx.ps1，因为在此系统上禁止运行脚本
```

**解决办法**（复制这行执行，只影响当前窗口，不改系统设置）：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

然后重新执行 `.\setup.ps1`。

### 3.4 脚本会做什么

```
[1/4] 检查 Python...        ✅ 已安装
[2/4] 检查 Git...           ℹ️ 未检测到 Git —— 不影响使用
[3/4] 检查 curl...          ✅ Windows 10+ 自带
[4/4] 安装 Python 依赖...    requests / aiohttp / brotlicffi
正在创建 .gitignore...      ✅
已从 config.json.example 生成 config.json    ✅
已从 git_sync.ps1.example 生成 git_sync.ps1   ✅
正在配置 Windows 计划任务...  ✅ 每天14:00-次日02:00 每2小时一次
```

**全部绿色对勾就是成功了。**

> 如果第 4 步装依赖失败，多半是网络问题，重新跑一次这个脚本即可（它是幂等的，不会重复安装）。

### 3.5 脚本结束时会问你要不要立即测试

```
是否立即运行一次 main.py 进行测试？(Y/N)
```

输入 `Y` 回车。**这一步会跑几分钟**，是正常的（要测 2 万多个 IP）。

跑完看到这些就对了：
```
================ 最终优选节点 ================
1. xxx.xxx.xxx.xxx:443#SG 65ms 32.62Mbps
...
结果已保存到 D:\cf-ip-stable\ip.txt（共 15 个节点）
正在同步到 GitHub (尝试 1/3)...
已自动推送到 GitHub。
```

**推送成功就意味着部署完成了一大半。** 按 Enter 退出脚本。

---

## 第 4 步：填两个密钥（5 分钟）

前面脚本自动生成了配置文件，但**密钥还得手填**（密钥不能放 GitHub 上）。

### 4.1 先搞 GitHub Token（这个必须有）

1. 打开 <https://github.com/settings/tokens>
2. 滚到最下面 **Generate new token (classic)**
3. Note 随便填个名字，比如 `cf-ip`
4. **Expiration 选个长的**（比如 90 天，或 No expiration）
5. 勾选权限 **`repo`**（就勾这一个，勾多了有安全风险）
6. 拉到底，点 **Generate token**
7. 复制那串 `ghp_...`（**只显示一次，关掉就看不到了**）

### 4.2 填进 git_sync.ps1

1. 在 `D:\cf-ip-stable` 里右键 `git_sync.ps1` → **打开方式** → 记事本
   （如果记事本打不开，用 VS Code 也行）
2. 找到开头这几行：

```powershell
$github_token    = if ($env:GITHUB_TOKEN) { $env:GITHUB_TOKEN } else { "your_github_token_here" }
$github_username = "Bher12"
$repo_name       = "cf-ip-stable"
```

3. 改成：

```powershell
$github_token    = "ghp_把你复制的粘这里"
$github_username = "Bher12"
$repo_name       = "cf-ip-stable"
```

> **用户名和仓库名不用改**，因为还是用你原来那个仓库。
> 如果你要推到新仓库，才需要改这两行。

4. Ctrl+S 保存

### 4.3 填 WxPusher（可选，不填也能用）

1. 打开 <https://wxpusher.zjiecode.com>，微信扫码登录
2. 复制它的 **APP_TOKEN**（形如 `AT_xxxxx`）和 **UID**（形如 `UID_xxxxx`）
3. 用记事本打开 `config.json`，找到这两行：

```json
  "WXPUSHER_APP_TOKEN": "AT_xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "WXPUSHER_UIDS": ["UID_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"],
```

4. 把引号里的 `xxx` 换成真实值，Ctrl+S 保存

> **不想要推送通知？** 把 `"ENABLE_WXPUSHER": true` 改成 `false` 就行。

---

## 第 5 步：确认订阅地址能用了（2 分钟）

浏览器打开这个地址：

```
https://raw.githubusercontent.com/Bher12/cf-ip-stable/refs/heads/main/ip.txt
```

应该能看到一列 IP 地址，像这样：

```
139.162.60.149:443#SG 132ms 20.70Mbps
162.4.172.66:443#SG 136ms 20.11Mbps
[2a06:98c1:58:0:f574:97ea:5972:166e]:443#US 207ms 14.56Mbps
...
```

**能看到内容 = 部署成功。** 这个地址就是你的订阅链接，填到 Clash / sing-box / v2rayN 等客户端里就能用。

---

## 第 6 步：让旧机器别再抢着推（重要！）

**如果你 Linux 那台机器还在跑定时任务，两边会互相覆盖 ip.txt**，你会发现订阅内容莫名其妙在变。

### 停掉 Linux 的定时任务

在 Linux 那台机器上执行：

```bash
crontab -l
```

如果看到有 cf-ip 相关的行，把它们删掉，或者直接：

```bash
crontab -r      # 清空所有定时任务（确认没有别的任务再用）
```

也可以更精准地注释掉那几行。改完记得 `crontab -e` 保存。

---

## 第 7 步：以后怎么用

### 自动跑

计划任务已经建好了：**每天 14:00 到次日 02:00，每 2 小时跑一次**。

想看跑得怎么样：
1. 按 `Win + R` → 输入 `taskschd.msc` → 回车
2. 左侧找到「任务计划程序库」→ 里面有 **"Cloudflare IP 优选"**
3. 双击能看到「历史状态」，上次运行是成功(0)还是失败

想改时间：右键任务 → 属性 → 触发器 → 编辑。

### 手动跑一次

在 `D:\cf-ip-stable` 里的终端（普通权限就行）：

```cmd
cd D:\cf-ip-stable
python main.py
```

### 换电脑 / 重装系统

直接看仓库里的 **`DEPLOY_NEW_MACHINE.md`**，步骤和本文档一样。

---

## 出问题了怎么办

| 症状 | 原因 | 解决 |
|---|---|---|
| `python 不是内部或外部命令` | 装 Python 时没勾 Add to PATH | 重装，勾上；或加系统环境变量 PATH |
| `无法加载文件 xxx.ps1，因为在此系统上禁止运行脚本` | PowerShell 安全策略 | 执行 `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` |
| 推送报 `401 Bad credentials` | Token 填错/过期 | 重新生成 Token；注意别把引号删了 |
| 推送报 `404 Not Found` | 仓库名或用户名写错 | 检查 `git_sync.ps1` 里的两行；或手动在 GitHub 建个同名仓库 |
| `ipinfo` 查询全失败 | 缺 brotlicffi | `pip install brotlicffi`（系统自带的 brotli 和 aiohttp 不兼容，必须用这个） |
| 产出里没有 IPv6 节点 | 白名单没配 | 检查 `config.json` 的 `IPV6_PREFIX_ALLOWLIST` 应含 `2a06:98c1:58` |
| 产出里有 `0.00Mbps` 的节点 | 带宽门槛没配 | 检查 `config.json` 的 `BANDWIDTH_MIN_SPEED_MBPS` 和 `BANDWIDTH_MIN_SPEED_MBPS_IPV6` |
| 计划任务不执行 | 电池供电时会被暂停 | 任务属性 → 条件 → 取消勾选「只有在交流电源时才启动」 |
| 跑很久不结束 | 正常，要测 2 万个 IP | 耐心等，通常 4~6 分钟 |

---

## 一句话总结流程

```
下载 ZIP → 装 Python（勾 Add to PATH！）→ 跑 setup.ps1 → 填两个密钥 → 看订阅地址
```
