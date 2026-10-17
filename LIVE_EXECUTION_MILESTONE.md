# 下一里程碑D：真实执行接入

本轮只授权研究/Paper，没有可激活Live路径。下一阶段独立授权后：

1. 按账户与市场协议版本复核官方当前统一SDK、资产ID和认证，严格区分Polymarket.com与US。当前PublicAPI仅公开GET/WS。
2. 验证账户/地区资格、实际最小规模/tick、费用及返佣资格。Paper费率不是真实费率，EXPECTED/CONFIRMED不是到账。
3. 真实适配器复用OMS/预算，覆盖幂等意图、接受/拒绝、服务器到期、post-only、部分成交、撤单确认及丢回报，禁止网页绕过OMS。
4. 接入认证自身订单/成交流及REST独立核对。当前Paper authority同进程SQLite，不代表独立交易所故障域。
5. 独立真实余额、持仓、订单与本地FIFO/费用/结算流水核对；方向仓位纳入全账户风险。
6. 签名凭证隔离、秘密不入日志。首次资金和每场预算需用户明确批准；本轮不实现或保存私钥。
7. 完成整场真实数据与Paper演练，覆盖阶段、断网、重启、末段和退出，测真实传输/接受/取消延迟，不拿虚拟时钟冒充。
8. 最小额度、急停、监控和故障处置通过有限检查清单后，才提出Limited Live方案。

本轮复核：[官方实时数据](https://docs.polymarket.com/market-data/realtime-data)、[订单生命周期](https://docs.polymarket.com/concepts/order-lifecycle)、[Maker返佣](https://docs.polymarket.com/programs/maker-rebates)。未将文档阅读描述成真实执行验收。
