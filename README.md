# OONE-JobScout · BOSS直聘智能接管平台

> **核心原则**：**不新建浏览器、不伪造指纹、不绕过验证码**。
> 脚本基于 **Chrome DevTools Protocol (CDP)** 远程调试协议，安全接管用户已登录的真实 Chromium 内核浏览器（支持 Google Chrome、Microsoft Edge、Brave、Arc、Chromium 等）。
>
> 平台全面支持 **Windows 10/11** 与 **macOS (Intel / Apple Silicon M系列)** 跨平台协同。

---

---

## 🚀 极速开箱（推荐：一键双击即开即用）

本项目已全面内置**智能自愈与一键就绪启动器**，无论在新机器还是现有设备中，均无需繁琐手动配置命令行：

- **Windows 10/11**: 直接双击运行项目根目录下的 **`start.bat`**
- **macOS / Linux**: 终端运行 **`chmod +x start.sh && ./start.sh`**
- **使用 uv 全局免克隆即开即用（现代极速方式）**:
  ```bash
  # 一行命令拉起 Web 控制台：
  uvx --from git+https://github.com/MCxxxDT/OONE-JobScout boss-apply

  # 或一键安装为系统全局命令：
  uv tool install git+https://github.com/MCxxxDT/OONE-JobScout
  boss-apply
  ```

启动器将全自动完成：
1. 自动检测系统 Python/uv 并秒级搭建专属 `.venv` 虚拟环境与依赖；
2. 自动生成本地私有安全配置文件 `config.local.json`（绝不上云）；
3. 自动自检并拉起独立调试 Chrome 浏览器（端口 9335，独立 Profile 隔离）；
4. 自动在默认浏览器中弹出现代 Web 审批工作台 (`http://127.0.0.1:8788/?token=boss-apply`)，并在顶栏呈现系统就绪体检看板。

---

## 🗑️ 一键卸载（永久删除）

- **Windows**：双击根目录 `uninstall.bat`。
- **macOS**：双击根目录 `uninstall.command`；如系统未允许执行，在终端运行 `bash uninstall.sh`。
- **Linux**：终端运行 `bash uninstall.sh`。

入口会列出安装目录、同一 Git 仓库的全部 Worktree（含外部目录）及要停止的专属进程；输入 `DELETE` 后永久删除。项目目录内的源码、`.git`、未提交修改、`.venv`、配置、API 密钥、简历、日志、台账和专属浏览器登录数据均会删除，请先把需要保留的内容移出这些目录。

```bash
# 只读预览，不停止进程、不删除文件
python3 -B boss_apply/uninstall.py --dry-run

# 如果旧版本用过 ~/chrome-cdp-profile，确认该目录只属于本项目后纳入删除
python3 -B boss_apply/uninstall.py --include-legacy-profile

# 同时处理 uv 全局工具和项目包缓存（先预览）
python3 -B boss_apply/uninstall.py --include-legacy-profile --uv-tools --dry-run
python3 -B boss_apply/uninstall.py --include-legacy-profile --uv-tools

# 仅卸载 uv 工具，不删除源码副本（新版本提供此命令）
oone-uninstall --uv-only --include-legacy-profile
```

Windows 命令行可用 `py -3 -B` 替换 `python3 -B`。自动化调用只有在审阅预览后才添加 `--yes`。旧版本全局安装若没有 `oone-uninstall`，可从本仓库运行 `boss_apply/uninstall.py --uv-only`；不要通过 uvx 下载卸载器，以免再次生成安装缓存。

新版调试浏览器使用 `state/browser-profile`，工作台窗口使用 `state/console-profile`，首次启动需要重新扫码；旧目录不会自动迁移或删除。卸载器只停止已确认归属的进程，不按 Chrome 名称或端口杀进程，不跟随 `.venv` 符号链接或目录联接删除共享环境。

**范围与结果**：该功能清除当前用户、预览清单内可识别的本项目安装和专属数据，并核验目录与进程。它不提供“整台电脑任何地方零痕迹”或磁盘安全擦除保证。旧浏览器目录、外部数据链接、uv 缓存等未覆盖项会明确显示；系统日志、备份、其他源码副本、手动添加的 MCP 配置、共享 Python/uv/浏览器/Tailscale，以及第三方账户中的记录需另行处理。详细说明与返回码见 [卸载说明](docs/UNINSTALL.md)。

---

## 🛠️ 开发者手动安装（可选 / 进阶）

- **Windows (PowerShell)**:
  ```powershell
  uv venv
  .\.venv\Scripts\Activate.ps1
  uv pip install -r requirements.txt
  ```

- **macOS / Linux (Terminal)**:
  ```bash
  uv venv
  source .venv/bin/activate
  uv pip install -r requirements.txt
  ```

---

#### 🐍 方式 B：使用标准 PIP（开箱即用，无需第三方管理工具）

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

## 🔀 Git Worktree 团队多人并行开发与分支调优（零成本切分支）

在多人协同、多特性并行实验或 A/B 模型比对阶段，频繁切换分支容易污染依赖、中断本地调试。
本项目内置专属 **Git Worktree 协同管理器**，支持秒级创建独立分支工作区，**自动复用主环境 `.venv`（无需重新装包），自动同步私有配置与 Key**：

```bash
# 1. 一键创建并初始化新分支工作区（Windows 直接双击 wt.bat 或在终端运行）
wt.bat add feat/ai-prompt-v2
# macOS / Linux:
python3 scripts/worktree.py add feat/ai-prompt-v2

# 2. 列出当前所有并行工作区
wt.bat list

# 3. 独立工作区即开即用（工作区内拥有完全隔离的源码树）
cd .worktrees/feat-ai-prompt-v2
start.bat     # Windows 双击或运行，自动共享主环境极速启动

# 4. 调试/合并完成后，一键安全注销并清理分支与环境
wt.bat remove feat/ai-prompt-v2 -f -d
```

> 💡 **端口隔离提示**：若需要同时运行多个分支的 Web 控制台，可使用独立端口：
> `python -m boss_apply.web_server --port 8789`

---

## 🔌 接入 IDE / MCP 智能体客户端（可选）

本项目原生支持 **FastMCP** 标准协议，可作为 AI Agent 专属插件无缝接入 **Trae**、**Cursor**、**WorkBuddy** 或 **Claude Code**：

#### 方式 A：免克隆、免配置环境直接调用（推荐，基于 uvx 极速运行）
无需下载仓库，只需在 IDE MCP 配置（如 `~/.cursor/mcp.json` 或 `Claude Desktop` 配置）中添加：

```json
{
  "mcpServers": {
    "boss-apply": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/MCxxxDT/OONE-JobScout", "boss-mcp"]
    }
  }
}
```

#### 方式 B：本地源码或已安装环境调用
```json
{
  "mcpServers": {
    "boss-apply": {
      "command": "boss-mcp"
    }
  }
}
```
*(注：如果使用虚拟环境，将 `"command"` 填入虚拟环境生成的可执行文件，如 Windows 下 `D:/.../.venv/Scripts/boss-mcp.exe`)*

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
