# NBA MM MVP Acceptance — 2026-10-14

**历史基线说明：** 2026-10-15 主线已按 v2 改为独立 BOOK_DRIVEN；外部赔率不再是该模式准入依赖，且新增了统一异步 OMS/FIFO、阶段控制和单写者/异常退出测试。最新结果与未通过项见 [INPLAY_BOOK_DRIVEN_ACCEPTANCE.md](INPLAY_BOOK_DRIVEN_ACCEPTANCE.md)。以下保持 Lesson 7 当时的验收记录，不作为 v2 当前状态。

截至当前代码：**公开盘口采集 Data GO；受控离线 Paper GO；真实赔率驱动的无人值守 Paper NO-GO；Limited Live NO-GO。** 不因 10 月 20 日截止日期跳过 P0，不承诺 10 月 21 日能实盘做市。

## 1. 仓库实际审查与模块完成度

状态以代码和运行证据为准。IMPLEMENTED 表示指定本地功能已实现并通过测试，不能解释为生产就绪。PARTIAL 表示存在明确验收依赖。此次在原仓库复用 Lesson 2–6，没有新建独立项目。

1. **NBA Market Scanner — IMPLEMENTED**：`nba_recorder/api.py`，严格 NBA/赛季/赛前/全场 Moneyline 筛选；`tests/test_core.py::test_exclusion_matrix`、`test_started_and_live`、`test_missing_roles_never_guessed_from_slug`。当前扫描选中 3 场；联赛日历筛选尚无逐场官方 NBA schedule ID 核验。
2. **Order Book Recorder — IMPLEMENTED**：`nba_recorder/book.py`、`recorder.py`、`storage.py`；`test_absolute_delta_and_delete`、`test_restart_dedupe_atomic_snapshot`、`test_transport_reconnect_resnapshots_and_stops`。真实短采集及既有 30 分钟验收均有记录。公开流无已证实全局连续序列，依靠 REST 重同步而非声称严格增量连续。
3. **Odds Collector — PARTIAL**：`nba_recorder/fair_value/providers.py`，The Odds API 和 JSON adapter；`tests/test_fair_value.py::test_missing_key_no_request`、`test_http_get_and_key_not_persisted_or_error_leaked`、`test_json_receipt_cannot_be_backdated`。授权 key 未配置，没有真实赔率连通性/两来源验收；书商规则兼容性未验证。
4. **Fair Value Engine — IMPLEMENTED**：`fair_value/maths.py`、`matcher.py`、`engine.py`；`test_asymmetric_proportional_formula`、`test_formats_equivalent`、`test_conflicting_sources`、`test_future_odds_books_metadata_do_not_change_past_signal`。计算/匹配有证据，实际有效输入仍依赖第 3 项。
5. **Quote Engine — IMPLEMENTED**：`quote_engine/model.py`；`tests/test_quote_engine.py::test_four_states`、`test_supported_ticks_and_maker_constraints`、`test_risk_limits_and_minimum_size`。纯目标输出，独立 1/2/3¢ 反事实实验。
6. **Risk Engine — PARTIAL**：`paper_execution/risk.py`、`manager.py`，五态、提交边界复检、Kill Switch、日亏损/状态/时效/承诺风险已有实现；`tests/test_stress.py::test_submission_uses_latest_persisted_reference`、`test_reconciling_blocks_submission_and_orphan_authority` 及全部故障矩阵通过。持续服务的外部更新、完整账户估值和跨进程控制尚未完成整体验收。
7. **Paper Execution Adapter — PARTIAL**：`paper_execution/adapter.py`，虚拟延迟/回报/权威订单和独立 fill-ledger 余额 reducer；`tests/test_paper_execution.py::test_submit_timeout_but_exchange_accepted`、`tests/test_stress.py::test_fault_account_mismatch`。仍与 OMS 共进程、同 SQLite；不是独立交易所服务或真实账户余额。
8. **Order State Machine — IMPLEMENTED**：`paper_execution/manager.py`；`test_paper_state_machine_rejects_impossible_transition`、`test_partial_fill_during_cancel`、`test_cancel_timeout_not_success_and_unknown_never_resumes`、`test_restart_recovers_intent_authority_and_stays_halted`。UNKNOWN 不等于取消，核对后仍需人工复位。
9. **Inventory Ledger — IMPLEMENTED**：`fill_management/ledger.py`、`paper_import.py`；`tests/test_fill_management.py::test_multiple_batches_fifo_partial_exit`、`test_partial_fill_duplicate_and_cash_reservation`、`test_restart_preserves_equity_and_queue`、`test_paper_import_idempotent_and_read_only_source`。
10. **Exit & Hedge Evaluator — PARTIAL**：`fill_management/exits.py`、`paper_execution/hedge.py`；`test_exit_candidates_consider_depth_fee_and_cash`、`test_existing_complementary_inventory_not_bought_twice`、`test_partial_hedge_remains_directional`。可评估数量/费用/深度和候选；多 lot 候选尚无共享深度与资金统一预算，不执行。
11. **PnL Attribution — IMPLEMENTED**：`fill_management/ledger.py`、`report.py`；`test_asymmetric_fair_value_attribution`、`test_report_net_pnl_matches_cash_positions_and_daily_rates`、`test_partial_pair_then_settlement_is_not_double_payout`。缺成本/估值则未知，重叠诊断不相加。费用、奖励与结算仍需真实证据。
12. **Dashboard / CLI — IMPLEMENTED（CLI）**：`nba_recorder/__main__.py`、各模块 `__main__.py`、新 `stress/__main__.py`；`tests/test_fair_value.py::test_read_only_cli_and_replay`，此次 CLI demo、审计、CSV 和压力命令实际运行成功。没有独立 Dashboard。

## 2. 真实数据连通性

本次白名单公开 GET 探测成功：Polymarket.com Gamma sports/events/event detail 和 CLOB book，NBA series `10345`。探测样例 `5420695` 仅证明接口连通，不冒充合格常规赛市场。

随后使用正式 Scanner/Recorder 采集 `data/lesson7-connectivity.sqlite`：扫描 3 个合格市场，跟踪第 1 场 `5676016` 的 2 个资产。上海时间 2026-10-14 22:47:35.909–22:48:07.329，31.42 秒、3 次连接、2 次主动断线、6 次快照核验；原始 book=2、REST snapshot=2，标准化快照=4，重复跳过=4。SQLite integrity=ok，无重复主键/可识别 payload，无未标记交叉/锁盘/空盘。

证据：`lesson7-connectivity.log`、[lesson7-data-validation.json](lesson7-data-validation.json)。本次是短复核，不伪称新增 30 分钟测试；既有真实长采集证据见 [VALIDATION.md](VALIDATION.md)。静态快照确认不等于独立交易样本。

沙箱内初次公开 probe 未返回，已中断；获准的沙箱外 GET 探测及 REST/WS 采集成功。这不是赔率成功证明。The Odds API 未配置授权 key；真实赔率连通性 **NOT VERIFIED**。此前真实封存回放 72 NO_QUOTE、0 订单，见 `LESSON6_VALIDATION.md`。

仅支持 Polymarket.com；不连接 Polymarket US，不加载真实账户、钱包、签名或资金接口。

## 3. 单元测试与集成测试

本次完整 `python -m unittest discover -s tests -v`：**155 项通过，0 失败，5.682 秒**，日志 `lesson7-tests.log`。此前 135 项与新 20 项均运行，没有跳过 P0。新测试包含 16 个独立场景、两次完整矩阵结果一致、禁止覆盖证据，以及最新参考/核对屏障验证。

Mock 集成覆盖公共数据重连、REST 重同步、Fair Value 到报价回放、OMS 异步成交与撤单、FIFO 导入/恢复、跨日权益及报表。离线压力矩阵在禁止 socket/HTTP 的测试中通过。没有真实下单适配器，Paper adapter 与 manager 的真实提交计数均为 0；不是仅依靠日志里的常数证明安全边界。

## 4. 确定性压力测试结果

实现：`nba_recorder/stress/engine.py`，版本 `lesson7-v1`，固定 fixture/虚拟时钟，无随机种子依赖。每个场景独立 SQLite，拒绝覆盖；错误保留 FAIL 和原因，不吞掉失败，CLI 有失败返回非零退出码。

实际 CLI 结果：**16 PASS / 0 FAIL**，见 [lesson7-stress-results.json](lesson7-stress-results.json)。覆盖：Fair Value 突变、赔率过期、来源冲突、WebSocket 中断、REST 超时、撤单期间部分成交、已接受但下单响应丢失、重复成交、同场同时成交、服务恢复、开赛保护边界、日亏损熔断、独立账户成交账不一致，以及过期提交边界、人工复位屏障和 PAUSED。

源冲突场景注入的是 Fair Value Engine 的拒绝结果；真实赔率去水/冲突传播另由 `test_conflicting_sources` 验证。WebSocket 场景注入断线状态；网络重连本身由 recorder Mock 与本次公开采集验证。REST 超时为 Paper 权威查询超时，不声称真实交易所耗时。

重启场景保留未收到的部分成交，从持久化日志恢复 UNKNOWN、核对现金/库存并保持阻断；另有事务回滚测试。**尚未完成 OS 强杀、独立 authority 进程及大规模吞吐压力验收**，不把有限故障矩阵描述为生产级灾难恢复证明。

## 5. 风控状态机与提交边界

五态：ACTIVE、PAUSED、HALTED、RECONCILING、READY_FOR_MANUAL_RESET。旧 RUNNING 仅兼容读取为 ACTIVE；服务重新启动总是 fail-closed。

- 仅 ACTIVE 可 enqueue/dispatch 新风险；其余状态拒绝。Kill Switch 原子关闭权限、清除本地队列、发起模拟撤单，成交仍继续记账。
- HALTED/PAUSED → RECONCILING；核对失败/超时 → HALTED。仅全部订单权威终态、无未知或撤单在途且账本一致，才进入 READY_FOR_MANUAL_RESET。
- READY 仍不可提交；明确 `resume()` 才可 ACTIVE。严重熔断无自动恢复；日亏损条件未解除时人工恢复也失败。
- UNKNOWN/TIMEOUT 从不假设已取消；部分成交始终更新现金/库存与剩余承诺。
- dispatch 在交易提交边界重新验证时效、Edge、tick、maker-only、资金、库存、开赛与订单状态；优先使用最新持久化 risk frame。新参考已落盘但检测循环尚未完成，也能拒绝丢失 Edge 的报价。
- 独立 Paper 余额 reducer 从 `pe_fills` + exchange 权威价格/方向重算，交叉核对 OMS cash/positions/累计成交；额外 authority 订单也拒绝恢复。seed 为本地 Paper 初始资本，不是独立真实账户凭证。

## 6. 账本一致性

新九实验 run：`d8e9b5ea07ef29863951b3ae28943e622d1f0debd3ae6c8edaeb3dc7ad66e3c8`，数据库 `data/lesson7-research.sqlite`。

只读审计 [lesson7-ledger-audit.json](lesson7-ledger-audit.json)：tape hash、496 个订单数量守恒、九账户 FIFO/物化状态、Net PnL 与现金/持仓/资本贡献均通过。恒等式为 equity = cash + inventory value；Net PnL = equity − capital contributions = trading PnL − fees + rewards。初始 $1000 cash 加两资产各 10 份、假设成本 0.5，贡献为 $1010。

Markout 仅使用报价后观察作事后诊断，不改变历史决定；未来 odds/book 不参与历史信号。配对/结算现金流不重复给剩余库存付款。Unknown valuation 保持未知，不补 0。

## 7. 策略研究结果（全部合成、非实盘）

同一封存流 `a4660cd0244dd310f9ad1bb1f0d2989f7a9512aee871fc07b7e6bc432f50669f`，350 秒、353 时间桶、706 asset frames、702 有效、仅 2 个模拟 public prints。覆盖率 **99.43% 仅指所记录窗口**，不能代表一天或真实赔率覆盖。九实验共 14 个代理成交事件，无法校准真实概率或策略排名。

三阈值 × Touch(L0) / Trade-through(L1) / Queue-aware(L2)：

- **1¢**：模拟成交订单率 L0=3.64%、L1=3.45%、L2=3.45%；Net PnL 分别 $0.09 / $0.25 / $0.10；峰值保守承诺 $35 / $35 / $32；目标平均 Gross Edge 约 1.246¢ / 1.006¢ / 1.006¢。
- **2¢**：模拟成交订单率 1.85% / 3.57% / 3.57%；Net PnL $0.08 / $0.25 / $0.10；峰值 $30 / $35 / $32；目标平均 Gross Edge 三者约 2.001¢。
- **3¢**：模拟成交订单率 0% / 1.89% / 3.70%；Net PnL $0 / $0.15 / $0.12；峰值 $30 / $30 / $32；目标平均 Gross Edge 三者 3¢。

各 lane Quote Availability=702/706≈99.43%，分母是资产时间决策桶，不是挂单持续时长。Gross Edge 是目标报价理论每份值，不是已实现盈亏。

四期限 10s/30s/60s/300s 的模拟 fill 数量加权 Markout 在此静态 fixture 恰好相同：1¢ L0=1¢、L1/L2=2.5¢；2¢ L0=2¢、L1/L2=2.5¢；3¢ L0 无样本，L1/L2=3¢。全量 Quote/Fill Markout、观察数量/不可用/尾部截断状态与库存见 [lesson7-research-results.json](lesson7-research-results.json)，10 个 CSV 在 `data/lesson7-csv/`。

Hedge Cost、成功率及 Stale Quote Loss 在本样本无对应执行/证据，输出 **null/N/A**，不冒充零成本或无损失；相关部分对冲与旧报价诊断由单元测试覆盖。maker fee 假设 0，奖励记录 0；未证实真实市场费用/激励。Markout/Entry Edge/Stale Quote Loss 等重叠项不加到 Net PnL。九账户是独立反事实，盈亏与成交量不能相加。

模拟延迟 P50/P95/P99 按场景输出：撤单竞态为 10/10/10 ms（n=1）；重启状态恢复 45/45/45 ms（n=1）、权威查询 5/5/5 ms（n=1）；超时场景查询 100/100/100 ms（n=1）；风险检测 1/1/1 ms。均为 **synthetic_paper_clock 参数验证**，不是实际传输/交易延迟；样本不足以估计尾分位数。九实验回放采用即时撤单，其 latency 单独置 null，不混用 OMS 虚拟延迟。

L0 触碰无法证明成交，L1 依赖成交事件完整性和方向假设，L2 依赖初始可见队列/FIFO/隐藏量，未可靠重建新增优价队列与取消位置。三模型不等同真实成交，未计自身市场影响。

## 8. 未解决 P0 / P1 / P2

**P0（阻止端到端无人值守 Paper / 任意 Live）：**

- 真实授权赔率、两个兼容独立来源及比赛/OT/取消规则证据尚缺；不能生成已验证的真实 FV。
- Lesson 5 异步 OMS 与 Lesson 6 即时撤单研究回放仍为两条显式路径，没有统一持续运行、完整账户估值/风险监控的整体故障验收。
- authority 与 OMS 同进程/SQLite、尚无跨进程单写者与 OS 强杀/独立 authority 的恢复证明。不能推广为分布式账户一致性。
- 没有真实交易适配器、真实独立账户核对或实盘授权。Live 锁定；禁止以日期解除。

**P1：** 多 lot 候选共享资金/深度预算；更长多场真实记录、开赛/暂停/跨日复杂场景；队列隐藏量/优价新增量/撤单延迟校准；真实费用/奖励/结算证据；行情身份/日程映射与事件序列不确定性。

**P2：** 存储容量/备份恢复、版本迁移、CI 与证据归档、Dashboard、更多统计样本与敏感性图表。Lesson 3–7 尚为本地工作区开发，未推送远端；Git HEAD 仍为 Lesson 2 的 `12ffdb1`。

## 9. Data GO / NO-GO

**Data GO，仅公开市场/盘口采集及只读查看。** 启动必须使用 scanner 当时真实结果；无合格市场明确退出，断线重同步、过期标记、异常保留。只有收集有效市场时才产生记录，不伪造实时数据。

**完整赔率数据链 NO-GO**，直至授权 key、来源/规则兼容性与真实时效验收通过。此次数据连通性不能保证 10 月 21 日的 API/市场持续可用，需当天预检。

## 10. Paper GO / NO-GO

**Paper GO，仅单进程、受控离线回放/故障研究。** 可运行九实验、单独 OMS 故障矩阵、账本/退出候选/日报及 CSV，所有输入标来源、保留假设，遇无有效数据 NO_QUOTE。

**无人值守真实赔率驱动的整体 Paper NO-GO**，P0 未清不能把两个局部通过的链路当成统一服务验收。候选不执行，研究 Paper 不改变真实账户。

## 11. Limited Live GO / NO-GO

**NO-GO。真实交易调用为 0；没有钱包、签名、真实订单或资金操作权限。** 后续即使 Paper P0 清零，也必须有独立审批、真实技术证据及用户明确实盘授权；当前代码无 Live 解锁开关。

## 12. 10 月 21 日能安全运行的功能与命令

可运行真实盘口采集/只读 BBO、有效来源条件下的只读 FV 研究、明确标记合成或历史数据的 Paper 回放、退出候选、会计审计及报告。不能承诺自动做市、真实下单、真实成交概率或盈利。

```powershell
# 公开数据采集与只读查看
.\.venv\Scripts\python.exe -m nba_recorder scan
.\.venv\Scripts\python.exe -m nba_recorder record --minutes 30 --limit 3
.\.venv\Scripts\python.exe -m nba_recorder show --limit 3 --depth 5

# 离线故障矩阵：目录必须全新，失败退出非零
.\.venv\Scripts\python.exe -m nba_recorder.stress --out data/lesson7-next-stress
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

# 九实验合成验收：两个路径必须全新
.\.venv\Scripts\python.exe -m nba_recorder.fill_management demo --source data/lesson7-next-source.sqlite --db data/lesson7-next-research.sqlite
# 只读现有验收数据与完整研究指标
.\.venv\Scripts\python.exe scripts/summarize_acceptance.py --db data/lesson7-research.sqlite --run d8e9b5ea07ef29863951b3ae28943e622d1f0debd3ae6c8edaeb3dc7ad66e3c8 --out data/lesson7-next-report.json
.\.venv\Scripts\python.exe -m nba_recorder.paper_execution show --db data/lesson7-stress/service_restart.sqlite
```

模块细节见 `LESSON3.md` 至 `LESSON6.md`；本报告是当前实现的验收边界，不是对未来开幕日实盘能力的承诺。
