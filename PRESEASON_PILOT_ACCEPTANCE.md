# 季前赛真实输入试点：实现与验收

2026-10-16，Asia/Shanghai。基线 `96c59c942be1c7e6de67a1c01a76e2b998d8f88e`，起始工作区干净，没有回退到旧版本。保留 Python、单 owner、BookSession、OMS、FIFO、RETAIN、epoch/watchdog、迟到成交和暂停估值分离。

## 真实预检与主样本

[PRESEASON_PREFLIGHT.json](PRESEASON_PREFLIGHT.json) 是本机实际公共 GET 结果，包含请求/HTTP状态、版本、规则原文、资产身份、双结果完整深度、tick/最小数量及观测时间。最初探索请求及原始返回在本机 `data/preseason-preflight/`；没有把失败解释成无市场。

- 目标窗口：2026-10-17 00:00至24:00 UTC+8。按 Gamma 实际 `/sports` 的 NBA tag 745 有界发现12场；没有预设季前赛一定共用常规赛 series。本次实际数据确实使用 series 10345。11场缺少独立类别核验，保留 UNKNOWN 只读证据，不按日期直接标成季前赛。
- 主样本：Celtics @ 76ers，PRESEASON；NBA game ID `0012600018`，Polymarket game ID `20023840`（两个命名空间不混用）。计划开赛2026-10-16 23:00 UTC，即10月17日07:00 UTC+8。
- event `1162057`、market `5477728`、slug `nba-bos-phi-2026-10-16`，condition与两个实际token ID见预检JSON。协议v1，tick 0.01、minimum 5，当时接受订单；预检BBO分别为Celtics 0.31/0.32、76ers 0.68/0.69，均为历史观测，不能视为以后可成交价格。
- NBA官方[比赛页](https://www.nba.com/game/0012600018)与[季前赛公告](https://www.nba.com/celtics/news/celtics-announce-2026-27-preseason-schedule)交叉核对了类别、日期、主客及时间。本机直接访问比赛页和NBA比分JSON均403，已记录；官方页面通过独立网页读取核验。`game_state.VERIFIED_GAMES`只固定这一条已核验赛程证据，不是人工体育阶段许可，也不是通用完整赛程数据库。
- Gamma规则确认全场胜负含加时；延期等待补赛；完全取消且无补赛按50-50。保留原规则，不把比赛结束当作市场结算。主样本选择先要求身份/规则/类别与双盘口有效，再比较合格样本观测spread；本次仅此一场满足独立分类证据，不宣称其为全联盟流动性最佳。

## 代码入口与运行

用户仍然首次双击 `install-console.cmd`，日常双击 `start-console.cmd`。网页选择“季前赛 · 显式研究范围”→“明天 · UTC+8”→刷新→选实际比赛→“真实公开数据 · 只观察”。正式试点建议10月17日06:45 UTC+8启动，采集上限4小时、确认结束后继续10分钟；这些是实验运行参数，不是数据质量阈值。保持电脑联网、不休眠；程序不修改电源策略。没有定时自动启动，也没有擅自开启明天的会话。

- `api.py`：season_type与game_phase/market_type分离，常规赛默认规则保留。季前赛必须显式开启并限定日期；未知分类只读。沿用已验证的实际slug schema，不猜测其他命名；不支持的元数据仍需后续适配。
- `game_state.py`：精确Gamma gameId与主客队映射的信息适配器。比分、节次、原始时钟可独立展示/持久化；无上游时间、暂停或时钟语义证据时仍不授权报价。心跳不刷新单场信息，墙钟不推进比赛时钟，不编造NBA球权。
- `pilot.py`、`inplay/runner.py`：同一账本连续运行，可配置60秒至4小时；确认结束后尾段采集。末段关闭/策略HALTED不终止公开采集。停止Paper单独撤单、持久锁定报价并继续采集；停止全部采集才结束Runner。结束观测锁定不会因后续字段回退而重新开报价；延期/超限/人工提前停均NOT_COMPLETE。正常操作与故障保留原状态和原日志，仅区分展示严重度。
- `console/`：类别/UTC+8日期/进行中筛选，单场信息头部、服务/采集/报价三层状态、运行计时/剩余上限、旧账本提示和导出文件位置。STOPPED后显示最后快照，恢复按钮与快捷选项禁用；服务端同样拒绝。网页人工阶段授权仍只读，不自动续期。
- 数据在 `data/console/<session_id>.sqlite`（Paper输入/OMS/FIFO/回执）和 `<session_id>-source.sqlite`（公共原始WS、REST观测、元数据版本、sports_raw/game_information及处理时间）中。REST周期快照不是逐笔完整盘口；WS仅保留原始更新与公开成交，不假称增量连续。重复原始事件去重，但每次快照观测保留；互补两侧不相加成双倍流动性。

开发诊断命令（正常用户不需要手填ID）：

```powershell
.\.venv\Scripts\python.exe -m nba_recorder.preflight --start 2026-10-16T16:00:00Z --end 2026-10-17T16:00:00Z
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# 如使用原CLI做季前赛受控Paper研究，需要显式 --research-preseason；阶段门仍保留。
.\.venv\Scripts\python.exe -m nba_recorder.inplay run-paper --db data/pilot-paper.sqlite --source-db data/pilot-source.sqlite --event-slug nba-bos-phi-2026-10-16 --market-id 5477728 --research-preseason --seconds 14400 --postgame-s 600
```

预检分类分别支持MARKET_FOUND、NO_MATCHING_MARKET、ACCESS_FAILED、SCHEMA_UNSUPPORTED、IDENTITY_AMBIGUOUS。预检成功不会创建订单或启动采集账户。

## 实际短窗结果

最终浏览器真实会话 `31af7e6155734dd9b0aa690b0aba59f5`，OBSERVATION，硬上限60秒（约61.6秒含清理），在真实比赛开赛前运行。网页选择、启动、停止Paper、到限停止、导出均有实际后台回执。没有用synthetic状态补齐。

- [最终导出](evidence/preseason/final-real-short-window-export.json)、[回执](evidence/preseason/final-browser-receipts.json)、[真实界面](evidence/preseason/final-real-books.jpg)、[只读账本复核](evidence/preseason/final-ledger-audit.json)。导出基线commit加dirty工作区，不能把基线哈希当成已经包含本轮修改。
- [源库独立审计](evidence/preseason/final-source-audit.json)确认：停止Paper后仍有24次盘口观测，整个会话仅1次资本初始化，真实提交0。原始SQLite留在本机，审计记录文件哈希。
- 41批REST双资产快照，共82个去重观测；80个通过质量门，样本可用率97.56%。这是观测样本比例，不能外推整场或墙钟覆盖率。原始相同快照按hash去重，因此raw_events中的REST条数小于观测数。
- 元数据更新6次，市场WS连接1次、体育WS连接1次。体育流收到22个全局比赛更新，本场匹配更新0；比分/时钟缺失，阶段授权可用率0。观测到公开成交0，不代表市场整天没有成交。
- Paper意图/提交/接受/代理成交均0，真实提交0。账本、Paper权威状态与库存一致，最终HALTED，无残余库存。无交易Paper账本PnL=0，不是实盘盈利，不估计真实费用或奖励。
- 本地事件处理延迟90个样本，P50/P95/P99=0/1/1毫秒；只测收到数据后写库处理，不是网络传输、报价响应或真实交易延迟。没有已检测REST错误，但短窗无错误不等于整场无缺口。
- 首轮会话 `498d43355d2a44328de0dbba6d633837` 发现统计把贸易/体育触发的盘口复用帧也计入样本。已修正为asset/book_ref/接收时间唯一观测并补测；旧导出保留在证据中，最终判断以上述第二次会话为准。

已将第一轮实际处理流封存并成功无网络回放：[offline-replay.json](evidence/preseason/offline-replay.json)。`scripts/freeze_pilot_session.py` 保留WATCHDOG、原接收/处理/可用时间及连接/操作员许可；同毫秒事件按本地原顺序封存，不伪造交易所顺序或挪动时间。它重放输入，不宣称自动重演所有网页命令或原执行延迟；完整控制事件仍在原账本/回执中。

```powershell
.\.venv\Scripts\python.exe scripts/freeze_pilot_session.py --source data/console/<session_id>.sqlite --out data/pilot-tape.sqlite
# 上条返回tape_id；以下使用新研究账户，不覆盖原账本。
.\.venv\Scripts\python.exe -m nba_recorder.inplay replay --db data/pilot-replay.sqlite --tape-db data/pilot-tape.sqlite --tape <tape_id> --model L2 --strategy B
```

## 测试、缺口与结论

**242 PASS / 0 FAIL**，[完整日志](evidence/preseason/full-tests.txt)。原223项不删除不改写，新增19项：分类/主客/时区/异日隔离、开赛后发现、缺字段与冲突展示而不授权、30分钟以上模拟时间单账户无重复资本、结束/加时/市场关闭不混淆、不结算、停止Paper仍采集/跨重启保留、样本去重、同毫秒封存顺序、接口失败/无市场/不支持schema分开。

尚未验证：真实进行中或整场采集、真实比分/时钟字段契约、结束后10分钟实际尾段、真实延期/加时/断线恢复整场覆盖。本轮只做软件测试与真实赛前短窗；没有生成事前的 `REAL_GAME_SESSION_REPORT.json`。代码只在观察到匹配比赛结束且完成尾段后生成该文件，仍保守标NOT_COMPLETE/Full-game NO-GO，完整覆盖须独立人工审计缺口。源数据丢失不能靠重连快照填补历史。

季前赛支持当前只有一条独立核验赛程；扩展其他场次需要真实类别/时间/主客证据。NBA官方源本机403，当前显示与采集优先用Polymarket体育流；其信息用途与局限依据[官方实时数据说明](https://docs.polymarket.com/market-data/realtime-data)，不把联网视为阶段已验证。上游时间、暂停、剩余时间语义仍未验证，自动Paper报价继续NO_QUOTE。完整四小时运行、磁盘压力与跨会话旧账户恢复向导尚未验收。

**Public Observation：限定窗口GO；Controlled Paper：软件受控GO、真实阶段报价NOT_VERIFIED；Full-game Coverage：NO-GO/NOT_COMPLETE；Live：固定NO-GO。** 季前赛样本只能用于工程连通性与初步研究，不能推导常规赛盈利、队列成交概率、最优报价距离或安全库存。
