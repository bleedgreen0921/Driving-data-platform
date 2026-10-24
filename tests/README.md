# 验证范围

默认回归只使用本项目生成的小样本，不连接真实组件：

```bash
/home/qjj/.venvs/py312/bin/python -m pytest -q
```

当前 73 项通过，另有 2 项真实 HDFS 故障测试默认跳过。覆盖配置/资产证据、ODS 身份/质量/提交/并发、部署字段不改变快照、资源门禁边界与缺口、HDFS 输入路径/远端清单及摘要错误。原 50 项 ODS/预检回归保留；共享小样本 fixture 移入 conftest.py 供组件验收使用。

真实 HDFS 故障测试仅在显式设置 DDP_REAL_HDFS_ENV_FILE 后运行 tests/test_hdfs_integration.py。测试使用真实 HDFS、项目自有小样本和客户端故障注入，验证提交前中断无完成标志、重跑恢复。它会在外部项目 ODS/HDFS 中留下独立测试快照和诊断暂存，不修改真实 mini，不自动清理。每次运行使用独立 fixture 身份；报告保存于外部 execution-baseline-20261024，标注 fixture=true。

真实 mini 的首次上传、重复上传与重建后 29 项产物回读由独立报告记录，不能用小样本测试替代。PostgreSQL 实测验证三次本库连接成功、六次跨库连接拒绝及容器重建数据保留。最终 Hive/Spark、发布、调度、页面、回滚和全栈验收仍为**设计约定，尚未实现**，不能从默认测试数推断通过。
