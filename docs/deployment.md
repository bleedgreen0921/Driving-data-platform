# 本机配置与运行

先设置解释器和仓库外路径；不自动寻找原机器的数据或连接配置。下面变量由部署环境填写：

```sh
export DDP_PYTHON="<专用Python3.12环境>/bin/python"
export DDP_CONFIG="<仓库外应用配置文件>"
./platform.sh --help
```

也可使用 `--config`、`--root`、`--dataset-root`。优先级为命令行、环境变量、显式配置文件、不绑定机器的默认值。配置文件中的相对路径相对该文件目录，环境与命令行相对调用时工作目录；支持 ~，不执行Shell表达式。

配置文件包含 dataset.root、dataset.profile、dataset.assets.policy、storage.output_root、assets.evidence_file；外部服务另设置 embedding.provider_file。模板见[配置示例](../configs/application.example.yaml)。

```yaml
dataset:
  profile: nuscenes-mini
  root: <仓库外数据目录>
  version: v1.0-mini
  assets:
    policy: registered   # strict | registered
```

`strict` 要求 13 张 JSON 及引用的媒体/地图文件都存在并校验摘要；`registered` 只校验元数据，其他资产登记路径和状态。两种策略共用 ODS、数仓和服务版本，不产生第二套执行路径。

端口由 ports 配置或 DDP_POSTGRES_PORT、DDP_API_PORT、DDP_MODEL_PORT、DDP_TRIAL_PORT、DDP_TRIAL_API_PORT、DDP_RESTORE_PORT、DDP_DIAGNOSTIC_PORT 覆盖，默认分别为55433、18080、18090、55434、18081、55435、18092。配置端口必须互不冲突，修改端口不允许静默复用不匹配的已有部署登记。

日常启动与执行：

```sh
./platform.sh up
./platform.sh api-up
./platform.sh run
./platform.sh status
./platform.sh usage
```

默认处理版本为 `warehouse-v6`。工作台为 <http://127.0.0.1:18080>，PostgreSQL 仅发布 `127.0.0.1:55433`；Metastore 不发布宿主端口。`up` 启动 PostgreSQL 与 Metastore，API 单独启动。已有数据可直接启动查询，首次安装须先执行一次 `run` 再启动 API。

```sh
./platform.sh run --version warehouse-v6
./platform.sh load --version warehouse-v6
./platform.sh rollback --generation g_替换为已验证版本
./verify.sh dependencies repository regression preflight warehouse api
./verify.sh pipeline publication metastore --faults
./verify.sh integration
./platform.sh stop
```

`run` 执行预检→ODS→Parquet→Metastore 登记→按表名消费→独立对账→事务装载→CAS 发布。`load` 同样经过目录消费但不发布。Metastore 不可用时必须失败，包括已有消费缓存的重跑；不会旁路消费原 JSON。`build` 仅生成并提交数仓产物。新代码或运行环境变化需要新的处理版本；旧活动版本在新批次加工期间持续可用。

结果在仓库外产物根 `reports/`，每次默认任务的逐阶段报告为 `pipeline-<ID>.json`。验证报告位于 `reports/<UTC轮次>/report.json`，正式联调明细位于 `reports/integration-<ID>/`。默认生产目录记录位于 `metastore/generations/<稳定ID>/`，故障试验位于 `metastore/trials/<ID>/`。失败查看本轮日志，不能用历史成功抵消；修复后同版本重跑，遇到已完成产物的代码/环境身份冲突须使用新版本。

验证与服务操作串行互斥。验证不隐式安装、下载或启动既有依赖，默认不覆盖快照、不停止既有服务；`--faults` 显式允许停机、缺输入及提交故障，执行后恢复。`./verify.sh integration --duration 20` 可作短预演，不能替代正式验收。`integration` 默认至少观察 1800 秒，覆盖五客户端、两轮默认流程与重跑、更新/回滚及两库备份恢复；固定负载通过不等于最大容量。

首次准备：

```sh
python3.12 -m venv <专用Python3.12环境>
"$DDP_PYTHON" -m pip install -r requirements.lock
"$DDP_PYTHON" -m pip install --no-deps -e .
./platform.sh prepare
./platform.sh check
./platform.sh prepare-images
./platform.sh postgres-up
./platform.sh init-db
./platform.sh hms-prepare
./platform.sh up
./platform.sh run
./platform.sh api-up
```

`postgres-up` 仅用于首次准备数据库及故障恢复。`hms-prepare` 显式校验并准备镜像、离线客户端、JDBC、独立元数据库和配置；正常 `up/run/load` 不进行这些准备工作。已有缓存与命名卷可复用。

Python环境、数据集根目录和仓库外产物目录由部署环境显式设置。入口支持 `DDP_PYTHON`、`DDP_DATASET_ROOT`、`DDP_OUTPUT_ROOT`；真实输入路径以外部配置为准。秘密由环境或外部 `compose.env`、`metastore/runtime.env` 提供，不进入仓库。

`./platform.sh verify backup` 验证服务库及 Metastore 库恢复到独立 `restore_*` 数据库，并使用短时隔离的恢复 HMS 按表名读取真实 Parquet；最终移除该验证容器。两库备份及报告位于 `backups/`。旧 generation、备份和恢复数据库不自动删除；`usage` 显示占用观察值。

`stop` 停止本项目 API 与 Docker 服务；模型单独使用 embedding-model-stop，正式整组服务使用 embedding-service-stop。这些入口不删除卷、镜像、JAR 或构建缓存。更多目录操作见[Metastore 说明](metastore-validation.md)。

契约、运行追踪、结构化错误与发布差异命令见[运行证据说明](run-evidence.md)。正式组合验收至少1800秒，短预演不能替代正式验收。

本轮默认处理版本为 `warehouse-v6`，ODS 版本为 `nuscenes-ods-v2`。历史版本及完成清单不改写。消费代码与模型编码实现摘要变化时，旧空间或旧处理版本若报身份冲突，必须重新评测新空间并使用新版本加工；不能修改旧摘要来绕过检查。

备份默认位于产物根的 backups，可用 storage.backup_root 或 DDP_BACKUP_ROOT 配置；备份目录不得包含产物根或与数据集重叠。不可变产物暂存仍在对应产物目录内，以保证原子提交，不提供跨文件系统移动暂存的选项。

当前Python环境没有pip时，可使用已有uv执行 `uv pip install --python "$DDP_PYTHON" -r requirements.lock` 与 `uv pip install --python "$DDP_PYTHON" --no-deps --no-build-isolation -e .`。依赖验证自动使用当前环境的pip或PATH中的uv；不会为检查自动安装工具。

## 资产策略

配置 `dataset.assets.policy`：

```yaml
dataset:
  assets:
    policy: registered
```

`registered` 使用仓库外的13张 JSON，媒体、地图等资产缺少时仍可完成结构化摄取、数仓和发布；媒体接口返回409，页面显示“媒体未校验”。`strict` 需要完整引用文件，缺媒体时在摄取阶段失败。

页面的通道和媒体数表示登记关系；`capabilities.original_media` 为 false 时，原图核对和图文检索不可用。导出清单携带 profile、asset policy 与 capabilities。

pgAdmin 使用已安装桌面版，先备份配置库，再通过自带 `setup.py load-servers <servers.json> --no-replace` 增量登记。连接 `127.0.0.1:55433`、数据库及用户 `ddp`；专用 PassFile 放仓库外并设为0600，不输出密码。需实际连接、展开三个 schema、查询源表及活动 generation，CLI 登记成功不等于界面接入通过。

NocoDB 表格界面的独立配置与启停见[运行说明](../infra/nocodb/README.md)。它使用独立应用库，通过只读账号访问 `ddp`。
