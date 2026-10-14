# Lesson 2 验收记录

日期：2026-10-14，时间均为中国标准时间 UTC+8。只读采集；采集进程已正常结束。

## 真实接口与市场

Gamma sports/events、CLOB GET /book 与 market WebSocket 均成功连通。没有使用 Polymarket US API、账户或市场，没有加载凭证、私钥或签名模块，也没有创建或提交订单。

NBA series 由 GET /sports 动态发现为 `10345`。发现的两场常规赛开幕日比赛，与 [NBA 官方日程公告](https://pr.nba.com/2026-27-nba-regular-season-schedule) 交叉核对一致：

- **Celtics vs. Pistons**：event `1185700`，market `5676016`，北京时间 2026-10-21 03:00。
  - Celtics asset：`67105986151420762389444558710598385187601372419417593599928887317008220174126`
  - Pistons asset：`6194634649894872222114139800075806437965693537103509072979175541757029916386`
  - [市场页面](https://polymarket.com/event/nba-bos-det-2026-10-20)
- **76ers vs. Knicks**：event `1185701`，market `5676021`，北京时间 2026-10-21 07:00。
  - 76ers asset：`18530775482813086164830807568132305463750341655031391169642129768649683734274`
  - Knicks asset：`66905676566583664317573429201649106692138646973280275919092487111590728334465`
  - [市场页面](https://polymarket.com/event/nba-phi-nyk-2026-10-20)

两场均为 v1，tick size `0.01`，minimum order size `5`。发现时 active/acceptingOrders/enableOrderBook 为 true，closed/archived 为 false，restricted 为 true。YES/NO asset 字段为 null，因为实际 outcome 标签为球队名；没有擅自将球队名改称 YES/NO。condition ID、完整状态与所有元数据保存在 markets 表及日志中。

官方接口依据：[市场发现](https://docs.polymarket.com/market-data/discover-markets)、[盘口](https://docs.polymarket.com/market-data/prices-order-books)、[实时流](https://docs.polymarket.com/market-data/realtime-data)。初始 urllib 请求曾返回 403；使用项目 HTTPX 客户端后 Gamma/CLOB 正常。NBA CDN 逐场赛程接口仍返回 403，因此未实现逐场 NBA schedule ID 校验。

## 30 分钟真实运行

命令：

```powershell
.\.venv\Scripts\python.exe -m nba_recorder record --db data/acceptance.sqlite --minutes 30 --force-reconnect-seconds 180
```

- Session：`c40c0dda-0dcf-4e92-bc9b-6c47efa83c10`。
- 开始：13:00:32.740；结束：13:30:34.043。
- 运行 **1801.303 秒（30 分 1.303 秒）**，正常退出，exit code 0。
- 10 次连接，**9 次主动断线及恢复**，每次重新获取四个资产的完整盘口。
- 590 次完整盘口验证；首尾验证跨度 1784.915 秒。运行时长含重连间隙，不能解释为无缺口的逐笔历史；结束前最后一段静态盘口没有新事件也不会伪造事件。
- 保存 509 条标准化快照；唯一事件含 book 34、price_change 177、rest_snapshot 121。
- 另保存 351 条公共流 new_market 消息，其中可能包含未订阅比赛的全局新市场通知，不能将这些消息算作 NBA 盘口更新。
- 重复 WS 事件跳过 6 次；可识别的相同 payload 重复写入为 0。
- 真实样本 crossed/locked/empty 均为 0，未标记 BBO 异常为 0。异常状态的标记与原值保留由合成自动化测试覆盖。

原始证据：`validation-30m.json`、`acceptance-live.log`、`data/acceptance.sqlite`。

## 重启续采与最终代码验证

用同一数据库再运行 `--minutes 0.5`：

- 新 Session：`5c0ae35e-001d-46ec-a8df-4e2bc915718b`。
- 13:30:47.758 至 13:31:18.119，运行 30.361 秒，正常退出。
- 启动后重新获取盘口，30 次验证，原始记录保留并追加新事件。
- 最终共 **545 条标准化快照，4 个资产**；唯一 book 38、price_change 190、rest_snapshot 127，new_market 351。
- 545/545 条快照均具备 exchange timestamp、local receive timestamp、market ID、asset ID、event type 及 depth 字段。
- 原始事件主键重复为 0；有交易所 timestamp 的相同 source/payload 重复为 0；SQLite integrity_check 为 `ok`。
- 停止后只读 show 正确显示 disconnected，随后按年龄标 stale，不把已停止采集的盘口伪装为实时数据。

27 项自动化测试全部通过，结果见 `test-results.log`。覆盖真实接口之外的断线模拟、初始化前增量、乱序、错误快照、缺失 asset、非有限数值、tick/minimum size、锁盘/交叉盘/空盘口、重启去重、已结算状态恢复、分页/无市场与网络错误区分、v2 ID 映射，以及交易/资金/US HTTP 路由拒绝。没有运行任何账户或交易集成测试。

长时间运行期间补充了异常输入校验及结算状态恢复防御；最终源码通过全部 27 项自动化测试，并由上述 30 秒真实重启运行覆盖。最终新增的异常分支没有另做一次完整 30 分钟耐久运行。

最终证据：`validation.json`、`acceptance-restart.log`。核验命令：

```powershell
.\.venv\Scripts\python.exe scripts/validate_recording.py --db data/acceptance.sqlite
.\.venv\Scripts\python.exe -m nba_recorder show --db data/acceptance.sqlite --limit 2 --depth 5
```

## 验收对应与风险

- A：发现并持续跟踪两场真实 NBA 常规赛赛前 Moneyline；无市场与接口失败的不同输出有测试覆盖。
- B：真实采集运行超过 30 分钟，9 次主动断线恢复。没有声称记录了断线间隙的所有中间状态。
- C：原始事件和快照含 exchange/local receive timestamp；快照含 market/asset ID、bid/ask、完整 depth、event_type。错误事件的旧盘口时间另行标记。
- D：真实样本正常；锁盘、交叉盘、空盘口用自动化测试验证，保留原值，不静默纠正。
- E：同库重启成功，跨重启去重与历史保留通过测试；重复确认只更新最新盘口视图及健康日志。
- F：采集器只允许公共 GET 和 market 订阅/PING，没有账户、签名、下单或资金路径。

尚存风险：公开流无连续序号/回放保证，不能补齐断线缺口；每个增量后重新取快照会增加 REST 请求量；赛季筛选依赖官方联赛元数据和日历，未逐场核验赛程 ID；真实网络验收只覆盖 v1，v2 仅有映射测试。API、元数据及日历变化需复核。所有数据仅适合明确处理质量标记与缺口的 Record-only 回测。
