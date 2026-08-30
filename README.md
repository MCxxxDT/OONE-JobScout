# boss-apply · BOSS直聘自动投递（接管真实 Chrome 版）

> 核心原则：**不新建浏览器、不伪造指纹、不绕过验证码**。脚本只接管你已经登录的真实 Chrome。
> 完整方案背景见 `../自动投递方案.html`。

## 项目结构

```
boss-apply/
├── launch_debug_chrome.bat   # 启动调试Chrome（独立profile，不影响日常浏览器）
├── config.json               # 城市/名额/关键词/词表/公司池/护栏参数/文案
├── requirements.txt
├── boss_apply/
│   ├── server.py             # FastMCP Server（挂进 Trae/WorkBuddy 用）
│   ├── flows.py              # 高层流程：scan / plan / execute / probe
│   ├── browser.py            # CDP连接 + 风控信号检测
│   ├── scraper.py            # 岗位列表/详情抓取（改版优先改这里）
│   ├── scorer.py             # 打分：关键词加权 + 公司池加成
│   ├── greeter.py            # 打招呼执行（profile=test 用中性文案）
│   ├── guard.py              # 护栏：限速/限次/城市配额/风控熔断（状态持久化）
│   ├── ledger.py             # 投递台账（jsonl，可复盘审计）
│   └── config.py
├── scripts/
│   ├── t1_check_login.py     # 验收1：登录态连通
│   ├── t2_readonly_scan.py   # 验收2：只读扫描（0沟通）
│   ├── t3_single_greet.py    # 验收3：单次沟通链路
│   └── probe_readonly_rate.py# 只读频率探针（仅测试账号）
└── state/                    # 运行产物：guard_state.json / ledger.jsonl / plan.json / 截图
```

## 快速开始（5 步）

```bat
:: 1. 安装依赖（已完成可跳过；不下载Chromium，用真实Chrome）
cd /d D:\LENOVO\Desktop\简历\boss-apply
C:\Users\LENOVO\.workbuddy\binaries\python\envs\default\Scripts\python.exe -m pip install -r requirements.txt

:: 2. 启动调试Chrome（弹出的窗口里扫码登录BOSS，只需一次，登录态会保存在独立profile）
launch_debug_chrome.bat

:: 3. T1 连通性验收
C:\Users\LENOVO\.workbuddy\binaries\python\envs\default\Scripts\python.exe scripts\t1_check_login.py

:: 4. T2 只读扫描（先看打分准不准，不投递）
C:\Users\LENOVO\.workbuddy\binaries\python\envs\default\Scripts\python.exe scripts\t2_readonly_scan.py 杭州 1

:: 5. T3 单次沟通链路（测试账号）→ 人工检查消息列表 → 生成计划并小批量执行
C:\Users\LENOVO\.workbuddy\binaries\python\envs\default\Scripts\python.exe scripts\t3_single_greet.py
```

## 挂进 MCP 客户端（可选，强烈推荐）

WorkBuddy：把下面配置加进 `~/.workbuddy/mcp.json`（连接器页面信任后生效）：

```json
{
  "mcpServers": {
    "boss-apply": {
      "command": "C:\\Users\\LENOVO\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe",
      "args": ["D:\\LENOVO\\Desktop\\简历\\boss-apply\\boss_apply\\server.py"]
    }
  }
}
```

之后可以直接对话式操作：**「扫一下杭州，把 8 分以上的列出来」→「我看行，执行前 10 条」**。
Trae 同理，把 `server.py` 注册为 stdio MCP server 即可。

## 城市名额（2026-08-30 版，可改 config.json）

| 档位 | 城市 | 每日名额 |
|------|------|---------|
| 主投 | 杭州（DeepSeek/宇树/群核…）、上海（MiniMax/阶跃/语核…） | 25+25 |
| 主投 | 深圳（千寻/越疆/拓竹…）、广州（小马/文远…） | 15+10 |
| 次投 | 成都、武汉、长沙 | 10+8+7 |

**城市码验证**：在调试 Chrome 里打开 BOSS 职位搜索页，选中城市后从 URL `city=` 参数读真实值，与 config.json 比对。平台可能调整。

## 测试账号的「暴力测试」怎么打才有效

**可以暴力：只读维度。** `probe_readonly_rate.py` 用阶梯频率（3s→0.5s）轮换关键词搜索，找到滑块出现的档位。这是伤害最小、结论最硬的实验——搜索不打扰任何招聘方。

**不要暴力：沟通维度。** 原因是技术性的，不是道德性的：
1. **画像不可迁移**：测试号是新设备新账号无历史，风控阈值和正式号完全不同。用测试号测出「150条/天没事」，对正式号毫无参考价值——你测的不是同一个风控模型。
2. **触发即有记录**：风控触发的代价是账号+实名手机号维度的信誉损伤，测试号被封的数据点也进了平台的风控样本库，与你正式号同 IP/同设备的话会关联。
3. **正确姿势是探针递增**：1 条 → 观察 → 10 条 → 观察 → 30 条 → 观察。每步之间看三样：有无安全验证、有无「操作过于频繁」提示、有无页面回退。任何一步出现 → 当天停，第二天再从上一步的 1/2 量继续。

**隐私保护**：`config.json` 的 `profile: "test"` 时，所有打招呼文案是中性模板（不含姓名/学校/项目）。切正式账号时改成 `"real"` 才会启用三段个人文案。台账只记岗位与分数，不落个人信息。

## 红线（写在代码护栏里的）

- 遇到安全验证页/滑块 → **全流程立即熔断**（`guard.pause`），人工确认后 `resume_guard()` 才能继续
- 每日沟通硬上限 100（平台原生约 100-150，取保守侧）
- 单次沟通间隔 4-10 秒**随机**（匀速=机器特征）
- BOSS 活跃度 >14 天直接跳过（投了也是石沉大海）
- 页面被回退/掉线 → 当天停投（get_jobs 作者血泪原话）

## 已知维护点

1. **选择器失效**：BOSS 前端改版频繁。列表解析在 `scraper.parse_card`，沟通弹窗在 `greeter.py` 的 `CHAT_SELECTORS/SEND_SELECTORS`。T3 失败时优先核对这两处。
2. **城市码**：见上，运行时验证。
3. **打分阈值**：`min_score: 8` 是初值。跑两天台账后按误杀/漏杀调整 `score_words`。

## 当前进度

- [x] 依赖安装（playwright + fastmcp，不下载 Chromium）
- [x] 项目代码 + FastMCP Server + 测试脚本
- [ ] T1：启动调试 Chrome，扫码登录（需要你操作）
- [ ] T2：只读扫描验收
- [ ] T3：单次沟通链路验收
- [ ] 切 `profile: real`，正式放量（20→50→100 逐步）
