# 正式多模态图文检索部署

## 日常使用

正式部署完成后，使用统一入口启动数据库、Metastore、本地模型和原18080 API：

```sh
./platform.sh embedding-service-up
./platform.sh embedding-service-status
# 打开 http://127.0.0.1:18080
./platform.sh embedding-service-stop
```

停止命令只管理本项目登记的组件，不停止Docker daemon。Spark按既有批任务入口运行；统一启动不启动空闲Spark任务。模型、缓存、备份和演练数据卷均保留。

原 `api-up` 在已启用正式部署时自动绑定正式登记；它不会自动启动模型。尚未正式部署时，默认API仍禁用检索。隔离18081实例继续要求trial，与正式环境互斥。

CLI必须显式选择环境：

```sh
./platform.sh embedding-search --deployment <正式部署ID> \
  --generation <产物绑定的业务generation> --text '路口有汽车' --limit 20
```

部署ID从 `embedding-service-status` 获取。筛选、超时、版本绑定和报告脱敏沿用[检索说明](embedding-search.md)。

## 首次部署与恢复演练

以下操作串行运行，每轮产生独立报告；维护窗口要求18080 API原本停止。只使用既有真实产物，不重编码图片。

```sh
./platform.sh embedding-deploy-rehearse
./platform.sh embedding-deploy-verify --deployment <返回的演练ID> --faults
./platform.sh embedding-deploy-apply --deployment <同一演练ID>
```

`rehearse`读取正式库一致性快照并完整备份，在新建、独立登记的55435恢复实例校验业务指纹、迁移幂等、真实向量全量回读和发布回滚。`verify --faults`明确选择重建该演练数据库，验证文本HTTP与独立余弦、完整备份恢复和重启持久化；不操作另两个向量试验库。

`apply`要求前述演练通过，源业务指纹及迁移摘要仍一致。先实际恢复本轮最终业务备份，再将原正式数据卷切换到锁定的PostgreSQL 16.15＋pgvector 0.8.7镜像，执行增量迁移、装载与发布。含向量备份还需在独立演练库恢复并通过真实多模态图文检索；正式API与原业务回归通过后，才启用本地正式部署指针。业务活动指针不随向量发布改变。

`001_embedding.sql`保持原摘要。`002`增加部署身份及数据库约束；`003`修复跨表触发器字段访问，保留已执行迁移，不覆盖历史文件。正式与恢复演练均拒绝人工测试空间。容器、端口、卷、镜像、数据库OID和集群身份必须与登记一致；恢复须在指纹核对后显式重新绑定，不能自动接管任意恢复库。

## 回退

向量版本回退使用现有 `embedding-rollback --deployment ... --embedding-generation ... --expected-revision ...`，只切换向量活动指针。

首次部署的整库回退：

```sh
./platform.sh embedding-deploy-rollback --deployment <正式部署ID> --faults
```

此命令会先完整备份现状，再恢复部署前业务库、扩展状态与原镜像，并禁用正式检索。要求API停止、当前部署身份一致，且部署后数据库指纹完全未变；存在后续业务写入或向量发布时拒绝覆盖，需制定保留新写入的恢复方案。

## 最终组合验收资源门禁

统一服务启动后执行 `./platform.sh embedding-deploy-final-preflight`。此命令只核对最低备份/恢复增长，返回0也不代表1800秒联调通过；两轮新业务产物仍需另计。资源不足返回2并记录RESOURCE_BLOCKED，不能自动清理保留卷、备份或扩大64 GiB预算。

## 报告与失败处理

外部 `reports/embedding-deploy-{rehearsal,verify,apply,rollback}-<ID>/` 保存独立报告、备份摘要与归档目录。任务报告位于 `embedding/reports/`。部署登记、正式启用指针位于外部 `embedding/deployments/`、`embedding/production.json`，不提交仓库；备份旁的旧运行配置可能含凭据，权限为600。

遇到资源不足、源指纹变化或身份错误即停止，不清理缓存/备份或自动扩大预算。首次升级中途失败时保留报告与当前数据库；禁止在已有向量表的卷上手工换回无pgvector镜像。先核对完成阶段和备份，再决定恢复。首次部署入口不承担已有正式部署的任意后续升级。

实现与真实验收、资源余量及最终30分钟组合验收状态统一见[当前状态](current-status.md)。短轮验证不替代最终联调。


用户已确认项目总占用上限为64 GiB；宿主与Docker盘仍各保留至少12 GiB。最终组合验收入口：

```sh
./platform.sh embedding-service-up
./platform.sh verify integration --duration 1800
./platform.sh embedding-service-stop
```

验收前额外预留8 GiB增长。启用正式向量部署时，组合入口自动增加一个串行文本/图片探针，与五个业务客户端并行；报告分别记录业务P95及编码/SQL/总耗时，并核对独立余弦结果和图片摘要。30分钟报告在外部 `reports/integration-*/`。命令失败仍需查看本轮报告和服务初始状态，不能用历史报告抵消。停止命令仅用于本轮原本停止的项目服务；原本在线的服务应保持运行。

新机器首次部署演练必须在应用配置 embedding.deployment_artifacts 中显式提供 main 与 auxiliary 两个不同的真实产物ID。演练、恢复及正式应用保存并沿用这组身份，不再依赖原机器的固定产物ID；旧演练报告缺少该身份时须重新演练。已有正式服务日常启停按当前业务/向量活动指针选择空间。原有指定试验库验收脚本仍保留原始验收身份限制，不能通过放宽名称代替新环境验收；外部服务只读验收使用 verify embedding-external。
