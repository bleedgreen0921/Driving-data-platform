# Metastore 正式目录消费

Metastore 已设为默认链路的一部分。实现与最新真实验收统一见[当前状态](current-status.md)；日常命令见[运行说明](deployment.md)。

```sh
./platform.sh up
./platform.sh run
./platform.sh load
./platform.sh hms-validate
./verify.sh pipeline --faults
./verify.sh metastore --faults
```

`run/load` 使用稳定的生产目录身份，保存在 `metastore/generations/<ID>/`。同一 Parquet 完成清单复用数据库和表 ID、消费结果及 generation，重跑仍连接真实 Metastore 并读取数据。消费代码、字段契约或运行镜像变化时阻断同版本复用，要求新处理版本。登记与读取分别启动新 Spark 进程。

表位置为 `file:/warehouse/.../parquet`，HMS 与消费者均只读挂载正式产物。消费严格通过 `spark.table(数据库.表名)`，不会把旧数仓 JSON 当作装载源。按表名读取后生成新的服务交换 JSON，经独立 ODS 基准逐行对账并提交完成清单，再调用 PostgreSQL 事务装载。缺表、位置/版本标记不符、数据缺失、目录服务故障均阻断；已有缓存不会绕过该检查。

核对外部表类型、Parquet provider、位置、完成清单摘要、表集合、字段名、顺序和类型。目录可能归一化顶层 nullable，对生产契约要求非空的字段额外检查实际 NULL；数组元素等类型约束严格比较。完成清单继续负责数据完整性和输入身份，表目录不能替代数据验收。

运行组合为 Spark 4.2.0/JDK 21、Hive 客户端及服务端 4.1.0、HMS JDK 17、PostgreSQL 16.15，原生 arm64。Spark 客户端使用其镜像内 Hadoop 3.5.0 API/runtime，HMS 镜像包含 Hadoop 3.4.1；仅对本项目操作集作兼容性声明。339 个客户端 JAR 离线入镜像，来源与摘要在 `infra/metastore/` 锁文件。运行任务不从 Maven 下载。

元数据使用独立数据库 `ddp_metastore_v410` 和角色 `ddp_metastore`。服务库及元数据库分别备份并恢复到隔离身份，恢复后的 HMS 只读验证真实表；不自动切换现有元数据库。主服务不运行 HiveServer2、HDFS 或 YARN。

隔离登记、消费及故障恢复由统一验证入口执行，不公开单独的候选消费命令：

```sh
./platform.sh verify metastore --faults
```

默认 run/load 完成正式目录登记与消费。隔离 READY generation 不自动发布；日志位于外部 metastore/trials/<ID>/，故障只操作独立身份。

`--faults` 覆盖部分登记、缺表、错误位置、外部表删除保留文件、缺文件预检及 Spark 阻断、停机及重建恢复；文件故障仅作用于复制产物。不自动删除任何缓存或历史 generation。

配置依据 [Spark 官方 Hive 集成文档](https://spark.apache.org/docs/4.2.0/sql-data-sources-hive-tables.html)与 [Hive Java 要求](https://github.com/apache/hive/blob/master/README.md)，实际结论以本轮证据为准。分布式集群、大规模计算和机器级高可用不属于设计方向。
