# Lesson 6 实际验收 — 2026-10-14，Asia/Shanghai

当前 NBA 仓库内完成，无独立项目、无 Entropy–Lighter 修改。新模块复用 scanner/Fair Value/Quote Engine、L0/L1、封存流和 Paper 订单日志。真实订单、钱包签名和资金接口调用为 **0**；退出引擎只产生候选。

## 自动化测试

最终 `python -m unittest discover -s tests -v`：**135 项通过、0 失败**，约 4.7 秒；其中原有 109 项及 Lesson 6 新增 26 项。日志为 `lesson6-all-tests.log`。Windows 临时 SQLite / asyncio 测试在获准的沙箱外执行，没有绕过失败测试。

全部强制场景覆盖：部分成交后撤单、重复成交、不完全对冲、YES+NO 成本小于/等于/大于1、多批次 FIFO/部分退出、三模型分别产生4/5/2份的不同结果、重启前后权益一致、部分 PAIR 后仅结算剩余量、未结算/结算估值衔接、日报现金/库存/费用与 Net PnL 对账，以及禁止 socket/httpx 的零真实交易测试。

另外覆盖：未知成本/缺估值不能声明净收益、事件 ID 冲突、非有限值/余额不足、原生 Yes/No 的反向数组顺序、未见互补盘口时零库存、未来深度不改变历史队列、队列倍数/隐藏量、费用追溯、Markout 不读未处理 future input、跨 UTC 日衔接及前日入场的后续 Markout、Paper authority 只读/重复导入、游标与 FIFO 原子回滚、tick/min size 数值等价，以及部分可执行对冲按数量计值、已有互补持仓不重复购买。

发现并修复：`0.01`/`.01` 的字符串比较误触发 grid 变更撤单；42 位 Fair Value 乘小数数量的归因抵消误差；深度不足时按全量计算部分对冲收益；已有完整份额重复计入新增对冲需求。缺少互补库存估值时保持 Net PnL 未知。完整 hedge success 与部分 quantity completion 已明确区分，2/5 完成不是完整成功。

## 实际合成样本

主要交付 `data/lesson6-accepted-v2.sqlite`，来自 `data/lesson6-accepted-source.sqlite` 的封存投影，tape=`a4660cd0244dd310f9ad1bb1f0d2989f7a9512aee871fc07b7e6bc432f50669f`，run=`d8e9b5ea07ef29863951b3ae28943e622d1f0debd3ae6c8edaeb3dc7ad66e3c8`。源为明确 synthetic 的双来源 NBA 规则 fixture，不是实际博彩公司赔率或比赛。

记录区间 UTC 05:00:00–05:05:50（上海13:00:00–13:05:50），**350 秒、353 个时间桶、706 个 asset frames、2 个 public-print fixture**。702 frames 有效，条件覆盖率约 **99.43%**；4 个断线/无效参考 frames 保留标记。702 个不同盘口确认，不能将静态重复观察视作702个独立统计样本。这个窗口不代表一天或足够的赛季覆盖。

1/2/3¢ × L0/L1/L2，九条独立 paper 账户，共 **496 个虚拟订单、14 个代理成交事件、59 个会计事件、8 个 FIFO 关闭片段**。每个 lane 有702个有效目标机会桶。订单数量守恒、tape 哈希链、虚拟账户/FIFO/日志审计均通过；重启读取及只读 CSV/report 实际运行成功。开发中间版本 DB 留在 ignored 的 data/ 下，正式结果以上述 v2 为准。

1¢ lane 的总模拟成交量为 L0=9、L1=10、L2=4 份；成交订单率约3.64%、3.45%、3.45%，其中部分订单占已成交订单比例分别50%、0%、100%。这些只描述 forced synthetic scenario，并不估计真实成交概率。

四 horizon 的 HYPOTHETICAL_FILL Markout 共56条 OBSERVED；QUOTE Markout 36072 OBSERVED、72 UNAVAILABLE、14400 RIGHT_CENSORED。所有 future observations 只用于之后的 Markout，不用于历史报价。候选共25221条：4 HEDGE_CANDIDATE、15735 HOLD_CANDIDATE、9482 MANUAL_REVIEW。本合成窗口顶档通常仅4份，小于5份最小订单，因而无 EXIT_CANDIDATE；深度充足的 EXIT 在自动化测试中通过。候选没有被执行。

Paper seed 为 cash=$1000、每个资产10份，首次 mid 成本假设为0.5，因此资本贡献为$1010，不是只用$1000计算回报。所有模型的会计 Net PnL 与 cash + inventory value - contributions 精确对账。1¢ 的诊断 Net PnL 为 L0=$0.09、L1=$0.25、L2=$0.10；只说明模型与库存成本假设造成不同反事实结果，**不是实际收益，也不是策略排名证据**。这里 maker fee 假设0、记录奖励0；没有执行 hedge intent，成功率为 N/A，不能因已有 seeded pairs 或 HEDGE_CANDIDATE 写成100%对冲成功。

保守最大承诺敞口为每 lane $30–$35（$1/share + working BUY，不抵消完整份额）。2¢ 与1¢ 的 L1/L2 价格可能相同，因为 maker/tick/BBO约束同时起作用，不能强行制造策略差异。

结构化核验：[lesson6-validation.json](lesson6-validation.json)；完整研究日报 `data/lesson6-report-v2.json`；10个 CSV 在 `data/lesson6-csv-v2/`。生成与验证方法见 [LESSON6.md](LESSON6.md)。

## 真实记录与 Paper OMS 桥接

对此前真实 `data/lesson3-integration.sqlite` 的已封存区间，用 v2 在 `data/lesson6-real-noquote-v2.sqlite` 实际回放：**72 个 NO_QUOTE、0 个订单**。没有有效真实赔率，不能用合成来源替代；没有真实交易盈亏。Polymarket.com Gamma REST、CLOB REST/WS 可由既有 recorder 使用；公开 GET 的此前连通性证据见 Lesson 5。The Odds API 与 JSON adapter 实际存在，但本机未配置真实 API key、逐书商结算兼容性尚未核验。

只读导入 `data/lesson5-verified.sqlite` 到 `data/lesson6-paper-import.sqlite`，已确认 source PAPER、权威累计成交/现金/库存一致且 source 保持 HALTED。cash=999.02、home=12、away=10，预留现金1.47、available cash=997.55，权益1010.02；同样是 synthetic Paper 数据。导入保留撤单在途的剩余承诺，重复导入不重复记账，也不改变源 OMS。

## 模型可靠性与依赖假设

L0 只用触碰代理，不能证明排队/成交；L1 依赖公开 print 完整性和 aggressor-side 假设；L2 依赖 FIFO、初始可见队列、hidden-ahead 配置和成交量分配，未动态重建新增优价量或真实取消位置。三者都无法可靠推断真实队列、成交率、实际延迟或自身订单的市场影响。九账户互为反事实，不能把成交量/权益/利润加总当成一个账户。

成本基准、mid 估值、maker/taker 参数、反事实成交价、公开事件时间顺序、结算/pair条件现金流、初始库存资本与归因 reference 都有显式假设。未实现值不是可立即卖出的现金；理论 Entry Edge、Markout、Hedge Slippage、Stale Quote Loss 与交易 PnL 存在重叠，不能全部相加。奖励无权威或显式模拟证据就不入账。2 个 trade fixture 与14个代理事件的样本不足以校准任何真实概率或尾部风险。

## Lesson 7 待解决

跨进程单写者与崩溃点、独立 paper authority、长时间高频/断线/乱序流；多 lot 退出对共享盘口和资金的组合约束；队列/隐藏量/新增优价量和撤单延迟的敏感性；真实市场费用、奖励、成本及 oracle 结算证据；跨日估值缺口与市场暂停；记录的独立性、版本迁移及存储规模。当前不授权真实交易，也不把模拟正 PnL 当实盘能力。
