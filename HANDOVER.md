# boss-apply 任务交接文档（HANDOVER）

> 交接时间：2026-08-31 01:30 · 移交方：WorkBuddy（GLM）· 接收方：新 Agent
> 本文档自包含。接手后先通读本文档，再读 `.workbuddy/memory/2026-08-30.md`（完整排障过程）与 `.workbuddy/memory/MEMORY.md`（长期备忘）。

---

## 1. 一句话项目概述

面向求职 AI/Agent 产品经理实习的 **BOSS直聘自动投递工具链**：
裸 CDP 接管已登录的真实 Chrome → 搜索岗位 → 打分筛选 → 自动打招呼 → 台账审计 + 护栏熔断。
**T0/T1/T2/T3 四级验收 + 频率探针已全部通过，工具链可用，等待放量。**

## 2. 用户背景与画像约定

- 求职者画像：高校在读（预计 2027 拿双证）。沟通口径勿写"已毕业"。
- 方向：AI / Agent 产品经理 / 商业化产品经理 实习。差异化卖点："能写代码的商业化 PM"。
- 核心能力：Chrome CDP 自动化、FastMCP 封装、业务落地。
- 地域策略：优先江浙沪核心城市，配额在 `config.json` 或 `config.local.json` 维护。
- 模式约定：默认支持测试模式（中性文案）与实操模式，真实打招呼在本地 `config.local.json` 注入。

## 3. 当前状态（截至交接）

| 项 | 状态 |
|---|---|
| T0 离线干跑 | ✅ 15/15（逮到并修了 sys.path、import 两个真 bug） |
| T1 连通验收 | ✅ CDP 连 9333 → 登录检测 → 风控信号无 |
| T2 只读扫描 | ✅ 杭州×"AI产品经理" 1页=15岗、详情 15/15、活跃度全拿到；高分：淘宝闪购25.0、阿里国际23.0、整数智能20.0、不息20.0(claude code命中)、Babycare17.0；同花顺-3.0 被正确拒绝 |
| T3 打招呼 | ✅ 整数智能 + 不息 两家完整走通（连接→自动默认招呼→自定义跟发），`executed:1 ok:true` |
| 频率探针 | ✅ 24 请求阶梯 3s→0.5s **全程零风控**（只读维度余量 > 2请求/秒；生产仍保持 4-10s 随机） |
| 护栏 | 日切 2026-08-31，greet 1/100，杭州 1/25，无熔断（state/guard_state.json） |
| 台账 | state/ledger.jsonl 全量审计记录（含 note 补记条目，status=note 不计配额） |
| MCP | server.py 已写进 `C:\Users\LENOVO\.workbuddy\mcp.json`（stdio，venv python），**待用户在 WorkBuddy 连接器管理页对 boss-apply 点"信任"激活** |

## 4. 文件地图

```
D:\LENOVO\Desktop\简历\boss-apply\
├── launch_debug_chrome.bat      # 调试Chrome启动器（9335端口+专用profile）
├── .git / .gitignore            # git仓库已建（见第10节三板斧）；state/guard_state.json与*.png不入库
├── config.json                  # 全部配置：城市码/关键词/打分词表/公司池/文案/护栏参数
├── boss_apply\
│   ├── rawcdp.py                # ★核心：裸CDP层（websocket直连，不enable任何CDP域）
│   ├── server.py                # FastMCP Server（7工具，入口 --http 或 stdio）
│   ├── flows.py                 # scan_city / execute_jobs / probe_readonly（均已裸CDP化）
│   ├── greeter.py               # send_greeting_raw（裸CDP打招呼）；旧playwright版保留仅作参考
│   ├── scorer.py                # 打分（boss_active: -1/None=未知放行）
│   ├── guard.py                 # 护栏：配额/城市配额/熔断/日切（独立guard_state.json）
│   ├── ledger.py                # 台账（append-only JSONL）
│   ├── browser.py               # 旧playwright层（RiskControl 异常类定义在此，rawcdp 复用）
│   └── scraper.py
├── scripts\                     # 诊断/测试脚本沉淀（wait_login.py v2纯被动值守、t0_dryrun、diag_*系列）
└── state\                       # guard_state.json / ledger.jsonl / 截图
```

## 5. 运行环境（踩过坑，照做）

- **启动调试 Chrome**：跑 `launch_debug_chrome.bat`（= 9335 端口 + `--user-data-dir=C:\Users\LENOVO\chrome-cdp-profile`）。用户登录态在此 profile 的浏览器里。
- **端口迁移记录（2026-08-31）**：9333 → **9335**。原因：用户的另一个爬虫项目占用 9333，共存必冲突。同步更新 config.json cdp_endpoint / launch_debug_chrome.bat / rawcdp.py 默认参数 / diag 脚本。历史验收记录中的 9333 均为迁移前事实，不影响现状。
- **Python 解释器**（venv，playwright/fastmcp/websocket-client 已装）：
  - Git Bash 路径：`/d/work buddy/C-migrated/Users/LENOVO/.workbuddy/binaries/python/envs/default/Scripts/python.exe`
  - 注意 `C:\Users\LENOVO\.workbuddy` 是 junction，真实路径在 `D:\work buddy\C-migrated\...`，引用要走真实路径。
- **端口探测规范**：`netstat + curl -w "%{http_code}"`。勿信 `curl -s` 空输出（可能被代理劫持或静默吞 404）。
- **红线**：
  - **D 盘根目录不可写**（任何 `--user-data-dir=D:\xxx` 会静默失败回退默认 profile）。
  - **9222 端口弃用**（Chrome 136+ 默认 profile 会"占端口废接口"）。
  - **勿杀用户日常 Chrome 进程**（与调试实例并存）。
  - **`.workbuddy` 目录勿删**（工作区记忆）。

## 6. 反爬核心经验（勿走回头路）

1. **playwright `connect_over_cdp` 会被 BOSS 安全 JS 检测 → 清空 DOM**（URL 保留或跳 about:blank）。这是本项目的根因级发现：手动打开的页面、裸 CDP 开的页面完全存活，playwright 新建页必死。**所以全链路用 rawcdp.py，不要再回到 playwright。**
2. 裸 CDP 要点：浏览器级 WS（`suppress_origin=True`）+ 单标签页复用 + 一次性 `Runtime.evaluate`（`returnByValue:True` + `awaitPromise:True`）；async 必须 IIFE `(async()=>{})()`。
3. BOSS 新版 DOM：卡片 `li.job-card-box`，路由 `/web/geek/jobs`；**薪资数字不进 DOM**，需页内 fetch `/wapi/zpgeek/search/joblist.json` 按 encryptJobId 回填；详情全文 `.job-detail`；沟通按钮 `.btn-startchat`；聊天输入 `div.chat-input[contenteditable]`，发送 `.btn-send` 或 Enter。
4. **BOSS 沟通机制**：点"立即沟通"= 建立连接 + **自动发出账号自己设置的默认打招呼语**；工具的自定义文案是聊天窗跟发。**已提醒用户检查小号默认语**（"您好，我是27年毕业生…"已实际送达 3 次）。
5. 只读维度频率余量 > 2请求/秒（实测）；但**沟通维度永远不做频率探针**——触发风控是不可逆账号伤害。
6. 值守/监控脚本必须纯只读（wait_login v1 曾因自动导航打断用户扫码被骂"闪退"）。

## 7. 下一步任务（按优先级）

1. **放量试水**：杭州 × 3 关键词（`AI产品经理` / `Agent 产品经理` / `大模型 产品`）跑一轮 `flows.scan_city`（只读扫描+打分，不发沟通），核对结果后向用户汇报。
2. **用户确认后** → 9 关键词 × 7 城市全量扫描；高分岗（≥min_score=8）列表给用户过目。
3. **用户点头后** → `execute_jobs` 自动打招呼（test 文案）；每日 greet 上限 100，间隔 4-10s 随机，护栏自动熔断。
4. **切 `profile: "real"`**（仅用户明确指令后）：启用三段个人文案；投放节奏 20→50→100 爬坡。
5. 提醒用户：连接器管理页对 boss-apply 点"信任"激活 MCP；到 BOSS 消息列表人工确认整数智能/不息的送达状态。
6. 可选优化：server.py 的 check_login 仍走 playwright（对已存活标签页有效，够用）；probe 步进可进一步压（1s 以下）校准但收益低。

## 8. 关键命令速查

```bash
# 启动调试Chrome（用户操作或代跑）
D:\LENOVO\Desktop\简历\boss-apply\launch_debug_chrome.bat

# 探针/扫描/执行（venv python，经真实路径）
cd "D:/LENOVO/Desktop/简历/boss-apply"
PY="/d/work buddy/C-migrated/Users/LENOVO/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
$PY scripts/t1_check_login.py        # T1 连通
$PY scripts/probe_readonly_rate.py   # 频率探针
$PY scripts/t3_single_greet.py       # 单次沟通验证（有 y/N 闸）
```

## 9. 硬性行为规范

- 回复用简体中文；对用户直给结论，别客套。
- 每完成一段实质工作，追加记录到 `.workbuddy/memory/当日.md`（append-only）。
- 交付文件用 present_files 展示。
- 涉及真实沟通（打招呼）的动作，先扫描给用户看，再执行；不要自作主张群发。

## 10. 用户三板斧原则（必须逐字遵循）

**任何代码修改都要走三步：修改 → 测试 → git。**

1. **修改**：动手改代码前不需要请示，但一次改动聚焦一件事。
2. **测试**（改完立即跑，全绿才算过）：
   ```bash
   cd "D:/LENOVO/Desktop/简历/boss-apply"
   PY="/d/work buddy/C-migrated/Users/LENOVO/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
   $PY -m compileall -q boss_apply        # ① 语法/导入检查
   $PY scripts/t0_dryrun.py               # ② 离线全链路干跑（15项断言，不连浏览器）
   # ③ 与改动相关的专项验证（如改greeter就跑t3_single_greet.py，改flows扫描就跑T2式单页扫描）
   ```
3. **git**（两分支，无例外）：
   - 测试**通过** → 存档：`git add -A && git commit -m "<改动一句话>（测试：compileall+t0+<专项> 通过）"`
   - 测试**不通过** → 回滚：`git restore .`（必要时 `git restore --staged .` 先），回滚后重新排查，**禁止把失败状态留在工作区或带病 commit**。

仓库现状：已 `git init`（main 分支），基线 commit = `3236374`（T0-T3 验收+探针通过的稳定态）。commit 身份统一使用 GitHub 账号配置（`git -c user.name="MCxxxDT" -c user.email="MCxxxDT@users.noreply.github.com"`）。运行时状态（guard_state.json/截图/本地隐私配置）均在 .gitignore 里，不入库。
