# boss-apply 现状与竞品对比评估（COMPARISON）

> 版本：2026-08-31 · 评估性质：**先评估后开发**（本轮仅文档，插件化与多账号代码在下一轮）
> 数据核实：竞品星标/分支/特性经浏览器直接打开 GitHub 页面确认（2026-08-31 20:26 UTC+8），并与社区评测交叉验证。

---

## 1. 现状盘点（本项目实测）

### 1.1 一句话
健康度：**工具链已完成 T0-T3 + 全量扫描 + 打招呼/回复 + 台账护栏，处于"等待放量"状态**；缺的是"装好即用"的插件体验与多账号扩量。

### 1.2 已实现
| 层 | 内容 | 状态 |
|---|---|---|
| 抓取 | [rawcdp.py](file:///d:/LENOVO/Desktop/简历/boss-apply/boss_apply/rawcdp.py) 裸CDP直连真实Chrome（不 enable 任何域规避检测）；页内 `/wapi/zpgeek/search/joblist.json` 拿明文薪资（绕字体反爬）；`passport/zp/verify` 安全页识别 | ✅ 实测零风控 |
| 登录/风控 | `flows.login_state` 复用登录态 + 截图留痕；护栏遇 verify 自动熔断 | ✅ |
| 扫描/打分 | 7城×9关键词只读扫描；规则打分（关键词加权 + title_kill 岗位门槛 + 届别27届 + BOSS活跃度 + 薪资区间 + 黑名单 + 公司池） | ✅ 990条/706去重/待投345 |
| 沟通 | 打招呼（建连+默认招呼+自定义跟发，适配 BOSS 聊天移至 /web/geek/chat）；`send_message_via_chat` 消息中心回复HR；受信任点击；输入框清零验证 | ✅ 实弹通过 |
| 隐私 | HR 索要联系方式 → `needs_human` 转人工；代发文案含联系方式 → `blocked_privacy` 拒绝（用户红线） | ✅ |
| 工程化 | 护栏（每日100/城市配额/熔断/日切）、台账 ledger.jsonl、T0-T3+探针四级验收、7城高分报告、git 三板斧 | ✅ |
| 插件 | `server.py` FastMCP Server（stdio，7 工具）已写入 mcp.json | ⏳ **待连接器页"信任"激活** |

### 1.3 待实现（roadmap，按优先级）
1. 插件激活体验化 + Agent-first 能力发现（工具清单 / JSON 信封规范 / 使用引导文档）。
2. 沟通主题可定制（R4，配置化不硬编码）+ 打招呼/回复文案个性化（可选 LLM 生成）。
3. 通知渠道（Telegram / Discord / 企微）。
4. 定时投递（cron 编排）。
5. **矩阵化多账号投递（多账号扩量）** —— 下一轮核心。
6. 调优项：title_kill 补"工程师"、Base 城市 mismatch 规则、search_daily_limit 600→40（昨晚 43 次搜索+沟通即触发验证的实测校准）。

### 1.4 预期效果（下一轮开发验收标准）
- **插件化**：TRAE / WorkBuddy 一次安装启用 → 对话直呼"扫杭州→列出8分以上→批准执行→跟进HR回复"，全程护栏+台账+隐私红线约束。
- **多账号扩量**：N 个独立真实 BOSS 账号（独立 Chrome profile/端口/登录态），每账号独立配额、错峰节奏，单账号风控只熔断该账号；台账/报告按账号隔离聚合；日均触达≈单账号×N。
- 行为不变：先扫描后沟通、先汇报后执行、profile=real 切换需用户明确指令。

---

## 2. 竞品对比（联网 + 浏览器核实）

| 维度 | **本项目 boss-apply** | boss-agent-cli ★1637/141fork | eatmoreduck scraper ★1266/158fork | loks666 get_jobs ★8210/971fork | jobclaw ★224/28fork | autopilot-jobhunt ★198 |
|---|---|---|---|---|---|---|
| 抓取分层 | 裸CDP真实Chrome+页内API明文薪资 | 本地浏览器/CDP | 同左（同源架构） | Java+Selenium+webdriver | 浏览器自动化+OpenClaw | 官网爬取(130+ careers) |
| 反爬策略 | 不enable域、实测零风控、verify识别+熔断 | 低风险默认+结构化错误 | 专用隔离profile | 有封号警示"掉线即停" | — | 只读草稿 |
| 打分筛选 | 规则打分+届别/活跃/薪资/黑名单/公司池 | LLM/规则+福利AND+本地匹配分 | 基础筛选 | AI匹配+打招呼生成 | LLM匹配 | LLM逐岗打分 |
| 打招呼/回复 | 打招呼+消息中心回复HR+隐私拦截 | greet/apply/chat/reply | 仅抓取 | AI个性化+图片简历+定时 | AI生成+批量投 | 仅草稿不代投 |
| 护栏/台账/审计 | **护栏+熔断+台账+隐私红线（独有）** | 结构化错误+recovery | 增量去重 | 150上限提醒 | — | 隐私友好 |
| Agent 集成 | FastMCP server(7工具,未激活)+脚本 | **schema/JSON信封/MCP/SDK（Agent-first标杆）** | 可作 Hermes Skill | 无 | OpenClaw skill | MCP官方注册+Skill |
| 通知/定时 | 无 | digest/跟进 | 无 | 企微+定时 | Telegram/Discord | Telegram |
| 多账号矩阵 | **规划中（多账号扩量）** | 未明确 | 未明确 | 全局cookie(单号) | 未明确 | 未明确 |

### 2.1 逐项差距分析
1. **Agent-first 设计落后（最大差距）**：boss-agent-cli 提供 `boss schema` 能力自发现 + `{ok,data,error,hints}` JSON 信封 + 明确 MCP/Python SDK 接入面，是当前对 agent 最友好的形态。我们脚本/工具已有 JSON 化输出习惯，server.py 有工具描述，但缺"能力清单 + 信封规范 + 使用引导"，agent 接入靠人肉了解 → **插件化交付物首先补齐这三样**。
2. **LLM 赋能可选**：竞品普遍用 LLM 做匹配理由/打招呼生成（loks666、jobclaw）；我们规则打分稳定可控但个性化弱 → 维持规则为主，AI 生成仅作候选，不阻塞。
3. **通知/定时缺失**：loks666（企微+定时）、jobclaw（Telegram/Discord）、autopilot（Telegram）均有 → roadmap 后续轮次，本轮不做。
4. **矩阵化多账号 = 差异化机会**：核实全部主流开源方案（含 ★8210 的 get_jobs）均无完整多账号矩阵（get_jobs 的"全局 cookie"是单号 cookie 复用）→ 自行实现，详见第 4 节。
5. **抓取架构获印证**：eatmoreduck ★1266 与我们完全同源（CDP + joblist.json 明文薪资）→ 确认**不引入外部项目**，避免反爬倒退；其踩坑经验（城市码曾错配，成都 101270100 被误配成长沙 101250100，后加回归测试）→ 我们的 config 城市码需定期校准流程（见第 6 节）。
6. **反爬纪律被社区反复印证**：get_jobs 作者明言"Boss 掉线当天停投、明天再投，否则可能封号"；我们 8-31 实测 43 次搜索+沟通叠加即触发验证 → 确认"沟通维度叠加搜索量加速风控"的判断与"search_daily_limit 600→40"校准方向正确。

---

## 3. 插件化可行性结论

**结论：可行，成本低，且不引入外部竞品。**

- 通路已存在：`server.py` 是 FastMCP Server（stdio），MCP 配置已写入 `mcp.json`（TRAE/WorkBuddy 同一套机制），只差用户在连接器页点"信任"。
- 需补齐的交付物（下一轮）：
  1. 仓库根 `SKILL.md`：name/description（何时调用）+ 能力清单 + 调用纪律（先扫后沟通、汇报后执行、隐私红线、profile=real 需用户指令）。
  2. `scripts/install_mcp.ps1`：写 mcp.json、校验 venv、打印激活指引。
  3. 能力清单/信封规范：`server.py` 工具列表 + 输出 JSON 规范（对齐 boss-agent-cli 的 schema + JSON 信封思路，但按我们的台账/护栏语义实现）。
- 不引入外部竞品理由：核心架构同源已被 eatmoreduck(eatmoreduck) ★1266 印证；引入 playwright 系项目 = 反爬倒退（本项目根因级教训）；外部项目普遍缺护栏/台账/隐私红线，与我们红线不符。

---

## 4. 多账号扩量方案评估

**结论：可行，属差异化能力；核心风险是同 IP 多账号关联风控，必须内置缓解策略。**

### 4.1 目标形态
N 个独立真实 BOSS 账号，各自独立登录态/配额/节奏，错峰投递以扩大触达量。

### 4.2 设计要点（下一轮实现）
- `config.json` 增 `accounts[]`：`name` / `chrome_profile`（独立 `--user-data-dir`）/ `cdp_port`（独立端口，遵守 9333→9335 迁移纪律，9222 弃用，D 盘根目录不可写）/ `greeting_profile`(test|real) / 城市配额倍率或独立配额。
- 护栏 per-account 分账（guard_state 按 account 隔离：日切/城市配额/每日上限/熔断）；现有单账号调用向后兼容 = accounts[0]。
- 台账 ledger 记录追加 `account` 字段；报告/查询可按账号过滤聚合。
- 启动器：`launch_chrome_for_account.ps1`，每账号独立 profile + 端口。
- 调度：per-account 错峰窗口（起始时间偏移），避免同一 IP 上多账号同时高频动作。

### 4.3 同 IP 关联风控风险与缓解
- 风险：多账号同 IP/同设备指纹可能被平台关联，一个号触发风控牵连其他号；**红线内绝不绕过验证**。
- 缓解（写入实现与文档）：
  1. 独立 Chrome profile（独立登录态/指纹隔离）；
  2. 错峰调度（账号间操作窗口不复用同一时刻）；
  3. 单账号熔断隔离：该账号 verify/风控 → 仅 pause 该账号，其余继续，并告警人工处理；
  4. 每账号小步爬坡（20→50→100 观察）而非一步登顶；
  5. 不同账号建议不同岗位方向/文案（顺带满足 R4 主题定制），降低行为指纹趋同。

---

## 5. 沟通主题可定制（R4，用户新需求）

- 原则：**打招呼/回复文案不硬编码**，由配置文件驱动（岗位方向人设、文案语气、模板变量、联系方式纪律）。
- 现状：config.json 已按 profile 分 test/real 文案；下一步把"主题"概念显式化（如 `greeting.theme`：ai-pm / 商业化 / 自定义），同一账号可切换。
- 测试阶段：按用户本人（AI/Agent 产品经理方向）需求配置即可，无需当模板需求全面铺开。

---

## 6. 维护备注（沉淀入 README）
- **城市码校准**：BOSS 城市码可能调整（eatmoreduck 曾踩坑：成都 101270100 曾与长沙 101250100 错配）。校准方式：调试 Chrome 打开 BOSS 职位搜索页，从 URL `city=` 读真实值，与 config.json 比对；重大校准附回归断言。
- **风控节奏**：沟通维度叠加搜索量会加速风控（8-31 实测 43 次搜索+10 沟通即触发验证）→ 搜索与打招呼建议分时段/分天，search_daily_limit 目标 30-40/日。
- **对比文档**：本文档随版本更新，新增竞品/结论时在"来源清单"补链接与核实日期。

## 8. 开源采纳记录（2026-08-31 晚，方案 B+C 执行结果）

### 8.1 boss-agent-cli 试水（方案 C）✅
- 安装：`uv tool install boss-agent-cli` → v1.19.1，命令 `boss` / `boss-mcp`（路径 `简历\.tools\uv\bin`，因沙箱限制未用默认 `D:\LENOVO\tools\uv`）。
- 冒烟通过：`boss platforms` 返回 JSON 信封 `{ok,data,error,hints}`，zhipin 求职者+招聘者侧均 `available`。登录/沟通等真实链路待后续（登录需 patchright chromium 内核，按需再装）。
- 结论：其 schema/JSON 信封/能力发现为我们的插件面提供了可照搬的设计蓝本（下一轮插件化实现参考）。

### 8.2 eatmoreduck 采纳进 rawcdp（方案 B）✅ commit 9267f31
- **风控词表**：`code∈{31,37}` 或 message 命中（环境存在异常/访问频繁/操作太频繁/安全校验/滑块/验证）→ 判 restricted → raise RiskControl → 护栏熔断。全链路生效（t0 新增 4 用例）。
- **被动捕获框架**：`Network.enable` 旁听页面自身 `/wapi/zpgeek/search/joblist.json` 响应 + `Emulation.setFocusEmulationEnabled` 焦点仿真 + 事件按 sessionId 过滤。
- **重要的实测结论（诚实记录）**：本环境（Windows Chrome 151 + 浏览器级扁平 WS）下 `Network.getResponseBody` 对该接口**持续返回空串**（响应体被 Chrome 修剪，与 eatmoreduck 在其环境可用不同）。→ 实现自适应金丝雀：每会话仅首次尝试（≤8s），取不到体自动 `_passive_disabled`，后续页零开销走既有 DOM+注入兜底；在响应体可用的环境（如 macOS 版 Chrome）会自动走零注入主路径。
- 收益盘点：风控词表升级（防"已风控当正常"漏判）已实时生效；被动捕获在支持环境自动启用；本项目注入式薪资回填保留为兜底（990 条扫描零风控的既有证据下风险可控）。

### 8.3 Hermes 技能接入（用户新增需求，2026-08-31 晚）
- Hermes v0.20.5（`D:\Hermes`，HERMES_HOME=D:\Hermes；skills 分类目录 `D:\Hermes\skills`）。
- 技能放置（用户指定主目录）：`D:\AGENT\skills\boss-zhipin-scraper\`（SKILL.md + scripts/ + data/city_codes.json）。
- Hermes 发现机制：config.yaml `skills.external_dirs: [ D:\AGENT\skills\boss-zhipin-scraper ]`（经验证 `get_external_skills_dirs`/`get_all_skills_dirs` 已纳入该外部根）。
- **Chrome 136+ Origin 修复（commit 2e309db）**：eatmoreduck 脚本的 websocket 连接不带 `suppress_origin`，Chrome 136+ 默认拒绝带 Origin 的调试 WS → 在 `launch_debug_chrome.bat` 增加 `--remote-allow-origins=*`。修复后原样技能即可直连本机 9335 已登录 Chrome。
- 运行环境：技能脚本需要 requests+websocket-client；专用 venv `简历\.tools\boss-scraper\venv`（沙箱不允许写入 C 盘 site-packages，故独立建在工作区）。
- 实弹验证：`boss_cdp_raw.py --check --cdp-port 9335` 三项全绿（依赖/CDP/已登录）；杭州「AI产品经理」1 页实抓 15 岗、明文薪资、BOSS 活跃，JSON 落盘。

## 7. 来源清单（核实日期 2026-08-31）
- [boss-agent-cli（★1637/141fork，浏览器核实）](https://github.com/can4hou6joeng4/boss-agent-cli) · [掘金评测](https://juejin.cn/post/7641029004167872522)
- [eatmoreduck/boss-zhipin-scraper（★1266/158fork，浏览器核实）](https://github.com/eatmoreduck/boss-zhipin-scraper) · [作者自述](https://blog.xiaohuangyu.space/p/boss-zhipin-scraper-open-source/) · [城市码 issue#5](https://github.com/eatmoreduck/boss-zhipin-scraper/issues/5)
- [loks666/get_jobs（★8210/971fork，浏览器核实）](https://github.com/loks666/get_jobs) · [搭建教程](https://blog.csdn.net/weixin_63742939/article/details/158456325)
- [jobclaw（★224/28fork，浏览器核实）](https://github.com/slothsheepking/jobclaw) · [fork crrowbot/jobclaw](https://github.com/crrowbot/jobclaw)
- [autopilot-jobhunt（★198，skillsllm 快照）](https://github.com/tarunlnmiit/autopilot-jobhunt)
- [GitHub topic: zhipin / job-automation 榜单](https://github.com/topics/zhipin)（排序取数基准）
- [boss_batch_push（油猴批量投递，gitcode 速览）](https://gitcode.com/gh_mirrors/bo/boss_batch_push) · [autoconnect-boss（Chrome 扩展，★极简）](https://github.com/Frankli9986/autoconnect-boss)
- 本项目现状数据：`boss-apply/state/ledger.jsonl`、`state/scan_report_2026-08-31_full.md`、HANDOVER.md（2026-08-31 实时台账）。