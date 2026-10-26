Hive Metastore 是默认 Parquet 消费链路的表目录服务，不运行 HiveServer2、存储集群或资源管理服务。

`./platform.sh up` 启动正式服务；首次使用 `postgres-up`、`init-db` 和 `hms-prepare` 显式准备。`hms-validate` 检查真实元数据库 schema。密码和 JDBC 在仓库外，镜像与 JAR 摘要在本目录。

`metastore-restore` 仅在备份验证时短时启动，不代替正常服务，不切换活动元数据库。完整入口见[正式目录消费](../../docs/metastore-validation.md)。
