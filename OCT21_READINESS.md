# 10月21日采集准备

2026-10-19检查：旧19日Windows任务禁用；`NBA-MM-Paper-2026-10-21`已注册，下一次触发为北京时间2026-10-20 23:00。注册脚本 `scripts/install_unattended_task.ps1 -Date 2026-10-21`。当前是预检和预约，不冒称比赛采集已开始。

公开Gamma发现3个市场：Celtics/Pistons（5676016，21日03:00）、76ers/Knicks（5676021，07:00）、Thunder/Spurs（5680507，09:30），均为北京时间。2026-10-19已用NBA官方开幕赛公告和独立比赛页核对球队、主客场、精确UTC时间与常规赛类别，三者均REGULAR_SEASON、Paper准入通过。原始发现结果在本机data/unattended/2026-10-21/discovery.jsonl；官方JSON仍403时仅复用这三场精确匹配的已核验证据，不把日期范围内所有比赛猜成常规赛。后续每120秒继续发现和重新确认身份。对应采集最早开始02:00、06:00、08:30；改期或身份/类别冲突必须重新核实。

三场各30秒真实公开输入联测通过：市场WS与体育WS各连接1次，收到book快照、price_change或REST快照，REST错误0；三个隔离Paper账户账本一致、实际赛前模拟订单0。完整结果及本轮回归见OPENING_MARKET_ACCEPTANCE.md和evidence/opening-markets/。这不是整场WS连续性或未来服务可用性保证。

结论：这三场的UNKNOWN分类阻塞已解决，已具备采集与自动研究Paper的准入条件。赛前仍NO_QUOTE；只有当时真实行情及身份匹配的体育更新满足现有阶段/风控条件才生成模拟订单，不保证必然有报价或成交。未知/冲突/改期仍拒绝Paper；不手工伪造时钟、不自动解除HALTED/UNKNOWN。每场独立25虚拟美元；缺可信结束信号到硬上限标记不完整。官方完整赛程镜像、真实整场覆盖和实盘执行均未验收。其余恢复、尾录与存储行为见UNATTENDED_OCT19.md。

须保持当前Windows用户登录、开机联网、不休眠。当前任务是READY待触发；不等于RUNNING。Live始终NO-GO。
