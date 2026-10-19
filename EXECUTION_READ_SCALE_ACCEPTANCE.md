# 非比赛数据依赖项补充验收（2026-10-19）

本轮只做离线研究、执行格式准备和账户读取契约。没有签名器、真实交易客户端、真实下单/撤单/资金操作；Live NO-GO。21日采集计划没有修改。本报告不覆盖真实比赛策略有效性。

## 完成内容

- `research/scale.py`：可复现合成负载，显式标签、双资产快照、唯一公开打印、阶段边界；合成阶段拉伸不是NBA比赛时钟。
- `inplay/observations.py`、`inplay/report.py`、`research/review.py`：紧凑价格索引、流式窗口读取；复盘Markout按目标时点以前的新鲜观察二分查找，最新坏盘口会使估值未知。`inplay/session.py`的L0不再逐点扫描已终结历史订单，撤单中及未知订单仍参与检查，L1/L2晚到成交逻辑保留。
- `research/engine.py`：九组同源封存流、每组独立账本，记录墙钟/CPU时间、进程生命周期峰值RSS、数据库及导出大小。`research/jobs.py`可把已有CLI结果登记进网页，不复制或修改输入事件。
- `execution/normalization.py`：统一SDK、原始CLOB REST、用户回报的数量/时间戳/订单状态/成交归属映射；部分撤单成功按订单区分；改单仅为“撤单后核对再报价”不可发送描述。没有实现签名HTTP订单。
- `execution/account.py`：公开持仓默认Data API v2不透明游标、current_size；显式v1兼容。公开数据缺认证现金/订单/自身成交，保持null，不冒充完整账户。
- `execution/read_account.py`：未配置真实transport的只读接口及明确Mock实现；余额固定6位单位、订单/成交分页、自身Maker数量、结算状态更新、SQLite去重与重启恢复；独立比较现金/仓位/订单，只报告差异、不自动恢复交易，也不修改OMS库存。
- `/offline`新增Mock只读演示；完整回放/报价/成交/库存图继续复用现有页面。

## 运行方法

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m nba_recorder.execution account-mock --journal data/account-read.sqlite
.\.venv\Scripts\python.exe -m nba_recorder.execution account --address 0x1111111111111111111111111111111111111111
.\.venv\Scripts\python.exe -m nba_recorder.execution preview --file examples/unsigned-order-preview.json
.\.venv\Scripts\python.exe -m nba_recorder.research run --synthetic --synthetic-seconds 14400 --split-shares 5 --out data/research-scale
.\.venv\Scripts\python.exe -m nba_recorder.console --no-browser
```

输出目录需全新。示例地址为公开占位地址，不是用户已连接账户。Mock数值全为fixture。四小时是虚拟输入跨度，回放没有等待四小时墙钟。9组为顺序运行，各组资金不可相加。

## 自动化与实际连通性

完整回归 **315 tests，33.434秒，OK**，见`evidence/execution-read-scale/full-tests.txt`。覆盖原有风险/撤单竞态/重启测试，以及新增官方字段契约、v2分页与当前持仓数量、Maker自身成交、重复确认/失败结算、Mock重启去重、网页HTTP演示、确定性负载、登记与绘图。

初次新增契约测试受沙箱临时目录权限影响有3个WinError5；原失败日志保留为`initial-sandbox-test-errors.txt`，授权后12项通过；最终315项回归无失败。负载首次基线运行发现L0历史订单扫描热点后主动中断，未生成完整比较，不能作通过证据。优化后独立重跑，结果见下节。

2026-10-19公开官方文档/规格GET成功，URL与SHA256见`official-spec-index.json`，可运行`scripts/fetch_execution_specs.py`复查。[Data API迁移文档](https://docs.polymarket.com/migrate/data-api-v1-to-v2)、[订单管理](https://docs.polymarket.com/trading/manage-orders)、[用户回报](https://docs.polymarket.com/trading/realtime-order-updates)、[钱包活动](https://docs.polymarket.com/trading/wallet-activity)是本轮字段依据。Data API v2与市场Protocol V2是两个版本维度，不据一个推断另一个。

公开v2实际请求：零地址返回HTTP400，服务明确拒绝协议合约地址；换用上述公开占位地址返回HTTP200、1页、0持仓、分页完整。记录见`public-v2-probe.json`；它只证明公共接口/空持仓响应可读，非空分页由Mock测试验证，**没有证明真实认证账户接通**。网页Mock演示证据见`account-mock-browser.json/.png`，现金100、买单累计成交2/10、仅一笔确认成交，全部为合成。

## 整场规模负载结果

九组全部完成；每组14,401个输入回调、28,802个资产帧，虚拟跨度14,400秒。L0/L1/L2各自运行1/2/3美分距中点方案，预算100、明确现金构造5对完整份额。逐行输入SHA256在九组完全一致；全部shutdown_ledger_consistent与账户reconciled为true。独立校验equity=cash+market_value、net_pnl=equity-capital_contributions，全部通过。见`scale-summary.json`；可用`scripts/summarize_scale_evidence.py`对本地完整结果重新核对。

总墙钟1,689.373秒（28.16分钟）、CPU1,615.672秒；进程生命周期峰值RSS1,154,416,640字节（约1.08 GiB）；比较文件写入前输出9,781,822,018字节（约9.11 GiB）。单组约168–204秒。性能是本机一次合成负载实测，非交易延迟、吞吐SLA或任意规模保证。

L0三组模拟成交均为0，L1/L2六组各238个模拟成交事件。该固定盘口及受控打印夹具让L1与L2结果相同，不能据此推断真实两种模型一致；原有队列与部分成交用例另行覆盖模型差异。L1/L2每组300秒Fill Markout有235项OBSERVED、3项PENDING、0项UNKNOWN；末尾不补未来数据。Quote与Fill Markout分别导出，不加到PnL；所有净PnL仅为合成研究值，没有真实盈利含义。

本地完整结果：`data/research/e43e28bd1f604cde9faaf5e9075314bc/`，网页[四小时结果](http://127.0.0.1:8765/offline#e43e28bd1f604cde9faaf5e9075314bc)。网页已实际打开九组，选择p0-L1-d0检查图及导出；绘图HTTP200，响应约2.273秒、790,917字节，从28,802点投影到1,922点，覆盖完整四小时、238笔模拟成交。抽样前建立真实连续片段，避免把抽样间隔当作断档；坏数据和实际间断仍断线。见`full-window-plot.json`及`full-window-browser.png`。

本次长负载记录起始HEAD为2e6ebd5且working_tree_dirty=true，使用本轮未提交的负载/报告优化代码；没有将旧HEAD说成已经包含这些实现。后续网页抽样修复不改变回放事件或模拟账本，已通过最终回归与上述实际绘图验证。大数据库和导出保留本机、未上传GitHub，仓库只提交有界证据和可运行代码。

## 仍然依赖数据或接入的信息

1. 真实整场输入覆盖、赛中时钟/暂停/加时、成交方向与深度突发，需要21日及后续采集验证。合成4小时测试不提供统计显著性或真实成交模型校准。
2. 真实队列位置、成交概率、撤单/网络延迟、手续费和奖励尚未知；Paper虚拟时间延迟不可叫真实交易延迟。
3. 真实认证读取transport、SDK账户对象、用户WebSocket、非空真实持仓与订单核对仍未联调。接口、Mock和规范映射已完成，账号接通未完成。
4. 无签名、授权、真实HTTP订单/撤单发送器；真实执行仍是后续独立审批项目。当前完全不能实盘交易。
5. 整场导出仍较大，完整订单/决策/绘图分析会占用内存；有限规模通过不意味着任意天数/并发场数可无限扩展。

就绪边界：可继续采集真实输入、运行Paper和可追溯离线研究；真实比赛有效性与认证账户尚待验证。发送权限保持关闭。
