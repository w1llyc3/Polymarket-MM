# NBA 通用发现与单图验收

日期：2026-10-16 UTC+8。基线 `3c5a9e73d013898f1f8cf7517152b9b43351d782`，开始时工作区干净。保留旧代码/测试/报告；没有回退、另起项目或接入真实订单。升级前确认原会话已 STOPPED，再重启服务；旧SQLite保留。

## 实现与复用

- 复用 PublicAPI、REST周期双资产快照、市场/体育WS原始事件、PaperRunner、BookSession、唯一owner、OMS、FIFO和既有4小时上限/赛后尾段。没有新策略、钱包、签名、实盘开关、Rust、Node构建或CDN。
- `nba_recorder/discovery.py`：新增通用发现路径，网页所有类别都走此路径。NBA标签/series从实际 `/sports` 取得；有界Gamma分页（最多500个event，达到上限明确SCHEMA_UNSUPPORTED/不完整）、官方NBA赛程适配、主客/时间/结果/协议/规则核对；不固定赛季日期、球队或NBA slug模板。不同market slug需全场规则证据，否则待核验。半场、其他联赛、夏季联赛/全明星不能借NBA标签绕过。
- 类别：PRESEASON / REGULAR_SEASON / PLAY_IN / PLAYOFFS / UNKNOWN；轮次来自明确标签，缺失UNKNOWN。Cup属性单独保存，不一概排除或猜测是否计入常规赛。season优先来源字段，否则按7月至翌年6月的**显示约定**生成，并标明证据；该约定不授予类别或报价资格。
- 官方赛程可用时按唯一主客/开赛时间映射，赛程存在而平台未列出时显示NO_LISTED_MARKET。接口失败不当作未上架。独立呈现MARKET_FOUND / NO_LISTED_MARKET / IDENTITY_AMBIGUOUS / ACCESS_FAILED / SCHEMA_UNSUPPORTED / EXCLUDED_BY_FILTER。
- `console/service.py`：全部类别、今天/明天/未来7天/自定义1至8天、阶段筛选；元数据缓存30秒、最多8个查询窗口，保留原观测时间，不每个盘口tick扫历史。运行场次在列表置顶，不因过滤或刷新切换账户。身份明确但类别未知可OBSERVATION；不能升级Paper。新增类别未独立验收时提交边界返回`competition_paper_scope_not_approved`。旧严格scanner继续作为兼容接口，不是新版网页的准入限制。
- `console/chart.py`：只读连接既有SQLite，不构造OMS或写库。价格/公开成交来自ip_inputs，已接受挂单/部分成交/撤单在途/UNKNOWN来自pe_journal权威回报，阶段来自ip_phase_log，原始比赛信息从ip_game_observations读取。持仓来自当时OMS，已实现/未实现PnL来自当时写入的FIFO估值；旧日志缺项显示未知。断流后迟到成交仍更新库存，没有新估值时PnL未知。
- `inplay/manager.py`仅补充已有权益日志的FIFO估值；`runner.py`复用同一写者保存比赛信息可见时点。输入、接收、处理、交易所和策略可见时间不互相替代。期初split、费用、奖励继续由原账本处理；前端不算资金或额外添加Markout收益。
- `console/static/chart.js`：02页只有一个Canvas。盘口中点阶梯线、买卖一价差带、已接受Paper挂单阶梯、公开成交灰点、Paper成交黄方块、阶段/风险/操作标记。公开side保持主动方向UNKNOWN，点大小只用有证据的成交价格×数量；不累计互补结果成交量。
- 做市/趋势仅为显示预设，不调用命令接口。结果切换、1m/5m/15m/1h/全会话、缩放/拖动/按钮平移、回到最新、图层开关、本地按会话/结果/市场保存水平价位与备注。历史视窗不被新数据拉回。没有LIVE_EXECUTION层；来源/执行标签也直接绘制在画布内，滚动后仍可辨认合成数据。

## 图表真实性与接口

薄深度仍展示实际有效BBO，并保留reference_quality中的原深度拒绝原因；这不放宽报价或估值门。主轴为美分价格，不是胜率；默认横轴为当时本地可见时间，显示UTC+8。线不跨过无效/陈旧/断线点，也不延伸到记录终点之后；未记录的历史留空。只有目标或SUBMITTING不能变成有效单；有效区间从收到接受证据开始，撤单请求/超时/UNKNOWN画在途或不确定样式，确认取消才终止。记录边界不是安全撤单证明。

悬停只取不晚于所查看时刻的记录，不拿当前或最终库存/PnL倒填。同毫秒跨通道顺序未被证明，明确保留歧义。订单、成交和风险保留原ID；同像素事件合并显示数量，详情/导出保留事件，价格按像素保留首尾、买价低点、卖价高点及gap。风险详情先给摘要和原journal序号，可另取原始证据。

- `GET /api/chart?session_id=...&asset_id=...&from=...&to=...&input=...&journal=...&phase=...&game=...`：当前会话、已知结果、最长4小时加60秒清理裕量；各流独立游标，每页最多600条，has_more显式表示还没加载完整。重复读取不提交订单、不创建账户。
- `GET /api/chart-event?session_id=...&seq=...`：单条原始日志只读；大于64KiB明确提示原文保留在SQLite，不偷偷截断冒充完整。
- 图表导出为**已加载范围**，包含游标/has_more/来源/标注；不是完整数据库备份。停止后的会话报告与原始SQLite继续可导出/审计。类别、赛季、模式保留，未做跨类别或合成/真实混合胜率统计。
- 沿用本地Host/Origin、GET来源检查、命令令牌/幂等/会话版本与单写者。图表查询不在owner执行；只在锁内读小量身份字段，排序缓存/像素绘图留在浏览器。后台失联后图表灰显旧值，不把历史画面当实时。

## 实际运行证据

参考官方[市场发现](https://docs.polymarket.com/market-data/discover-markets)与[实时数据](https://docs.polymarket.com/market-data/realtime-data)，实际使用Polymarket.com公共GET/WS。NBA官方赛程URL `https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json` 本机返回403，没有绕过。Gamma动态发现真实12场；11场类别仍UNKNOWN但可只读记录，不借日期推断。单场旧核验赛程只作为额外分类证据，不再是发现/采集白名单。

[实际发现响应与映射](evidence/general-chart/real-discovery.json)。季前赛独立核验仍只有此前官方证据；常规赛/附加赛/季后赛/总决赛与跨赛季通用路径本轮主要由fixture验证，未声称已经采集了未来比赛或所有真实类别。未上架与源失败的区分有自动化证据，但本轮没有成功读取官方NBA赛程来实测未上架案例。

浏览器实际进行了两段真实Heat–Magic观察，以及独立合成演示：

- 首段真实 `00ca9b591df24b0bb81ed452cea4a58b`：从全部类别中选非白名单场次，OBSERVATION；切换预设和结果、添加并清除“验收标注/不交易”，会话不变。手动停止APPLIED，见[回执](evidence/general-chart/first-real-receipts.json)。
- 最终真实 `2537ddedc47c40e6bcf222cb9aba90b4`：[主图截图](evidence/general-chart/final-real-chart.jpg)、[最终带来源水印的停止画面](evidence/general-chart/final-real-stopped-chart.jpg)、[完整分页投影](evidence/general-chart/real-complete-chart.json)、[回执](evidence/general-chart/real-receipts.json)、[会话导出](evidence/general-chart/real-session-export.json)、[账本复核](evidence/general-chart/real-ledger-audit.json)。实际40批REST双资产快照，0个观测公开成交，0个Paper订单/成交，真实提交0，资本仅初始化1次。311条图表价格状态包含watchdog/复用/gap，**不是311个独立盘口样本**；11页读取完毕，无静默截断。
- 缩放和平移后，后续刷新仍显示“历史视窗锁定”；切换到Magic/回到最新不改Runner或账户。最终会话达60秒硬上限正常停止并保持HALTED，随后停止请求REJECTED（已无运行会话），导出APPLIED。没有把该拒绝回执描述成手动停止成功。此前也实际验证目录过期时启动被拒绝，没有偷偷绕过60秒目录门。
- 合成 `4528d8d5216848ee940ccecb8968ec9d`：[图](evidence/general-chart/synthetic-chart.jpg)、[投影](evidence/general-chart/synthetic-complete-chart.json)、[报告](evidence/general-chart/synthetic-session-export.json)、[账本复核](evidence/general-chart/synthetic-ledger-audit.json)。6页完整读取；被选结果含3个合成公开成交、1次实际Paper部分成交，98个订单状态观测（不是98个订单），全账户24次模拟提交。价格线、接受挂单、部分成交、取消及阶段/风险同图。常驻SYNTHETIC_DATA + PAPER_EXECUTION；虚拟盈亏不是实盘盈利。

两套数据库均独立，authority/OMS/FIFO一致，真实提交0。所有运行在本机实际执行，导出标明基线commit加dirty工作区，不能把基线哈希当成已包含新代码。

## 测试与未完成验证

命令：`.\.venv\Scripts\python.exe -m unittest discover -s tests -v`。

**256 PASS / 0 FAIL**，[完整日志](evidence/general-chart/full-tests.txt)，原242项保留，新增`tests/test_general_chart.py`14项。覆盖各赛季类别/总决赛/跨赛季动态发现、Cup/其他联赛、身份时间冲突、未上架与ACCESS_FAILED、只读与Paper隔离、价格/带/gap、目标未接受、部分成交、撤单超时UNKNOWN、迟到成交历史库存/未知估值、分页、重复读取无写入/无新增资本、仅一张图。原RETAIN、epoch、暂停估值分离、重启/人工恢复、单写者、末段锁定均通过。

保留两份中途失败日志：旧严格扫描接口误用真实时间导致固定时钟回归失败，已恢复兼容路径时钟；另一次故障夹具向已在撤单中的订单再次注入“丢回报”，实际先前正常回报已确认取消，修正为尚未请求撤单的活动单后验证UNKNOWN，并兼容非字典超时日志详情。没有删除测试或掩盖失败。

仍未验证：真实赛中Paper报价/撤单/成交、真实季后赛/附加赛完整流程、整场覆盖、真实队列位置/执行延迟。4小时最大日志量、极端事件密度与浏览器内存/首次加载耗时未做压力验收；分页可能需要持续加载，has_more期间不能当作完整图。鼠标拖动/滚轮已实现，浏览器本轮实测的是按钮缩放/平移与视窗锁定，不夸大为所有设备手势验收。标注存浏览器本地，需另导出，不是云端备份。图表旧日志缺少历史估值时保持未知，不能自动补全。

结论分别为：**Discovery：通用软件路径GO、真实来源受限；Recording：真实赛前限定窗口GO、整场NOT_COMPLETE；Chart：受控GO、4小时压力未验收；Controlled Paper：合成/Mock受控GO；Real-game Quote：NOT_VERIFIED/NO-GO；Live：固定NO-GO。**
