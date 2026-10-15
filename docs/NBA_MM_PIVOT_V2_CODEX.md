# Polymarket-MM — v2 开发范围调整

日期：2026-10-15（UTC+8）。这是一份下一轮开发规范，不是已实施变更、账户审计完成报告或实盘授权。

## 1. 决策

主路线：Polymarket.com NBA 全场 Moneyline、赛中、盘口驱动、小额、库存受限的做市研究。检验价差与可核实 Maker Rebates 能否覆盖不利成交、风险退出和运行成本。

不以博彩公司赔率或最终赛果预测为 MVP 的必需输入。保留原来的 EXTERNAL_FAIR 研究模块，但它的赔率 key / 两来源要求不再阻塞新的 BOOK_DRIVEN 模式。

逆转机会不进入做市报价、库存转移或自动执行逻辑。未来另一个只读研究模块可消费同一份不可修改的行情数据；本轮不开发自动逆转策略，也不开发方向仓位分账功能。

复用当前仓库。首先读取实际 HEAD、README、报价/风险/OMS/账本代码与测试，不假定本规范所述修改已经完成。不要删除原测试；模式专属测试必须分别保留。

## 2. 轻量研究任务：两个公开账户的行为核验

候选地址：
- 0x216050de6080fac3926226b2b8ce3d5544784c78（官方页面快照对应 iWasEnough）
- 0x0979bad57d7a1403db89cbcd9c52bf43f2138d9b（官方页面快照对应 askilika）

本次对话只取得官方页面/搜索快照；没有成功获取完整逐笔成交，因此不能把任一账号标为已验证的“几美元 NBA Maker”。第二个账号有大额单腿 NBA 持仓快照，也不能据此否定它曾有小额成交，或推断其全部净风险。

这是研究脚本，不是跟单器，不进入报价关键路径，也不应成为主工具开发的大型依赖。

### 数据采集契约

1. 用官方 profile 数据确认输入地址与 proxy wallet 的关系，禁止静默替换地址。记录抓取时间、API 版本、完整参数、HTTP 状态、页数、原始响应和文件摘要。
2. 获取完整的指定时间窗。先固定结束时间，再分页；达到接口分页上限时分割时间窗，并处理相同时间戳边界。接口失败不等于空样本；截断必须显式报告。
3. 在官方 /trades 当前语义中 takerOnly 默认 true；请求包含 Maker 的样本需显式 false。false 不是 Maker-only。调用前核对当前版本，不把 v1/v2 的字段和语义混用。
4. 公共 trades 中 BUY/SELL 不是 Maker/Taker 标签。优先使用有可验证交易角色的数据；角色不可确定时输出 UNKNOWN。只有证明时间窗、身份、分页和记录口径一致后，才允许进行全成交与 taker-only 的差分研究，并明确其推断性质。
5. 统计实际成交金额与份数：TRADE 行的金额按已核实 cash 字段或 price × size；份数不能当美元。持仓总值、事件累计买入、单次 fill、原始订单 size 分开。
6. 一个 transaction hash 不能自动视为一张订单。只有 order ID/hash 或等价证据充分时，才统计“原始挂单规模”；否则标 UNKNOWN。
7. 用官方 event/market 元数据筛选 NBA，并区分常规赛、季前赛、全场胜负盘、让分、大小分及期货。未匹配市场单列，不能靠标题中出现球队名就纳入。
8. 核对 TRADE、SPLIT、MERGE、REDEEM、份额转入/转出及期初库存；缺少后两项时只报告窗口库存变化，不能声称恢复了完整库存。需要时使用合法公开链上数据交叉核验。
9. 单列 MAKER_REBATE、REWARD 等收入。无法归属特定市场/比赛的账户级奖励保留 unallocated，不能全部分配给 NBA。官方汇总 PnL 不替代独立流水对账。
10. 未获取当时公开盘口与订单队列，不得从成交历史还原未成交挂单、撤单速度或真实队列位置；不能用今天盘口计算历史 markout。

### 最小输出

- wallet_summary.json：时间范围、完整性、NBA 样本数、金额中位数/P90、<=5 美元与<=10 美元的笔数占比和金额占比、角色已知覆盖率、已知 Maker 占比、库存/往返证据、缺失项。
- nba_fills.csv：去重后的逐笔原始关联 ID、赛事/市场/资产、时间、BUY/SELL、价格、份数、金额、Maker/Taker/UNKNOWN 与证据来源。
- wallet_evidence.md：逐项列 FACT / INFERENCE / UNKNOWN，不用网站的自动策略标签代替验证。

API 连通受阻时返回 BLOCKED 与错误，禁止生成伪造统计。先完成一份可复核 NBA 比赛样本，再扩展批量分析。两个账号没有符合样本时，如实给出结果。

## 3. P0：独立 BOOK_DRIVEN 模式

新增明确的 strategy_mode，不静默回退：
- EXTERNAL_FAIR：保留现有赔率、规则兼容性、来源数、来源年龄检查。
- BOOK_DRIVEN：使用有效盘口作为报价参考，外部赔率不是准入条件；缺盘口、状态不明、数据失效仍禁止报价。

BOOK_DRIVEN 使用 reference_mid / quote_center / quote_distance 等字段，不把它们写成真实 fair_probability 或最终赛果 Edge。不可通过注入两个 synthetic 来源绕过旧风控。

初版以有效 BBO 中点为基线；盘口不平衡、加权参考等仅作为独立实验变量，不能默认是真实胜率。至少记录多档深度、成交、短期价格变化、数据接收/处理时间与积压、库存/库存年龄、候选报价和状态原因。

报价沿用 tick、minimum size、post-only、余额、可卖库存、活动订单承诺、异常价格和提交边界检查。每笔美元上限与份数分别限制；平台最小数量超过风险预算就不报价。

## 4. P0：比赛阶段与行情有效性

采集范围与报价许可完全分离。可以采集赛前、整场、末段、加时和结束，而报价只在通过检查的允许阶段。

建议状态：PRE_GAME_OBSERVE / SYNCING / ACTIVE_MM / REDUCE_ONLY / MM_CLOSED / UNKNOWN。

- 赛中准入不得靠删除 start_protection 一次性开放；新增明确模式和状态规则。
- 比赛时钟必须识别节次、字段语义、暂停、延迟和时钟修正；不把墙钟经过时间当比赛剩余时间。
- 末段减仓/关闭阈值为可配置研究参数，不宣称已校准。
- 一旦进入 MM_CLOSED，重启、加时、重连不得自动恢复。
- 体育数据可能延迟。不能因 Sports Stream 显示正常就宣布风险解除；状态源不可靠或冲突时只能更保守。此类数据不是最终胜率模型。
- 所有阶段仍监测盘口跳变、深度抽空、连续单边成交、行情滞后和订单未知。
- REDUCE_ONLY 根据订单执行后的组合风险判断，不简单把 SELL 等同于减风险。风险退出有独立预算/滑点限制，不被普通入场报价距离条件锁死，也不能绕过账户约束。
- 无法及时退出的残余库存是异常风险记录，不是逆转持仓机会。

## 5. P0：统一 Paper 执行与账本

复用异步 Paper OMS，成交模型的模拟 fill 必须经过该 OMS 更新同一账本。不得把即时撤单回放与异步撤单结果混称为同一条端到端策略。

保持 UNKNOWN、撤单竞态、Kill Switch、单写者、重复事件幂等、持久化和重启后核对。禁止真实钱包签名、订单提交及资金操作。

双边报价需要可卖库存；不得凭空给账户增加两个资产。Paper 初始化库存必须记录成本和资本贡献；若模拟 complete-set split，则明确扣除相应抵押现金。

收益账本分为交易盈亏、费用、实际/模拟成本、预计返佣、确认返佣、到账收入和残余库存估值。预计返佣不等于到账现金；Maker Rebate 与其他奖励分别记录。

盘口驱动下，距离中间价不称为真实 Edge。评价 fill 后 1/3/5/10/30 秒（仅在数据支持时）的 signed markout：side_sign × (future_mid - fill_price)，同时保留成交时中间价。报价收益、未来漂移和已实现交易收益不得重复加总。

## 6. 实验与验收

最先比较两组同数据、同资金、同成交假设的策略：
A. 固定报价距离 + 固定库存限额的基线。
B. A + 异常暂停 + 库存偏移 + 提前减风险。

然后才增加订单规模或报价距离等单一变量。按比赛划分样本，不能把同一场大量相关事件视作独立试验。输出不同成交与延迟假设；Touch 只是潜在成交信号。

新增强制测试：
- 无外部赔率、盘口与阶段合格：BOOK_DRIVEN 可以生成 Paper 候选。
- 无外部赔率：EXTERNAL_FAIR 仍拒绝。
- 陈旧/断流/交叉/身份错误盘口：所有模式拒绝。
- 阶段 UNKNOWN：不新增风险。
- 同时出现新报价与末段截止：提交边界拒绝。
- 加时/重启：不解锁 MM_CLOSED。
- 连续小额同向成交：总风险不能突破上限。
- 撤单在途 fill 正常入账，不凭空取消风险。
- 减仓不能创建反向未预算敞口或拆坏完整对冲。
- 不允许逆转标记、胜率猜测或后验比赛结果改变 MM 订单。
- 未归属返佣、缺失估值、历史窗口不完整保持 UNKNOWN。

交付 INPLAY_BOOK_DRIVEN_ACCEPTANCE.md：实际 HEAD、代码改动、原测试与新测试、真实数据范围、未通过项、Data/Paper 判定。Live 继续 NO-GO。

截至 2026-10-20，优先目标是可靠的整场记录、阶段控制和统一 Paper，不承诺策略盈利或开幕日实盘能力。

## 官方接口参考（实现前复核当前版本）
- https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets
- https://docs.polymarket.com/api-reference/core/get-user-activity
- https://docs.polymarket.com/api-reference/profiles/get-public-profile-by-wallet-address
- https://docs.polymarket.com/market-data/realtime-data
- https://docs.polymarket.com/trading/fees
- https://docs.polymarket.com/programs/maker-rebates
