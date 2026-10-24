# Driving Data Platform

独立的 nuScenes 大数据工程项目，覆盖数据接入、分层数仓、质量校验、任务编排、分析查询及可追溯发布。

2026-10-24 明确调整：取消向量化与模型推理，保留大数据工程主线。已验收配置、数据预检、本地 ODS 与幂等重跑；HDFS、Hive、Spark、ClickHouse、PostgreSQL、调度、API 和前端均为**设计约定，尚未实现**。GPU 不再是项目依赖或验收条件。

## 目标链路

nuScenes → 本地接入及清单 → HDFS ODS → Spark / Hive DIM、DWD、DWS、ADS → ClickHouse 分析数据与 PostgreSQL 发布注册 → API → 前端。

DolphinScheduler 编排接入、计算、校验和发布；DataX 仅在实际同步来源与插件验收后接入，不假定原生支持 nuScenes。业务采用场景、关键帧、传感器和标注类别的结构化筛选、统计及清单导出，不包含语义检索或模型任务。

本机开发固定使用 `/home/qjj/.venvs/py312`；数据库与大数据基础服务使用本项目专用 Docker 资源，按阶段运行。应用沿用 FastAPI、React/TypeScript/Vite 方向；新增组件版本和依赖必须实测后锁定。

## 当前可用能力

```bash
PYTHONPATH=src /home/qjj/.venvs/py312/bin/python -m driving_data_platform.common.preflight --config configs/application.local.yaml
PYTHONPATH=src /home/qjj/.venvs/py312/bin/python -m driving_data_platform.ingestion.nuscenes --config configs/application.local.yaml
/home/qjj/.venvs/py312/bin/python -m pytest -q
```

预检默认只检查数据，不要求模型配置、权重、Torch 或 GPU。旧 `--task all`、`--task model` 已删除；`--task dataset` 仍兼容。接入保持原快照身份和产物格式；成功不等于 HDFS 或完整数仓验收。

## 文档入口

- [当前状态](docs/current-status.md)、[实施契约](docs/implementation.md)
- [架构](docs/architecture.md)、[路线图](docs/roadmap.md)
- [数据模型](docs/data-model.md)、[流水线](docs/pipelines.md)
- [服务与跨库发布](docs/serving-and-publishing.md)、[部署](docs/deployment.md)
- [已实现 ODS](docs/ingestion.md)、[配置](configs/README.md)
- [历史归档](docs/archive/README.md)、[开发约束](AGENTS.md)

`src/` 当前只实现 common 与 ingestion，其余包是预留。`infra/`、`sql/`、`workflows/` 和 `frontend/` 为对应阶段预留，尚无真实部署脚本或服务。向量代码、测试、探针及配置已从活动工程移除；仓库外模型、诊断证据和共享依赖保留，不再调用。
