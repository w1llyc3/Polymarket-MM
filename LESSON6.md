# Lesson 6 — Fill Management、Exit Candidates 与 Paper PnL

在现有 `nba_recorder/` 中新增 `fill_management/`，没有新建独立项目，也没有修改 Entropy–Lighter。保留 Lesson 2 的 scanner/recorder、Lesson 3 的去水/匹配、Lesson 4 的报价和 L0/L1、Lesson 5 的 Paper OMS。没有真实下单、签名、钱包、资金接口或 live 开关。

## 开发前的差距与实际复用

此前已有 L0 Touch、L1 Trade-through、时间封存、重启游标、虚拟账户和 Markout；没有 L2、FIFO 成本账本、批次退出成本、结算对账和两层 PnL。Lesson 5 有独立 Paper Exchange 和撤单竞态，但其日 equity 不是完整交易会计。

`ResearchExecutor` 继承原 Executor，默认旧六条实验行为保持兼容；本课为 1/2/3¢ × L0/L1/L2 共九条独立反事实账本，共享一条不可修改的 tape。`paper_import` 则只读读取 Lesson 5 已核对的 authority、回报、seed 和成交，不改变 OMS/HALTED 状态。三模型比较仍使用 Lesson 4 的即时取消代理；Lesson 5 的异步竞态账本作为另一条有明确来源的 Paper 数据导入，不能混成同一执行轨迹。

## 模块与接口

```text
nba_recorder/fill_management/
  models.py       三模型假设、队列估计、订单数量守恒
  engine.py       复用报价/风控/Markout；九 lane 的成交和估值
  ledger.py       append-only 事件、FIFO 成本、现金流、结算与 equity
  exits.py        每个未退出 lot 的三类独立研究候选
  paper_import.py Lesson 5 本地 Paper authority 的只读桥接
  report.py       UTC 日报、数据覆盖、会计与独立归因
  storage.py      SQLite 会计与研究表
  demo.py         明确标记的 synthetic 场景
  __main__.py     demo / replay / import-paper / paper-event / report / export / audit
tests/test_fill_management.py
scripts/validate_fill_management.py
```

所有关键会计事件保存在 `l6_events`，SQL 禁止更新/删除；`l6_accounts`、`l6_closures` 是可审计的派生状态。每个真实观察桶的成交、FIFO 账本、虚拟账户和 cursor 在同一 SQLite 事务提交。重启按事件重放；账户或成本账本不一致就失败，不静默修正。当前单写者模型，跨进程锁和多服务权威恢复留给压力测试。

## 三种成交模型

- L0：后续新盘口的对手 BBO 触及挂单限价，以观察的顶档量限制模拟数量；一次确认不能重复使用。置信度 `low_uncalibrated`。
- L1：公开 print 必须严格穿过限价，使用 print size 作为数量预算。价格相等不成交，`side` 暂按 aggressor 解释。置信度 `low_uncalibrated`。
- L2：接受订单时按当时同侧更优价格和同价的可见深度估计 ahead，乘以 `queue_multiplier`，再加 `hidden_ahead`。假设 FIFO/价格优先；后续有效同向 print 消耗 ahead，剩余数量才可部分成交。允许 at-price print；相同事件跨重启只用一次。置信度 `very_low_uncalibrated`。

L2 不是已知真实队列。未来订单簿不能改变历史初始队列；订单接受只用该桶 as-of 快照。当前不会因为撤单或深度消失而前移队列，也未动态重建之后新增的更优价排队量。隐藏量、公开 print 完整性、side 语义、真实价格优先和自身订单对市场的影响均不可验证。报价改价或补量采用取消替换，会重置估计。模型不提供校准后的真实成交概率。

订单数量明示在 `l6_order_status`：submitted、filled、unfilled、working、cancelled、expired。`submitted = filled + working + cancelled + expired`；unfilled 包含已取消/过期的未成交量，不能再与 cancelled 简单相加。成交保存唯一 ID、order ID、market/asset、价格、数量、时间、原始观察引用、赔率/盘口来源和模型假设。原始 exchange timestamp 在引用证据内；代理 fill 时间是本地观察时间，不是实际撮合时间。

## 库存、退出与对冲

按 market/asset 保留 YES/NO 角色、实际标签和互补 ID。原生 Yes/No 按标签识别；球队 Moneyline 使用 outcome index 的二元角色别名，并始终保留球队标签，不能声称原生 YES 一定代表主队。资产映射冻结，重排或身份不一致被拒绝。

每笔 BUY 建立 FIFO lot；部分 SELL、完整份额和结算按实际数量消费最早成本批次。没有短仓。资金区分 total cash、活动 BUY 占用、available cash；库存区分 held、活动 SELL 占用和 available inventory。权益使用 total cash，不能把已预留现金再次从 equity 扣掉。

每个 remaining lot 同时评估 EXIT / HEDGE / HOLD，输出 EXIT_CANDIDATE、HEDGE_CANDIDATE、HOLD_CANDIDATE 或 MANUAL_REVIEW。A 扫已有资产 bids，B 扫互补 asks，C 给出持有的参考预期与最坏终值；不发送任何订单。

已有互补持仓按 FIFO 分配给 lots，B 只计算尚未配对且未被待退出订单占用的数量；已有完整份额不重复购买。深度不足时按 executable quantity 计算现金、费用及可配对价值，同时明确 residual inventory；部分对冲不会写成全量成功。低于 minimum size、缺互补资产/规则、断线/过期/坏盘口、未知费用、资金不足均需要 MANUAL_REVIEW。候选是独立比较，不能同时选中所有 lots 的方案再假定深度/资金足够；组合优化尚未实现。

费用按配置的 taker rate 使用价格形状 `quantity * rate * p * (1-p)`，每档按 5 位小数近似现金费用。默认 taker rate 未知，直接退出/新增互补计划转 MANUAL_REVIEW。maker 默认 0；非零 maker rate 只作研究敏感性假设，按成交名义金额比例记现金费。每个费用来源都记录，不能把示例 sports rate 当某个实际市场已核验参数。奖励只有显式、非负且非未来的证据事件才记收入，不估计未来返佣。

## 会计与归因

会计恒等式：

```text
Equity = total cash + marked remaining inventory
Trading PnL = realized gross trading PnL + unrealized gross trading PnL
Net PnL = Trading PnL - recorded fees + recorded rewards
Net PnL = Equity - capital contributions
```

保留成交现金流、费用、奖励、PAIR 现金流、SETTLEMENT 现金流与资本投入。初始 virtual holdings 若非 0，成本采用首次已知有效 mid，作为显式假设的库存资本投入；缺初始成本就不能宣称 Net PnL。未实现项按当前有效 mid 估值，不是可立即变现的收益。缺任一非零库存的有效估值时 equity/Net PnL 标未知，不补零。

PAIR 仅是本地 accounting event：两个同市场互补资产各消费 q，记录合计 $q 的条件现金流及成本；不做链上 merge/redeem。SETTLEMENT 必须有 as-of 证据、完整二元 payout map，总 payout 为 1；只消费仍持有的数量，已 PAIR 或已 SELL 的量不再结算。不自动根据最后 mid 推断赢家。

交易归因与会计独立。覆盖到有效 Fair Value 的已实现 lot 使用三个不重叠项对账：entry reference edge、fair-value movement（Directional Residual）、exit premium to exit Fair Value（本实现的 Spread Capture 定义）。三个项合计该 lot 的毛 realized PnL，费用另外记账。不是把完整买卖价差再次算一次。高精度参考的归因使用 126 位 Decimal，避免抵消舍入误差。

Entry Edge at fill、10/30/60/300s Markout、Hedge Slippage 和 Stale Quote Loss 同时作为诊断输出。它们与交易成本和已实现/未实现项有重叠，**不得与上述分解或 Net PnL 再加总**。缺 exit fair 的关闭批次标未覆盖。Stale Quote Loss 仅指已标 stale 入场 lot 的负已实现/当前负浮动部分，不能证明过期数据造成损失的因果关系。Hedge Slippage 需要显式事前预期价；没有执行样本时 N/A。

## 每日研究报告

每次 replay 自动更新覆盖日期的 `l6_daily`；CLI 也可只读重算。日期按 UTC，明确首尾观察区间，**不是声称有 24 小时数据**。订单与会计用日末 as-of 状态，未来订单状态不回填；前日缺有效 opening valuation 时当天 Net PnL 未知。Markout 是入场日 cohort 的回顾诊断，只能用已处理 cursor 内的之后观察，单独标 evaluation cutoff；不用于历史报价。

输出机会桶数、三模型样本、订单/数量成交率、部分订单率、四种 horizon Markout、对冲意图完整成功率、数量完成率、残余方向量、Trading/Net PnL、费用/奖励、保守最大承诺敞口、覆盖与异常。完整 hedge success 必须 filled == requested，部分成交只增加 quantity completion rate；没有 hedge intent 时成功率为 null。预先 seeded 的完整份额也不是已执行对冲样本。

最大敞口复用保守 `$1 * 持仓数量 + 未成交 BUY 数量` 口径，不抵消已有完整份额，不当成精确经济最大亏损。Quote opportunity 是 asset/time bucket 的有效目标状态数，静态重复观察不等于独立市场机会。三模型/三 Edge 的账户不能合并成一个收益或统计样本。

## 可运行命令

```powershell
# 无凭证的完整合成演示。已有文件会拒绝覆盖，重复运行使用新路径。
.\.venv\Scripts\python.exe -m nba_recorder.fill_management demo --source data/l6-new-source.sqlite --db data/l6-new.sqlite

# 复用此前封存流；真实赔率无效会 NO_QUOTE，不借 synthetic 填补。
.\.venv\Scripts\python.exe -m nba_recorder.fill_management replay --tape-db data/paper-quotes.sqlite --tape <tape> --db data/research.sqlite --policy examples/quote-policy.json --models examples/lesson6-models.json --max-frames 100
# 重启使用输出 run_id，恢复相同模型配置；执行版本不匹配则拒绝恢复。
.\.venv\Scripts\python.exe -m nba_recorder.fill_management replay --tape-db data/paper-quotes.sqlite --tape <tape> --db data/research.sqlite --resume-run <run_id> --close

.\.venv\Scripts\python.exe -m nba_recorder.fill_management import-paper --source data/lesson5-verified.sqlite --db data/l6-paper-import-new.sqlite
.\.venv\Scripts\python.exe -m nba_recorder.fill_management report --db data/research.sqlite --run <run_id> --day 2026-10-14 --out data/daily.json
.\.venv\Scripts\python.exe -m nba_recorder.fill_management export --db data/research.sqlite --run <run_id> --out data/l6-csv
.\.venv\Scripts\python.exe -m nba_recorder.fill_management audit --db data/research.sqlite --run <run_id>
.\.venv\Scripts\python.exe scripts/validate_fill_management.py --db data/research.sqlite --run <run_id>
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

report/export/audit 的 DB 连接只读；封存源和 Lesson 5 DB 也只读。CSV 包含订单数量、逐事件账本、关闭批次、估值、退出候选、每日摘要和原报价生命周期/Markout。只读 Paper import 核验 mode、authority/库存/cash；未核对时拒绝导入。它记录 actual simulated execution time 与 first observed/report time，不把后来的权威查询拿去生成历史信号。

开发/单元场景可在 closed run 后导入显式 synthetic 会计事件：

```powershell
.\.venv\Scripts\python.exe -m nba_recorder.fill_management paper-event --db data/research.sqlite --run <run_id> --lane 1c-L1 --event examples/lesson6-synthetic-reward.json
```

支持 FILL、HEDGE_INTENT、PAIR、SETTLEMENT、REWARD，仅写本地会计。时间不能早于已结束的回放；有证据与合法库存/资金才能记账。事件 ID 重复且内容一致不再次记账。该命令不调用 Paper 或真实下单，不代表退出引擎自动执行。`examples/lesson6-synthetic-reward.json` 是假的奖励来源说明，只作显式输入示例，不能用于真实研究利润。

## 实际数据、假设与 Lesson 7

真实公开数据来自现有 Polymarket.com Gamma REST、CLOB REST/market WS；The Odds API 与 JSON provider 已实现，但当前真实 key/逐书商结算核验仍缺失。没有两家有效来源时不生成真实参考报价。本课真实录簿回放无订单，详见 [LESSON6_VALIDATION.md](LESSON6_VALIDATION.md)。

核对 [Unified Python SDK](https://github.com/Polymarket/py-sdk)、[公开市场流](https://docs.polymarket.com/market-data/realtime-data)、[官方费用](https://docs.polymarket.com/trading/fees)、[结算](https://docs.polymarket.com/concepts/resolution)，并参考 [polymm](https://github.com/kachence/polymm)、[poly-maker](https://github.com/warproxxx/poly-maker)。没有复制或启用实盘提交/资金代码，Polymarket US 不在支持范围。

Lesson 7 待压力测试：跨进程单写者锁与崩溃恢复；独立 Paper authority；大流量 WAL/存储性能、更多断线和错序/重复 print；隐藏队列与新增优价量、partial fill/cancel latency 的敏感性；多 lot 组合退出的共享深度/资金预算；真实市场费用参数和返佣证据；oracle/延期/取消/部分结算与跨日覆盖缺口；不把单个合成窗口、forced fill 或 seeded inventory 作为实盘盈利证据。
