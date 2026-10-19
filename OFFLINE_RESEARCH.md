# 离线研究与执行准备

本轮复用inplay/tape、BookSession、OMS、FIFO、共享分析器，增加样本目录、一键封存回放、批量实验和复盘页面。没有连接签名器，没有真实订单发送路径；Limited Live仍NO-GO。

## 网页操作

启动现有控制台后打开 http://127.0.0.1:8765/offline ，或首页“05 离线回放与执行准备”。

1. 扫描本地样本，查看来源、时间、PRE_GAME/SYNTHETIC/未知阶段；记录数量包括watchdog，不能当作独立行情样本量。已结束的采集会话无需转换即可出现在动态目录。
2. 选择注册会话或明确合成数据；设置每组虚拟资金，默认不注入库存。若填写完整份额数量，会显式从现金扣除$1/对、生成双边份额，不是免费库存。
3. 网页默认明确选择真实样本前180秒，避免误启动整场大实验；可以修改窗口。冻结后的所有实验读取同一个哈希封存tape，不修改源账户、时间戳、赛事证据或原报告。保留原观察/人工暂停等权限；缺阶段仍NO_QUOTE，不以合成阶段填充真实样本。
4. 运行L0/L1/L2乘1/2/3美分距中点的九组实验，或勾选队列假设运行27组。各组资金独立。当前BOOK_DRIVEN没有外部公允值，距中点距离不是Gross Edge。
5. 比较模拟成交、资金与库存；选择实验组，在共用时间轴查看中点、目标报价、已接受挂单、模拟成交、库存、阶段与核对事件。图上投影明确显示总量/抽样数；完整依据在导出中。
6. 导出CSV/JSON、输入/订单journal、决策、成交、Quote Markout、Fill Markout、退出分析、逐单校准诊断。地址栏保留任务ID，刷新可查看已完成结果。离线worker不操作采集Runner；若服务退出中断实验，需要重新创建实验，不作为完成结果。

## CLI

```powershell
.\.venv\Scripts\python.exe -m nba_recorder.research catalog
.\.venv\Scripts\python.exe -m nba_recorder.research run --synthetic --out data/research-demo --split-shares 5
.\.venv\Scripts\python.exe -m nba_recorder.research run --sample <目录中的样本ID> --seconds 180 --out data/research-recorded
.\.venv\Scripts\python.exe -m nba_recorder.research run --synthetic --sensitivity --out data/research-queue --split-shares 5
.\.venv\Scripts\python.exe -m nba_recorder.research run --synthetic --profiles examples/research-profiles.json --out data/research-profiles --split-shares 5
.\.venv\Scripts\python.exe -m nba_recorder.execution mock-demo --out data/execution-mock
.\.venv\Scripts\python.exe -m nba_recorder.execution preview --file examples/unsigned-order-preview.json
.\.venv\Scripts\python.exe -m nba_recorder.execution account --address <公开钱包地址>
.\.venv\Scripts\python.exe -m nba_recorder.execution account-mock --journal data/account-read.sqlite
.\.venv\Scripts\python.exe -m nba_recorder.research run --synthetic --synthetic-seconds 14400 --out data/research-scale --split-shares 5
```

输出目录必须全新；CLI不传seconds会处理完整样本，不能把多组独立资本收益相加。profiles可改变方案白名单中的挂单寿命、库存倾斜、减仓/退出阶段边界、规模等，并测试Paper提交/撤单/传输延迟与队列倍数；距离网格覆盖profile中的quote_distance。所有参数完整保存在每组result.json。这里不做最佳参数推荐或收益显著性结论。

## 数据与指标语义

封存复制ip_inputs中的原始可见回调，按time_ms和原rowid保留同毫秒顺序，不恢复缺失时段、不用未来赔率。seal_stream使用SQLite临时表落盘；报告和Markout改为流式读取完整窗口、保留紧凑价格索引，并通过二分查找目标时点。订单、决策和导出仍会占用内存与磁盘，不能无限扩展。四小时合成负载的实测及限制见EXECUTION_READ_SCALE_ACCEPTANCE.md；它不证明真实比赛覆盖或收益。

净PnL由现有资金/持仓估值账本计算；未知估值保持未知，不用最后一次过期价格强行结算。回放结束时分别保留输入截止的研究结果与后续撤单核对一致性，不自动平仓。Markout为事后研究诊断，10/30/60/300秒未到期PENDING、缺新鲜参考UNKNOWN；取目标时刻以前最后可见且仍新鲜的价格，不向后插值，不参与净PnL加总。Quote Markout和Fill Markout独立导出。

校准工具对比每个订单的可见触碰、符合方向/价格的公开打印、队列假设和模拟成交。它是校准输入工具，不是已经校准的真实成交模型；某打印可能出现在多个订单诊断中，不能加总为市场成交量。没有真实自身订单/队列位置，就没有可靠真实成交概率。真实手续费、真实对冲成本、真实延迟、实盘收益保持未知；合成或Paper记录的费用不代表真实收费。

## 执行准备的实际边界

`execution/contract.py`定义PaperAuthority协议与不可激活的DisabledLiveAdapter。现有Paper Adapter和独立进程RemotePaperAdapter都经过同一个OrderManager提交边界。独立Mock只绑定127.0.0.1，持有自己的SQLite权威订单、成交和可重放outbox，OMS数据库不复制pe_exchange/pe_fills来冒充权威。RPC重发返回原收据，去重记账；这不等于真实网络故障域、撮合或资金系统。

订单preview仅为无签名的统一Python SDK意图字段映射，不是可发送的HTTP订单。支持显式CTF v1 token_id / Protocol v2 position_id区分；未实现maker/taker金额、签名域、授权、钱包或发送器。SDK回报live可映射OPEN；matched/delayed等仅是接受/撮合状态，缺成交数量必须UNKNOWN并查询，不制造库存。

参考2026-10-19读取的[官方SDK](https://github.com/Polymarket/py-sdk)、[下单文档](https://docs.polymarket.com/trading/place-orders)、[公开持仓接口](https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user)。官方统一Python SDK包为polymarket-client，本轮未安装或调用认证交易客户端。Polymarket US接口、市场、账户不接入。

网页与CLI的公开持仓默认GET Data API v2 /positions，使用不透明游标分页和current_size；可显式传--api-version v1兼容旧接口。Data API版本不等于市场协议版本，返回的outcome标识仍须绑定对应市场元数据。公开接口不提供认证现金、活动订单或自身成交流，不伪造零余额、不作为权威对账。

网页“演示认证只读接口（Mock）”和account-mock命令仅加载examples/account-read-fixture.json。独立读日志持久化订单累计成交及自身Maker/Taker成交份额；重复成交与结算确认共享唯一标识，不重复计数，不修改OMS库存。固定6位余额单位与已经是shares的订单数量分别处理；订单、成交分页、部分撤单回报、用户流更新时间有纯函数映射及测试。compare_account仅比较独立现金、仓位、订单，不自动恢复交易。真实认证transport仍未配置，没有连接真实用户流或验证实际账户；需要以后明确账户信息及独立授权才能继续。

完成的CLI实验若放在data/research/<32位十六进制ID>，可运行`python -m nba_recorder.research register --out <该目录>`注册到网页；它只登记已有完整Paper导出，不重新回放、不覆盖旧任务。访问返回的/offline#ID查看图和导出。

## 验证与证据

此前验收见OFFLINE_RESEARCH_ACCEPTANCE.md，本轮补充见EXECUTION_READ_SCALE_ACCEPTANCE.md。真实比赛效果、真实队列校准、认证账户和发送仍未验收；21日数据到手可直接选择会话进行离线研究。
