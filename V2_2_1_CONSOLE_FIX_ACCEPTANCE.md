# v2.2.1 操作台两项修复验收

日期：2026-10-16。基线 HEAD：`66b4ebd72e3e7b33a7a5973f604cbf9c790cf65a`，开始时工作区干净。保留原 Python、单 owner、BookSession、Paper OMS、FIFO 与全部原215项测试。无真实订单、签名或资金接口。

## 本轮修改

- `nba_recorder/api.py`、`console/service.py`：网页 CURRENT 默认扫描过去1天至未来7天的全场 NBA Moneyline，覆盖 scheduled/live；HISTORY 为过去7天只读。使用上游比赛时间过滤并本地复核，分页上限500条，不回退无界历史。历史、结束和失效市场禁止新启动。刷新不更换已绑定比赛或重建资本。
- `console/static/`：范围选择、赛前/进行中/结束/未知、market ID、发现时间与窗口。真实会话固定1800秒，显示剩余时间与到时停止/撤单/核对/保持 HALTED 的动作；强调旧库存保留旧账本。合成演示改为每秒一帧，便于人工操作验证，策略及虚拟事件流未改。
- `inplay/runner.py`：操作员开关不再修改 connected。运行证据分列 physical_connected、epoch_matches、watchdog_alive、quotes_enabled、account_new_risk；connected 仍要求观测连接、当前连接、epoch 和 watchdog 全部通过。
- `inplay/reference.py`：book_data_risk 检查盘口质量；book_risk 在此基础上联合操作员、阶段、体育源许可。估值、报告、Touch 代理使用质量检查。暂停仍阻止最终提交，但自身不会触发数据质量硬熔断。
- `console/service.py`：恢复在同一 owner 内同步检查双资产、阶段、数据与账户，再开启开关；无临时开放或 await。断线、UNKNOWN、过期、关闭阶段及原提交边界保护保留。

比赛时间过滤参数依据[官方 Public SDK](https://github.com/Polymarket/py-sdk/blob/main/src/polymarket/clients/public.py)的 start_time_min/max；只使用 Polymarket.com 公共 GET，不混用 US，不导入交易客户端。

## 测试与证据

命令：`.\.venv\Scripts\python.exe -m unittest discover -s tests -v`。最终 **223 PASS / 0 FAIL**，原215项未删除或修改，新增 `tests/test_v221_console.py` 八项。

- [来源核验](evidence/v221/review-source-verification.json)：外部证据包的SHA256全部匹配，四个方法 AST 与基线Git对象一致；外部提取方法测试不被当作完整项目验收。
- [修复前](evidence/v221/before-tests.txt)：首批4项完整组件回归中2项失败，分别为 catalog 丢失 live、暂停后估值为 None。另2项通过。最终扩大为8项独立测试，避免负向故障间状态串扰。
- [修复后新增回归](evidence/v221/after-tests.txt)与[完整日志](evidence/v221/full-tests.txt)：真实 OMS/Adapter/FIFO 的非零库存暂停→watchdog→撤单确认→实际核对→更多watchdog→明确恢复；无新增提交；迟到的撤单前部分成交更新现金/库存；断线、UNKNOWN、陈旧盘口、MM_CLOSED 分别拒绝恢复；最终 dispatch 再检查操作员开关。原 disconnect→queued trade→before watchdog 测试仍通过。

## 实际浏览器验证（明确合成）

同一会话 `3233a95a06ee47a382e629ac032c345d`：通过网页启动 SYNTHETIC_DEMO、暂停、核对、人工恢复、停止、导出，全部取得 APPLIED 后台回执。使用实际服务、Paper OMS 与持久账本，没有修改/伪造风险状态。

- [命令回执](evidence/v221/browser-command-receipts.json)、[会话导出](evidence/v221/browser-session-export.json)。导出记录的是基线 commit 加 dirty 工作区，不能把基线哈希当作已包含修复的提交。
- [暂停区间审计](evidence/v221/browser-pause-audit.json)：暂停回执至恢复回执间，84次 watchdog、93次有效权益记账、0次新提交、0次硬熔断。持仓 a=5、b=5，现金95，权益100，Paper PnL=0；恢复前 READY_FOR_MANUAL_RESET，明确恢复后 ACTIVE。
- [核对后截图](evidence/v221/browser-ready.jpg)、[恢复截图](evidence/v221/browser-resumed.jpg)。最终停止仍 HALTED，残余库存 a=5、b=5，未宣称清仓。
- [独立只读账本复核](evidence/v221/browser-ledger-audit.json)：`scripts/validate_inplay.py` 检查 authority/OMS/FIFO 一致、SQLite integrity=ok、真实提交次数0。合成演示不是实盘或真实赛中验证。

## 真实数据与未验证项

[真实网页扫描结果](evidence/v221/real-catalog.json)：CURRENT / READY，15个市场，均为赛前；有实际窗口、市场/资产身份、发现时间。未取得真实进行中 NBA 样本，所以真实在赛 discovery 与全场报价/成交/退出闭环仍未验收。发现 live 的软件链由合成 fixture 验证，不能代替真实比赛。

自动体育阶段契约未通过真实赛中验证；人工阶段在网页只读、最多15秒且不自动续期。未重新运行30分钟采集或全场覆盖；1800秒预算不构成完整比赛覆盖承诺。队列、模拟成交概率和真实执行延迟仍未校准。本轮不要求也不报告非零盈利。

用户仍是首次 `install-console.cmd`、日常 `start-console.cmd`。**受控离线 Paper GO；真实数据限定窗口观察 GO；真实报价闭环 NOT_VERIFIED；无人值守和 Live NO-GO。**
