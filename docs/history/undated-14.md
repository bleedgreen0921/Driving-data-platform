<!-- 历史记录：保留原阶段结论，不作为当前能力或运行状态。 -->

## 当前运行状态

2026-11-02 多模态基础改造验收后，已恢复原先停止状态：本项目正式和隔离 API、容器及项目网络全部释放，卷、镜像、缓存、备份和证据保留；未自动提交 Git。证据 `reports/embedding-final-stopped-20261102.json`。正式库仍未安装 pgvector，后续启用按[说明](../embedding-foundation.md)推进。

2026-11-02 本轮已恢复停止状态：本项目 API、容器及网络已释放，卷、镜像、缓存和全部证据保留。停机后的文件运行查询仍可返回，发布及活动状态标为未知；证据 `reports/observability-final-stopped-20261102.json`。

2026-10-28 本轮验收完成后恢复停止状态，释放本项目 API、容器及网络；镜像、构建缓存、JAR、命名卷、专用 Python 环境和外部产物保留，不执行 prune 或删除卷。日后使用 `./platform.sh up` 和 `./platform.sh api-up` 即可恢复 PostgreSQL、Metastore 与工作台，无需重新接入或加工；活动数据为已验证的 `warehouse-v4`。停止状态证据 `reports/evidence-final-stopped.json`。

