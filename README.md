# OONE-JobScout · BOSS直聘智能接管平台

> **核心原则**：**不新建浏览器、不伪造指纹、不绕过验证码**。
> 脚本基于 **Chrome DevTools Protocol (CDP)** 远程调试协议，安全接管用户已登录的真实 Chromium 内核浏览器（支持 Google Chrome、Microsoft Edge、Brave、Arc、Chromium 等）。
>
> 平台全面支持 **Windows 10/11** 与 **macOS (Intel / Apple Silicon M系列)** 跨平台协同。

---

## 🛠️ 快速开始（跨平台开箱 5 步）

### 1. 克隆代码仓库
```bash
git clone https://github.com/MCxxxDT/OONE-JobScout.git
cd OONE-JobScout
```

### 2. 初始化 Python 虚拟环境并安装依赖
建议使用 **Python 3.10 ~ 3.13**。

- **Windows (PowerShell)**:
  ```powershell
  python -m venv .venv
  .\.venv\Scripts\Activate.ps1
  pip install -r requirements.txt
  ```

- **macOS / Linux (Terminal)**:
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  ```

### 3. 配置本地专属环境变量（安全隔离）
复制样例配置，生成本地私有配置文件（该文件已被 `.gitignore` 彻底忽略，**绝不上云，保护个人隐私**）：
```bash
# Windows (PowerShell) / macOS / Linux 通用
cp config.local.example.json config.local.json
```
打开 `config.local.json`，填入你个人的专属配置：
- `llm.api_key`: 你的大模型 API 密钥（兼容 DeepSeek、通义千问、OpenAI 等）
- `privacy_policy`: 设置你的微信号、联系电话及自动交换策略

### 4. 启动调试浏览器并扫码登录
启动脚本会自动探测本地已安装的 Chromium 浏览器，并以独立 Profile 启动（端口 `9335`，完全不污染你日常的浏览器数据）：

- **Windows**: 双击根目录下的 `launch_debug_chrome.bat`（或在终端运行）
- **macOS**: 终端运行：
  ```bash
  chmod +x launch_debug_chrome.sh
  ./launch_debug_chrome.sh
  ```
> **提示**：浏览器窗口打开后，直接扫码登录你的 BOSS直聘 账号。登录态会自动沉淀在本地，只需扫码一次。

### 5. 验证环境并启动 Web 审批工作台

- **第一步：无损全量回归测试（不消耗投递名额）**
  ```bash
  python scripts/t0_dryrun.py
  ```
  *终端输出 `结果: 全部通过` 说明本地管线与环境完全就绪。*

- **第二步：启动 Web 控制台**
  ```bash
  python -m boss_apply.web_server
  ```
  浏览器访问：`http://127.0.0.1:8788/?token=boss-apply` 进入工作台，享受可视化人机协同审批、多轮推演演练场、在线微简历拉取与城市名额配置。

---

## 🔌 接入 IDE / MCP 智能体客户端（可选）

本项目原生支持 **FastMCP** 标准协议，可接入 **Trae**、**Cursor**、**WorkBuddy** 或 **Claude Code**：

以通用 MCP 配置文件（如 `~/.cursor/mcp.json` 或 `~/.workbuddy/mcp.json`）为例：

```json
{
  "mcpServers": {
    "boss-apply": {
      "command": "python",
      "args": ["-m", "boss_apply.server"],
      "env": {
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```
*(注：如果使用虚拟环境，将 `"command"` 填入你的虚拟环境 Python 绝对路径，如 Windows 下 `D:/.../.venv/Scripts/python.exe` 或 Mac 下 `/Users/.../.venv/bin/python`)*

配置完成后，即可在 Agent 中进行自然语言调度：
- *“帮我扫描杭州的 AI产品经理 岗位，筛选出 8 分以上的优质机会。”*
- *“查看今日投递台账与待审批事项。”*

---

## 📁 项目结构索引

```text
boss-apply/
├── launch_debug_chrome.bat   # Windows 一键拉起调试浏览器（支持 Chrome/Edge/Brave）
├── launch_debug_chrome.sh    # macOS 一键拉起调试浏览器（支持 Chrome/Edge/Brave/Arc）
├── config.json               # 公共基准配置（城市列表、打分规则、默认时段）
├── config.local.example.json # 本地私有配置模板（复制为 config.local.json 使用）
├── requirements.txt          # Python 依赖清单
├── boss_apply/
│   ├── web_server.py         # 现代 Web 人机协同审批台与 REST API
│   ├── server.py             # FastMCP Server 服务端
│   ├── flows.py              # 全局核心业务流：智能扫描、择优计划、投递执行、日间守护
│   ├── browser.py            # CDP 协议连接、标签页管理与风控检测
│   ├── scraper.py            # BOSS 直聘 Vue3 架构岗位列表与详情页解析
│   ├── scorer.py             # 智能打分引擎：意向匹配、时令感知、薪资与公司池加权
│   ├── greeter.py            # 沟通招呼引擎与安全发送校验
│   ├── guard.py              # 运行护栏：限频限速、风控熔断、单城与全天配额控制
│   ├── ledger.py             # 结构化投递审计台账（JSONL 格式）
│   ├── ai_reply.py           # 大模型对话决策与安全脱敏审核
│   ├── qr_login.py           # 登录态探测与微简历双向同步
│   └── profile_store.py      # 本地候选人画像提取与存储
├── scripts/
│   ├── t0_dryrun.py          # 跨平台全量仿真回归测试套件（35 项核心断言）
│   ├── t1_check_login.py     # 快速连通性检测脚本
│   ├── t2_readonly_scan.py   # 只读岗位检索测试
│   └── t3_single_greet.py    # 单条实弹沟通探针
└── state/                    # 运行时数据与台账（已 gitignore，严禁入库）
```

---

## 🛡️ 核心风控护栏规范（代码级保障）

1. **绝对真机仿真**：基于真实 CDP 会话通信，不注入高危 Webdriver 变量，彻底规避平台指纹识别与反爬特征检测。
2. **滑块/风控一票熔断**：一旦探测到滑块验证码或安全验证页，系统立即切入熔断阻断状态，杜绝暴力撞库造成封号。
3. **频率与行为拟人化**：单次沟通随机休眠 4~10 秒，严格避免固定节奏机器特征；单日打招呼总数硬上限 100 次（远低于平台封禁线）。
4. **时效过滤机制**：HR 活跃度超过 14 天未上线自动弃投，确保投递有效性。
5. **本地隐私严格隔离**：个人真实简历、敏感联系方式、大模型 Key 仅保存在本地 `*.local.json`，绝不上云。
