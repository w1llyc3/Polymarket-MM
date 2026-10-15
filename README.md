# Polymarket NBA MM v2 — In-play BOOK_DRIVEN / Paper

主路线调整为 **Polymarket.com NBA 全场 Moneyline、赛中、盘口驱动、小额及库存受限的做市研究**。使用盘口中点作为可观察参考，不把它解释为最终胜率；研究价差、可核实返佣、不利成交和风险退出成本。原赛前 EXTERNAL_FAIR 保留。

**新主线 BOOK_DRIVEN 不需要博彩赔率 key 或两个外部来源。** 它要求有效盘口、可信比赛阶段与时钟语义、时效、资金/库存及提交边界风控；缺失时 NO_QUOTE，不静默回退至另一模式。

v2 实际改动及验收以 [INPLAY_BOOK_DRIVEN_ACCEPTANCE.md](INPLAY_BOOK_DRIVEN_ACCEPTANCE.md) 为准。下方 Lesson 2–7 记录是保留的历史基线。

## v2 入口

```powershell
# 采集范围与报价权限分离。显式指定一场真实市场，允许继续记录赛中/末段/加时。
.\.venv\Scripts\python.exe -m nba_recorder scan --scope full-game
.\.venv\Scripts\python.exe -m nba_recorder record --scope full-game --market-id 5676016 --db data/full-game.sqlite --minutes 30 --limit 1 --stale 3 --resync 2
# 上述 ID 为曾经验证的样例；运行时以 scan 当时结果为准。
.\.venv\Scripts\python.exe -m nba_recorder.inplay sports-record --db data/sports.sqlite --seconds 60
# A/B × L0/L1/L2 × 两组虚拟延迟，同资金、同一封存流；输出 JSON/CSV。
.\.venv\Scripts\python.exe -m nba_recorder.inplay demo --out data/v2-new-demo
.\.venv\Scripts\python.exe -m nba_recorder.inplay show --db data/v2-new-demo/B-L2-2ms.sqlite
.\.venv\Scripts\python.exe scripts/validate_inplay.py --db data/v2-new-demo/B-L2-2ms.sqlite --out data/v2-audit.json
```

`--out` 演示目录必须全新。A 为固定距离/库存限制基线；B 增加异常暂停、库存偏移和更早 REDUCE_ONLY。三模型的模拟 fill 均经异步 Paper OMS 更新同一 FIFO 账本；不混用旧即时撤单回放结果。默认现金初始化、零份额；演示显式用 $5 抵押现金换两资产各5份，资本贡献不变。

比赛阶段为 PRE_GAME_OBSERVE / SYNCING / ACTIVE_MM / REDUCE_ONLY / MM_CLOSED / UNKNOWN。MM_CLOSED 持久锁定，加时/重启不能自动开放。REDUCE_ONLY 按组合不平衡与活动承诺限量，禁止拆坏完整对冲或制造反向敞口。风险退出有独立距离/规模预算，仍遵守 post-only 和账户限制。

官方 sports 原始流可记录，但 `elapsed`、上游更新时间、暂停语义和 game→market 关系必须逐项核验；默认 **UNKNOWN，不开放真实赛中 Paper 报价**。不通过墙钟推算比赛剩余时间。时钟修正、冲突或失效只会更保守。读盘口构建封存流无需赔率：

```powershell
.\.venv\Scripts\python.exe -m nba_recorder.inplay freeze-book --source data/full-game.sqlite --db data/book-tape.sqlite --start-ms <UTC毫秒起点> --end-ms <UTC毫秒终点> --sports-source data/sports.sqlite
# 使用输出 tape ID；没有经核验的 sports contract 时预期 NO_QUOTE。
.\.venv\Scripts\python.exe -m nba_recorder.inplay replay --db data/book-paper.sqlite --tape-db data/book-tape.sqlite --tape <输出的tapeID> --model L2 --strategy B
```

`freeze-book --contracts` 接受经核验的显式字段/身份契约；不提供可直接用于真实比赛的假契约。旧记录缺少处理延迟证据时 backlog 为 UNKNOWN，不能授权报价。BOOK_DRIVEN 输出 reference_mid / quote_center / quote_distance；Markout 为成交后1/3/5/10/30秒的事后 signed 诊断。预计返佣、确认返佣和到账现金分层，未归属奖励保持 unallocated，不算 NBA 收益。

公开账户研究独立于报价路径：

```powershell
.\.venv\Scripts\python.exe -m nba_recorder.wallet_research --address 0x216050de6080fac3926226b2b8ce3d5544784c78 --out data/wallet-new
```

默认只采固定一小时窗口，保留 profile/proxy、完整请求参数、HTTP 状态、原始响应及摘要；分页超限拆分时间窗，同秒超限/请求预算耗尽显式 TRUNCATED。`takerOnly=false` 明确请求包含 Maker 的全成交，绝不把 BUY/SELL 当角色；无法核实则 UNKNOWN。没有完整窗口不生成金额统计。本轮两账户的一小时窗口均无符合 NBA 样本，不能称为已验证的“几美元 NBA Maker”。

v2 新代码：`nba_recorder/inplay/`（阶段、盘口参考、统一 OMS/账本、单写者、成交代理、报告、体育流及无赔率封存流）；`nba_recorder/wallet_research/`（只读证据）。仍无自动逆转策略、跟单、方向分账或真实执行接口。

**当前阶段：v2 盘口驱动 Paper，保留 Lesson 2–7；禁止真实交易。** 本项目没有真实订单提交适配器，不读取钱包私钥、不签名、不修改真实账户资金，也没有 Live 解锁配置。仅支持 **Polymarket.com**，不混用 Polymarket US 的 API、市场、账户或 SDK。

README 更新：2026-10-15（Asia/Shanghai）。v2 完整回归 **183 项通过、0 失败**，包含全部原有155项及新增28项；原 Lesson 7 的16个故障场景保留。这些证据不表示当前真实比赛阶段、盘口或赔率持续可用。

## 当前可以用来做什么

- **Data GO**：扫描 NBA 2026–27 常规赛赛前全场 Moneyline，记录真实公开盘口，查看 BBO、深度和数据新鲜度。
- **受控离线 Paper GO**：在同一封存事件流上比较 1¢ / 2¢ / 3¢ 阈值及三种模拟成交模型，测试撤单竞态、风险熔断、恢复核对，生成账本及报告。
- **无人值守整体 Paper NO-GO**：BOOK_DRIVEN 的真实比赛身份/时钟/暂停/上游新鲜度、完整比赛和长时间故障验收仍有缺口。外部赔率仅阻塞 EXTERNAL_FAIR，不再阻塞 BOOK_DRIVEN。
- **Limited Live NO-GO**：当前不具备实盘做市验收证据，也没有实盘授权。模拟 Edge、Markout、成交或 PnL 均不得解释为真实收益。

完整证据、12 项模块状态及 P0/P1/P2 清单见 [MVP_ACCEPTANCE_REPORT.md](MVP_ACCEPTANCE_REPORT.md)。截至当前代码，开幕比赛期间可运行公开采集与受控模拟研究，不能承诺自动实盘做市。

## 已实现的模块

- **Lesson 2 — Scanner / Recorder**：动态发现 NBA series，校验赛季、赛前、Moneyline、球队与主客角色；输出市场/资产 ID、开赛时间、tick、最小数量和状态。REST + WebSocket、重连/重同步、原始事件/标准化盘口、SQLite 去重及只读 CLI。
- **Lesson 3 — Odds / Fair Value**：The Odds API 与本地 JSON Provider，American/Decimal Odds、Proportional De-vig、严格事件/规则匹配、来源时效与冲突检查、Consensus Fair Value、Edge 及四期限 Markout。缺数据输出 NO_QUOTE。
- **Lesson 4 — Quote Engine**：TWO_SIDED / BUY_ONLY / SELL_ONLY / NO_QUOTE；价格、tick、maker-only、最小数量、资金/库存与风险约束；独立阈值实验、封存回放及生命周期日志。
- **Lesson 5 — Paper OMS**：独立 Order Manager、Paper Exchange、部分成交、撤单请求/确认、拒单、TIMEOUT / UNKNOWN / RECONCILED、Kill Switch、承诺风险、日亏损熔断及持久化恢复。
- **Lesson 6 — Fill / Inventory / Exit / PnL**：Touch、Trade-through、Queue-aware 三种代理；FIFO 多批次账本、部分退出、完整份额配对/结算；退出与对冲候选、会计与策略归因、UTC 日报、CSV。
- **Lesson 7 — Stress / Acceptance**：确定性故障矩阵、五态风险控制、提交边界最新参考复检、独立 Paper 成交账余额 reducer、权威订单/现金/库存核对及 GO/NO-GO 报告。

风险控制支持 ACTIVE、PAUSED、HALTED、RECONCILING、READY_FOR_MANUAL_RESET。仅 ACTIVE 可提交新的 Paper 风险订单；核对失败保持 HALTED，严重熔断不自动恢复，UNKNOWN 不视为已取消。核对通过后仍需显式人工复位。

**集成边界：** 保留的 Lesson 5/6 旧路径仍是异步 OMS 与即时撤单回放两套研究工具。v2 新 `inplay` 路径已经统一 OMS 与 FIFO，事件事务原子化，拥有跨进程排他写锁和子进程异常退出测试；Paper authority 仍与 OMS 同进程、同 SQLite，不能声称独立服务故障域已验收。

## 快速开始

需要 Python 3.12+。以下命令在 Windows PowerShell、仓库根目录执行，无需激活虚拟环境。

```powershell
git clone https://github.com/w1llyc3/Polymarket-MM.git
cd Polymarket-MM
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Linux/macOS 可将 `.\.venv\Scripts\python.exe` 换为 `.venv/bin/python`。依赖为固定版本 `httpx`、`websockets`；没有含真实交易能力的 SDK 依赖。

### 1. 扫描和采集真实公开盘口

```powershell
.\.venv\Scripts\python.exe -m nba_recorder scan
.\.venv\Scripts\python.exe -m nba_recorder record --db data/nba.sqlite --minutes 30 --limit 3
# 在另一终端只读查看
.\.venv\Scripts\python.exe -m nba_recorder show --db data/nba.sqlite --limit 3 --depth 5
# 主动重连故障验证
.\.venv\Scripts\python.exe -m nba_recorder record --db data/nba.sqlite --minutes 30 --force-reconnect-seconds 300
.\.venv\Scripts\python.exe scripts/validate_recording.py --db data/nba.sqlite
```

没有合格市场时明确报告 `no_matching_markets`；403、超时和 schema 错误不会伪装成没有市场。Ctrl+C 可停止；重启使用同一 DB，重新取快照，不以磁盘盘口直接续接增量。`show` 不联网、不写 DB；停止采集后显示 disconnected/stale。

真实采集只证明 API 和记录功能可用。赛季筛选基于联赛元数据及日历，尚未逐场接入官方 NBA schedule ID；延期和异常命名仍需复核。

### 2. 赔率和 Fair Value

```powershell
# 无 API key 的合成演示；数据库路径需未使用
.\.venv\Scripts\python.exe -m nba_recorder.fair_value demo --demo-db data/fv-demo.sqlite
# 查看真实采集 DB 在当前时点是否具有有效参考
.\.venv\Scripts\python.exe -m nba_recorder.fair_value evaluate --db data/nba.sqlite
# 本地授权 JSON 快照格式示例
.\.venv\Scripts\python.exe -m nba_recorder.fair_value ingest --db data/nba.sqlite --json examples/odds.normalized.example.json
```

授权 The Odds API 的调用方式：

```powershell
# ODDS_API_KEY 请通过本机环境配置，不发到聊天或提交到 Git
.\.venv\Scripts\python.exe -m nba_recorder.fair_value run --db data/nba.sqlite --provider the-odds-api --rules examples/bookmaker_rules.example.json --minutes 30 --interval 30
```

`bookmaker_rules.example.json` 是结构示例，不是已核验的真实书商规则；必须按授权数据与实际结算条款逐书商核验。配置 key 并不自动满足两独立来源、OT/取消/延期兼容性和时效要求。少于两个有效来源仅可研究；不合格输入 NO_QUOTE。示例 JSON 是 fixture，不是真实当前赔率，不能用于冒充有效实时参考。

当前验收环境尚无真实授权赔率 key 或兼容双来源证据。读取赔率可能消耗 Provider 配额，调用者需自行管理授权与频率。

### 3. 三种成交模型 × 三档阈值

```powershell
# 生成明确标记的合成事件流与九个独立 Paper 账户
# 两个路径必须全新；禁止覆盖旧证据
.\.venv\Scripts\python.exe -m nba_recorder.fill_management demo --source data/research-source.sqlite --db data/research.sqlite
```

命令输出 `run_id` 和 `tape`。将实际 `run_id` 用于后续命令：

```powershell
$researchRun = '<填入上一步输出的 run_id>'
.\.venv\Scripts\python.exe -m nba_recorder.fill_management report --db data/research.sqlite --run $researchRun --out data/research-report.json
.\.venv\Scripts\python.exe -m nba_recorder.fill_management audit --db data/research.sqlite --run $researchRun
.\.venv\Scripts\python.exe -m nba_recorder.fill_management export --db data/research.sqlite --run $researchRun --out data/research-csv
.\.venv\Scripts\python.exe scripts/summarize_acceptance.py --db data/research.sqlite --run $researchRun --out data/acceptance-research.json
```

日报包含报价机会、模拟成交率、部分成交率、10s/30s/60s/300s Markout、库存/敞口、Trading/Net PnL、费用/奖励、对冲及数据覆盖。缺少对冲成交或旧报价证据时相应指标为 null/N/A。会计以现金流、持仓估值和资本贡献对账；重叠归因不加到总 PnL。

L0 触碰不能证明排队成交；L1 依赖公开成交事件及方向假设；L2 依赖 FIFO、初始可见队列和隐藏量估计。三者均未校准真实成交概率、市场影响或实际延迟。九账户互为反事实，不能相加当成一个账户。

### 4. 确定性故障测试及 Paper OMS

```powershell
# 新目录；失败返回非零退出码，保留每个场景结果及 SQLite
.\.venv\Scripts\python.exe -m nba_recorder.stress --out data/stress-new
.\.venv\Scripts\python.exe -m nba_recorder.paper_execution show --db data/stress-new/service_restart.sqlite
.\.venv\Scripts\python.exe -m nba_recorder.paper_execution export --db data/stress-new/service_restart.sqlite --out data/paper-csv
```

矩阵覆盖 FV 突变、赔率过期/冲突、WS 断线、REST 超时、撤单/部分成交竞态、提交响应丢失、重复/同时成交、服务恢复、开赛边界、日亏损与账户不一致，另含提交边界、人工复位及 PAUSED。全部使用 Mock/Paper；延迟 P50/P95/P99 来自 `synthetic_paper_clock`，不能当成真实交易延迟。

更多封存流回放、Paper kill/reconcile/resume 及只读导入命令见 [LESSON5.md](LESSON5.md)、[LESSON6.md](LESSON6.md)。没有退出候选自动执行功能。

## 配置与持久化

[.env.example](.env.example) 列出 `NBA_DB`、`NBA_STALE_SECONDS`、`NBA_RESYNC_SECONDS`、可选 `ODDS_API_KEY`。程序读取进程环境，**不自动加载 `.env`**；例如 `$env:NBA_DB='data/nba.sqlite'`。不要配置私钥。交易所端点不能通过环境变量替换。

[examples/](examples/) 包含报价/Paper 风险策略、模型假设、书商规则结构及赔率 fixture。`lesson6-models-demo.json` 的费用为合成假设；实际费用未知时应人工复核。

- SQLite 保存原始事件、exchange timestamp（如有）、local receive timestamp、市场/资产 ID、深度、事件类型、连接与核验状态。
- 使用 Decimal 字符串；空盘、锁盘、交叉盘明确标记，不静默修正。
- 可识别事件跨重启去重；无法识别的相同观察不强行合并。静态盘口重复确认不等于独立行情样本。
- 公开流尚不能证明全局增量连续性；不可保证同步时重取 REST 快照。断线期间历史事件不能靠快照补回。
- 封存事件流具备哈希链与不可修改约束；历史决策只读当时已收到的数据，Markout 是之后的事后观察。
- `data/`、SQLite、日志、`.env` 和 `.venv/` 被 Git 忽略。克隆仓库后需要运行命令生成本地 DB；JSON 验收摘要随代码提供，不能假设 DB 已下载。

## 代码结构

```text
nba_recorder/
  api.py / teams.py             只读接口、扫描和球队映射
  book.py / recorder.py         本地盘口、WS 与重同步
  storage.py                   原始数据与标准化 SQLite
  fair_value/                  Provider、去水、匹配、共识、Markout
  quote_engine/                纯目标报价、封存流、回放及虚拟执行
  paper_execution/             OMS、Paper authority、风险、核对与延迟
  fill_management/             L0/L1/L2、FIFO、退出候选、PnL 与日报
  stress/                      确定性故障矩阵与研究指标投影
tests/                         单元与 Mock/Paper 集成测试
scripts/                       连通性、采集/账本核验与验收摘要
examples/                      不含凭证的配置/合成输入
```

## 验收证据及下一步

- [MVP_ACCEPTANCE_REPORT.md](MVP_ACCEPTANCE_REPORT.md)：模块状态、真实连接、测试、风险与 GO/NO-GO。
- [lesson7-stress-results.json](lesson7-stress-results.json)：16 个确定性故障结果。
- [lesson7-data-validation.json](lesson7-data-validation.json)：真实约 31 秒复核、两次主动断线/三次连接；不是新增长时间验收。
- [lesson7-ledger-audit.json](lesson7-ledger-audit.json)、[lesson7-research-results.json](lesson7-research-results.json)：九实验账本与研究指标。
- [VALIDATION.md](VALIDATION.md)：早期真实 30 分钟采集验收；[LESSON3_VALIDATION.md](LESSON3_VALIDATION.md)、[LESSON45_VALIDATION.md](LESSON45_VALIDATION.md)、[LESSON6_VALIDATION.md](LESSON6_VALIDATION.md)：各阶段证据。
- [LESSON3.md](LESSON3.md)、[LESSON4.md](LESSON4.md)、[LESSON5.md](LESSON5.md)、[LESSON6.md](LESSON6.md)：详细运行方法；[PROJECT_STATUS.md](PROJECT_STATUS.md)：阶段进度记录。文档中的“未提交/未推送”描述为验收当时状态，当前版本以 GitHub 提交历史为准。

已有九实验样本只有 **350 秒、706 asset frames、2 个合成 public prints、14 个代理成交事件**；窗口有效率约 99.43%，不代表全天覆盖或可信策略收益。真实封存盘口在缺少有效赔率时得到 72 NO_QUOTE、0 订单。

v2 后续优先核验真实比赛阶段/时钟来源，完成整场数据及长时间 Paper 验收，校准队列/延迟、退出预算与返佣证据。原 EXTERNAL_FAIR 单独保留授权赔率/规则依赖。P0 未解决不能解除 Live NO-GO。

## 给 ChatGPT / 代码审阅者

建议依次阅读本 README、`MVP_ACCEPTANCE_REPORT.md`、`nba_recorder/stress/engine.py`、`paper_execution/manager.py`、`fill_management/ledger.py` 及对应测试。请重点审查提交边界风险、UNKNOWN/撤单竞态、恢复核对、时间前视、重复记账及 PnL 恒等式；不要仅凭测试通过或模拟正收益推断实盘就绪。若无法访问仓库，请粘贴上述文件，且不要提供 API key 或私钥。

## 官方接口与参考

使用 Polymarket.com Gamma `https://gamma-api.polymarket.com`、CLOB `https://clob.polymarket.com` 的明确 GET 白名单，以及 `wss://ws-subscriptions-clob.polymarket.com/ws/market` 公共市场订阅；不发送交易请求，不跟随接口重定向。

官方资料：[市场发现](https://docs.polymarket.com/market-data/discover-markets)、[实时数据](https://docs.polymarket.com/market-data/realtime-data)、[Python SDK](https://github.com/Polymarket/py-sdk)。工程参考：[polymm](https://github.com/kachence/polymm)、[poly-maker](https://github.com/warproxxx/poly-maker)、[polymaker](https://github.com/yluoc/polymaker)。未复制其真实交易/账户代码；接口与规则变化需重新核验。
