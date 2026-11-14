# 驾驶数据质量与样本筛选平台

可配置的本机 nuScenes mini → Python ODS → Docker Spark SQL local/Parquet → Hive Metastore 表名消费 → PostgreSQL → FastAPI/原生静态页面。

```sh
./platform.sh up
./platform.sh run
./platform.sh api-up
./verify.sh dependencies repository regression preflight warehouse api
```

默认处理版本为 `warehouse-v6`，ODS 版本为 `nuscenes-ods-v2`。数据契约只有一个：[contracts/nuscenes-mini-v2.json](contracts/nuscenes-mini-v2.json)。资产完整性由 `dataset.assets.policy` 控制：

- `strict`：元数据与引用资产都必须存在并校验摘要。
- `registered`：元数据校验摘要，其他资产登记路径和状态，不阻塞加工。

详细设计与运行记录保存在本地 `docs/`，不随仓库发布。默认工作台：<http://127.0.0.1:18080>，可配置宿主端口。首次准备、版本更新/回滚和故障处理见本地 `docs/deployment.md`，当前状态见 `docs/current-status.md`，剩余修改见 `CHANGE_REQUESTS.md`。产物、日志、密码和备份均在仓库外，不自动提交 Git。

契约引用、运行报告及发布差异见本地 `docs/run-evidence.md`。运行列表、阶段详情与表级血缘见本地 `docs/run-observability.md`。

图片清单、pgvector 隔离验证及向量版本管理见本地 `docs/embedding-foundation.md`。已实现真实编码、文本检索 CLI 及隔离库检索 API 与原生页面，见本地 `docs/embedding-web.md`；正式部署与统一启停见本地 `docs/embedding-deployment.md`。本次图片上传查询见本地 `docs/embedding-image.md`，只用于本次编码与检索，不自动入库。

真实图文编码、模型独立启停及可恢复向量文件加工见本地 `docs/embedding-encoding.md`。文本检索 CLI 与隔离真实向量验收见本地 `docs/embedding-search.md`。跨机器配置、Python选择及端口覆盖见本地 `docs/deployment.md`，本地/外部模型与独立空间见本地 `docs/embedding-backends.md`，旧命令替代关系见本地 `docs/command-migration.md`。
