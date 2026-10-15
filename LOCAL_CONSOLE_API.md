# v2.2 本地操作台 API 与单写者边界

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

允许 action：scan、start、pause、stop、reconcile、resume、export。start另带 mode、market_id；mode仅 OBSERVATION / REAL_PAPER / SYNTHETIC_DEMO。比赛必须来自60秒内真实 scanner 结果，不能由HTTP提供盘口或伪造市场结构。离线回放首版网页显式禁用，保留原CLI。

所有命令经过同一个有界串行队列，带预期会话与状态版本。相同request_id/相同payload只作用一次；同ID不同内容拒绝。两个不同ID的重复启动也不会创建第二个Runner。请求记录与结果写 `commands.jsonl`，运行中的控制结果还进入 `pe_journal`。幂等回执缓存只在当前服务进程有效；重启后令牌改变，旧请求不得自动重发，历史命令日志保留审计用途。

pause：禁止新增并请求撤单，状态按原风控进入PAUSED或保持更严格状态；不出售库存。stop：禁止新增、kill并请求撤单，先回执“请求已生效”，随后界面必须等STOPPED并检查未确认订单/库存。清理期间禁止恢复。reconcile：调用当前owner的权威Paper核对，不自动resume。resume：数据、阶段、epoch、watchdog、账户与原有恢复条件全部通过才可恢复；观察模式不能升级，UNKNOWN/MM_CLOSED不能跳过。网页不写入/续期人工阶段确认。

每次明确启动是独立Paper研究会话，资金初始化和会话ID可审计；旧账本不清除、旧库存不转移，新会话不能被解释为旧仓位已退出。旧文件位于 `data/console/<session_id>.sqlite`，结果包在同目录。当前网页展示一个会话，不是跨会话组合账户管理平台。

## 传输与故障边界

feed、watchdog和最终dispatch共用实时授权：保留observed_connected和原始raw，connected是当前许可；连接epoch不匹配、排队旧会话消息、watchdog退出/取消/心跳停滞均不能重新授权。迟到公开成交仍按原exchange时点判断原挂单，不为保持零库存丢弃成交。

前端每秒读取展示投影，后端行情/成交/订单事件不合并丢弃。页面关闭/刷新不影响owner的watchdog。HTTP失败或owner心跳超过4秒，旧盘口变灰、显示离线并禁用命令；失联不等于已经撤单。异常重启不自动恢复账户写者，Paper账户原日志保留，需要独立核对；本版UI不提供旧会话恢复向导。

跨端口实例也受仓库级操作台文件锁保护，每个Paper DB继续保留原有跨进程写锁。重复双击默认入口只打开已存在且健康的同仓库服务；端口被别的应用占用、owner不健康、目录不可写时给中文错误，不启动第二个写者。
