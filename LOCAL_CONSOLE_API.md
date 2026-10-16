# 季前赛试点：本地操作台 API 与单写者边界

默认 `http://127.0.0.1:8765`，只监听 IPv4 回环。没有 Live 模式、钱包字段或交易凭证。HTTP处理线程不打开账户数据库，不创建 OMS；只有一个 asyncio owner 操作当前 Runner / OMS / FIFO。

## 页面与接口

- `GET /`、`/app.js`、`/style.css`：本地静态页面，无 CDN、Node 或前端安装步骤。
- `GET /api/health`：应用标识、仓库路径摘要、owner存活与心跳状态；不返回真实路径或环境变量。
- `GET /api/bootstrap`：给同源页面返回随机进程会话令牌；只保存在网页内存，不写URL、报告或日志。
- `GET /api/state`：owner发布的不可变展示副本；包含会话、来源、版本、阶段、双结果盘口、订单、账本估值、返佣阶段、风险和最后150条日志。读取不触发恢复或新增资本。`server_ms` 是owner发布心跳，不用HTTP请求时间伪造引擎活性。
- `POST /api/command`：仅接收有限命令，返回202/PENDING只表示入队。
- `GET /api/receipt/<request_id>`：PENDING / APPLIED / REJECTED、原因、后台时点与版本；前端等回执，不以HTTP成功或颜色变化宣告生效。
- `GET /api/export/<随机导出ID>`：下载当前会话JSON包。运行中为partial projection+有界日志尾部；停止后附完整既有策略报告。完整生命周期原始日志仍在该会话SQLite中，导出不是完整数据库备份。

写接口要求：精确的 Host `127.0.0.1:端口` / `localhost:端口`；精确同源 Origin；`X-Console-Token`；`application/json`；Content-Length≤32768；拒绝chunked/额外字段。没有 wildcard CORS；GET也检查Host和来源，静态文件严格白名单，不能下载 `.env` 或任意路径。CSP只允许本源脚本/样式/连接，禁止嵌框；5秒套接字读取超时。此机制不声称抵御同机恶意软件、浏览器扩展或已获得当前用户权限的攻击者。

## 命令格式与回执

```json
{
  "request_id": "客户端生成的唯一UUID",
  "session_id": "当前会话ID，空会话为null",
  "expected_version": 5,
  "action": "pause",
  "confirmed": true
}
```

scan 可带 discovery_scope=CURRENT（默认过去1天至未来7天，赛前与进行中）或 HISTORY（过去7天，只读）。上游 start_time_min/max 与本地 game_start_ms 双重过滤，最多500条分页；扫描失败不回退到无界历史。结束/无效市场不能新启动；catalog变化不切换已绑定会话或重建资本。发现不是阶段授权。

允许 action：scan、start、pause、stop_paper、stop、reconcile、resume、export。start另带 mode、market_id；mode仅 OBSERVATION / REAL_PAPER / SYNTHETIC_DEMO。比赛必须来自60秒内真实 scanner 结果，不能由HTTP提供盘口或伪造市场结构。离线回放首版网页显式禁用，保留原CLI。

所有命令经过同一个有界串行队列，带预期会话与状态版本。相同request_id/相同payload只作用一次；同ID不同内容拒绝。两个不同ID的重复启动也不会创建第二个Runner。请求记录与结果写 `commands.jsonl`，运行中的控制结果还进入 `pe_journal`。幂等回执缓存只在当前服务进程有效；重启后令牌改变，旧请求不得自动重发，历史命令日志保留审计用途。

pause：禁止新增并请求撤单，状态按原风控进入PAUSED或保持更严格状态；不出售库存。stop：禁止新增、kill并请求撤单，先回执“请求已生效”，随后界面必须等STOPPED并检查未确认订单/库存。清理期间禁止恢复。reconcile：调用当前owner的权威Paper核对，不自动resume。resume：数据、阶段、epoch、watchdog、账户与原有恢复条件全部通过才可恢复；观察模式不能升级，UNKNOWN/MM_CLOSED不能跳过。网页不写入/续期人工阶段确认。

每次明确启动是独立Paper研究会话，资金初始化和会话ID可审计；旧账本不清除、旧库存不转移，新会话不能被解释为旧仓位已退出。旧文件位于 `data/console/<session_id>.sqlite`，结果包在同目录。当前网页展示一个会话，不是跨会话组合账户管理平台。

## 传输与故障边界

feed、watchdog和最终dispatch共用实时授权：保留observed_connected和原始raw，connected是结合epoch/watchdog的有效数据通道，不包含操作员开关；runtime_permission独立列出physical_connected、epoch_matches、watchdog_alive、quotes_enabled、account_new_risk。book_data_risk控制估值质量，book_risk另加阶段和操作员许可；连接epoch不匹配、排队旧会话消息、watchdog退出/取消/心跳停滞均不能重新授权。迟到公开成交仍按原exchange时点判断原挂单，不为保持零库存丢弃成交。

前端每秒读取展示投影，后端行情/成交/订单事件不合并丢弃。页面关闭/刷新不影响owner的watchdog。HTTP失败或owner心跳超过4秒，旧盘口变灰、显示离线并禁用命令；失联不等于已经撤单。异常重启不自动恢复账户写者，Paper账户原日志保留，需要独立核对；本版UI不提供旧会话恢复向导。

跨端口实例也受仓库级操作台文件锁保护，每个Paper DB继续保留原有跨进程写锁。重复双击默认入口只打开已存在且健康的同仓库服务；端口被别的应用占用、owner不健康、目录不可写时给中文错误，不启动第二个写者。

真实会话 runtime.limit_s=1800、deadline_ms 和 expiry_action 只读展示；到时按原Runner停止/撤单/核对/保持HALTED，不自动续期或平库存。暂停不使connected变假，核对不会自动恢复；resume在owner同步检查双资产、数据通道、阶段和账户后才开启开关，无临时放行。


## 季前赛试点扩展

scan新增season_type=REGULAR_SEASON（默认）或PRESEASON（显式研究）、day_filter=WINDOW/TODAY/TOMORROW；日期按UTC+8转换查询窗口，不用于拼slug。PRESEASON用/sports实际NBA primaryTagId发现，类别须有独立赛程或明确元数据证据；未核验项在catalog.unclassified_markets只读，不授予报价。常规赛过滤保留。

start新增limit_s（整数60..14400，默认14400）、postgame_s（整数0..1800，默认600）。上限在分配会话/资本前检查；runtime输出started_ms、deadline_ms、limit_s、postgame_s。运行中不因为30分钟到达而创建新账户。确认比赛结束才开始赛后尾段，延期或超限不推定完整覆盖。旧的固定1800秒说明仅是历史版本。

stop_paper需要confirmed=true，持久化停止意图、禁止本会话再报价并请求撤单，但不停止公开采集；重新打开同一账本也保留该意图。pause仍为可经核对/人工恢复的暂停。stop结束全部采集并撤单；停止后resume由服务端拒绝且网页禁用。阶段HALTED不暗中停止数据源。

state新增game_information、bound_market、collection：比分、节次、原始时钟、来源、接收/处理/可用时间、是否身份匹配、game_finished、冲突与局限。display_eligible不是quote_authorized；不补上游时间/paused/剩余时钟，未合格体育源继续NO_QUOTE。实时体育源不与元数据心跳混为新比分。

停止导出包含已有策略报告、pilot研究计数和NOT_COMPLETE覆盖状态，database_files指向本机会话文件。输入流可由scripts/freeze_pilot_session.py封存后通过原replay命令离线回放；同毫秒保留本地顺序、不伪造交易所顺序。只有观测到匹配比赛结束且完成尾段后，才在本机写<session>.sqlite-REAL_GAME_SESSION_REPORT.json；即使生成也不自动认证整场覆盖或实盘许可。


## 通用发现与单图

网页scan走discovery.discover：season_type=ALL/PRESEASON/REGULAR_SEASON/PLAY_IN/PLAYOFFS，day_filter=TODAY/TOMORROW/NEXT7/CUSTOM；CUSTOM带custom_start/custom_end（UTC+8日历日期、含首尾1至8天）。缓存30秒/8窗口，缓存不刷新来源时间。旧scan函数默认调用兼容旧接口，仅新版网页显式走通用路径。发现结果独立携带discovery_status、schedule_only、evidence、season/season_type/competition_round/game_phase/规则证据。未知类别身份明确可OBSERVATION，Paper依然拒绝；比赛选择不是报价授权。

GET /api/chart限制当前会话、已知asset、最长4小时加60秒清理裕量；input/journal/phase/game为独立增量游标。每流每页600条，返回has_more；无订单或风险事件静默丢弃。返回prices、orders、public_trades、paper_fills、accounts、events和独立data_source/execution_source。价格raw steps在浏览器按像素保留首尾/极值/gap，事件可合并标记但保留原ID与数量。历史持仓来自当时OMS控制快照，历史PnL仅用当时FIFO估值，缺失留空。撤单请求不视为终点，UNKNOWN继续画不确定样式；尾部边界不是取消确认。

GET /api/chart-event?session_id=...&seq=...只读取一条pe_journal原文；超过64KiB提示保留完整SQLite，不伪称已完整返回。两条图表接口沿用Host/Origin检查，不能指定任意路径或其他会话；不构造OMS或写SQLite，磁盘读取在HTTP线程执行。

单图显示预设、结果/窗口/缩放/标注不调用POST命令端点。标注仅浏览器localStorage，按会话/结果/市场分隔；本地注释与已加载图表需单独导出。服务失联图表置灰并提示旧值。新增加的ip_game_observations和权益日志ledger_valuation由原owner写入；旧日志历史估值未知，不追填。
