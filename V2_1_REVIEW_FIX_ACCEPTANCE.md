# v2.1 审计修复与真实输入验收

日期：2026-10-15（Asia/Shanghai）。范围为 BOOK_DRIVEN、NBA 全场 Moneyline、赛中、小额库存受限的受控 Paper。EXTERNAL_FAIR 保留但不阻塞此路径；没有逆转预测、跟单、方向策略或真实交易权限。

## 基线与修复证据

修改前 HEAD：`1a1eb9b304b6e47ea7e75c623d6ecd75ba86748c`，与审计文档一致。审查 ZIP 中还原的 reference.py / tape.py blob 分别为 `97369bafc2e0d007950837af69d589b4cf531955` / `8619652851518e497eed6ba7d8ba5a0147b161dd`，在修复前核对一致。没有覆盖后续提交或修改其他项目。

先将审计提供的两个集成回归原样加入测试，并在修复前运行：稳定 SELL、REDUCE_ONLY 均失败，实际状态 CANCEL_PENDING。另先构建接收后处理延迟 fixture：book 已正确延迟，sports/trade 两项失败，提前可见800ms。修复后上述五项均通过。[完整前后记录](evidence/v21/)包括 before-retention、before-timing、after-retention、after-timing；原始审计测试文件的说明文字保留，现已在完整仓库实际运行。

开发中失败没有隐藏：新增 controls 测试曾有缺 account.events 的 fixture 错误，以及把“已过 TTL、平仓且成功撤单”错误要求为必定 HALTED 的断言；修正为验证活动风险清空、独立断线场景仍要求 HALTED。另一新增同毫秒日志测试暴露传给 `_log` 的订单类型错误，已修复为订单对象。开发记录见 working-controls / working-new-tests；最终状态以 full-tests 为准。

变更主要位于：

- `inplay/reference.py`、`session.py`：先验证 RETAIN，排除自身一笔预留；其他活动单、撤单待确认与 UNKNOWN 仍占用预算。部分成交可保留剩余量，不据新单 minimum size 强撤。价格变化先取消，待确认再创建。
- `inplay/tape.py`、`storage.py`：全分支 processing-available 封存；元数据/健康增加 nullable processed_ms，历史缺证据行不回填；只读取区间截止前接收的数据，后追加未来记录不改变历史 tape。
- `paper_execution/adapter.py`、`manager.py`：取消权威时点与执行/观察时点分离，保留取消前已发生的迟到模拟成交。终态出现更高累计成交进入 UNKNOWN、更正账本并 HALTED；不得以撤单成功抹掉成交。
- `inplay/manager.py`、`phase.py`、`manual.py`、`policy.py`：自身成交诊断、有限人工确认、持久阶段与提交边界复检；不兼容旧政策在写新账本/资本前拒绝，避免误改历史。
- `inplay/runner.py`、`__main__.py`：复用同一 BookSession、异步 OMS/FIFO 和单写者，增加单场真实输入入口、独立 watchdog、停止时取消/核对/保持 HALTED。
- `inplay/report.py`、`scripts/validate_inplay.py`：分离公共流量与自身成交/方向库存/年龄/不利 Markout；保留历史 run_id 的只读账本核验。
- `scripts/compare_v21_retention.py`、`collect_v21_evidence.py`、`tests/test_v21_*.py`、本报告与 README/体育契约：可运行验收与证据。

## 自动化测试与对照

最终完整命令：`.\.venv\Scripts\python.exe -m unittest discover -s tests -v`。**204 PASS / 0 FAIL / 0 ERROR**，9.456秒，见 [full-tests.txt](evidence/v21/full-tests.txt)。原183项全部保留，新增21项；审计/时序/控制回归覆盖：连续20帧挂单与 REDUCE_ONLY 保留、近资金/库存上限、部分成交剩余、替换待撤确认、互补方向共同减仓容量、未知订单预留、缺体育时间戳、静默断线 watchdog、有限人工失效、公共16份零自身成交、因果迟到成交、同毫秒不确定性、未来追加不影响封存、不兼容重启不写新资本。原有重启恢复、重复成交、提交边界、日亏损、事务/进程故障测试继续通过。

[同 fixture 对照](evidence/v21/retention-comparison.json)：23个合成时点、相同资金100、现金抵押 split5、相同 L1、相同延迟、skew=0，隔离 P0-A。加载基线 reference/session 与修复后 reference/session，双方共享当前 OMS/FIFO；**不是整个旧服务的端到端历史重跑**。

- 基线：22次撤单、24个订单，决策前有效挂单保留42/64=65.625%。
- 修复后：0次撤单、4个订单，保留84/84=100%。
- 两边均模拟成交2份、Net PnL +0.04、最终 a3/b5，账本一致。没有盈利提升结论；样本只验证无故撤单消失。

另运行12组 A/B × L0/L1/L2 × 2/20ms虚拟传输延迟，全部 OMS/FIFO 一致：[demo-comparison.csv](evidence/v21/demo-comparison.csv)。这是合成研究，延迟和成交概率未经真实校准。A/B 的信息、初始资金/显式 split 与流一致，虚拟延迟为单独因素。不是 NBA 实赛收益。

## 输入时钟与因果约束

exchange_ms 是公开成交发生时间，收到时保留原值；received_ms 是本地回调接收时点；processed_ms 是解析/处理完成证据；strategy_available_ms 是策略可用时点。它们不互相冒充，不把未知值替成0或当前时间。

封存盘口、公开成交、体育、元数据、连接健康都必须有有效处理完成证据且 completion<=策略时点；时间桶包含各自可用时点，不再只按接收分桶。元数据/健康原回调完成时点可作为已知本地完成时间，不声称是网络首字节或上游生成时间。旧行新增列为 NULL，不能授权报价。runner 的消费队列还保留源处理时间，实际消费时点才进入策略；陈旧盘口不能靠 sports/watchdog 刷新年龄。

取消前发生、处理在 cutoff 后的公开成交，可在明确 submit<exchange<cancel-confirm 时更正原挂单的模拟成交；确认取消后发生的成交不得倒灌。更正不创建历史信号，FIFO按本地回报可见时点入账并保存交易所时点；未知先后顺序保持不确定性。同毫秒 submit/cancel 与公开成交不自动判成交，写 SAME_MS_EXECUTION_AMBIGUOUS，不能把保守排除解释为真实零成交概率。

当前没有跨通道权威序列号；Queue-aware 也只是初始可见深度/FIFO假设。缺源处理证据的旧录制可以继续读数据，但不能声称有可执行历史报价。

## 体育字段契约与阶段权限

见 [SPORTS_FIELD_CONTRACT_V2_1.md](SPORTS_FIELD_CONTRACT_V2_1.md)、[CSV字段表](evidence/v21/sports-field-contract.csv)、[20条真实 raw](evidence/v21/sports-raw-evidence.json)。真实样本NBA=0；连接成功不构成 NBA 时钟验收。官方 sports 结构没有已核实的逐条上游 ms、bool pause、逐场<=3秒更新保证。finishedAt 不是赛中更新时间，ping不是赛事新鲜度。

自动模式仍 UNKNOWN/NO_QUOTE。人工模式必须显式启用 MANUAL_CONFIRMED，最多15秒，确认/证据/操作者/失效与人工退出指示落库；过期撤单、保留未清风险供人工核对，不自动实盘退出。不会伪造 paused=False 或上游时间，不能算自动契约通过。比赛暂停、时钟停止、市场停单、网络断流分别处理。reduce180秒/close60秒为未经校准、未经用户策略确认的实验默认值。

## 真实输入窗口与账本

实际命令：`python -m nba_recorder.inplay run-paper --db data/v21-final-paper.sqlite --source-db data/v21-final-source.sqlite --event-slug nba-bos-det-2026-10-20 --market-id 5676016 --seconds 35 --interval 1 --model L2`。

结果：[real-run.json](evidence/v21/real-run.json)，UTC 起止毫秒 `1792072461658` 至 `1792072498124`（包含停止核对时间）；35秒输入窗口。身份/类型/赛季/主客角色通过 Gamma 明确市场核验，为尚未开赛的常规赛市场，不是季前赛、不是赛中策略验证。

- 元数据4次；REST双资产盘口24批；市场WS与体育WS各连接1次；体育20条、其中NBA0条；公开成交0条；REST错误0。
- 41个数据批次、134次独立墙钟 watchdog，共175个输入事务；没有有效自动阶段来源，始终 NO_QUOTE。
- 订单0、模拟成交0、实际提交0，报价执行闭环 **未验证**，完整比赛 **未验证**，没有非零收益要求。
- 虚拟现金100、初始份额0，未人为注入库存/未split；结束现金/权益100，Net PnL 0，费用0、奖励0、活动预留0、残余库存0，状态HALTED。
- [只读账本核验](evidence/v21/ledger-audit.json)：SQLite integrity=ok、OMS/权威Paper/现金/FIFO一致。旧 v2 数据库亦[只读核验一致](evidence/v21/legacy-readonly-audit.json)，没有写新账本或迁移历史仓位。

真实 runner 使用周期 REST 完整快照作可执行盘口，市场 WS 提供连接/生命周期/公开成交；不假装使用了连续增量队列。断线时门控关闭，快照随下一次 REST刷新；原 Lesson 2 的独立 WS增量/重同步 recorder 保留。35秒证明有界采集、正常 watchdog 与安全拒报价，不能代替30分钟/完整比赛、真实断网演练、实际撤单延迟或收益验证。

## 诊断、费用及限制

公共单边成交量默认 DIAGNOSTIC，记录可见深度比与前一段流量，近期市场成交基线仍 UNAVAILABLE；可显式配置公共流量 PAUSE，但没有校准“15份”NBA阈值。机器人零成交、公共16份不默认触发账户硬熔断。自身同向成交、净方向变化、库存年龄及负 Markout 另外记录；账户超限、UNKNOWN、核对失败与日亏损仍硬停机。

库存偏移明确为 SUPPRESS_BUY_ON_NET_LONG；配对 a10/b10 不等同 a20/b0。卖价受 mid+distance 下限约束，未宣称已实现双边偏移或积极清库存。挂单与风险退出仍 post-only，有预算/完整份额保护；不保证退出一定成交。

交易账本现金/权益是总 PnL 来源；Spread/Directional/Markout/陈旧诊断可能重叠，不重复求和。费用在本轮Paper fixture中为显式0假设，运行成本须另有来源记录；EXPECTED/CONFIRMED返佣不算现金，只有有证据的 RECEIVED 记账，未归属奖励不算NBA收益。真实窗口无成交，不证明真实手续费为零。Cancel/Reconciliation延迟仍是 synthetic_paper_clock，绝不是实盘测量。

两账号旧一小时窗口NBA样本0，账户研究仍非阻塞且未重复采集；未来应先定位真实NBA历史窗口，BUY/SELL不能推断Maker/Taker。

## GO / NO-GO 与未解决问题

- **Data GO（限定）**：真实公开接口短窗采集与不可用字段识别通过；NBA自动体育契约、完整赛中覆盖与长时稳定性尚未通过。
- **受控离线 Paper GO**：本轮回归/同fixture对照/12组合成账本一致；仅用于工程与假设研究。
- **真实输入受控观察 GO；真实输入报价执行验收 NO-GO**：run-paper 可持续输入并安全拒报价；暂无真实阶段授权下的报价/成交/减仓样本。人工模式仅有合成测试，需真实比赛中人工复核后有限研究，不代表已通过验收。
- **无人值守 Paper NO-GO**：可信源时间/暂停/时钟契约、完整比赛、故障域独立性、长时网络故障验收与可控残余退出仍缺失。
- **Limited Live NO-GO**：无实盘适配器、无签名、无权限、无资金操作；本轮实际执行路径真实提交为0。任何后续实盘必须独立审批与技术证据，不能靠配置切换解锁。

P0：真实NBA自动体育契约未具备；真实输入报价/撤单/迟到成交闭环没有样本；无人值守故障域未验收。P1：Queue-aware位置、成交概率、真实延迟/费用、参数和公共流量基线未校准，完整场退出/残余库存处置未验证。P2：历史NBA Maker窗口、跨场统计与更丰富诊断/导出。已复现的自身预留与800ms提前可见错误已修复，但没有据此扩大上述运行权限。
