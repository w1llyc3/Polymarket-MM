# Lesson 5 — Execution & Risk Engine（PAPER only）

## 检查结果与复用

开发前已确认 Lesson 2 scanner/WS recorder 和 Lesson 3 去水、匹配、多源 Fair Value 存在；Lesson 4 的纯报价函数、风险约束、封存流、六条虚拟实验、生命周期和两类 Markout 已实现。缺口是独立 OMS、异步撤单、请求未知状态、权威核对、Kill Switch、日亏损和延迟研究。本课新增 `nba_recorder/paper_execution/`，复用 `quote_engine.model.targets()` 和 `hard_risk()`，不复制扫描或 Fair Value；现有功能保持可独立运行。

当前真实数据缺口依旧存在：没有授权且已配置的赔率 key，没有逐书商同规则核验。真实 Polymarket 盘口不等于有效参考赔率。实时数据没有赔率时 NO_QUOTE；样例和故障注入全部标注 synthetic/PAPER。这个模块只有本地 Paper Exchange Adapter，不存在真实 REST 下单、用户 WS、SDK SecureClient、钱包签名或资金动作。

## 模块

- `manager.py`：独立 Order Manager，先持久化 client intent，再模拟提交。CREATED、QUEUED、SUBMITTING、OPEN、PARTIALLY_FILLED、FILLED、CANCEL_PENDING、CANCELLED、REJECTED、TIMEOUT、UNKNOWN、RECONCILED；明确的转换表拒绝非法转换。全量日志附带账户和控制状态，用于重建物化视图。
- `adapter.py`：本地权威订单、成交表及回报队列，模拟接受/拒绝、丢失回报、延迟回报、延迟撤单和执行。这里的“权威”仅指持久化 Paper Exchange 状态，绝非真实交易所确认。
- `risk.py`：现金/SELL 份额预留、单笔名义金额、单场与账户承诺、单资产库存、赔率/盘口/元数据时效、maker-only、tick、开赛保护及 Edge。CANCEL_PENDING、UNKNOWN、RECONCILED 均保守保留剩余承诺，不把撤单请求当成功。
- `session.py`：把 pure quote targets 接到 OMS，目标变化先请求撤单，等待确认后才替换。读取同一封存 tape；重复 client intent、回报及成交跨重启去重。默认最小 Edge 1¢；其他阈值用独立 paper DB，禁止混用已有账户的 tape。
- `hedge.py`：只读深度扫单研究，比较直接卖出、购买互补份额、持有；显示深度不足、费用假设、可选现金可行性。没有生成自动执行动作，没有 merge/split/mint/redeem。
- `store.py`：SQLite WAL：控制权限、订单缓存、append-only 事件、paper 权威订单/成交、延迟队列、风险、指标、报价和回放游标。

## 撤单竞态、未知状态与恢复

请求撤单后订单仍承诺现金/库存。收到部分成交会立即按累计成交差额更新账户，但状态保持 CANCEL_PENDING；全部成交可先转 FILLED。撤单回报到达后才转 CANCELLED。过期只会清除未发送意图或请求撤单，不认为活动订单已经消失。

submit/cancel 超时先记录 TIMEOUT，再 UNKNOWN 并 HALTED。晚到成交仍计入库存，晚到确认或权威核对最多转 RECONCILED；不会自行恢复交易。`reconcile()` 有 HALTED 屏障，检查身份、累计成交、现金和库存；REST 模拟超时或身份/账本不一致保持 HALTED。只有显式 `resume()`，且权威核对一致、无未知/活动核对订单、无未确认撤单、无日亏损熔断，才允许重新生成 paper 风险订单。

重启从日志恢复 client intents、账户和控制；所有未终结的已发送订单变 UNKNOWN，未发送队列清空，并再次请求模拟撤单。持久化 Paper Exchange 状态查询是最终核对依据。缓存损坏可以从日志重建；权威库存、成交或身份不符不能被“修正为正常”。当前假设单进程/单个 Order Manager 驱动同一 paper DB；不承诺多个 OMS 同时消费回报的分布式 exactly-once。

Kill Switch 在 SQLite `BEGIN IMMEDIATE` 事务中先 HALTED，再清除本地队列、请求撤销活动订单并记录原因/时间/关联订单。enqueue/dispatch 同样锁住权限检查和写入。触发后拒绝所有新订单；已经在权威 Paper Exchange 的订单在撤单确认前仍可能成交，必须持续计账。驱动方式是 `advance()` 或 replay，不是后台的真实账户监控服务。

## 风险与延迟

Replay 每个时间桶检查过期/断线、赔率失效、Edge 丢失、开赛/暂停/关闭、未知状态，以及上一盘口中点相较本次的价格跳动（默认 5¢）。每桶完整新鲜持仓盘口用于 Paper mark-to-mid equity；按 UTC 日首次有效观察设置基准，亏损达到默认 $20 熔断。本阶段日损益只是虚拟基准到当前的资产估值变化，非真实收益，也不是有种子成本价的会计损益；缺少任一持仓的新鲜估值会 HALTED。跨日重设基准必须有有效数据，恢复仍需显式操作。

`ExecutionPolicy` 配置提交回报、数据回报、风险检测、撤单、REST 核对和请求超时的虚拟毫秒数。数据传输从 Paper exchange event 到本地回报；风险检测从检查开始到检测结束；模拟撤单从请求到回报；状态恢复从 UNKNOWN/restart 到核对。按 nearest-rank 输出 P50/P95/P99 与样本数。未观察到某种样本就不输出该指标，不用零代替缺测。全部 provenance=`synthetic_paper_clock`，不得写成真实交易延迟。

公开 print 的 L1 Trade-through 可作为 replay 的模拟执行输入，严格穿价并限制累计 print 数量、拒绝同时间创建订单与重复成交；它没有真实排队、撮合或私有成交回报的含义。Lesson 4 的六条实验与 Markout 仍可独立使用；Lesson 5 不将即时撤单代理与异步竞态账本混为同一轨迹。

## 运行与查看

```powershell
# 先按 LESSON4.md 生成并记下 tape（合成 demo 拒绝覆盖旧文件）。
.\.venv\Scripts\python.exe -m nba_recorder.quote_engine demo --source data/l4-input-new.sqlite --db data/l4-quotes-new.sqlite

# demo 使用封存合成流，演示撤单部分成交、超时、核对；最终保持 HALTED。
.\.venv\Scripts\python.exe -m nba_recorder.paper_execution demo --db data/l5-paper-new.sqlite --tape-db data/l4-quotes-new.sqlite --tape <tape>
# 任意录制流：未核验真实赔率会停止报价，不构造虚假引用。
.\.venv\Scripts\python.exe -m nba_recorder.paper_execution replay --db data/paper-execution.sqlite --tape-db data/paper-quotes.sqlite --tape <tape> --policy examples/paper-policy.json
.\.venv\Scripts\python.exe -m nba_recorder.paper_execution show --db data/paper-execution.sqlite
.\.venv\Scripts\python.exe -m nba_recorder.paper_execution export --db data/paper-execution.sqlite --out data/execution-csv
.\.venv\Scripts\python.exe -m nba_recorder.paper_execution kill --db data/paper-execution.sqlite
.\.venv\Scripts\python.exe -m nba_recorder.paper_execution reconcile --db data/paper-execution.sqlite
# 只有操作人员明确想恢复 PAPER 时才运行；失败继续 HALTED。
.\.venv\Scripts\python.exe -m nba_recorder.paper_execution resume --db data/paper-execution.sqlite
.\.venv\Scripts\python.exe -m nba_recorder.paper_execution hedge --input examples/hedge-research.json
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts/validate_paper.py --quotes data/lesson4-demo-v2.sqlite --execution data/lesson5-verified.sqlite
```

`show/export/hedge` 只读输入，show 展示订单状态、账户和模拟延迟分位数。CSV 有订单、日志、成交、权威 paper 状态、风险、指标、报价和游标。所有配置是 JSON/CLI；新增模块不需要也不读取任何钱包环境变量。`.env.example` 的 ODDS_API_KEY 仅供之前的合法赔率读取，不是交易凭证。

本地初始库存默认 0，资金默认虚拟 $1000；禁止卖出没有的份额、禁止短仓。demo 的初始份额为合成种子 10/asset。JSON 配置在数据库生命周期内冻结；restart 使用原配置。不要把 demo 配置的种子库存当真实资产。

## 官方核验与未解风险

已核对 [官方当前 Unified SDK](https://github.com/Polymarket/py-sdk)、[订单价格/数量/post-only/GTD 文档](https://docs.polymarket.com/trading/place-orders) 和 [公开流](https://docs.polymarket.com/market-data/realtime-data)；还阅读 [polymm](https://github.com/kachence/polymm)、[poly-maker](https://github.com/warproxxx/poly-maker) 作工程参考，没有移植其旧实盘执行。OMS 的本地 UNKNOWN/RECONCILED 不是声称交易所会发送这些枚举。Paper TTL 也不是官方 GTD 参数。Polymarket US 与 .com 不混用。

当前不能可靠估计队列位置、实际成交概率、真实 submit/cancel 延迟、真实回报缺口、手续费/奖励后收益和实盘熔断效果。Paper Exchange 与 OMS 共用本地 SQLite，适合确定性注入测试，不能证明网络分区和跨系统提交的生产恢复能力。公共 WS 没有本系统可证明的全序列续传；断线缺口无法补齐。规则匹配、赛前识别和逐书商结算差异仍是有效报价的硬依赖。

## Lesson 6 技术准备清单

- 获得授权赔率 API 配置；核验至少两个独立来源的 NBA 全场、加时、取消和延期规则，持续记录原始来源与时效。
- 扩展真实 record-only 时长及赛事覆盖，收集开赛、停盘、tick 变化与断线证据；保留不可变原始流。
- 在多种模拟延迟/队列假设下验证异步 OMS，与 1/2/3¢ 六条研究轨迹做一致口径的 Markout 分析；保持 right-censoring 与无未来信息。
- 增加跨进程单写者锁、进程崩溃点注入、独立 paper authority 服务，以及日志版本迁移/备份/校验与操作审计。
- 明确费用、持仓成本和 UTC 日基准、暂停/恢复策略与全资产估值质量；补齐异常监控和人工恢复流程。
- 如未来课程涉及真实 adapter，必须另行明确授权、核对最新平台/地区/账户权限、原生协议与权威订单/成交游标。当前代码没有真实交易权限，不能通过改环境变量启用。
