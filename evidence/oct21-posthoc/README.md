# 2026-10-21 独立事后 Paper 研究证据

方法、逐场结果与限制见 [研究报告](../../OCT21_POSTHOC_STUDY.md)。原始运行三场均零订单、零成交；这里的 REDECODED 结果均为修正消息解析后的事后模拟，不是真实执行或实盘盈利。

- 三个市场目录各含价格/成交、深度热力图、流动性/活跃度三张 PNG。
- summary.json：12 个独立模拟分支、来源哈希、模型账户与状态。
- comparison.csv：报价、模拟成交率和10/30/60/300秒Markout；未知总PnL留空，actual_net_pnl明确为非真实执行。
- source-integrity.json：源输入不变、三个原账户仍零订单零成交。
- execution-audit.json：成交去重、订单/成交数量一致、HALTED后无新提交、账本一致、真实提交为0。
- sports-time-audit.txt：三场均没有附带晚于回放时点的体育消息。
- index-equivalence.json：第一场Touch索引前后完整订单/成交文件哈希和账户相同。
- 初次失败、中间失败及最终测试日志一并保留；最终333项全量测试及6项目标测试通过。
- publication-worktree.json：发布时工作树指纹，明确记录未提交状态。
- report-preview.png：本地网页验收截图。

原始行情、冻结事件流、各分支完整SQLite和订单/成交明细保留在本机 data/day-studies/2026-10-21，未复制到这份紧凑证据中。模型可能HALTED或有未估值库存，不构成盈利/可实盘验收。
