# Lesson 3 验证结果

2026-10-14，在现有 NBA MM 仓库内完成；新增子包 `nba_recorder/fair_value`。记录阶段，无真实订单、签名或资金操作。

## 自动化测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

**69 项通过，0 项失败**，包括原 Lesson 2 的全部 27 项回归测试、新的逐场主客队检查及 Fair Value/CLI 测试。完整本地输出 `lesson3-tests.log`（日志不加入 Git）。

覆盖 American/Decimal、Proportional De-vig、underround、无效赔率、Consensus/离散度、确定性、单源、旧赔率、未来时间、迟到 receipt、同书商跨 Provider 去重、来源冲突、主客队/球队/时间/结算错配、否定规则文本、取消、赛前转赛中、Provider 事件消失、元数据历史、重复盘口确认、四档 Markout、缺失/坏观察及不使用已保存的未来 Markout、只读 CLI 不改库、缺少 key 不请求及不输出密钥。

## 真实 Polymarket 集成

短时运行修改后的 Lesson 2 recorder，分别采集 12 秒和 6 秒，没有请求账户或交易接口。Gamma 与 CLOB REST/WS 成功；逐场 `/events/slug/...` 返回明确的 `teams.ordering`。两场市场 `5676016` 和 `5676021` 的角色证据均存为 event_teams，description 中的加时/取消/延期规则识别成功。

按 `decision_ms=1791958468089`（UTC 毫秒）进行 as-of 计算，四个资产均输出 `NO_QUOTE / no_valid_reference`，每个结果带 metadata version、book observation、snapshot ID 与原始 event key，auto_live_quote_allowed=false。没有使用停止采集后的新状态计算过去，也没有伪造博彩公司概率。

旧 Lesson 2 真实验收数据库 `data/acceptance.sqlite` 仍为 **545 条快照**，未覆盖。集成数据单独保存在同项目的 `data/lesson3-integration.sqlite`，新旧表兼容。

## 明确合成的端到端演示

`python -m nba_recorder.fair_value demo` 生成本地 `data/lesson3-demo.sqlite`：

- 两个 synthetic 来源，一组 American -110/-110、一组 Decimal 2/2。
- 公允概率均为 0.5，两个资产各有一买一卖研究候选，共 4 个。
- 候选按 maker bid/ask 计算 gross edge，同时输出即时跨盘口 taker edge。
- 10s、30s、60s、300s 各 4 个 Markout，共 **16 个 OBSERVED**；未来盘口为预置合成数据，不是等待真实报价、成交或利润。
- 只读 CLI 重放与最初信号完全一致，读取前后数据库主体字节不变；SQLite integrity_check=ok。
- 结果带 synthetic / hypothetical / executed=false / not_profit=true，confidence 不是盈利概率。

演示已存在时不会覆盖，可直接 evaluate/show，或者用 `--demo-db` 选择新路径。开发阶段的旧合成演示保留为备份，不与真实数据混合。

摘要见 `lesson3-validation.json`。操作、字段、公式及配置见 `LESSON3.md`。

## 数据源限制与未完成的外部验证

当前没有 `ODDS_API_KEY`。The Odds API 适配器仅完成官方格式解析、MockTransport 和缺少 key 的真实 CLI 验证；**未验证真实博彩公司价格、API 账户配额或真实正 Edge**。缺少 key 时不发请求，明确 NO_QUOTE，单次 ingestion 退出码 1。

逐博彩公司取消/延期/加时合同必须由授权使用者从条款核验，不能凭 h2h 或球队名假设相同。常见 void/refund 与 Polymarket 50/50 不一致，当前严格匹配会拒绝；提供 key 也不自动解除规则限制。JSON Provider 的来源真实性、授权、独立性和报价更新时间由输入方负责，实际导入 receipt 不允许回填。

置信度为未校准启发式，来源等权；Markout 没有成交/排队、费率、滑点或对冲模型。赔率 API 未提供权威实时状态时仅按 scheduled time 推断，需要 Gamma 状态和时间截止共同把关；提前开赛/状态延迟仍有残余风险。旧 Lesson 2 数据缺少 as-of 规则与角色元数据时拒绝生成信号。尚未做授权博彩公司与真实盘口联动的长期耐久验证。
