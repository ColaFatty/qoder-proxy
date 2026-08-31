# QoderProxy — Qoder 额度反代助手（跨平台）

用你自己的 Qoder 账户额度，在本机开放一个 **OpenAI 兼容 API**，供 Claude Code / Cursor / 自写脚本等任何 OpenAI 客户端使用。**支持 Windows / macOS / Linux**。

> **独立反代工具**：纯通用个人工具，与任何特定平台/产品无关。
> Qoder 账户额度 = 你的 Qoder 订阅（含免费档）。

> ⚠️ **合规声明**：本工具**仅用于你自己的 Qoder 账户**，在你的本机使用。请勿用于共享账号、转售/出租额度、绕过官方付费或任何规模化分发用途。使用者须自行遵守 Qoder 的服务条款与当地法律法规。

## 三步使用

1. **装 qodercli 并登录**（只需一次，各平台通用）：
   - **Windows**：PowerShell 执行 `irm https://qoder.com/install.ps1 | iex`
   - **macOS / Linux**：终端执行 `curl -fsSL https://qoder.com/install.sh | bash`（或按 qoder.com 官方指引）
   - 运行 `qodercli` 按提示登录你的 Qoder 账户
2. **运行 QoderProxy**：
   - **有 exe / 已打包**：双击打开 `QoderProxy.exe`（无需安装任何东西）
   - **源码运行（所有平台）**：`python3 main.py`（需要 Python 3.10+，零第三方依赖）
   - 输入你的 **Qoder 用户名（邮箱）**，密码留空 → 点「开始反代」→ 自动弹浏览器登录
   - （也可以去 qoder.com/account/integrations 生成 PAT 填进去，免浏览器）
3. **把 API 地址指到客户端**：
   - 看到 ✅ 反代已启动后，把页面上的 Base URL 和 API Key 填进你的 Claude Code / Cursor：
     - Base URL: `http://127.0.0.1:8080/v1`
     - API Key: 页面上显示的那串（每个账户固定）
     - 模型: 页面里有全部模型和倍数，常用 `deepseek-v4-pro`（0.5×）、`lite`（免费）

之后每次打开：确认用户名（预填上次）→ 点「开始反代」即可。想换账户就改用户名。

## 🔧 工具能力（核心卖点）

QoderProxy 让 AI **能动手**，两条路：

**① 标准 tools 协议**（默认，任何 OpenAI 客户端通用）
- 客户端传 `tools` 清单 → qodercli 决策 → 返回 `[TOOL_CALL]` → 客户端执行工具 → 回传结果继续
- 不绑定任何平台，Claude Code / Cursor / 自写脚本都能用

**② Agent 模式**（可选勾选，AI 自己动手）
- AI 在你选的目录内**读文件 / 改代码 / 执行命令**
- 权限分级：保守 / 允许编辑 / 全部自动
- 勾选后 AI 像本地助手一样干活，不用你手动代跑

> 💡 简单说：①是「AI 让客户端工具动手」，②是「AI 自己动手」。两者都支持，都实测跑通。

## 两种跑法

- **源码跑（所有平台）**：`python3 main.py`（需要 Python 3.10+，纯标准库零第三方依赖）——首选
- **打包 exe（仅 Windows）**：`build.bat` 生成 `dist\QoderProxy.exe`，双击即用

## 功能

- ✅ OpenAI 兼容 `/v1/chat/completions`（含流式 SSE）、`/v1/models`、`/health`
- ✅ **tools 扩展协议**：客户端可传 `tools` 清单，qodercli 决策输出 `[TOOL_CALL]` JSON，由客户端执行并回传（通用工具协议，不绑定任何平台）
- ✅ **Agent 模式**（可选勾选）：AI 可在你选的目录内读文件、改代码、执行命令（subprocess 驱动，权限分级：保守 / 允许编辑 / 全部自动）
- ✅ 多账户隔离：改用户名即可，各账户登录态互不影响

## 安全说明

- 只监听本机 `127.0.0.1`，别人访问不到
- API 调用需要 API Key，本机客户端要填对了才能用
- 不保存你的 Qoder 网站密码；PAT 在 **Windows 用系统加密（DPAPI）** 保存；**macOS/Linux 明文存本地（600 权限文件）**，如在意可每次手动输入不勾选记住
- 不做开机自启，关掉程序即停止

## 常见问题

**点开始提示"未检测到 qodercli"** → 点「安装 qodercli」按钮，等它跑完即可（需要网络）。

**提示未登录 / 登录没反应** → 确认浏览器弹出的页面里登录的是自己的账号；若浏览器已登录其他账号，先退出再登录。

**端口 8080 被占用** → 关闭占用程序，或重启电脑后再开。

**换人用** → 改用户名重新开始，各账户登录态互相隔离，不会串号。

## 开发/打包（给维护者）

```powershell
python -m pip install pyinstaller   # 只需这一个，零第三方运行时依赖
build.bat                           # 生成 dist\QoderProxy.exe（或 build.ps1）
```

源码结构：

- `main.py` — 入口
- `gui.py` — tkinter 界面（账户登录 / 反代结果页 / Agent 模式配置）
- `qodercli_mgr.py` — qodercli 探测/登录/调用、多账户隔离、DPAPI 加密、配置、动态模型列表（--list-models）
- `proxy_server.py` — OpenAI 兼容反代服务（标准库 http.server）：/health、/v1/models、/v1/chat/completions（流式 SSE）+ [TOOL_CALL] 通用工具协议 + 在途去重

## 行为说明（内部实现）

- 纯对话请求（不带 tools、不勾 Agent）→ 每次请求 spawn 一个 qodercli 进程（stdin 传 prompt，无命令行长度限制）；首请求有冷启动开销（Qoder CLI 启动几秒~十几秒），后续依赖 qodercli 常驻登录态
- 客户端带 `tools` → qodercli 只做工具决策（输出 `[TOOL_CALL]` JSON），由客户端执行工具后回传结果；防刷屏：同一 send_message 连续 3 次自动熔断
- 勾选 Agent 模式 → subprocess 驱动 qodercli 完整 agent（读文件/改代码/跑命令），权限分级控住本地操作边界；流式 SSE 为「全量完成后分段输出」