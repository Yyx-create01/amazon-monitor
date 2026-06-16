# Amazon Listing Monitor 部署指南

## 概述

每日自动检查 Amazon 产品页面变化，通过飞书通知。支持 10 个维度：标题、价格、促销、五点描述、购物车、Sold By、类目、变体、评分、评论数、销售排名（BSR）。

**运行方式**：Windows 定时任务，每天 6 批分散执行，避免被 Amazon 限流。完全本地运行，无需服务器或 GitHub。限流时可选的 GitHub Actions 备份（需额外配置）。

---

## 1. 前提条件

- Windows 电脑（24 小时开机或设置自动登录）
- Python 3.12+
- 飞书企业账号（自建应用权限）
- 卖家精灵（SellerSprite）MCP 密钥
- （可选）GitHub 账号，用于限流时 US IP 备份

---

## 2. 项目部署

将项目文件夹复制到本地任意目录，然后：

```bash
# 进入项目目录
cd 项目路径\自己ASIN监控

# 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## 3. 飞书配置

### 3.1 创建飞书自建应用

1. 打开 [飞书开发者控制台](https://open.feishu.cn/app)
2. 创建企业自建应用 → 获取 **App ID** 和 **App Secret**
3. 权限配置：
   - `doc:sheet`（电子表格读写）
   - `im:message:send`（发送消息）
4. 发布应用

### 3.2 创建飞书表格

**监控列表**（ASIN 源表格）：
| ASIN | 品名 | 父体名 | 产品线 |
|------|------|--------|--------|
| B09P4XPJ5V | 焊锡-蓝黄盒子-1000pcs | 120焊锡 | 焊锡接线端子 |

**监控结果**（变化日志表）：
同一表格不同 Sheet 或新建表格均可。

### 3.3 获取飞书 ID

- **Sheet Token**：飞书表格 URL 中 `/sheets/` 后面的字符串
- **Sheet ID**：URL 中 `?sheet=` 后面的字符串
- **Chat ID**：飞书群聊设置 → 群机器人 → 添加应用 → 获取 Chat ID

### 3.4 配置 .env 文件

```env
# 飞书
FEISHU_APP_ID=cli_xxxxxxxx
FEISHU_APP_SECRET=xxxxxxxx
FEISHU_SOURCE_SHEET_TOKEN=源表格Token
FEISHU_SOURCE_SHEET_ID=源SheetId
FEISHU_RESULT_SHEET_TOKEN=结果表格Token
FEISHU_RESULT_SHEET_ID=结果SheetId
FEISHU_CHAT_ID=oc_xxxxxxxx,oc_yyyyyyyy

# 卖家精灵 MCP
SELLERSPRITE_MCP_URL=https://mcp.sellersprite.com/mcp
SELLERSPRITE_SECRET_KEY=你的MCP密钥

# 以下可选：限流时自动触发 GitHub Actions（US IP 备份）
# GITHUB_REPO=你的账号/amazon-monitor
# GITHUB_DISPATCH_TOKEN=ghp_xxxxxxxx
```

## 4. 卖家精灵 MCP

注册 [卖家精灵](https://www.sellersprite.com/) → API 设置 → 获取 MCP 密钥。

MCP 提供：BSR 排名、类目节点、变体数量（每个父体只调用 1 次，约 ~50 次/天）。

## 5. Windows 定时任务

以管理员身份打开 PowerShell，复制以下内容保存为 `setup_tasks.ps1` 并运行：

```powershell
$times = @("08:00", "08:20", "08:40", "09:00", "09:20", "09:40")
for ($i = 0; $i -lt 6; $i++) {
    $n = "AmazonMonitor-Batch" + ($i + 1)
    $batch = ($i + 1).ToString() + "/6"
    $cmd = '"C:\Users\Administrator\amazon-monitor\run_local.bat" --batch ' + $batch
    schtasks /create /tn $n /tr $cmd /sc daily /st $times[$i] /ru Administrator /rl highest /f
}
```

> **重要**：需要设置 Windows 自动登录（否则定时任务可能因未登录而跳过）。
> `Win+R` → `netplwiz` → 取消勾选"要使用本计算机，用户必须输入用户名和密码"

## 6. GitHub Actions（可选备胎）

纯本地运行不需要。仅当本地频繁被限流时，可配置 GitHub Actions 用美国 IP 接力。

### 6.0 前置

1. Fork 本项目到你的 GitHub
2. 仓库 URL 填入 `.env` 的 `GITHUB_REPO`
3. 创建 GitHub Token 填入 `GITHUB_DISPATCH_TOKEN`

### 6.1 配置 Secrets

在 GitHub 仓库 → Settings → Secrets and variables → Actions，添加：

| Secret | 说明 |
|--------|------|
| `FEISHU_APP_ID` | 飞书 App ID |
| `FEISHU_APP_SECRET` | 飞书 App Secret |
| `FEISHU_SOURCE_SHEET_TOKEN` | 源表格 Token |
| `FEISHU_RESULT_SHEET_TOKEN` | 结果表格 Token |
| `FEISHU_CHAT_ID` | 飞书 Chat ID |
| `FEISHU_SOURCE_SHEET_ID` | 源 Sheet ID |
| `FEISHU_RESULT_SHEET_ID` | 结果 Sheet ID |
| `CF_PROXY` | Cloudflare Worker 代理地址（可选） |

### 6.2 创建 GitHub Token

`Settings → Developer settings → Personal access tokens → Tokens (classic)` → Generate new token（勾选 `repo`，不设过期）。

将 Token 填入本地 `.env` 的 `GITHUB_DISPATCH_TOKEN`。

## 7. 验证

```bash
# 手动跑一批测试
.venv\Scripts\python run_with_env.py --batch 1/6
```

飞书应收到通知。若一切正常，定时任务将在次日 08:00 自动执行。

## 8. 应急模式

如果需要跳过 Amazon 静态抓取，仅用 MCP 数据：

```bash
.venv\Scripts\python run_with_env.py --mcp-only
```

## 9. 通知说明

每人只收一条飞书汇总消息，按变化类型分组：

```
⚠️ 每日检查完成 | 2026-01-01
共检查 300/300 个 ASIN

📊 销售排名变化（5）
 · 焊锡接线端子
   - 120焊锡: Spade Terminals: #4；Electrical Equipment: 1199→1279↓

💰 价格变化（2）
 · B0XXX（品名）— $19.99→$22.99

🔀 变体变化（1）
 · 热缩接线端子
   - 520热缩: 47→48（子体上架）
```

## 10. 常见问题

| 问题 | 解决 |
|------|------|
| 飞书收不到通知 | 检查 .env 中 FEISHU_CHAT_ID 是否正确 |
| 定时任务没跑 | 确认 Windows 已自动登录；在任务计划程序中查看"上次运行结果" |
| 总是限流中止 | 检查是否关闭了 VPN/代理；确认 SellerSprite MCP 密钥有效 |
| 价格显示 CNY | 确保 MCP 密钥有效（MCP 提供 USD 价格） |
| 销售排名显示"具体数据变动" | 首次采集后次日恢复正常 |
