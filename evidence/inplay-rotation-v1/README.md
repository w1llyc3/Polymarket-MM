# 盘中策略 V1：独立事后 Paper 运行证据

生成日期：2026-10-23。配置和完整说明见仓库根目录 `INPLAY_ROTATION_STUDY.md`。

本目录保存轻量证据，原始行情和63个独立账户数据库在本机 `data/strategy-studies/inplay-rotation-v1-20261023/`。这些是假设成交与结算估值，不是真实利润。真实提交调用为0，Live未启用。

- `manifest.json`：21个有用真实会话、原始输入哈希、参数和1个无市场帧会话的排除原因。
- `summary.json`、`comparison.csv`、`metrics.csv`：全部63个分支结果与逐场指标，保留零成交和风控停机。
- `audit.json`、`old-results-sha256.json`：旧212个结果文件与原始账户订单/成交数量保持不变。
- `validation.json`：63份Paper/FIFO账本、订单状态、无未来引用、停机后零提交及相同输入验证通过。14场盘中样本三模型终值PnL分别为-$9.30、-$9.12333347、+$4.45；不能相加。
- `full-tests.txt`：353项完整回归通过；`integration-tests.txt`：20项策略/报告测试通过；`final-audit-tests.txt`：最终10项策略/审计回归通过。测试集合重叠，不累计为383项独立测试。
- `http-checks.json`：报告、指标CSV、验证结果与控制台健康均返回200；数据库不通过HTTP下载（404）。
- `launch-code-sha256.json`：启动时源码指纹；`final-code-sha256.json`：最终源码指纹。启动后仅完善汇总、独立审计、只读报告路由与测试，没有据结果调整报价参数或重跑挑优。

L1有9场、L2有4场因`late_terminal_fill_correction`停机并保持停机。L2的+$4.45只有7次卖出初始库存，没有完成买入后卖出的轮转，不能作为稳定赚价差的证据。手续费、返佣、奖励与gas均为0研究假设；尚无样本外验证或真实队列/延迟校准。

复核命令：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_rotation tests.test_day_study
.\.venv\Scripts\python.exe -m nba_recorder.research.rotation_audit --out data/strategy-studies/inplay-rotation-v1-20261023
```

逐笔订单与模拟成交可从本机新报告打开，旧的21日、22日报告仍保留原路径。
