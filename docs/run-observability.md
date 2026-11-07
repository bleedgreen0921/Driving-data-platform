# 运行状态与轻量血缘

```sh
./platform.sh run-list --limit 20 --offset 0
./platform.sh run-list --status failed
./platform.sh run-show --run-id <32位运行ID>
./platform.sh lineage --run-id <32位运行ID>
./platform.sh lineage --generation <generation> > lineage.json
./platform.sh verify observability --faults
```

`run-show` 原样读取显式schema v1报告，旧格式明确拒绝；`run-list` 和 `lineage` 只读、不获取修改锁、不生成业务产物或查询报告。Python 接口为 `common.run_query.list_runs/normalized`、`lineage.lineage`。CLI 与 API 复用查询层，无新增依赖或迁移。默认处理版本仍为 `warehouse-v5`。

结果与失败：完整流程报告位于外部产物根 `reports/pipeline-<ID>.json`；验证每轮写独立 `reports/observability-<ID>/report.json`，统一入口另有轮次日志。查询不存在、损坏或不支持的单份报告时明确报错，列表跳过并返回警告；超过 5000 份匹配报告时须先人工归档。单份 JSON 限 8 MiB，拒绝路径越界及符号链接。

API：`GET /api/runs?status=&limit=20&offset=0`、`GET /api/runs/{run_id}`、`GET /api/lineage?run_id=` 或 `?generation=`。运行列表 limit 为 1–100；状态参数省略表示全部；血缘参数必须且只能有一个。非法参数 422、不存在 404、证据损坏 409、数据库不可用 503。运行查询在数据库离线时仍返回文件报告，发布状态未知并警告。API 不回传原始异常、日志、绝对路径或连接配置；错误类别和恢复建议可用，详细异常由本机 `run-show` 核对。

工作台有独立运行区和血缘区。运行区展开且页面可见时每 5 秒刷新，支持状态筛选、分页、详情。运行详情和所选 generation 均可查询血缘并下载 JSON；没有运行或发布操作按钮。

报告记录 `running` 只表示最后一次写入状态，未核实进程存活，不按年龄推断失败。已完成阶段显示记录耗时，未完成阶段单列持续时间估算。旧报告缺失字段保留未知，排序使用文件修改时间并标注来源；运行成功、曾发布、当前活动分别展示。

血缘只检查清单与身份关联，不重新扫描媒体/Parquet，不证明当前 HMS 服务中实际表仍然存在。集合节点保存文件清单摘要和条目数，避免将全部媒体展开。`matched` 表示清单关联已核对，`recorded_only` 表示仅有历史记录，`inconsistent` 表示冲突，`unrecorded` 表示缺少用于核对的历史摘要。`partial` 表示整体证据不完整。失败运行只展示报告已记身份，旧 JSON 链路不补造 HMS。

人工审阅的 [表依赖声明](../configs/table-lineage-v1.json) 覆盖 11 组产物、直接上游、实际 serving 表与 dimension kind。查询同时核对声明、当前 SQL 及目标生产清单的 SQL 摘要，缺失或不匹配时表级依赖为 unknown，保留可证实的产物链路。修改 SQL 后须人工重新审阅声明；不能直接把新声明套到旧版本。

验证入口执行同版本真实重跑、阶段观察、现有/历史 generation 与真实 HTTP 核对；`--faults` 额外执行独立配置坏输入及复制清单的缺失/冲突，不修改正式输入和活动 generation。验证不执行持续并发观察、5 分钟替代测试或备份恢复。浏览器检查需另行完成，最新边界见[当前状态](current-status.md)。
