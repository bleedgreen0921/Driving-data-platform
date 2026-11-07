# 驾驶数据质量与样本筛选平台

可配置的本机 nuScenes mini → Python ODS → Docker Spark SQL local/Parquet → Hive Metastore 表名消费 → PostgreSQL → FastAPI/原生静态页面。

```sh
./platform.sh up
./platform.sh run
./platform.sh api-up
./verify.sh dependencies repository regression preflight warehouse api
```

已完成正式多模态图文检索部署的环境，日常使用 `./platform.sh embedding-service-up` 启动数据库、Metastore、模型及API，使用 `embedding-service-stop` 停止；无需重新加工图片。上方命令用于既有结构化加工流程。

默认工作台：<http://127.0.0.1:18080>，可配置宿主端口。首次准备、版本更新/回滚和故障处理见[运行说明](docs/deployment.md)，验收事实见[当前状态](docs/current-status.md)，剩余工作见[修改清单](CHANGE_REQUESTS.md)。产物、日志、密码和备份均在仓库外，不自动提交 Git。

契约引用、运行报告及发布差异见[运行证据说明](docs/run-evidence.md)。

运行列表、阶段详情与表级血缘查询见[运行状态与血缘](docs/run-observability.md)。

图片清单、pgvector 隔离验证及向量版本管理见[多模态检索基础能力](docs/embedding-foundation.md)。已实现真实编码、文本检索 CLI 及隔离库[检索 API 与原生页面](docs/embedding-web.md)；真实HTTP与浏览器验收均已通过；正式部署与统一启停见[操作说明](docs/embedding-deployment.md)。已实现[本次图片上传查询](docs/embedding-image.md)：原始图片只用于本次编码与检索，不自动入库；本轮验收进度见当前状态。

真实图文编码、模型独立启停及可恢复向量文件加工见[操作说明](docs/embedding-encoding.md)。

文本检索 CLI 与隔离真实向量验收见[操作说明](docs/embedding-search.md)。

跨机器配置、Python选择及端口覆盖见[部署说明](docs/deployment.md)，本地/外部模型与独立空间见[后端说明](docs/embedding-backends.md)，旧命令替代关系见[入口迁移](docs/command-migration.md)。
