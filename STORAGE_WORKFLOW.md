# 数据保留与压缩工作流程

采集仍为 Record-only；存储维护不下单、不重置账户、不删除真实行情。

## 完成一次研究后的流程

1. 冻结行情输入，保留原始时间戳及同毫秒内的观察顺序。
2. 独立运行各 Paper 模型，生成订单、成交、FIFO 账本及研究结果。
3. 发布网页、图表、收益汇总、参数、来源引用和共享输入。账本检查必须通过。
4. 所有模型写者关闭后，自动压缩该研究目录下的详细数据库和 JSONL 导出。
5. 逐文件比较压缩前后 SHA-256、长度和修改时间；保存实际磁盘占用和失败原因。
6. 网页、图表和历史审计按原路径读取，Windows 自动解压读取，无需建立临时恢复库。

这是 **NTFS/WOF 原位无损压缩**，不是独立备份，也不是删除后仅剩汇总。
文件“大小”不会变，实际“占用空间”下降；复制到其他文件系统可能恢复为未压缩大小。
此前 `data/archives/storage-cleanup-20261026` 的 ZIP 归档和清单继续保留。

## 范围与排除

- 压缩：`data/day-studies/**/paper.sqlite`、`data/strategy-studies/**/paper.sqlite`、`data/research/**/paper.sqlite` 和同级 JSONL 导出。
- 必须存在 `result.json`，其中 `shutdown_ledger_consistent=true`、`real_submission_calls=0`。
- 持有原有 Paper writer 锁；有未清空 WAL/journal、未完成目录、路径重定向时跳过。
- 压缩旧 `data/unattended/<日期>/discovery.jsonl`：须获取空闲 scheduler 锁，或确认该文件已被 V2 发现日志替代。V2 日志自身不在压缩范围。
- 保留不处理：`data/console` 原始采集库、source-tape、shared-inputs、所有图表/HTML/JSON/CSV、账户与历史结果。
- 错误不会删除任何源文件。逐文件失败会保留在维护报告，CLI 返回非零状态。

## 命令

在仓库根目录运行（Windows NTFS）：

```powershell
# 只列出候选文件和跳过原因
.\.venv\Scripts\python.exe -m nba_recorder.storage_workflow plan

# 已完成研究 + 退役旧日志，逐文件压缩及验证
.\.venv\Scripts\python.exe -m nba_recorder.storage_workflow apply

# 只处理一个完成的研究目录
.\.venv\Scripts\python.exe -m nba_recorder.storage_workflow apply --scope data/research/<研究ID>

# 可选更紧凑、但更耗 CPU 的算法；不要在比赛采集高峰做批量维护
.\.venv\Scripts\python.exe -m nba_recorder.storage_workflow apply --algorithm LZX

# 启用新研究完成后的自动维护；仅首次设置，已有配置不要覆盖
Copy-Item storage-policy.example.json data/storage-policy.json
```

维护清单：`data/storage-workflow/*.json`。其中 `allocated_before/after` 是实际磁盘占用，
`bytes` 是逻辑文件大小；`saved_bytes` 是本次节省量。已经压缩的文件可以重复验证。
不支持的系统会明确失败，保留文件；没有静默删除或迁移路径。
自动维护默认关闭，设置文件仅控制研究完成后的压缩，不控制行情采集。
维护失败保留研究结果并输出 `STORAGE_MAINTENANCE_FAILED`；无法启动维护时另写 `storage-maintenance-error.json`。

## 新离线回放的共享输入

新 `research.engine`、`day_study`、`rotation_study` 实验使用每组模型共用的
`shared-inputs.sqlite`：相同完整输入以 SHA-256 标识，UTF-8 内容 zlib 无损压缩。
各模型仅在 `ip_input_refs` 保存自己的事件 ID、时间及顺序；订单、成交和账本仍独立。
不同解码或不同内容产生不同摘要，不因为时间相同而合并。

`ip_inputs` 兼容视图通过应用的 `read_connection` 读取原始内容；普通 SQLite 工具
查看该视图需要注册 `nba_input_read` 函数，建议使用项目的审计/导出入口。
复制或备份新研究时必须携带整个研究目录及 `shared-inputs.sqlite`，不能只复制某个模型的 `paper.sqlite`。
共享库丢失会明确拒绝读取/恢复，不会重建空输入库。共享内容禁止修改、删除；
事务回滚可能留下未引用内容，但不会留下指向未持久化内容的引用。

已有真实采集库和旧研究库不迁移；source-tape 与派生共享池分别承担冻结证据和实际模型输入职责，
仍有这一层有意保留的重复。新方案不是对所有历史数据进行全局去重。

新发现日志每轮保存规范化元数据，原始大响应只在首次、每小时及限频状态变化时 gzip 保存。
它是明确的采样策略，不声称保存每次发现接口的完整原始响应；行情事件采集不受此策略影响。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_storage_workflow tests.test_shared_inputs -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

覆盖无损压缩后的 SQL 读取、活动写者排除、WAL 排除、修改检测、保护范围、
共享输入的逐字节/顺序一致、模型订单/成交/账户等价、重启、重复事件、依赖丢失和事务回滚。
