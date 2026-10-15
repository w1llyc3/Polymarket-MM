# Lesson 3 — Fair Value Engine（仅研究记录）

在原 `nba_recorder` 内新增 `fair_value` 子包，没有另建项目。没有订单、钱包、签名、资金或私钥读取模块。即使有两个以上有效来源，`auto_live_quote_allowed` 也固定为 false。Gross Edge 和 Hypothetical Markout **不是实盘利润**。

## Lesson 2 依赖与兼容性

已有 Gamma NBA Market Scanner、CLOB REST/WS Order Book Recorder、Decimal 多档盘口、SQLite 去重、重连和只读 CLI。原始验收数据库的 545 条快照保留。

Lesson 2 原有不足：markets 表只保留最新元数据；原市场记录没有完整结算描述或明确主客队；重复静态快照只更新 latest_books 和健康日志，没有完整的历史确认引用。Lesson 3 新增：

- Scanner 记录 description、resolution source、league/type、逐场 event.teams 中的 home/away ordering 证据、live/ended；列表缺少角色时读取官方 `/events/slug/...` 详情，不用 slug 或联赛默认顺序猜测主客队。
- `market_versions` 保存每次实际观察到的元数据版本与 observed_ms。
- `book_observations` 保存每次实际确认盘口的观察时间、原始 event_key 和 snapshot_id，包括相同快照的重复确认。原始事件和 snapshots 仍去重，原表及 Lesson 2 CLI 保持兼容。

**旧数据库不补造历史元数据。** 旧数据只有最新描述、缺少对应时点版本时输出 `NO_QUOTE / missing_asof_market_metadata`。要生成新的参考记录，需要重新启动 Lesson 2 采集，并接入授权的赔率数据。新确认不会反向改变旧快照时间。

## 安装与快速演示

使用现有 `.venv` 和 requirements，无新增第三方依赖：

```powershell
.\.venv\Scripts\python.exe -m nba_recorder.fair_value demo
.\.venv\Scripts\python.exe -m nba_recorder.fair_value evaluate --db data/lesson3-demo.sqlite --at-ms 1791954000000
.\.venv\Scripts\python.exe -m nba_recorder.fair_value show --db data/lesson3-demo.sqlite
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

demo 固定时钟为北京时间 2026-10-14 13:00，使用**合成**比赛、合成赔率、合成盘口和未来合成观察，不请求网络，不模拟真实成交；两场景格式 American/Decimal，两个独立源得到 0.5 的参考概率，每个资产有 BUY/SELL 研究候选，生成全部四档 Markout。数据库全部标 synthetic，不能与真实市场混用。已有 demo DB 时拒绝覆盖，可加 `--demo-db data/another-demo.sqlite`。

## 真数据采集流程

先在一个终端继续 Lesson 2：

```powershell
.\.venv\Scripts\python.exe -m nba_recorder record --db data/nba.sqlite --minutes 30
```

另一个终端用 The Odds API（只请求 h2h，NBA）：

```powershell
# 在本机设置授权的数据 API key；不要将真实 key 写入代码/提交或聊天。
$env:ODDS_API_KEY='YOUR_AUTHORIZED_ODDS_API_KEY'
.\.venv\Scripts\python.exe -m nba_recorder.fair_value ingest --db data/nba.sqlite --provider the-odds-api --regions us
.\.venv\Scripts\python.exe -m nba_recorder.fair_value run --db data/nba.sqlite --provider the-odds-api --rules bookmaker_rules.json --minutes 10 --interval 30
```

`ingest` 单次获取并保存原始赔率，显示当前参考结果；`run` 周期读取、记录信号和更新到期 Markout。该流程只有数据 GET，不创建订单。API key 只从进程环境读取，`.env` 不自动加载。HTTP 错误只输出状态码或净化消息，不输出包含 apiKey 的请求 URL、头或响应错误正文；含凭证字段的 JSON 输入直接拒绝存档。

本地 JSON Provider 可读取多个授权的数据快照：

```powershell
.\.venv\Scripts\python.exe -m nba_recorder.fair_value ingest --db data/nba.sqlite --json odds-feed-a.json --json odds-feed-b.json
.\.venv\Scripts\python.exe -m nba_recorder.fair_value run --db data/nba.sqlite --json odds-feed-a.json --json odds-feed-b.json --minutes 5 --interval 30
```

JSON 格式见 `examples/odds.normalized.example.json`（**它是合成模板，不是真实赔率**）。实际文件需来自授权来源，标 `recorded`，明确 source_id、event_id、league、season、home/away、start_ms、status、odds_format、两边赔率、outcome_count=2、updated_ms、rules 和 rule_evidence。即使文件声明了历史 received_ms，本地导入仍使用**实际读取时刻**；不能拿今天导入的数据冒充过去已知的数据。每个 Provider 的每个 batch 是完整当前快照：在最新 batch 消失的事件不会沿用上次赔率。

`OddsProvider.fetch() -> OddsBatch` 是可扩展接口；本版实现 The Odds API 和 JSON，支持同时使用。API 传输 Provider 和独立价格来源不同：同一 underlying sportsbook 经不同 Provider 重复出现只计一个来源，source_id 应始终用同一规范，如 `sportsbook:draftkings`。JSON 来源真实性、授权及独立性需使用者保证；系统不会把两个文件自动称为独立博彩公司。

## 数据源与规则限制

The Odds API 的官方 [v4 文档](https://the-odds-api.com/liveapi/guides/v4/) 提供 NBA/h2h、American/Decimal 及 bookmaker/market last_update。使用 market.last_update，缺少时用 bookmaker.last_update；缺少两者即拒绝。API 需要授权 key，存在配额、地区/博彩公司覆盖和更新延迟限制。当前环境没有配置 key，未声称验证了真实博彩公司报价或生成真实有效机会。

`h2h` 本身不能证明所有结算规则一致。API 文档未完整提供逐博彩公司加时、取消、延期合同。本版默认规则未知，**默认 NO_QUOTE**。复制 `examples/bookmaker_rules.example.json` 到本机，按真实博彩公司条款核验并填写 rules、条款 source_url、verified_ms；不能为了产生信号而伪造条款一致。

Event Matcher 严格核对：NBA/赛季、明确球队别名、主客队、开始时间（默认误差 0ms）、全场胜负含加时、两种 outcome、取消结算和延期处理，以及规则证据的可用时间。无 fuzzy team matching；城市名等歧义拒绝。Polymarket 规则从当时存档的 description 识别，未识别文本拒绝，YES/NO 资产不能未经题义解析就当作 home/away。本版只对明确球队标签的二元资产生成信号。

特别是：Polymarket 取消且无补赛的 50/50 结算，与博彩公司 void/refund **不完全相同**，会拒绝匹配。延期 wait-until-completed 与固定时限作废也不同。多数实际博彩公司来源可能因此无法满足严格规则条件；这不是通过默认值强行掩盖的差异。

API 没有权威 live/cancel 状态时仅按 commence_time 推断赛前，标 `status_evidence=scheduled_time_only`；同时检查 Polymarket 当时状态及截止时间。JSON 可提供明确 live/cancel 状态；任一新鲜、球队/时间匹配的取消或赛中报告会否决整个事件。事件从最新 Provider batch 消失也不会继续使用旧报价。比赛提前开赛、上游未及时发布状态仍是残余风险。

参考 [polymm](https://github.com/kachence/polymm) 的 odds→de-vig→matching 分层及 [官方 py-sdk](https://github.com/Polymarket/py-sdk) 的公开市场身份语义；没有复制交易、钱包、对冲或签名路径。

## 可复现计算与指标

使用 42 位 Decimal 工作精度，结果保存十进制字符串，不受进程外部 Decimal precision 影响：

- Decimal odds D：隐含概率 q=1/D，D>1。
- American odds A：A>=100 时 q=100/(A+100)；A<=-100 时 q=(-A)/(100-A)。只接受标准整数格式，拒绝零、abs(A)<100、NaN/Infinity、bool 和无效文本。
- Proportional De-vig：p_home=q_home/(q_home+q_away)，p_away=1-p_home。记录两边 implied probability 和 overround=q_home+q_away-1；underround 也显式记录，不伪装为正 margin。
- Consensus：每个有效、独立来源等权；先排序再计算均值、总体标准差和 range。保留全部来源原赔率、updated/received、去水结果、偏离 Consensus、是否参与及排除原因。range 超过默认 0.05 概率（5 个百分点）就 NO_QUOTE，不选择对自己有利的来源。
- BUY 候选在当前 best bid：Buy Gross Edge=fair-bid。
- SELL 候选在当前 best ask：Sell Gross Edge=ask-fair。
- 另外记录即时跨盘口的 buy_taker_gross_edge=fair-ask、sell_taker_gross_edge=bid-fair；信号同时展示 bid/ask，便于核对。所有 Edge 均未扣费用。
- Consensus Deviation=fair-(bid+ask)/2，按该资产对应球队概率计算。
- Reference Age：参与聚合来源中最老赔率的更新时间年龄；同时约束各来源真实 receipt age。
- Signal Confidence：来源数量、新鲜度、离散度的可复现启发式分数，**不是校准后的胜率、成交率或盈利概率**。单源上限 0.35，合成数据上限 0.25，NO_QUOTE 为 0。

少于两个有效独立来源输出 `RESEARCH_ONLY`，仍可记录明确标为研究的假设候选；reference_eligible 为 false。两个以上满足条件为 RESEARCH_SIGNAL；没有有效数据、匹配失败、不新鲜、跨盘/锁盘/空盘、断线或来源冲突为 NO_QUOTE。所有状态的 auto_live_quote_allowed 都为 false。

可配置 `--odds-age`（默认120秒）、`--book-age`（30秒）、`--metadata-age`（360秒）、`--start-tolerance-ms`（0）、`--conflict-range`（0.05，须>0）、`--min-edge`（0.01）、`--markout-window-ms`（5000）。每个信号保存配置和 engine_version，候选仅在 gross edge>=min-edge 时产生，仍不代表可成交。

## As-of 与 Markout

只读历史计算和已有研究记录展示：

```powershell
.\.venv\Scripts\python.exe -m nba_recorder.fair_value evaluate --db data/nba.sqlite --at-ms YOUR_PAST_UTC_MS
.\.venv\Scripts\python.exe -m nba_recorder.fair_value show --db data/nba.sqlite --limit 4
```

这两个命令用 SQLite mode=ro，不请求网络也不写库。`record` 将当前/as-of 计算写入 fv_signals/fv_candidates，`markouts` 补充已到期研究观察：

```powershell
.\.venv\Scripts\python.exe -m nba_recorder.fair_value record --db data/nba.sqlite
.\.venv\Scripts\python.exe -m nba_recorder.fair_value markouts --db data/nba.sqlite
```

决策 t 只使用 market_versions.observed_ms<=t、odds.received_ms<=t 且 updated_ms<=t、book_observations.received_ms<=t 且 exchange_timestamp<=t。不会读取 latest_books 或当前 markets 来计算过去。最新可见源若无效、取消、缺失或冲突，不回退到更早的有利赔率。信号时间是计算时点，真实收集命令不能用 --at-ms 回填 receipt。HTTP 历史赔率接口本版未接入；只有当时已经记录的赔率可用于真实历史判断。

候选冻结 entry quote、signal ID、盘口引用、metadata version、赔率 raw IDs 和配置。Markout 独立读取目标时刻之后**第一条**收到的盘口观察，最多允许延迟 5秒；保存实际 observed_ms/delay_ms，不声称精准落在 horizon 上。缺失观察、第一条无效、元数据过期、取消或已赛中即 UNAVAILABLE，不跳过坏数据找漂亮结果。未来价格只能进入这个事后评价路径，不能反向进入原信号。提前查询为 PENDING；历史查询不会泄露已经保存的未来 Markout。

- BUY mid markout=future_mid-entry_quote；SELL=entry_quote-future_mid。
- BUY gross liquidation markout=future_bid-entry_quote；SELL=entry_quote-future_ask。
- 每个候选记录 10s、30s、60s、300s 四行，状态 PENDING/OBSERVED/UNAVAILABLE。

全部带 hypothetical=true、executed=false、not_profit=true；假设在候选价格成交，没有排队/成交模型，不含手续费、滑点或对冲成本，**不能写成实盘利润**。

## 存储与追溯

复用同一 SQLite WAL：`fv_raw_odds` 原始无凭证响应；`fv_odds` 规范化赔率/时间与 raw_id；`fv_signals` 可复现计算、来源采用/排除和 metadata/book refs；`fv_candidates` 冻结候选；`fv_markouts` 四档事后评价及未来盘口 refs。候选和信号采用稳定内容哈希，重复相同 as-of 输入不重复写入。

信号可沿 market_version_id → market_versions、book_ref → book_observations/snapshots → raw_events、sources[].odds_id/raw_odds_id → fv_odds/fv_raw_odds 追溯。JSON 原文件来自授权来源且不含密钥才保存；规则声明同时保留在规范化赔率中。

## 测试及残余问题

运行上面的 unittest discover。覆盖正负 American、Decimal、格式等价、对称/非对称去水、underround、无效 odds、Consensus 离散度、确定性、过期/未来价格、主客队/球队/时间/合同错配、单来源、来源冲突、取消/转赛中、重复书商、Provider 事件消失、迟到数据、不可变元数据/确认时间、四档 Markout、无效目标/窗口/边界，以及只读 CLI/密钥拒绝和原 Lesson 2 回归。

真实博彩公司请求受 API key 和逐书商条款核验依赖限制；当前只验证了模拟适配器和显式合成端到端演示。等权 Consensus 及启发式 confidence 没有质量权重或统计校准；不能证明来源在经济上独立。Book/odds 时间以本机 receipt 和毫秒 UTC 为准，要求机器时间可靠；不存在断线期间完整回放保证。JSON 的历史更新时间无法替代实际采集时间或来源真实性证明。后续需要授权赔率、可核验且真正兼容的结算规则、权威状态源和真实数据耐久验证，当前仍只做研究记录。
