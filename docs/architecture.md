# 单机离线平台架构

nuScenes mini → Python 预检/不可变 ODS → Docker Spark SQL local → 本地 Parquet → Hive Metastore 外部表 → Spark 按表名消费与独立对账 → PostgreSQL → FastAPI/原生页面。

默认 run/load 必须经过 Metastore，不旁路读取原数仓 JSON。Parquet 完成清单负责完整性，Metastore 负责表目录，JSON 保留为校验副本及消费后的交换格式。登记成功不能代替数据质量检查。

原始媒体只读保留在仓库外，Spark 批任务并发为1。产物采用不可变 generation 与 CAS 活动指针，新加工期间旧版本保持可读。

输入完整性不产生第二套代码路径。每个快照记录实际资产状态：

```text
metadata      -> 始终校验摘要
camera_image  -> strict 校验文件；registered 只登记状态
sensor_data   -> strict 校验文件；registered 只登记状态
map           -> strict 校验文件；registered 只登记状态
```

下游只读取快照的 `assets` 与 `capabilities`，不判断“模式”。

多模态图文检索以已登记关键帧相机图片为单位。本地 llama 与外部 embedding 服务经统一接口编码，PostgreSQL + pgvector 按余弦距离精确检索。向量空间和向量版本独立维护并绑定业务 generation，真实查询向量必须与库内图片向量属于同一已验证空间。更换后端必须独立建库。

模型推理与结构化加工分离。外部后端不管理远端进程；其协议适配、真实能力与验收状态分别记录。不计算轨迹速度，不做点云三维渲染，不规划分布式集群或机器级高可用。

Python 3.12、目录和宿主端口通过配置选择。配置见[部署说明](deployment.md)，外部接口见[后端说明](embedding-backends.md)，通过及未通过范围见[当前状态](current-status.md)。
