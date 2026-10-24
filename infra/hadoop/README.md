# HDFS 阶段 1

单 NameNode/DataNode、复制数 1。当前 [Hadoop 配置](config/hdfs-site.xml)及[Compose](../docker/compose.yaml)提供独立持久化、日志和资源限额。YARN、Spark 和 Hive 均为**设计约定，尚未实现**。

本地 ODS 上传任务：

```bash
PYTHONPATH=src /home/qjj/.venvs/py312/bin/python -m driving_data_platform.ingestion.hdfs --config configs/application.local.yaml --snapshot <snapshot_id>-nuscenes-ods-v1 --env-file <外部持久化根>/compose.env
```

只接收固定格式快照身份；从只读 /input-ods 挂载上传 13 个原始 JSON、13 个 JSONL、文件清单、质量报告和本地 complete.json 共 29 个产物，不上传任何原始图片/点云。先本地摘要校验，上传到 HDFS 同一父目录的 .staging-UUID，逐文件回读核对 SHA-256 和大小，然后重命名到最终路径，最后原子提交 _COMPLETE.json。只有含有效远端完成清单且摘要一致的路径可被下游消费。

远端路径 /ddp/ods/snapshots/<snapshot_id>-nuscenes-ods-v1；重跑完整校验清单和文件后 reused=true，不重复快照。上传 run_id 沿用 ODS 首次 run_id，输入身份不变，上传处理版本为 hdfs-ods-v1。generation_id 不适用于阶段 1。

重命名前中断保留诊断暂存；重跑建立新的暂存，不消费旧暂存。重命名后完成标志前中断，重跑完整核验最终文件及本地清单后补交标志，recovered=true；不追加或覆盖已提交产物。失败/超时结果不确定时退出 1，不自动重试；配置/质量失败退出 2；成功 JSON 及退出 0。阶段 5 尚未实现临时错误细分与执行 API，不把所有 I/O 错误当作可重试。

所有上传使用项目外同一 flock 串行锁；仅支持本机单执行器协议。远端 SHA-256 回读逐文件在宿主机完成，实际 mini 全量验证约 100 秒；此耗时不是页面查询指标。诊断暂存不自动清理，恢复操作仅限项目资源；HDFS 副本 1 不提供冗余或完整备份能力。

真实故障测试须显式设置 DDP_REAL_HDFS_ENV_FILE 指向外部 compose.env，再运行 tests/test_hdfs_integration.py。测试用项目自有小样本产生独立快照，报告标注 fixture=true，保留暂存和测试产物；默认回归不会连接服务。真实 mini 的对账报告另存，不以测试样本替代。
