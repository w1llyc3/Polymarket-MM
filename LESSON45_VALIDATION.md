# Lesson 4 / 5 实际验收 — 2026-10-14，Asia/Shanghai

完成于当前 `D:\vibe coding\polymarket\nba`，未建立独立项目，未访问或修改 Entropy–Lighter 仓库。复用了已有 Lesson 2/3。所有订单和执行回报均是本地虚拟数据；真实提交调用为 0。新模块没有网络、SecureClient、钱包或资金执行入口。

## 测试结果

最终命令 `python -m unittest discover -s tests -v`：**109 项通过，0 失败**，运行约 3.9 秒。含此前 69 项、Lesson 4 新增 20 项、Lesson 5 新增 20 项。日志 `lesson45-tests.log`（本地忽略文件）。Windows 沙箱阻止临时 SQLite 和 asyncio socket pair，因此测试在获准的沙箱外运行；不是跳过异步测试。

Lesson 4 覆盖四种报价状态、6 种官方 tick、maker-only、金额/场次/账户/库存限制、来源与时效、开始/关闭/坏盘口、跨资产预留、L0/L1 差异、print 去重/数量限制、断线/grid 变化/到期先于模拟成交、部分成交后的撤单与重启、事务回滚、封存不可变、日志审计、未来资产库存泄漏防护、无未来 Markout、分段恢复与完整运行结果逐表一致。模拟执行测试禁止 socket/httpx 客户端构造。

Lesson 5 覆盖全部要求的故障：撤单前全成、撤单中部分成交、撤单请求丢回报并超时、submit 超时但 paper authority 已接受、断线、REST 延迟/超时、重复成交与回报、重启、突然过期、同场多单同时成交；另外覆盖 Kill Switch 队列清除/新风险拒绝、日亏损熔断、Edge 丢失、跳价/开赛/关闭、现金/SELL 预留、延迟 submit ACK 不倒退成交、意图到期、日志恢复缓存、报价到 OMS 回放、非法状态转换、风险检测与在途成交竞态、权威身份不匹配及 Hedge 深度/过期拒绝。

发现并修复的关键失败案例：权威状态只核对数量时，资产身份错配可能被允许恢复。现在连同 market/event/side/price/size 一并核验；错配状态保持 HALTED，自动化测试固定此案例。此前也修复了预先读取未来 tape 资产并分配虚拟种子库存的时间泄漏；库存只在资产首次 as-of 出现时分配。当前没有已知失败测试；这不意味着生产网络恢复已经验证。

## Lesson 4 合成运行

真实运行产物 `data/lesson4-source-v2.sqlite` → `data/lesson4-demo-v2.sqlite`：350 秒合成数据，353 个不可变时间桶、两种 outcome、6 条隔离 lane，**4236 个决策、329 个虚拟订单生命周期**。run=`7a4a3546321db1daba2d120e1a378c7d2d9247a067df16a734070da9450444b8`，tape=`2be33b057144336dfb1a81cf3cad8b43a7608d5e147b9f3b41d71b4323a524e7`。全部游标已处理，run CLOSED，哈希链和日志/订单/账户一致性审计通过。

记录 8 次代理成交事件及 32 条可观察 HYPOTHETICAL_FILL Markout；QUOTE Markout 为 24048 OBSERVED、48 UNAVAILABLE、9600 RIGHT_CENSORED。尾部截断和断线坏数据没有被补成模拟收益。1c-L0 两次、1c-L1 两次、2c-L0 一次、2c-L1 两次、3c-L1 一次；3c-L0 无 Touch。它们是不同模型的反事实，不是实际成交，也不是一次真实账户的并行成交。

## Lesson 5 合成运行

`data/lesson5-verified.sqlite` 从相同封存流的前 5 个桶获取 pure quote targets，再注入异步撤单、部分成交、丢回报、超时及延迟核对：4 个 paper 订单、1 个 paper 成交事件、97 个日志事件、9 个风险事件。最终 3 CANCELLED、1 RECONCILED；权威累计成交、库存和现金核对一致，**仍 HALTED**，没有自动恢复。虚拟 cash=999.02、home=12、away=10，初始为虚拟 cash=1000、每资产10；这些数字不是实盘资产或收益。

虚拟延迟 nearest-rank P50/P95/P99（毫秒）：数据回报 0/5/5，样本10；风险检测 1/1/1，样本10；模拟撤单 10/10/10，样本4；状态恢复 105/105/105，样本1；权威查询 5/100/100，样本2。单次恢复样本无法估计稳定尾延迟。所有指标 provenance=`synthetic_paper_clock`，**不是真实 API/撮合延迟**。

`scripts/validate_paper.py` 在只读连接上验证 sealed tape 和账本；结果在 `lesson45-validation.json`。CLI show、Hedge 示例和 CSV 导出均已实际运行；`data/lesson4-csv` 有6个 CSV，`data/lesson5-csv` 有8个 CSV，共14个，含嵌套 JSON 的审计证据。Hedge 三方案仅研究，没有交易动作。

## 真实数据与 API 连通性

2026-10-14 官方 GET probe 成功访问 Gamma `/sports`、NBA series `10345` 的 `/events`、事件详情和 CLOB `/book`；样本 market `5420695`，tick=`0.01`、minimum size=`5`。**这是连通性抽样，不是该样本符合 2026-27 常规赛筛选条件的证明**。记录在 `lesson5-public-probe.log`，脚本 `scripts/probe_public.py` 60 秒封顶且只有公开 GET。沙箱内完整 asyncio scan 未完成，已中止；没有把失败/中止写成“没有市场”。先前实际合格市场的采集见 Lesson 2/3 验收。

对先前真实录簿 `data/lesson3-integration.sqlite` 的 as-of 区间 1791958468089–1791958469089 实际封存并回放：**48 个 NO_QUOTE 决策、0 个虚拟订单**，输出 `data/lesson4-real-noquote.sqlite`。原因是没有有效参考赔率，部分观察还受连接健康限制；没有用 synthetic odds 填补真实引用。真实 The Odds API 未配置 key、规则未核验，因此没有有效真实公平价格，也没有实盘结果或真实延迟测量。

## 未解风险与下一课准备

无法由公开数据可靠识别队列位置、真实成交概率和真实 submit/cancel 延迟；L0/L1 不模拟完整队列、撮合、延迟竞价或隐藏订单。公共 print side 被当作方向代理，无法保证完整 aggressor 语义。断线缺口不可恢复；同毫秒桶无法证明子毫秒顺序。手续费、奖励、种子成本和结算延迟尚未进入利润模型。

OMS + Paper Exchange 同进程、同数据库的权威核对不能替代真实账户恢复；还需单写者锁、独立 paper 服务、更多进程崩溃点测试、日志迁移/备份/完整性与监控。真实赔率授权、逐书商比赛/结算规则匹配与长时间 record-only 研究仍待完成。

Lesson 6 准备清单及运行方式见 [LESSON5.md](LESSON5.md)。进入下一课也不代表获得真实交易权限；当前项目没有可以启用真实下单的环境变量或 adapter。
