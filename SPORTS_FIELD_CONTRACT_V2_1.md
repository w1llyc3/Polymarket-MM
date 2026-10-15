# v2.1 体育字段契约与人工 Paper 边界

核验日期：2026-10-15。官方来源：[Real-Time Data / Sports Stream](https://docs.polymarket.com/market-data/realtime-data)。采用其直接 WebSocket API；没有接入交易 SDK、认证账户或 Polymarket US。

逐字段实际证据、语义、更新保证、新鲜度及缺失动作见 [CSV 契约表](evidence/v21/sports-field-contract.csv)。[原始样本](evidence/v21/sports-raw-evidence.json)保留接收/处理时间、原始字段与 SHA256。20 条样本来自真实公共流，NBA 样本为 **0**，不能用其他运动或官方 NBA 示例冒充 NBA 时钟验收。

## 自动模式结论

官方直接流提供 gameId、联盟、球队、status/live/ended、score，以及可选 period/elapsed。文档中的 finishedAt 是结束时间，不能替代每条更新的上游生成时间；未找到逐场三秒内更新、上游毫秒时间戳或 bool pause 的契约承诺。应用层 ping/pong 只证明连接活性。

因此，默认 normalize 输出 `observed_ms=None`、`paused=None`、`clock_semantics=UNVERIFIED`；不给缺失字段填零、填当前时间或 False。只有经独立审查的显式字段契约且 raw 本身提供相应证据，才可能满足自动阶段门。当前没有这样的 NBA 契约，自动模式保持 UNKNOWN / NO_QUOTE；不通过放大允许陈旧时间制造订单。

必须分别判断：

- 比赛 Suspended/Delayed：体育事件状态；不等于所有盘口已停止交易。
- 比赛时钟停止：场内计时状态；不能仅由两条相同 elapsed 推定。
- 市场停单：Gamma/CLOB 的 acceptingOrders/closed 或生命周期事件；不等于比赛停止。
- 网络断流：传输状态；不代表比赛时钟停止，也不能刷新源数据年龄。

可审计的后续组合方案：取得有授权、带逐条生成时间/赛事 ID/计时运行状态定义的赛事实时源；记录原始响应与版本，映射 NBA 官方赛事 ID、主客队、Gamma event/market/gameId；对照官方计分台核验 Q4/暂停/加时；分别测量生成→接收、接收→处理、更新间隔和丢包。源冲突时 UNKNOWN。现阶段尚未购买、接通或验证此替代源；它不是博彩公司赔率依赖。

## MANUAL_CONFIRMED 受控研究

`confirm-phase` 生成带唯一 ID、操作者、观察证据、确认时点、失效时点及人工退出指示的文件，默认10秒、最大15秒。`run-paper --manual-file` 才启用此模式；同一 Paper 数据库不能切换自动/人工权限。文件确认到达后写入不可修改的确认日志，再在提交边界复检。不会改写 sports upstream timestamp 或 paused 字段。

人工 ACTIVE_MM/REDUCE_ONLY 还要求 Gamma 显示 event_live，操作者明确观察到 IN_PROGRESS、RUNNING 以及有效节次/剩余时间。过期/冲突会撤销 Paper 活动风险，持仓或未决订单存在时 HALTED，保留库存待人工核对；不自动卖出、不自动对冲、不假装已经清仓。重启不自动复位。MM_CLOSED 的持久锁定不被新确认或加时解除。

这只是在人工监督下测试同一 OMS 的有限授权，**不算自动体育契约通过，也不算无人值守许可**。remaining_s 不按墙钟倒计时；reduce=180秒、close=60秒是未经校准、未经用户策略确认的实验默认值。
