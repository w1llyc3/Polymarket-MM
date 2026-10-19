# 开幕赛分类修复与21日运行准备

2026-10-19完成；仅公开采集与本地Paper，没有真实下单、签名或资金操作。

## 根因和改动

1. Gamma赛事没有season_type显式标签，NBA赛程JSON403使通用发现缺分类依据。新增三场逐项核验的官方证据，精确匹配away/home/start_ms，绑定NBA比赛ID；不是按日期/slug猜类别，也不是全赛季人工授权。
2. 旧通用发现与旧Scanner都把任何schedule_match当作PRESEASON。现读取证据自身类别；多来源明确类别矛盾返回UNKNOWN，不再优先取某个标签。NBA JSON恢复但标签空白时，可在NBA game ID相同时合并该场已核验证据；身份或开赛时间不符拒绝采集/Paper准入。
3. 无人值守对已SCHEDULED项目原先不刷新保存的market，页面仍显示旧UNKNOWN。现在刷新待启动项目的元数据，保留原session_id和状态，不重建账户；RUNNING/结束会话不回写历史绑定。
4. 分类冲突即使存在研究授权也禁止报价；Runner重新验元数据时包含season_type，不允许研究会话静默换比赛类别。未知赛中阶段、过期行情、订单UNKNOWN/HALTED等原有检查保留。

## 官方依据与时间

[NBA开幕赛及常规赛公告](https://www.nba.com/news/2026-27-nba-regular-season-schedule)与各场比赛页于2026-10-19独立读取。以下均为北京时间2026-10-21：

- Celtics @ Pistons：03:00，采集02:00起，市场5676016，[NBA 0022600001](https://www.nba.com/game/bos-vs-det-0022600001)。
- 76ers @ Knicks：07:00，采集06:00起，市场5676021，[NBA 0022600002](https://www.nba.com/game/phi-vs-nyk-0022600002)。
- Thunder @ Spurs：09:30，采集08:30起，市场5680507，[NBA 0022600003](https://www.nba.com/game/okc-vs-sas-0022600003)。

三场均REGULAR_SEASON，Moneyline规则验证与Paper准入通过。NBA完整JSON仍403，其失败继续保存；已核验小集合不是整联盟赛程镜像，不能据此声称任意比赛都可发现。时间或球队不再相符时，缓存证据不会自动跟随改期。

## 实际验证

`scripts/check_opening_markets.py --out <全新目录> --seconds 30`使用当时真实公开GET和市场/体育WS、三个隔离Paper账户。实际联测三场均：市场WS连接1次、体育WS连接1次、REST错误0、账本一致、真实订单调用0。输入帧数分别96/100/110，REST批次20/20/21；收到的体育更新8/11/13是整个体育流消息数，不冒充这三场已开赛更新。Celtics盘口有16个price_change，Thunder盘口有2个，76ers联测没有price_change；来源没有数据时不补造事件。

比赛尚未开始，三个账户模拟订单数均0，这符合赛前阶段要求；真实联测没有生成伪造赛中状态。另有明确SYNTHETIC_PHASE_TEST_ONLY测试，用正常化后的比赛元数据和合成体育回报，验证赛前不报价、有效赛中条件下Paper订单被模拟适配器接受，真实调用仍0。这只证明代码路径可用，不证明未来实际成交率或收益。

新增测试覆盖三场精确匹配、偏移1ms/交换主客队拒绝缓存证据、通用与旧Scanner一致、分类冲突、错误官方身份、研究授权不能绕过冲突，以及刷新预约保留session_id/不启动Runner。完整回归与实际联测证据在`evidence/opening-markets/`。

完整回归：327项，35.506秒，全部通过；JavaScript语法和Git补丁检查通过。三场原预约session_id保持不变，未触发原预约Runner或重置原账户。

## 运行结论

Data：这三场公开数据链路已联测，可按原计划采集；赛后继续自动验收。

Paper：分类准入已通过，具备运行条件；届时体育更新须证明正确比赛已开始，盘口必须新鲜且合法，才有模拟报价。赛前NO_QUOTE，缺可信赛中数据也NO_QUOTE。未知剩余时间不伪造，原有研究阶段语义保持；不能承诺持续报价、模拟成交或完整覆盖。

Live：NO-GO。市场restricted标志没有改动，未接真实钱包/签名/订单发送，不据Paper联测推断账户资格。

Windows任务时间不改，仍北京时间10月20日23:00唤醒调度；各场赛前一小时才开始记录，可信比赛结束后再录一小时。电脑须登录、联网且不休眠；若缺可信终场信号，硬上限结束并标记不完整。控制台重启后查看/offline第5节可见常规赛与Paper准入状态。
