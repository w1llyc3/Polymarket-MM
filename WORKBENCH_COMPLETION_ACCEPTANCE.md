# 研究工作台补齐验收

2026-10-17，基于dfa52ac继续开发。仅Paper / Record-only，没有真实提交、签名或资金操作。

## 补齐范围与代码

- `console/plans.py`：跨场方案库、不可覆盖版本、乐观版本校验、启动精确绑定。预算单独明确设置；保存方案不重置账户、不热改活动策略。
- `console/insights.py`、`research.py`、`static/research-ui.js`：七类紧凑监控，包括库存增长、FIFO完成周转、持仓时间、分侧Markout样本、退出容量、取消原因、公开流动性迁移、资金阶段和系统案例。未知费用、过期与不足样本明确标识。
- Quote Availability分母为有效决策机会与ACTIVE权限窗口的交集，分子为其中已接受挂单时间；是可观测决策机会估计，不是所有理论可报价时刻。BBO记录窗口覆盖率不是整场覆盖率。
- `history.py`：历史残余持仓、现金、风险、核对和结束状态；末次盘口后撤单回报仍纳入时间线。读取历史不创建账户或新增资本。
- 原始报告独占版本保存，JSON/Markdown下载；后续路径对照保留原版SHA256，原始与后续信息分开。历史读取当时参数和输入游标，禁止未来数据回填。手填节次仅是生成时备注。
- `service.py`：明确准备方向研究指令，停止Paper做市、继续记录，等待撤单和权威Paper核对，展示残余仓位并保持HALTED；不创建方向订单。STOPPING拒绝新研究操作。
- 提交边界按最坏方向累计同资产买入与互补卖出承诺，不以相反挂单抵消风险。OMS活动订单索引优化保留迟到终态成交、恢复、核对与完整审计。

## 测试与运行证据

全量 `python -m unittest discover -s tests -v`：**286项通过**，21.821秒（原274项保留、新增12项）。见 [tests.txt](evidence/workbench-completion/tests.txt)。新增覆盖版本/资本边界、机会时间、历史参数截止、原报告不变、历史只读、方向准备、STOPPING、互补挂单风险、末次盘口后撤单确认；原UNKNOWN、重启、重复成交、撤单竞态和账本测试继续通过。

四个前端脚本通过node --check，git diff --check无空白错误。开发夹具L1/L2断言曾失败，导入测试类也曾产生重复执行；已修正，未删除保护测试。[原失败日志](evidence/workbench-completion/fixture-failure.txt)保留，其中300次执行不是300项独立测试。

[HTTP验收](evidence/workbench-completion/http-acceptance.json)：最终代码在空闲本机控制台使用SYNTHETIC_DEMO，方案v1精确绑定且v2不污染、25虚拟预算、暂停/核对/人工恢复、方向准备HALTED、历史只读、报告哈希不变、JSON/Markdown下载、最终STOPPED均通过，真实提交0。复跑 `scripts/validate_workbench_completion.py`，脚本拒绝打断活动会话。

[密集回放](evidence/workbench-completion/dense-four-hours.json)：四小时虚拟时间、14,401个合成时间点、每秒双资产快照、948个Paper订单，账本一致、真实提交0。实际151.26秒、CPU143.95秒，数据库520,015,872字节，统计/报告各约4.13秒。图表133页全部读完，末页has_more=false；服务端首批约23ms、最慢约32ms，不是浏览器渲染时间。

[墙钟演练](evidence/workbench-completion/wall-drill.json)：约10秒、40次watchdog，断流、暂停、明确人工恢复通过，暂停期间提交不增加，账本一致；不是四小时墙钟测试。压力脚本为 `scripts/stress_workbench_completion.py`。压力证据标明基准提交和dirty工作区，生成于补齐开发中；随后小范围历史/风险修正由最终回归覆盖，不冒称最终提交干净长跑。

## 剩余问题和准入

本轮没有新增整场真实NBA比赛样本，也未证明官方赛程访问限制解除。公开行情、模拟成交、人工阶段与源事实继续分开。L0/L1/L2代理不能证明真实成交概率、队列、交易延迟或实盘盈利。

- P0实盘：真实执行适配器、认证自身订单流、独立真实账户核对和授权未完成；Live NO-GO。
- P1验证：整场真实比赛闭环、四小时真实墙钟采集、浏览器长时渲染/内存/交互待验收。CUA两次focus/navigation超时；HTTP成功不能代替视觉验收。
- P2性能：四小时数据库约500MB、全量统计约4秒；默认5秒刷新有单请求防重入，大样本仍需增量统计/缓存和保留策略。案例/异常摘要有界且标明总数/完整性，完整journal保留。

Data沿用既有受限公开采集能力，本轮不扩大真实覆盖结论；Paper合成/Mock/本机HTTP受控GO，真实整场NOT_VERIFIED；Live NO-GO。软件缺口按以上范围补齐，外部数据及长时浏览器验证不能以模拟代替。
