# Polymarket NBA 做市研究工作台

Python + SQLite + 原生本地网页，复用单 Runner / Paper OMS / FIFO。主线为 BOOK_DRIVEN 盘口驱动研究，不需要博彩赔率 key。盘口中间价不是预测胜率。仅支持 Polymarket.com，本轮没有真实订单、钱包签名或资金操作。

## 日常操作

1. 首次双击 `install-console.cmd`；以后双击 `start-console.cmd`，打开 http://127.0.0.1:8765 。更新前停止会话并关闭旧后台，再重新启动。
2. 刷新并选择比赛、观察或Paper、本场虚拟预算及方案版本。SMALL/WIDE是内置预设；可在方案库另存或修订跨场方案，启动绑定明确版本，保存不修改正在运行的账户。未知类别的Paper须明确勾选批准；后台重验单场身份及全场规则。
3. 在02页确认研究阶段和当前节次，或进入减仓/关闭做市。MANUAL_RESEARCH是有界本场人工指令，不是上游事实；需要操作者推进，不外推精确倒计时。
4. 同场观察可明确开始Paper，沿用同一账户及记录，并写执行段起点；普通恢复不会偷偷升级模式。
5. 查看库存、活动预留、单张价格/执行图和公开成交分布。点击“刷新库存与退出研究”看FIFO、1/3/10/30秒Markout、逐档退出容量和费用情景。公开与Paper成交分开。
6. 可保存报价距离、份数、最大挂单寿命的版本，不重置资金。Maker退出默认保留；Paper主动退出需另行确认份数、最低卖价、研究费率。存在活动单时先等撤单确认，再重新确认退出；没有成交的库存继续留账。
7. 生成赛中卡、赛后初报或核对更新。有效第四节阶段触发一次自动报告；人工生成不限于第四节。JSON/Markdown另存不可覆盖版本，后到数据只进入新报告。
8. 暂停报价、停止Paper但继续记录、停止全部采集含义不同。停止不等于清仓。历史索引可查看旧图、浏览器本地标注和旧报告，不影响当前Runner，不合并不同账户资金。
9. 七类统计卡展示库存增长、成交质量、退出容量、报价机会时间、周转、公开流动性和资金/覆盖情况。统计是只读诊断，不授予订单权限。准备方向研究会停止做市、等待撤单和核对，剩余仓位保留，HALTED不自动恢复。
10. 在报告列表打开原版或对照后续价格路径；原版哈希与输入截止保持不变。手填“第三节结束”仅是生成报告时的备注，不修改比赛阶段、不回填成当时已知信息。

## 运行与测试

新增 [10月21日真实行情图表与独立事后 Paper](OCT21_POSTHOC_STUDY.md)：每场三张图；保留原解析零订单对照，并对当时已附带的原始体育消息进行可追溯的修正解析，分别运行 Touch / Trade-through / Queue-aware。网页入口 `/day-study/2026-10-21/`，原记录不修改，模拟成交与盈利均不代表真实执行。

新增 [离线回放与执行准备](OFFLINE_RESEARCH.md)：打开 http://127.0.0.1:8765/offline ，扫描真实/合成样本，冻结数据并运行九组/27组Paper实验，查看共用时间轴与导出。真实执行仅有无签名映射和独立Mock，发送继续禁用。

补充执行回报映射、公开 Data API v2 持仓、认证只读接口 Mock 与整场规模合成回放；实际测试与性能证据见 [本轮验收](EXECUTION_READ_SCALE_ACCEPTANCE.md)。认证真实账户尚未接通，Live 仍为 NO-GO。

新增 [比赛日数据验收与自动复盘](POSTCAPTURE_ACCEPTANCE.md)：无人值守采集结束后独立生成不可覆盖的快照报告，检查新鲜盘口覆盖、Paper账本与未决订单，输出10/30/60/300秒模拟成交Markout；在 `/offline` 第5节查看并下载。比赛尚未记录时明确显示等待，不生成比赛成绩。

最新预约见 [10月21日准备状态](OCT21_READINESS.md)；通用无人值守行为与限制见 [原部署说明](UNATTENDED_OCT19.md)。19日任务已禁用，21日任务独立于网页当前单场会话，仅公开采集和Paper。

21日三个开幕赛市场的UNKNOWN分类已修复，依据精确匹配的NBA官方比赛证据，均为常规赛且Paper准入通过；真实公开行情30秒联测、条件与限制见 [分类修复验收](OPENING_MARKET_ACCEPTANCE.md)。赛前及实时条件不足时仍NO_QUOTE，不保证必然报价或成交。

Python 3.12，固定依赖见 requirements.txt；无需Node构建。

```powershell
.\.venv\Scripts\python.exe -m nba_recorder.console --no-browser
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts/validate_next_iteration.py
.\.venv\Scripts\python.exe scripts/smoke_next_console.py
.\.venv\Scripts\python.exe scripts/validate_workbench_completion.py
.\.venv\Scripts\python.exe scripts/stress_workbench_completion.py
```

HTTP验收命令仅在操作台空闲时运行，使用明确合成Paper会话。压力脚本使用独立数据库，包含短墙钟故障演练和四小时虚拟时间、每秒双资产快照回放；不是四小时真实采集或浏览器长跑，数据库约500MB。完整数据和旧账户保留于 data/console；图表导出仅含已加载范围，has_more表示尚未完整。

## 结构与验收

- discovery.py：比赛发现、身份和规则证据。
- inplay/：研究阶段、盘口报价、统一Runner/OMS/FIFO、Paper主动退出。
- paper_execution/：持久化模拟交易所、订单状态、风险和核对。
- fill_management/：FIFO、深度计算和历史研究兼容模块。
- console/：本地网页、只读历史、共同统计和版本化报告。

[最新补齐验收](WORKBENCH_COMPLETION_ACCEPTANCE.md) · [前轮验收](NEXT_ITERATION_ACCEPTANCE.md) · [D01—D12调整](DEFENSIVE_GUARD_CHANGES.md) · [真实执行下一里程碑](LIVE_EXECUTION_MILESTONE.md) · [历史课程与CLI](README_HISTORY.md)

当前已保存10月21日3场、10月22日11场真实公开行情，并有独立事后Paper、合成故障与本机HTTP证据；整场真实Paper执行、浏览器四小时负载、真实成交概率仍未验收。10月21日原运行均为零订单；修正解析后的事后模拟不能回写成当日执行。旧严格模式及EXTERNAL_FAIR保留兼容对照。Paper PnL不代表真实盈利，Live没有实现或授权。


[10月22日11场独立事后回放](OCT22_POSTHOC_STUDY.md)：原账户只读、零订单；独立研究按相同25虚拟美元/5份/中间价两侧2¢参数运行三种成交代理。期末胜方1/负方0估值单独输出，不改写原账本或历史报价，不代表真实盈利。


[后续比赛连续自动采集](CONTINUOUS_RECORDING.md)：Windows常驻任务自动衔接比赛日，赛前30分钟至确认赛后30分钟，只读保存公开行情；控制台后台维护，每小时检查故障与完成状态。

独立盘中策略回放（2¢ 主参数、现金抵押建立两侧初始库存、Q4 只减仓）：[运行规则与研究边界](INPLAY_ROTATION_STUDY.md)。保留旧结果，不启用真实交易。

**10月24日采集故障：**12个已发现市场均因旧常驻进程与新策略配置不兼容而中途停止，仅保留约42–50分钟片段，不能视为完整比赛数据。兼容修复及354项回归已通过；详情与逐场证据见[故障记录](OCT24_RECORDING_INCIDENT.md)。

[数据保留与压缩工作流程](STORAGE_WORKFLOW.md)：完成研究后保留网页、图表、参数、汇总及共享输入，对详细 Paper 数据库、旧导出和退役扫描日志做无损透明压缩，逐文件核对 SHA-256。新离线实验共用压缩输入池，各模型保留独立订单与账本；原始采集库不迁移、不删除。

[10月26日存储验收](STORAGE_ACCEPTANCE_20261026.md)：232 个历史文件压缩校验通过，释放 97.07 GiB；382 项测试通过，旧网页和审计读取保持可用。

[第二轮采集库与 LZX 压缩验收](STORAGE_PHASE2_20261026.md)：332 个文件无损处理，额外释放 89.71 GiB，数据实际占用约 43.31 GiB；388 项测试通过。已停止采集库显式选择，原始数据不删除，暂不迁移；大库图表入口改为轻量只读身份校验。


### 2026-10-28 采集恢复修复

WebSocket 建连阶段断线清理兼容、准确匹配的 sports ended/closed 结束判定与部署限制，见 [COLLECTION_RECOVERY_FIX.md](COLLECTION_RECOVERY_FIX.md)。保持只读；正在采集的进程不热重载。
