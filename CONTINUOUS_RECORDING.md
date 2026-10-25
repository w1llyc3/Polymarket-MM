# 后续NBA比赛连续只读采集

Windows任务 `NBA-MM-Continuous-Recording` 已启用并立即运行，使用项目pythonw启动 `nba_recorder.recording_service`。每天午夜和登录时触发，已有实例运行时忽略新实例；进程异常退出由任务计划重启。守护进程持有项目级锁，每30秒检查；日期调度继续使用原有逐日锁与每场独立持久化账户，不创建真实交易权限。

每天自动维护北京时间当日和次日；凌晨7点前也检查前一日的跨夜尾段。每场计划开赛前30分钟开始，首次核实比赛结束后再记录30分钟。比赛身份/结束证据不可靠时，原记录器保留缺口/硬上限状态，不伪称完整。比赛发现每120秒刷新，已发现的比赛独立等待开赛窗口。只支持已发现的Polymarket.com NBA Moneyline，不能把市场发现清单当作官方全联盟赛程完整证明。

只读采集由 `--record-only` 强制，UNKNOWN分类不授予Paper或实盘报价许可。控制台8765作为独立子进程维护；健康实例复用，不终止外部服务或无响应的未知写者。关闭网页不停止采集，采集不依赖本聊天持续打开。

电脑必须保持开机、联网、不休眠并保持用户登录；锁屏可以，注销或关机不能采集。重启后登录可恢复计划；无法恢复关机期间错过的数据。日志和数据不自动删除，需保留足够磁盘空间。

## 扫描日志体积控制（2026-10-26）

逐日扫描使用 `discovery-summary.jsonl`（`DISCOVERY_SUMMARY_V2`）：每次保留完整标准化市场、分类、规则、时间戳、请求证据及错误，但不再重复追加庞大的未筛选 `raw_events`。每个扫描器首次运行、每小时，以及距上次完整快照至少5分钟的状态变化，另外保存一份完整响应到 `discovery-snapshots/*.json.gz`。`raw_response_saved` 和 `raw_snapshot` 明确标记本次是否有完整响应；未留样的轮询不能冒充保存了完整原始响应。游标持久化，重启不重新重复首份快照。原有 `discovery.jsonl` 不被重写或截断。

这仅改变发现扫描的诊断保存方式；每场的原始盘口、成交、体育状态、策略可见输入及采集窗口保持原样。历史原始行情和完整Paper研究结果不自动清理。

本次一次性整理只涵盖22/23/24日已退出扫描器的日志、21日5个中断回放目录。`scripts/archive_storage_20261026.py` 先生成 ZIP64，逐文件完整解压并核对大小及SHA-256，保留SQLite配套WAL/SHM。`scripts/remove_verified_storage_20261026.ps1 -Execute` 再核对源文件与归档哈希、目录边界和空闲写锁，才移除未压缩原件。归档及清单在 `data/archives/storage-cleanup-20261026/`；默认不带 `-Execute` 只验证，不删除。恢复时先解压到另一个目录核对，不能覆盖正在使用的数据库或调度状态。压缩归档仍在本机，不能替代异地备份。

## 安装与检查

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/install_recording_service.ps1
Get-ScheduledTask -TaskName NBA-MM-Continuous-Recording
Get-Content data/unattended/service/state.json
```

不要手动启动同一天的第二个采集进程。后台健康状态在 `data/unattended/service/state.json`，逐日市场及状态在 `data/unattended/YYYY-MM-DD/state.json`，日志在service目录。原始行情在 `data/console/<session_id>-source.sqlite`，策略可见记录在 `<session_id>.sqlite`。这些OBSERVATION账户不会创建模拟订单。控制台 http://127.0.0.1:8765/ 。

本聊天另外启用了“NBA 后续比赛采集检查”每小时巡检：核查进程、心跳、缺口与完成状态，只在故障、需要操作或新比赛日完成时通知，避免重复通知。准确采集时间由Windows后台执行，巡检不是采集定时器。巡检依赖本机Codex可运行；不影响独立Windows采集。

## 本次实际验证

- Windows任务Running，服务心跳RECORD_ONLY，pre/post均30分钟，live_enabled=false。
- 8765健康接口HTTP200，项目root匹配，owner_alive/owner_responsive均true。
- 已发现2026-10-24的12场真实市场，均已排队；这不等于采集已经完成。
- 2026-10-23发现两场市场，已有错过窗口的场次标记MISSED_WINDOW_NOT_COMPLETE；晚启动数据不得视作完整赛前采集。
- 完整测试343项通过，0失败，43.766秒；报告 `data/continuous-recording-full-tests.txt`。其中覆盖跨日、前日尾段、工作进程去重、异常重启等待、只读参数、旧模式保护及控制台复用。
- 不更改其他仓库、真实账户、钱包或实盘开关。
