# 项目基础设施

阶段 1 提供 [compose.yaml](compose.yaml)、独立账号初始化及[验证版本锁](versions.lock.json)。后续 YARN/Hive/Spark、ClickHouse、调度和应用部署均为**设计约定，尚未实现**。当前 full profile 只包含已实现的三个基础设施服务，不能将其健康视为完整平台通过。

实际验收见 [当前状态](../../docs/current-status.md)。宿主机使用固定 Python，容器使用镜像自身环境；不挂 Docker socket 或宿主机虚拟环境。没有主机端口发布，三个服务仅在本项目 internal 网络通信，宿主机任务通过固定 Compose exec 调用 HDFS。

## 准备与运行

将 [部署示例](../../configs/platform.example.yaml)复制为忽略的 platform.local.yaml，填仓库外专用 persistence_root 和完整磁盘预算；数据配置仍使用 application.local.yaml。初次 prepare 要求持久化根为空，使用项目标志防止接管已有数据；Hadoop 目录属主要求 uid 1000。先核验镜像锁，再显式拉取锁定摘要；管理脚本 start 不隐式下载镜像。

```bash
PYTHONPATH=src /home/qjj/.venvs/py312/bin/python scripts/platform.py prepare --platform-config configs/platform.local.yaml --data-config configs/application.local.yaml
PYTHONPATH=src /home/qjj/.venvs/py312/bin/python scripts/platform.py start --platform-config configs/platform.local.yaml --data-config configs/application.local.yaml
PYTHONPATH=src /home/qjj/.venvs/py312/bin/python scripts/platform.py status --platform-config configs/platform.local.yaml --data-config configs/application.local.yaml
```

首次准备优先消费部署环境 DDP_ADMIN_PASSWORD、DDP_CONTROL_PASSWORD、DDP_HIVE_PASSWORD、DDP_SCHEDULER_PASSWORD；未注入时为本地实验生成随机值。秘密只写专用外部根下 0600 的 compose.env，根目录 0700；不打印/提交该文件，不显示含秘密的 Compose 完整展开结果。再次 prepare 不轮换密码。初始化失败时先保留数据目录和日志检查，不能随意删除目录后伪装成功。

PostgreSQL 控制/Hive/调度账号各自拥有同名数据库，并撤销 PUBLIC 的连接及临时权限。管理员默认库 ddp_admin 用于基础运行，不承载业务控制表。阶段 1 的 ddp_infra_acceptance 只用于真实持久化验收，无业务发布能力。

## 停止及重建

```bash
PYTHONPATH=src /home/qjj/.venvs/py312/bin/python scripts/platform.py stop --platform-config configs/platform.local.yaml --data-config configs/application.local.yaml
PYTHONPATH=src /home/qjj/.venvs/py312/bin/python scripts/platform.py recreate --platform-config configs/platform.local.yaml --data-config configs/application.local.yaml
```

stop 保留容器和所有数据；recreate 只重建本项目三个容器，保持绑定目录。NameNode 只有空目录可初次格式化，非空且缺 VERSION 明确失败。不要使用全局 prune、down -v、清理共享缓存或删除其他项目卷。版本、网络归属和绑定路径与当前配置不符时管理脚本拒绝操作。
