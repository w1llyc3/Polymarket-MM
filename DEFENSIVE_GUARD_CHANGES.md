# 保护调整 · 2026-10-17

后续补齐：跨场方案不可覆盖版本及启动绑定，不变更活动账户资本；历史按当时参数和输入游标复盘；方向准备撤单核对后保持HALTED。提交边界按同资产买入/互补卖出累计最坏方向风险，不净额抵消待成交承诺。活动订单索引仅优化热查询，迟到成交、恢复和完整审计保留历史路径。详见WORKBENCH_COMPLETION_ACCEPTANCE.md。

基线2320261；现有单Runner/OMS/FIFO上修改，网页主路径research_phase，严格旧模式保留对照。

- D01：reference.book_data_risk移除min_top_depth错误；book_targets按深度缩量，中点/估值同口径。
- D02：discovery不再以历史单场决定Paper准入；research_eligible、明确用户授权及Runner身份/规则重验共同决定。
- D03：research_phase.py允许研究来源缺上游时间/独立pause，保留未知；网页人工本场阶段不依赖15秒文件，不外推倒计时。
- D04：研究路径临时数据问题取消相关单/不报价；身份绑定、UNKNOWN和账户事故仍硬停。估值缺失不是账本损坏。严格旧路径不改。
- D05：研究时钟回拨UNKNOWN，新的有效接收观测或人工阶段复核；closed_latch优先，不能重开末段。
- D06：最大挂单寿命、未发送意图期限、当前数据时效分开；dispatch/保留复检。不伪造真实服务器到期。
- D07：移除目录60秒全量重扫要求，Runner重验所选市场；停止/暂停放宽无关版本变化，仍绑定session/确认/幂等，重复停止同场幂等。
- D08：history.py以注册会话ID校验console_origin、拒任意路径和符号链接；history/chart/research只读，不构造Manager或资本。
- D09：规则归一化大小写/空白/cancelled/canceled/including overtime/50-50等已支持模板；额外未用公开字段不影响策略。未知规则不自动批准。
- D10：change_research_parameters兼容参数白名单，SQLite先备份、保存前后版本；资金、绑定、阶段模式不是可改参数。新增默认字段迁移保留run_id；旧关键配置缺失仍拒绝。
- D11：研究风险/权益/watchdog使用首次末次计数区间，省略的watchdog保留ID/digest去重。原始行情、订单、资金、权限变化不压缩。极端密度增长仍需验收。
- D12：exits.py不再强制frame['signal']；research.exit_depth聚合每结果库存后只消费一次深度。paper_exit.py显式授权的限价限量Paper主动退出，不全局关闭maker-only，费用是研究假设。

保留：单写者、事务、幂等、现金/份额守恒、未知订单承诺、提交边界风控、硬亏损、用户停止、末段锁定、Host/Origin/令牌/路径保护与来源标识。

修改旧政策测试：薄深度不再是reference_quality错误；额外预测字段测试比较报价不变；旧扫描fixture补足规则文本。原资金、迟到成交、撤单竞态、重启与无未来数据回归未删除。开发失败日志保留于evidence/next-iteration。
