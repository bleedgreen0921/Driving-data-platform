# 驾驶数据质量与样本筛选平台

本机 nuScenes mini → Python ODS → Docker Spark SQL local/Parquet → Hive Metastore 表名消费 → PostgreSQL → FastAPI/原生静态页面。

```sh
./platform.sh up
./platform.sh run
./platform.sh api-up
./verify.sh dependencies repository regression preflight warehouse api
```

工作台：<http://127.0.0.1:18080>。首次准备、版本更新/回滚和故障处理见[运行说明](docs/deployment.md)，验收事实见[当前状态](docs/current-status.md)，剩余工作见[修改清单](CHANGE_REQUESTS.md)。产物、日志、密码和备份均在仓库外，不自动提交 Git。
