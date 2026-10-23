# Driving Data Platform

独立的自动驾驶数据平台，规划接入 nuScenes，构建分层数仓、多模态检索与可追溯的数据发布能力。

**当前已确定开发框架，仓库仍为工程骨架，所有业务能力尚未实现。** 本机已有官方 nuScenes mini 和完整 Embedding 权重，并完成文件级校验；尚未验证推理或组件集成。本轮仅更新文档，没有安装依赖或启动服务。文档中的接口、表结构及部署说明均为“设计约定，尚未实现”。

## 技术方向

Python 3.12、Hadoop/HDFS/YARN、Hive、Spark、DataX、DolphinScheduler、PostgreSQL/pgvector、ClickHouse、本地 Qwen3-VL-Embedding-2B（目标输出 1024 维）；前端技术待选。组件兼容性尚待实测。

开发方式确定为：本机编辑、调试 Python 代码，Docker 运行数据库和大数据组件；按阶段启动，避免完整技术栈常驻。项目虚拟环境固定为 `/home/qjj/.venvs/py312`，不另建虚拟环境。GPU 容器化留待兼容性验证，不作为首轮编码验证的前提。

## 文档入口

- [当前状态与本机资产](docs/current-status.md)
- [架构](docs/architecture.md)
- [数据模型](docs/data-model.md)
- [流水线](docs/pipelines.md)
- [Embedding](docs/embedding.md)
- [服务与发布](docs/serving-and-publishing.md)
- [部署规划](docs/deployment.md)
- [路线图](docs/roadmap.md)
- [配置说明](configs/README.md)、[应用示例](configs/application.example.yaml)、[环境变量示例](.env.example)
- [开发约束](AGENTS.md)、[工程元信息](pyproject.toml)

## 目录导航

`src/driving_data_platform/` 预留 ingestion、embedding、warehouse、publishing、api 和 common 子包，当前均为空包。`sql/` 预留 Hive、PostgreSQL、ClickHouse 脚本；`workflows/` 预留 DataX 与 DolphinScheduler 任务；`infra/` 预留 Docker、Hadoop、Hive 和 Spark 配置；`frontend/`、`scripts/`、`tests/` 分别预留界面、维护工具及测试。

## 当前边界

没有 SQL 建表脚本、Compose 文件、API 路由、Spark 作业、前端工程、业务测试或业务命令入口。示例配置不可直接用于部署，无需执行安装或启动命令。数据及权重已在仓库外管理，示例不写真实资产路径。下一步按路线图从配置与资产预检开始，逐项增加实现及相应测试，不一次性启动完整平台。
