# v2.2 Runner 与本地操作台验收

验收日期：2026-10-16，Asia/Shanghai。审查基线/修改前HEAD：`ca6d569a7e47c075e6712d6ea8e7024c6f7f0f82`，与文档一致，开始时工作区干净。复用 `w1llyc3/Polymarket-MM`，没有修改其他仓库或重写策略/OMS/FIFO。执行模式始终是公开数据 / Paper，没有真实订单、钱包签名、资金操作、Live开关、赔率key、逆转或跟单策略。

本报告取代旧报告的当前验收结论，历史报告/204项测试继续保留。界面开通不等于赛中数据、成交概率、减仓能力或盈利已经验证。

## P0 连接边界与修复证据

提供的ZIP是方法提取级审查，不是完整OMS或真实交易违规复现。本轮先在完整仓库构造 `disconnect → queued trade → before watchdog` 回归：缓存两资产有效帧，仅a已有Paper挂单、b可以产生新意图，当前连接已断。修复前完整BookSession/OMS入口仍收到 connected=true，回归失败：[runner-before.txt](evidence/v22/runner-before.txt)。不把这个失败夸大为实盘下单证据。

修复后同一集成回归确认：watchdog次数仍为0，新Paper提交增量0，原a挂单的2份迟到模拟成交正确入账，HALTED且账本一致：[runner-after.txt](evidence/v22/runner-after.txt)。不是通过丢弃旧成交达成零新增风险。

- `PaperRunner.authorize()` 统一用于feed、tick和 `UnifiedPaperManager.prepare_submission_frame()`；不能靠旧缓存重新授权。
- 保留observed_connected与原始raw，connected表示当前运行许可；runtime_permission记录当前连接、原观测epoch、watchdog有效性、报价许可。
- 连接epoch随成功重连递增；REST请求批次保留发起epoch，公开成交队列保留接收epoch。重连后的旧快照/旧队列不能默认成为新连接证据，需新快照并经原风控核验。
- watchdog退出/取消、未开始心跳或心跳停滞均关闭授权；默认250ms独立tick，心跳健康限额至少1秒/3个周期。任务失败时消费循环进入停止清理，队列里的历史成交仍按exchange time保留因果性。
- 保留原始exchange/receive/processed/strategy_available时钟；处理积压仍拒绝报价，不伪造处理前可见。v2.1 RETAIN、迟到成交、未知状态、单写者、阶段锁定、人工15秒上限与原有熔断继续存在。

## P1 执行证据分项

删除当前runner中的 `quote_execution_loop_verified = bool(om.orders())`。`execution_evidence()` 从报价、订单日志、实际Paper回报和账本回执输出：input_loop_operational、eligible_quote_decisions、intents_created、local_rejections、paper_submissions、paper_orders_accepted、simulated_fill_events、cancel_acknowledged、inventory_reconciled、reduce_exit_observed、full_game_coverage_verified、quote_execution_acceptance_status及evidence_refs。

仅有本地REJECTED的回归：1个意图、1次本地/边界拒绝、0次提交/接受，验收仍NOT_VERIFIED。接受、成交与取消数不是通过订单对象数量推定；接受/取消依据已观察Paper回报（含权威核对），成交依据统一账本回执。分项非零也不会自动把总体执行验收置绿；本轮真实赛中闭环一直NOT_VERIFIED。

## 本地操作台 v0.1

Python标准库HTTP + 本地HTML/CSS/JavaScript，无新前端运行时、CDN或策略引擎。日常双击 `start-console.cmd` 自动打开浏览器；首次双击 `install-console.cmd` 安装现有锁定依赖。无需手输token/market ID、SQL、JSON或开多个CLI。

四页已实际接通：

1. 比赛与运行：调用真实NBA scanner，展示UTC+8时间、market ID、真实结果标签、交易状态，明确选择观察/真实数据Paper/合成演示。离线封存回放首版网页标为不可用，保留原CLI；不伪造此功能已接通。
2. 盘口与报价：双结果BBO、Spread、多档份数/金额、数据年龄/原始接收处理时点、阶段与中文拒报价原因、原始reason code和RETAIN目标。中心/距离不是Fair Value/胜率。后台失联/数据过期变灰，不把旧值当实时。
3. 订单与账本：真实Paper意图ID、价格/初始量/成交量/余量、状态、RETAIN、现金/预留、配对与净方向库存、年龄、限额、已实现/未实现Paper PnL、费用、分阶段返佣与未归属奖励。未知估值不补0。
4. 操作与诊断：暂停/停止/核对/人工恢复/导出，页面内重大操作确认和后台回执；只读展示人工确认限制，不自动授予或续期。日志按INFO/WARN/ERROR过滤并可随报告导出。

HTTP只读线程从内存投影读状态，命令进入唯一owner串行队列；不在点击时重建OMS或单独CLI写账户。每次明确启动是独立研究会话，旧库存不清除、不转移。当前UI不提供跨会话组合或旧会话恢复向导；原SQLite/报告保留在data/console。

暂停会请求撤销活动单，不代表清库存；stop回执只确认请求，页面随后等待STOPPED并显示未确认订单/残余库存。清理阶段禁止resume；恢复仍需原有风险检查，不能绕过UNKNOWN/MM_CLOSED。每个真实会话默认有界30分钟，用户可提前停止；不是完整NBA比赛覆盖保证。

## API 与本机安全

完整契约：[LOCAL_CONSOLE_API.md](LOCAL_CONSOLE_API.md)。服务仅监听127.0.0.1，精确Host/Origin + 随机进程令牌保护写接口，无wildcard CORS，限制JSON体大小、静态文件和导出ID，禁止任意路径/环境变量输出。令牌不进入截图、URL、日志或报告。此边界不声称防御同机恶意软件。

命令带request_id、预期session/version；相同命令只生效一次，不同payload复用幂等键拒绝，旧状态/旧会话拒绝。重复启动只能有一个Runner，跨端口还有仓库级实例锁，各Paper DB仍用原排他写锁。重复启动入口已实际返回复用已有实例；端口被其他应用占用、后台失活或目录不可写会给中文恢复提示，不静默创建第二个写者。

页面关闭/刷新不停止watchdog；前端以独立HTTP与owner心跳判离线，超时不宣告撤单成功。[backend-offline.jpg](evidence/v22/backend-offline.jpg)是停止后台后实际拍摄的离线警示，不是硬编码状态。

## 自动化测试

完整命令：`.\.venv\Scripts\python.exe -m unittest discover -s tests -v`。

**215 PASS / 0 FAIL / 0 ERROR，13.206秒**：[full-tests.txt](evidence/v22/full-tests.txt)。原204项保留，新增11项位于 `tests/test_v22_runner.py` / `test_v22_console.py`：

- 完整OMS断线排队成交回归；提交前连接再次变化；旧epoch/重连消息；watchdog任务退出/心跳停滞；处理积压；仅REJECTED不能通过验收。
- Host/Origin/令牌/JSON类型/任意路径保护；读状态无账户写者/资本副作用；重复命令/键冲突；跨端口单实例。
- 单owner启动/重复启动/暂停/停止/导出回执、无浏览器心跳仍继续watchdog；旧session/version拒绝；不存在真实扫描结果不能启动真实会话；UNKNOWN/MM_CLOSED不能经UI恢复。

早期完整回归205通过、随后新增测试9通过，最终扩充至215；本轮新增入口回归确实先失败再修复，未删除失败断言或原测试。浏览器验收曾被原生JS确认框阻塞，已经改为页面内确认，并实际验证按钮与键盘快捷菜单。该工具故障不计为OMS违规或真实执行故障。

首次安装没有在全新Windows镜像验证，当前复用Python3.12虚拟环境；目录权限/端口异常为明确错误路径与相关单实例测试，不宣称已做全部Windows环境矩阵。

## 从界面启动的真实观察

会话：`85f73b902f514b439d670643b42b116f`。真实scanner列出14场常规赛全场Moneyline，从界面选 Celtics vs. Pistons，market `5676016`，slug `nba-bos-det-2026-10-20`。界面显示UTC+8开赛2026-10-21 03:00，明确赛前；这不是赛中报价或季前赛验证。

启动、查看双结果真实盘口、停止、导出均经网页。UTC起止 `1792080651631`→`1792080711641`，约60秒（包括停止核对；原计划有界1800秒，未声称运行满30分钟）。39个REST双资产批次、6次元数据、市场/体育WS各1次连接、31条体育更新、0次REST错误、63个输入批次、222次watchdog、0条公开成交。

没有有效自动NBA阶段契约，0个有效报价决策、0个意图、0次Paper提交、0成交、0真实提交；库存0、虚拟现金/权益100、费用/奖励0、状态HALTED、未确认订单0。ledger/OMS独立只读核对一致，SQLite integrity=ok。0成交不是失败，也不构成报价闭环通过。

- [实际界面截图](evidence/v22/real-observation.jpg)，对应上述会话，Celtics/Pistons真实标签、BBO与数据年龄。
- [真实导出摘要](evidence/v22/real-observation-summary.json)，保留完整原导出SHA256、文件名、账本、阶段、输入标识和完成报告。
- [只读账本审计](evidence/v22/real-ledger-audit.json)、[命令回执](evidence/v22/console-command-receipts.jsonl)。

截图中commit为服务启动时的审计基线，运行的是其上的v2.2工作区，不能把截图代码说成未修改的ca6d569；最终代码来源见Git提交和source-manifest。最终服务投影增加working_tree_dirty_at_start；不把UNKNOWN commit补为伪造版本。

## 从界面启动的完整合成演示

最终会话：`2e466d06eb0f4b3ab88bf2d34aedb616`，明确SYNTHETIC_DEMO。通过网页启动与导出，45个原有合成输入帧，虚拟44秒，wall-clock受控演示约16秒；独立tick使用合成Paper时钟，不能充当真实链路延迟。旧会话 `281f...` 是前一轮浏览器冒烟，不混入统计。

同一BookSession/异步OMS/FIFO：46个可报价决策、24个意图/提交/接受、1次模拟部分成交、24个取消确认，最后MM_CLOSED/HALTED，账本一致。现金100显式抵押split5后买入3份，最终a8/b5（配对5、净方向3），预留0、现金93.56、估值6.50、权益100.06、Paper Net PnL +0.06。费用0与激励0是演示假设；0.06全部为未实现模拟PnL，**不是实盘盈利**。

没有减仓成交，残余库存没有自动清空；reduce_exit_observed=false，full_game_coverage_verified=false，quote_execution_acceptance_status仍NOT_VERIFIED。不能以24个订单/一次模拟成交把验收置绿，也不能把新研究会话初始化现金当旧库存已退出。

- [最终界面截图](evidence/v22/final-synthetic-console.jpg)，对应同一最终会话，常驻合成标识。
- [导出摘要](evidence/v22/synthetic-summary.json)、[只读账本核验](evidence/v22/synthetic-ledger-audit.json)。

## 当前结论与未通过项

- **Data GO，限定公开数据短窗**：真实scanner与公开盘口/体育连接可用；不保证NBA源字段完整或长期稳定。
- **Offline Paper GO，受控工程研究**：旧封存CLI、确定性fixture与账本/风控测试保留；网页离线回放选择器明确禁用。
- **Real-input Observation GO，受控**：日常本地入口、真实赛前观察、停止与导出有实际界面证据。
- **Quoting Acceptance NO-GO / NOT_VERIFIED**：真实赛中可信阶段、非零报价/回报/退出闭环没有样本；本地拒单、合成成交不能替代这些证据。
- **Unattended NO-GO**：自动体育字段契约不足，人工确认最长15秒且网页只读；完整比赛、网络灾难/故障域独立性、长时高事件量性能、恢复向导和残余退出仍未验收。
- **Live NO-GO**：真实交易接口调用始终0，未增加任何真实执行或签名能力，不允许配置解锁。

P0仍包括自动体育上游时间/暂停/clock语义与真实赛中闭环；不会靠无限放宽新鲜度造订单。P1包括未校准的队列/成交概率、真实延迟/费用、部分库存低于minimum size时的退出、跨会话风险展示、长时性能与独立故障域。暂停后部分估值可能因保守数据许可变为UNKNOWN，不补0。P2包括网页离线回放/历史恢复向导、完整日志包与更丰富图表；当前只提供可审计、有界日志尾部与本机完整SQLite。

默认reduce180秒、close60秒仍为未校准实验参数，不是用户确认的最优退出策略。Paper authority与OMS仍同进程/SQLite，不能声称已验证独立真实交易所故障恢复。截止日期不改变以上门槛。
