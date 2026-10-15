# NBA MM 开发对齐 — 2026-10-14

**2026-10-15 v2 更新：** 主线已改为独立 BOOK_DRIVEN 赛中 Paper 研究，新增统一异步 OMS/FIFO、阶段锁定、显式整场采集与只读公开账户核验。183项测试通过。外部赔率不再阻塞 BOOK_DRIVEN；真实体育状态契约及整场长时间验收仍未完成，Live NO-GO。最新范围与证据见 [INPLAY_BOOK_DRIVEN_ACCEPTANCE.md](INPLAY_BOOK_DRIVEN_ACCEPTANCE.md)，下文仅保留历史基线。

Lesson 7 已新增确定性故障矩阵、五态风险控制、提交边界最新参考检查与独立 Paper 成交账 reducer；完整测试更新为 **155 通过、0 失败**。当前验收与未解决 P0 以 [MVP_ACCEPTANCE_REPORT.md](MVP_ACCEPTANCE_REPORT.md) 为准。下文保留 Lesson 2–6 基线说明。

## 当前阶段

Lesson 2–6 已有实现及自动化验证，运行范围为 Polymarket.com 数据采集、Record-only 回放与本地 Paper 研究。真实下单、签名和资金操作调用为 0。Polymarket US 不在支持范围内。

当前 Git HEAD 为 `12ffdb1`（Lesson 2）。Lesson 3–6 的模块、文档及测试仍在本地工作区，未提交、未推送；“代码已实现”不等于“远端已有”。

## 已实现功能

- **Lesson 2 / `nba_recorder`**：NBA 常规赛赛前 Moneyline 扫描、市场/资产/时间/tick/min size/状态；REST 快照与 WebSocket 采集、断线重连及重新同步、异常盘口与新鲜度标记、SQLite 原始事件及标准化记录、只读 CLI。已有真实 30 分钟采集及重连验收记录。
- **Lesson 3 / `fair_value`**：The Odds API 与 JSON Provider、American/Decimal Odds、Proportional De-vig、球队/主客/时间/规则匹配、带来源与时间戳的共识概率、Edge/新鲜度/置信度及四期限 Markout。数据无效时 NO_QUOTE。
- **Lesson 4 / `quote_engine`**：四种报价状态、tick/maker-only/资金库存约束、风险检查、1/2/3¢ 独立实验、不可修改事件流、虚拟订单生命周期及 Quote/Fill Markout、CSV 导出。
- **Lesson 5 / `paper_execution`**：独立 Order Manager、Paper Exchange、部分成交与撤单竞态、TIMEOUT/UNKNOWN/RECONCILED、原子 Kill Switch、风险熔断、持久化恢复核对及模拟延迟分位数。撤单超时不等于成功；UNKNOWN 保持 HALTED。
- **Lesson 6 / `fill_management`**：L0/L1/L2 成交代理、FIFO 多批次库存与现金账本、部分退出及完整份额配对、只读退出/对冲候选、会计 PnL 与非重复加总的策略归因、UTC 日报、九条独立实验、Paper OMS 只读导入和账本审计。

## 验证成果及边界

- 最近保存的完整测试结果：**135 通过、0 失败**，见 `lesson6-all-tests.log`；本次进度对齐未重新运行测试。
- Lesson 6 验收样本为明确合成数据：350 秒、706 个资产帧、九条实验、14 个代理成交事件。99.43% 为该窗口条件覆盖率，不能代表全天覆盖或真实成交概率。
- 已有真实盘口记录回放产生 **72 个 NO_QUOTE、0 个订单**。尚未配置授权赔率 API key，逐书商结算规则兼容性也未核验；没有真实有效公允报价或实盘收益证据。
- Lesson 5 异步 OMS 与 Lesson 6 对比回放尚未组成一个持续运行的统一服务。Lesson 6 回放撤单即时生效，不能代表真实撤单延迟；Paper authority 当前与 OMS 共进程、共本地存储。
- L2 队列位置、隐藏量、公开成交方向、费用参数、mid 估值等仍依赖假设；队列位置、真实成交概率、实际交易延迟与市场影响无法可靠估计。
- 多批次退出候选尚未实现跨候选共享深度/资金的统一分配；候选不执行。九条实验是独立反事实，盈亏及成交量不得加总成一个账户。

## 下一阶段任务（Lesson 7，建议顺序）

1. **整理可复现基线**：整理 Lesson 3–6 工作区、固定验收数据版本与依赖、加入 CI；完成版本提交和远端同步后记录提交号。本次对齐不执行推送。
2. **补齐真实数据前提**：本机环境配置授权赔率读取 key，核验至少两个独立来源、逐场身份及结算规则；持续采集多场真实 NBA 盘口。无兼容来源继续 NO_QUOTE，不用合成数据代替。
3. **统一 Paper 运行链路**：保持 Quote Engine 纯目标输出，通过明确接口连接 Lesson 5 OMS、Lesson 6 成交/账本/退出与归因；让异步撤单、部分成交和重启核对进入同一生命周期。
4. **故障与压力测试**：跨进程单写者保护、独立 Paper authority、进程强杀/事务崩溃点、重复乱序/高频/延迟响应、长时间断线及跨日恢复；UNKNOWN 和账本不一致持续 HALTED。
5. **全局退出可行性**：统一盘口深度、资金与库存预留预算，防止多 lot 重复消耗相同流动性；覆盖部分对冲、同时成交、开赛/暂停窗口。仍仅 Paper 或研究候选。
6. **模型与报告校准**：队列/隐藏量/新增优价量/延迟敏感性；增加真实市场费用、奖励和结算证据，处理估值缺口、数据覆盖、存储规模与版本迁移。

Lesson 7 应以“故障后现金/库存/权益可核对、Kill Switch 后无新增风险、超时不伪装成功、无未来数据、共享资源不重复分配、真实交易调用仍为 0”为验收核心。当前任务不授权真实交易。

## 入口与详细证据

安装及采集命令见 `README.md`。模块入口分别为 `python -m nba_recorder`、`python -m nba_recorder.fair_value`、`python -m nba_recorder.quote_engine`、`python -m nba_recorder.paper_execution`、`python -m nba_recorder.fill_management`。

详细操作及限制见 `LESSON3.md`、`LESSON4.md`、`LESSON5.md`、`LESSON6.md`；验收见 `VALIDATION.md`、`LESSON3_VALIDATION.md`、`LESSON45_VALIDATION.md`、`LESSON6_VALIDATION.md`。
