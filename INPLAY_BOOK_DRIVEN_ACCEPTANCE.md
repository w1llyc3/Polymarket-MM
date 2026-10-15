# v2 In-play BOOK_DRIVEN Acceptance

日期：2026-10-15，Asia/Shanghai。规范见 [docs/NBA_MM_PIVOT_V2_CODEX.md](docs/NBA_MM_PIVOT_V2_CODEX.md)。本文件为实际开发结果，不是实盘授权，也不是两公开账户的 Maker 审计成功声明。

## 基线与范围

开始修改时实际 HEAD：`7afc9e51059e468db08fc6efb0957c4bed45faa2`。已读取 README、quote/model、risk、OMS、Paper adapter、FIFO、回放及原测试；本轮在该仓库内新增 v2，没有另起项目，没有改 Entropy–Lighter，没有删除旧测试。

新主路线为赛中 BOOK_DRIVEN。EXTERNAL_FAIR 的来源数/时效/匹配和规则检查继续保留，但不再作为 BOOK_DRIVEN 的准入条件。没有用两个 synthetic 赔率源伪装新模式，也没有将盘口中点写成 fair_probability 或赛果 Edge。

## 代码改动

- `quote_engine/model.py`：显式 `Policy.strategy_mode`，默认仍为 EXTERNAL_FAIR；BOOK_DRIVEN 明确路由到新盘口参考，模式不符拒绝。原 FVE 代码与测试保留。
- `inplay/policy.py`、`reference.py`：reference_mid / quote_center / quote_distance；tick、min size、post-only、每单美元与份数独立上限、库存、活动承诺、时效/身份/积压、深度质量及提交边界复检。平台最小数量不可行则不报价。
- `inplay/phase.py`：持久化 PRE_GAME_OBSERVE / SYNCING / ACTIVE_MM / REDUCE_ONLY / MM_CLOSED / UNKNOWN。不同新观察预热，识别规范化节次/剩余秒、暂停/延迟/冲突；同节时钟回拨进入持续人工复核。MM_CLOSED 重启、加时、重连均不自动解除。
- `inplay/manager.py`：复用异步 Paper OMS。累计成交增量与 FIFO 同一事务更新；独立公开 print/模拟回报证据，重复回报不会重复记账。默认零份额；显式完整份额 split 通过现金付出记录两份成本，不增加资本贡献。分层保存 MAKER_REBATE / REWARD / 运行成本的 EXPECTED / CONFIRMED / RECEIVED；预计或确认额不进入现金，现金凭证去重，无法归属的收入仍 unallocated。
- `inplay/store.py`、`session.py`：跨进程排他写锁；一个输入的 phase、Paper authority、OMS、FIFO、引用和游标完成标记共同提交/回滚。L0/L1/L2 均经相同异步 Paper 回报路径，撤单在途仍可成交。模型与源 tape 绑定，不允许重启换模型或换流。
- `inplay/report.py`：1/3/5/10/30秒 signed Markout、成交时 mid（无法确定则未知）、报价 mid、库存年龄、成本/收入阶段、残余库存、风险与延迟。mid-based 已实现分解为入场报价溢价 + mid 漂移 + 退出溢价，不把 Markout/旧报价诊断再加到 Net PnL。
- `api.py`、`recorder.py`、主 CLI：新增显式 `--scope full-game`；采集赛前、赛中、末段、加时、结束/解决状态，报价许可独立。整场 record 必须指定 market ID，避免发现刷新静默切比赛。默认旧 pregame 范围不变。结算后盘口可能停止，记录原始解决事件/健康状态，不伪造后续盘口。
- `storage.py`：新增 received/processed/lag 采集处理记录，不改写公开原始 payload；旧记录缺遥测保持 UNKNOWN。`inplay/tape.py` 按已接收且已处理的证据封存，不读取赔率或未来元数据。
- `inplay/sports.py`：官方体育原始流及 heartbeat 记录；识别 Q1/1Q、加时等格式，但不推断 elapsed 的时钟含义，不用墙钟外推比赛剩余时间。没有独立身份/时钟/暂停/上游时间证据的原始流不能授权报价。
- `wallet_research/`：独立、白名单 GET 研究工具，官方 profile→proxy、固定窗口、分页/拆窗、原始响应与 SHA256、元数据匹配、美元/份数分离。显式 v1 Data API 契约，不混入 v2 字段；`takerOnly=false` 不是 Maker-only，BUY/SELL 不作为执行角色。

整场范围的元数据重发现间隔缩短为20秒；v2建议 `--stale 3 --resync 2` 与3秒盘口门槛对齐。测得的 persistence lag 不是完整网络延迟；Paper 中同步风险检查的虚拟耗时为0，不能拿其尾分位数推断实际检测速度。

不开发自动逆转、跟单、方向分账、真实签名/提交/资金操作。BOOK_DRIVEN 拒绝方向预测、逆转和后验赛果字段进入订单意图。

## 原测试与新增测试

最终完整命令：`python -m unittest discover -s tests -v`，**183 PASS，0 FAIL**。原155项全部保留，新增 `tests/test_inplay.py` 23项、`tests/test_wallet_research.py` 5项。完整本机日志 `v2-all-tests.log`；结构化摘要见 `v2-test-results.json`。

新增覆盖：

- 无赔率且盘口/阶段有效，BOOK_DRIVEN 能产生 BUY_ONLY Paper 候选；EXTERNAL_FAIR 仍 NO_QUOTE。
- 过期、断线、交叉、身份错误与积压阻断；UNKNOWN 阶段不新增风险。
- 已有挂单遇到缺失盘口时，风险日志与 HALTED 正常持久化，成交代理不得因空字段异常回滚熔断。
- 旧目标排队后同刻收到末段截止，在 dispatch 边界拒绝；加时/重启不解除 MM_CLOSED。
- 连续1份部分成交不能突破总库存/承诺上限；撤单在途部分成交正常入账。
- REDUCE_ONLY 不把 SELL 自动当减风险；不得拆坏已有完整对冲，不超量反向，也不重复分配已承诺减仓量。
- 同一异步 OMS 下三代理在确定性场景分别产生 L0=4、L1=5、L2=3份，全部 FIFO/OMS 一致；只是模型差异证据，不是真实成交率。
- 真子进程 `os._exit(23)` 后，恢复未收到的2份成交并保持阻断、权威核对一致；事件级异常注入回滚 phase/订单/FIFO/输入完成标记。
- 不允许切换已绑定模型；不使用未来 frame、未处理快照或未来元数据。
- 预计/确认返佣不增加现金，到账 cash_flow_id 唯一；成本、现金、净值对账；缺估值则 Net PnL 未知。
- 时钟 elapsed 无证据、暂停状态不明或时钟修正无法开放；方向/后验字段不影响订单。
- 钱包分页分割不漏同秒边界，同秒超限显式失败；HTTP失败不是空样本；金额按美元而非份数，Maker角色 UNKNOWN。

旧故障矩阵、FVE 去水/匹配、盘口异常/重连、UNKNOWN/撤单超时和会计测试一并运行。无隐藏失败、无真实订单。

## 实际公开数据范围

### 公开盘口与体育流

2026-10-15 上海17:23:08.108–17:23:40.414，显式 full-game 范围跟踪市场 `5676016`，2个资产，32.306秒、3次连接、2次主动断线、6次快照核验；4个标准化快照、可识别重复跳过2次，SQLite及BBO异常标记核验通过。见 [v2-data-validation.json](v2-data-validation.json)。**这是赛前真实市场的整场采集模式复核，不是一场真实赛中全程记录。**

官方 sports WebSocket 实际连接1次、20秒收到11个原始更新。保留在 `data/v2-sports-public.sqlite`；这些原始更新不等于已核验 NBA 时钟。当前 game→market 身份、elapsed、暂停及上游新鲜度未取得完整可靠契约，报价权限 UNKNOWN。

真实盘口无赔率封存回放：57输入、114 asset frames、1场，**Quote Availability=0、0订单、0成交**；无可信 sports 状态不报价。见 [v2-public-paper-validation.json](v2-public-paper-validation.json)。该源记录生成于处理遥测 schema 加入前，不能补造原始 backlog；新采集会测量 before-commit lag，旧版本只能保留未知。

### 两个公开账户

官方 profile 实际确认给定地址的 proxy 与输入相同，name 分别为 iWasEnough、askilika；不静默替换地址。

- iWasEnough：固定窗口 epoch秒 `[1792052042,1792055642]`；5次GET，activity中669 TRADE、4 REDEEM，包含Maker的 trades 669行；**可匹配 NBA 全场 Moneyline=0**。
- askilika：固定窗口 `[1792052045,1792055645]`；4次GET，activity/trades为空；**NBA=0**。

两者时间窗口均完成分页，但窗口不同、不做角色差分或跨账户策略比较。没有 NBA 样本，NBA美元中位数/P90、小额金额/笔数占比、Maker占比均 N/A。不能据此证明或否定他们在其他时段做过小额 NBA Maker。未获取历史盘口、订单 ID 或队列，不恢复历史挂单、撤单速度或 markout。当前不推断 Maker/Taker，角色证据覆盖在无NBA样本时亦为 N/A。

初次宽窗口采集过大，在173请求后主动停止并标 TRUNCATED；不从其生成统计。随后改为轻量一小时及最多60请求预算。原始请求、状态、分页参数、响应与摘要均留存；失败/截断不等于空样本。

账户级奖励保持 unallocated；开仓库存和份额转账缺失，不声称恢复完整库存或净风险。SPLIT/MERGE/REDEEM等活动原样保留计数，但未核验转换/转账语义的库存变化仍 UNKNOWN。未获得第一份真实NBA成交样本，诚实记录为未完成目标，不扩展成大型批量依赖。

结构化证据：[v2-wallet-research-summary.json](v2-wallet-research-summary.json)。三个本地目录分别为 `data/v2-wallet-iwasenough-1h/`、`data/v2-wallet-askilika-1h/`、`data/v2-wallet-iwasenough/`，各含 wallet_summary.json、nba_fills.csv、wallet_evidence.md 与原始请求文件；data不上传Git。

## A/B 与执行假设研究

同一 synthetic tape：`14880f572b96831a2f7fe30553fe7c327f9d049ec67b290f15427723cb374efe`。45个输入/90 asset frames、44秒、仅1个合成比赛和3个公开print fixture，不是独立统计样本。

同初始资本$100，各实验显式用$5现金抵押生成两侧各5份（余额$95）；不凭空注入库存。A/B 对比保持同流、资金、模型和延迟，距离为2¢、每笔最多5份且$5上限。A固定参考和库存限制；B增加异常暂停、库存偏移、Q4剩余180秒减风险；A为90秒，二者60秒关闭。阈值均为未校准研究参数。

L0/L1/L2 × A/B × 两个延迟组，共12账户，全部 OMS/FIFO/权威余额审计一致。延迟组为数据传输2ms/撤单10ms与传输20ms/撤单100ms；不是实测网络延迟。成交有回报延迟，绝非即时撤单结果。

实际演示：A-L1有2个代理成交、诊断Net PnL $0.16；A-L2有1个、$0.10；B-L2有1个、$0.15；其余无成交、$0。两组延迟在这个稀疏fixture下未改变上述结果，不能推断延迟不重要。最终全部账户 HALTED：B触发单边成交量异常，A在持有库存进入末段关闭保护时停止。报告保留风险原因和剩余库存，不伪造退出成功。

三模型经OMS的4/5/3份差异在专门测试中验证；演示L0没有touch，不强造其成交。Maker Rebate 未估算、无到账凭证，不能作为模拟正 PnL 的保证。没有可执行对冲或未结算估值时保持未知/人工复核。

完整每账户 quote availability、成本/返佣阶段、1/3/5/10/30秒 Markout、库存年龄、残余仓位、风险与 P50/P95/P99 见 [v2-paper-comparison.json](v2-paper-comparison.json)，本机 CSV 在 `data/v2-unified-accepted/comparison.csv`。所有数值为 Paper/合成反事实，不是盈利验收或策略排名。

## 未通过项及风险

- **P0（真实赛中自动 Paper）：** 真实比赛时钟/暂停/上游年龄和比赛身份契约未完成核验；目前拒绝开放。未完成一场真实NBA整场记录、完整比赛Paper和长时间连续故障验收。
- **P0（Live）：** 无真实交易适配器、独立真实账户核对或授权；Live锁定，不允许因日期解除。
- **P1：** Paper authority仍在同进程/SQLite，单写者与OS异常退出测试不等于独立服务故障域；需要更多实际比赛/多市场/高频/跨日/断电覆盖。Windows POSIX以外文件系统的锁语义仍需目标环境验证。
- **P1：** L2初始队列/FIFO/隐藏量和公开成交方向未校准；不推断真实队列或成交概率。L0仅潜在触碰；缺失/重复/乱序公开数据无法恢复全部真实流。
- **P1：** 减仓承诺防止反向或拆对冲，但不承诺及时退出；无法退出的残余库存为异常风险，非逆转持仓。未实现自动taker退出或多市场组合优化。
- **P1：** 返佣、奖励、实际成本及结算凭证缺失时未知；不能把账户级收入分配给NBA。尚无真实NBA账户成交样本支持Maker研究。
- **P2：** 新mode字段会改变新回放run ID；旧EXTERNAL_FAIR会计与报告可读，旧封存版本不静默升级为v2结果。完整迁移工具、独立服务化、CI与可视化后续推进。

## 判定

**Data GO（公开记录）**：真实盘口及体育原始流可记录；`full-game`将采集与报价权限分离。尚无真实整场验证，不能声称已覆盖赛中开幕比赛。

**受控 Paper GO（合成/历史研究）**：BOOK_DRIVEN无需外部赔率，在可信输入、单写者及风险条件下可产候选并经统一异步 OMS/账本运行；原EXTERNAL_FAIR继续按旧规则拒绝缺赔率输入。

**真实赛中无人值守 Paper NO-GO**：体育状态语义和整场/长时间验收未完成。真实记录缺条件时 NO_QUOTE 是预期安全结果。

**Live NO-GO**：无真实签名、订单提交或资金修改。真实交易调用始终0；没有逆转自动执行或跟单。

## 可运行命令

安装和环境见 [README.md](README.md)。本地已验收目录不能覆盖；以下使用全新路径：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m nba_recorder scan --scope full-game
.\.venv\Scripts\python.exe -m nba_recorder record --scope full-game --market-id <scan中的ID> --db data/full-game-new.sqlite --minutes 30 --limit 1 --stale 3 --resync 2
.\.venv\Scripts\python.exe -m nba_recorder.inplay sports-record --db data/sports-new.sqlite --seconds 60
.\.venv\Scripts\python.exe -m nba_recorder.inplay demo --out data/v2-next-demo
.\.venv\Scripts\python.exe scripts/validate_inplay.py --db data/v2-next-demo/B-L2-2ms.sqlite --out data/v2-next-audit.json
.\.venv\Scripts\python.exe -m nba_recorder.wallet_research --address 0x216050de6080fac3926226b2b8ce3d5544784c78 --out data/next-wallet-evidence
```

## 本次复核的官方接口

只采用明确GET/公开WS，不复制第三方交易代码。Data API按本次官方标记的v1 legacy契约显式使用；无自动v1/v2回退。

- [Trades](https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets)、[Activity](https://docs.polymarket.com/api-reference/core/get-user-activity)：参数、分页/时间窗和字段证据。
- [Public profile](https://docs.polymarket.com/api-reference/profiles/get-public-profile-by-wallet-address)：输入与proxy关系。
- [Realtime data](https://docs.polymarket.com/market-data/realtime-data)：market/sports流、heartbeat及阶段字段。
- [Fees](https://docs.polymarket.com/trading/fees)、[Maker Rebates](https://docs.polymarket.com/programs/maker-rebates)：费用/返佣与实际到账不能混同；市场资格和份额未在本次证明，不固定承诺返佣。
