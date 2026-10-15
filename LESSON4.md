# Lesson 4 — Quote Engine + Record-only

在原 NBA 仓库新增 `nba_recorder/quote_engine/`；保留 Lesson 2/3 的扫描、录簿和去水逻辑。只有 Polymarket.com NBA 赛前 Moneyline 被支持。没有 US 适配器，没有真实交易适配器、签名器、钱包、资金接口或 live 开关。

## 实际依赖与边界

Lesson 2 的不可变 `market_versions`、`book_observations`/`snapshots`、原始 `raw_events` 和 `health` 是输入。Lesson 3 的 `evaluate()` 在每个决策时间重新做 as-of 匹配；无赔率表时明确 NO_QUOTE。真实赔率 API key 与逐书商规则核验尚未完成，真实盘口可读取，但不能虚构 Fair Value。一个来源不生成目标订单；需要至少两个独立、有效、已核验规则的来源。

这是离线、分段回放系统：先采集，再封存某个时间区间，再运行策略。不是常驻的实时模拟交易服务。封存连接只读，使用一致的 SQLite 读事务，不修改输入库。区间结束不是连接断线：关闭 run 时撤销剩余虚拟订单并把未成熟 Markout 标成 RIGHT_CENSORED。

## 报价与风险

纯函数 `model.targets()` 输出 TWO_SIDED / BUY_ONLY / SELL_ONLY / NO_QUOTE。库存偏移中心为 `FV - inventory_skew * (持仓 - target_inventory)`；半价差采用配置价差与观察价差的较大者的一半。BUY 向下取 tick，SELL 向上取 tick；同时约束 `[tick,1-tick]`、毛 Edge、maker-only、最小份额、两位数量精度、现金/持仓和风险承诺。拒绝空、锁、交叉、未来、过期、断线和连续性未验证数据。

单笔金额默认 $25；单场默认 $100、账户默认 $500 的保守最大损失承诺：所有已有份额按 $1 加所有活动 BUY 剩余份额，SELL 不抵消承诺。持仓不做二元结果净额抵消。最大单资产持仓 50。现金按 BUY 剩余份额×限价预留，SELL 按剩余份额预留。默认没有初始份额，所以通常 BUY_ONLY；所有初始资金/库存都是虚拟种子，资产首次在 as-of 输入中出现时才创建库存，避免读取未来资产。保护窗口为开赛前 5 分钟。报价到期取 TTL、赔率/盘口/元数据新鲜度和开赛保护的最早截止点。

## 同流并行与代理成交

`tape.freeze()` 保存原始赔率、完整盘口、市场版本、Fair Value 与引用，以及 `last_trade_price`。所有策略共享一条封存、哈希链验证、SQL 触发器禁止变更的 `qe_input`。1¢ / 2¢ / 3¢ 各运行 L0、L1 两条独立反事实账本，共六条 lane；互不共享现金或成交量预算。逻辑并行比较，非六线程实时执行。

L0 Touch：后续新确认盘口的对手 BBO 触及限价，最多按该档观察数量模拟成交。每张订单同一盘口确认仅使用一次。L1 Trade-through：后续公开成交严格穿过限价，BUY 假定对应 SELL print，SELL 假定对应 BUY print；按观察成交量限制分配。相等价不成交。订单创建的同一时间桶不成交。公开 `side` 的含义与完整性仍不能证明真实队列成交；这些是研究代理，不等价于真实成交概率。

到期、断线、异常和 grid 变化先取消，再评估代理成交；撤单在本课即时确认。取消替换记录 MODIFY，假设丢失原队列优先级。异步撤单竞态在 Lesson 5 的独立 Order Manager 中实现，不把此处即时撤单称为真实保障。

## 持久化与 Markout

`qe_journal` 为 append-only，存状态、原因、订单和完整虚拟账户；`qe_decisions` 存赔率来源/时间、FV、盘口、Edge、库存、规模、市场状态。交易与游标按每个时间桶原子提交；重启恢复游标，审计物化账户/订单与日志是否一致。L1 可识别重复 print 跨重启只处理一次。同毫秒消息采用保守时间桶：任何断线事件都会禁用该桶，不声称具备交易所总排序或子毫秒精度。

每个目标报价产生 QUOTE Markout，每个代理成交产生 HYPOTHETICAL_FILL Markout。10/30/60/300 秒使用目标时刻之后首次可观察的有效盘口中点，5 秒窗口；坏数据不跳过以挑选好结果。BUY=`future_mid-price`，SELL=`price-future_mid`，单位是每份额价格变化，不是净利润。只用已处理到当前游标的未来观察评价结果，不用未来赔率生成历史报价。状态 PENDING / OBSERVED / UNAVAILABLE / RIGHT_CENSORED。

## 运行

在仓库根目录，复用 `.venv` 与现有 requirements：

```powershell
.\.venv\Scripts\python.exe -m nba_recorder.quote_engine demo
# JSON 输出 tape 与 run_id；demo 拒绝覆盖已有文件，重复运行用新路径：
.\.venv\Scripts\python.exe -m nba_recorder.quote_engine demo --source data/demo-source-new.sqlite --db data/demo-quotes-new.sqlite

# 真实采集库：替换成实际观察区间；没有有效赔率时产生 NO_QUOTE。
.\.venv\Scripts\python.exe -m nba_recorder.quote_engine freeze --source data/nba.sqlite --db data/paper-quotes.sqlite --start-ms 1791958468089 --end-ms 1791958469089
.\.venv\Scripts\python.exe -m nba_recorder.quote_engine run --db data/paper-quotes.sqlite --tape <tape> --policy examples/quote-policy.json --close
# 不加 --close 可分段 --max-frames 100；相同 tape/config 再 run 继续游标。
.\.venv\Scripts\python.exe -m nba_recorder.quote_engine show --db data/paper-quotes.sqlite --run <run_id>
.\.venv\Scripts\python.exe -m nba_recorder.quote_engine export --db data/paper-quotes.sqlite --run <run_id> --out data/quote-csv
```

show 与 export 的输入连接只读。CSV 导出订单、生命周期、决策、两类 Markout、账户和完整封存流/manifest；JSON payload 列保留深度和原始引用。超过 100000 个时间桶需分段。阶段性快照序列不包含断线缺口内事件；不能据此估计队列位置、真实成交率、撤单成功率、滑点、费用后利润或真实交易延迟。

## 官方接口核验（2026-10-14）

核对 [当前 Python Unified SDK](https://github.com/Polymarket/py-sdk)、[官方价格精度与 post-only 规则](https://docs.polymarket.com/trading/place-orders)、[公开市场流](https://docs.polymarket.com/market-data/realtime-data)。支持 tick `0.1/0.01/0.005/0.0025/0.001/0.0001`；新市场 V2 position ID 与旧 CTF token ID 的解析复用 scanner。这里的 TTL 是本地模拟生命周期，不能直接映射成交易所 GTD（官方 GTD 有额外有效期约束）。参考 [polymm](https://github.com/kachence/polymm) 与 [polymaker](https://github.com/yluoc/polymaker) 的思想，没有复制实盘执行代码。
