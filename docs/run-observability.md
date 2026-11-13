# 运行状态与血缘

`run-show` 原样读取显式 schema v1 报告，旧格式明确拒绝；`run-list` 和 `lineage` 只读、不获取修改锁、不生成业务产物或查询报告。Python 接口为 `common.run_query.list_runs/normalized`、`lineage.lineage`。CLI 与 API 复用查询层，无新增依赖或迁移。默认处理版本为 `warehouse-v6`。

运行报告记录各阶段状态、复用、耗时、输入/产物身份和结构化错误。血缘根据完成清单关联 ODS、数仓、HMS 目录和 serving generation，不把清单关联冒充全量数据完整性检查。

结果与失败：完整流程报告位于外部产物根 `reports/pipeline-<ID>.json`；验证每轮写独立 `reports/observability-<ID>/report.json`。查询不存在、损坏或不支持的单份报告时明确报错，列表跳过并返回警告；超过 5000 份匹配报告时须先人工归档。单份 JSON 限 8 MiB，拒绝路径越界及符号链接。
