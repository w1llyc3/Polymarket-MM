# Lesson 2 — Polymarket.com NBA Market Scanner + Order Book Recorder

只读数据采集 MVP，目标为 NBA 2026–27 常规赛、赛前、全场 Moneyline。没有账户、钱包、签名、下单、资金或自动交易代码。无需任何 API key 或私钥；不要配置凭证。当前目录初始为空且无 Git 仓库，因此没有覆盖既有功能。

## 安装与运行（Python 3.12+，PowerShell）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m nba_recorder scan
.\.venv\Scripts\python.exe -m nba_recorder record --minutes 30 --limit 3
.\.venv\Scripts\python.exe -m nba_recorder show --limit 3 --depth 5
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

`show` 用 SQLite `mode=ro` 打开数据库，不连接网络、不写数据库，显示最多 1–3 场比赛各 outcome 的 BBO、Spread、前 N 档深度、新鲜度、tick/minimum size 和状态。没有盘口时明确报告等待快照。所有时间戳存 UTC Unix 毫秒，比赛时间带时区。中国时间为 UTC+8。

采集进行时，在另一个终端运行 `show` 可查看当前盘口；采集停止后会显示 disconnected，并按快照年龄标 stale。

```powershell
# 主动断线故障注入：每 5 分钟重连，重新获取快照。
.\.venv\Scripts\python.exe -m nba_recorder record --minutes 30 --force-reconnect-seconds 300
# 重启：使用相同数据库，原始事件及其标准化历史不会重复写入。
.\.venv\Scripts\python.exe -m nba_recorder record --db data/nba.sqlite --minutes 30
```

Ctrl+C 结束。`--minutes` 是总采集时长，包含重连等待；中断会在 health 表记录 stopped。突然断电时靠快照时间戳判过期，重启始终重新获取快照，不以磁盘盘口直接续接增量。可将控制台输出重定向至日志。CLI 网络操作失败退出码 1；成功扫描但没有合格市场输出 `no_matching_markets`，不把 403、超时或字段变化伪装成无市场。

`.env.example` 仅列出 `NBA_DB`、`NBA_STALE_SECONDS`、`NBA_RESYNC_SECONDS`。程序读取进程环境，不自动加载 `.env`；可用 `$env:NBA_DB='data/nba.sqlite'`。默认数据库 data/nba.sqlite，过期阈值 30 秒，周期重同步 60 秒（实际不晚于过期阈值）。不允许通过环境变量替换交易所端点。

## 交易所边界及官方接口

本项目只支持 **Polymarket.com / DeFi Predictions**：

- Gamma `https://gamma-api.polymarket.com/sports`：动态查 NBA series；`GET /events?series_id=...&closed=false&limit=100&offset=...` 完整分页。
- CLOB `https://clob.polymarket.com/book?token_id=...`：公共完整盘口。
- `wss://ws-subscriptions-clob.polymarket.com/ws/market`：匿名 market channel。只发送订阅及应用层 PING，10 秒一次；PONG 超时重连。

HTTP 入口只允许上述三个 GET 路由，不带认证头，不跟随重定向。代码不依赖含交易能力的 SDK。**Polymarket US** 是另一产品，市场、API、账户及凭证均不兼容；本项目不连接 `.us`，不做 ID 映射，也不加载其 SDK。

参考官方 [市场发现](https://docs.polymarket.com/market-data/discover-markets)、[实时数据](https://docs.polymarket.com/market-data/realtime-data)、[订单簿](https://docs.polymarket.com/market-data/prices-order-books)、[Python SDK](https://github.com/Polymarket/py-sdk)。[polymm](https://github.com/kachence/polymm) 与 [poly-maker](https://github.com/warproxxx/poly-maker) 仅作为项目参考，未复制其交易或账户模块。

## 市场筛选与身份

NBA series 必须由官方 sports 元数据发现且事件自身 series 匹配；事件 slug 必须为两个不同 NBA 球队的 `nba-away-home-YYYY-MM-DD`，市场 slug 与主事件完全相同，`sportsMarketType=moneyline`，不是半场、spread、total 或胜率期货。比赛开始必须晚于现在，live/ended 不得为真，事件和市场 active、非 closed，市场非 archived 且启用订单簿。acceptingOrders 仅报告为市场状态，不启用任何交易。

按 [NBA 官方 2026–27 日程公告](https://pr.nba.com/2026-27-nba-regular-season-schedule) 的当地日期，常规赛窗口为 2026-10-20 至 2027-04-11。排除季前、夏联、全明星、play-in、Cup final/championship 描述，保守排除 2026-12-11 Cup 决赛日及 2027-02-19 至 02-21 全明星周末；Cup 其他计入常规赛的阶段可采集。球赛 slug 日期与实际 gameStartTime 均校验赛季窗口。**这是联赛元数据与官方赛季日历筛选，未逐场接入 NBA 赛程 ID 校验**；延期、异常命名及日历修改需人工复核。NBA CDN 赛程接口在本机返回 403，不能声称逐场验证已完成。

输出 event ID、Gamma market ID、condition ID、outcome label 与 asset ID、gameStartTime、tick size、minimum order size、active/closed/archived/acceptingOrders/enableOrderBook/restricted。v1 使用 clobTokenIds，v2 使用 positionIds；未知协议拒绝。发现数据的 tick/minimum size 来自 Gamma，记录时以 CLOB 快照为准，tick change 后重新查询。v2 路径由官方 asset ID 语义实现，真实连通性验收目前只覆盖 v1。

NBA 市场经常以球队名作两个 outcome，而不是字面 YES/NO。本项目保留真实标签；只有标签确实为 Yes/No 才设置 `yes_asset_id`/`no_asset_id`，否则为 null，不能把球队名冒充 YES/NO。每场记录两个资产。

## 盘口同步与数据质量

- 每次连接/重启先订阅 WebSocket，再获取 REST 完整快照；服务器 book 也可替换完整盘口。REST 请求期间排队的旧 WS 事件按 exchange timestamp 拒绝，并重新取快照，不盲目叠加。
- price_change 的 size 是该价位**绝对数量**，不是差值；零删档。价格/数量用 Decimal，多档排序，批次原子更新。
- 官方公开流没有文档化的连续序号和恢复游标，因此**每个增量结果标记 unverified_continuity，立即重新 GET 完整快照**，不承诺无丢失的增量历史。这样以 API 请求成本换取明确的同步边界。深度变化期间记录中间增量和随后验证的快照。
- 断线、应用 heartbeat 超时、未知/畸形 JSON、身份不匹配、乱序、初始化前增量或处理异常使盘口失效；带随机抖动指数退避（最多 30 秒）重新连接并重新取快照。网络请求超时 20 秒，429/5xx 有有限退避重试。
- 即使没有增量，也周期重新取快照；超期在 CLI 标 stale。exchange timestamp 是交易所最后更新时间，不等同于 REST 确认时间。CLI 的 age 是本地最后确认盘口时间，quiet market 可能交易所时间较旧。
- 每 5 分钟重新发现市场并更新市场状态；订阅集合改变就重连和重同步。收到 market_resolved 就标记 resolved 并停止订阅这些资产；不再符合筛选的旧市场标 no_longer_eligible。达到 advertised game start 则退订，绝不继续以赛前市场处理；全部消失时每分钟重新发现直到运行期限。首次扫描无市场则明确返回并退出，可稍后重跑。
- 双边正常盘口 bid < ask；bid == ask 标 locked，bid > ask 标 crossed，任一边空标 empty。保留真实报价、负 spread 和完整原始事件，不夹断价格或伪造合法 BBO；回测须剔除 crossed、stale、unverified_continuity、disconnected 等不可信状态。
- 同 timestamp 的事件没有全序保证，增量仍强制重新验证。无法恢复断线期间所有中间变化；健康日志提供 gap 边界，回测不可把 gap 当成零变化。

## SQLite 与 Record-only 回测

WAL 模式，事件及标准化历史同事务提交：

- `markets`：市场元数据与更新时间。
- `raw_events`：完整解码 JSON 事件（保留全部字段）、event_key、session、source（ws/rest）、event_type、exchange_timestamp（可得时）、local_receive_timestamp。不是 WebSocket 二进制帧或 HTTP 原始字节存档。
- `snapshots`：market_id、asset_id、event_type、exchange/local timestamp、bid/ask、完整 bids/asks depth、flags、tick/min size 等 payload。无有效盘口时不假造 BBO。拒绝事件的快照记录 observation receive time，并用 book_receive_timestamp 指明实际旧盘口时间。
- `latest_books`：最新已验证或明确标记未验证的盘口物化视图。重复 REST 快照只更新此视图的新确认时间，不产生重复原始事件或重复历史。
- `health`：启动/结束、连接/断线、重同步原因、重复事件、逐资产 snapshot_verified、发现及状态变化日志。

event_key 是 source + canonical JSON 的 SHA256；包含交易所 timestamp/hash、event_id、transaction_hash 或生命周期事件 ID 的完全相同事件跨重启去重，不依赖本地时间。缺少交易所身份的相同消息不强行去重，防止误删真实独立观察。不能将只有同一 hash 但不同 timestamp 的消息认作同一事件，因为同一盘口状态可能在不同时间再次出现。

按 snapshots.id 回放观察顺序，join raw_events.event_key 取得原始数据。depth 是 JSON 的十进制字符串，可读成 Decimal。多次验证同一静态盘口的时间从 health 查询；不要仅根据历史最后一条 snapshot 的时间判采集结束。最新显示读 latest_books，停止/断线则依据 health 标记 disconnected。

## 项目结构

```text
nba_recorder/
  api.py        固定官方端点、只读 GET、NBA 扫描及严格筛选
  book.py       Decimal 多档盘口与质量标记
  storage.py    SQLite 原始事件、历史、最新视图与健康日志
  recorder.py   WS、heartbeat、重连、快照重同步及市场重发现
  __main__.py   scan / record / show CLI
tests/test_core.py
requirements.txt
.env.example
```

测试覆盖筛选、球队名/Yes-No/v2 asset 映射、分页、赛前截止、Decimal、数量覆盖与删档、空/锁/交叉盘口、乱序、断线失效、初始化前增量、非有限值、重启去重、重复快照的新鲜度、畸形时间戳存档、增量强制重同步、身份不匹配及 HTTP 交易/US 路由拒绝。真实网络验收结果见 VALIDATION.md。Windows 受限沙箱可能阻止 asyncio 的本地 socket pair 初始化，需在普通终端或获准的沙箱外执行网络/异步测试；同步 show 不需要 asyncio。

离线核验数据库（同样只读）：

```powershell
.\.venv\Scripts\python.exe scripts/validate_recording.py --db data/acceptance.sqlite
.\.venv\Scripts\python.exe -m nba_recorder show --db data/acceptance.sqlite --limit 2 --depth 5
```

验收脚本输出各 session 的运行时长、连接次数、验证快照跨度、事件计数、去重与 BBO 异常标记检查。运行时长包含断线间隙，不代表没有数据缺口。

## 尚存风险

公共流无法证明增量连续性，断线期间中间事件不可能补齐；全快照只能恢复最新状态。逐增量 REST 会提高请求量，规模扩大前应实现请求合并/限流并保持未验证标记。同步期间市场会继续变化，所有状态仅代表该观察时点。公共接口、协议、赛季日历及 Gamma 元数据可能变化；schema/身份异常应报错、重新同步并人工检查。API 可访问不代表账户交易权限，restricted 字段原样保留。
