# 10月21日采集准备

2026-10-19检查：旧19日Windows任务禁用；`NBA-MM-Paper-2026-10-21`已注册，下一次触发为北京时间2026-10-20 23:00。注册脚本 `scripts/install_unattended_task.ps1 -Date 2026-10-21`。当前是预检和预约，不冒称比赛采集已开始。

公开Gamma发现3个市场：Celtics/Pistons（5676016，21日03:00）、76ers/Knicks（5676021，07:00）、Thunder/Spurs（5680507，09:30），均为北京时间和Polymarket标注，未得到独立官方赛程验证，不保证即为当日全部比赛。原始发现结果在本机data/unattended/2026-10-21/discovery.jsonl，后续每120秒继续发现和重新确认身份。对应采集最早开始02:00、06:00、08:30；时间变化依据实时发现与重新验证，不按这份清单强行绑定。

6个资产的公开盘口GET均成功，asset/condition身份一致且双边有档位。证据见evidence/unattended-oct21/book-preflight.json；这不是整场WS连续性或未来服务可用性保证。4项调度回归复跑通过，此次没有改动Python运行策略。

重要限制：NBA官方赛程返回403，3个市场season_type仍UNKNOWN；无人值守paper_allowed按已有测试拒绝UNKNOWN类别，因此若开赛前仍无法核实，将只读采集，不创建Paper订单。赛中阶段不可靠同样NO_QUOTE，不手工伪造时钟，不自动解除HALTED/UNKNOWN。每场独立25虚拟美元；缺可信结束信号到硬上限标记不完整。其余恢复、尾录与存储行为见UNATTENDED_OCT19.md，日期改用21日目录。

须保持当前Windows用户登录、开机联网、不休眠。当前任务是READY待触发；不等于RUNNING。Live始终NO-GO。
