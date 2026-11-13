# 多模态检索基础能力

本页说明图片清单、空间契约、向量产物及隔离数据库操作。真实编码、检索API和正式部署已经交付过；最新重构与验收边界以[当前状态](current-status.md)为准。人工向量仅可用于明确登记的试验环境。

## 日常命令

```sh
./platform.sh up
./platform.sh embedding-input --generation <业务generation>
./platform.sh embedding-input --generation <业务generation> --verify-files
./platform.sh embedding-prepare
./platform.sh verify embedding --faults
```

`embedding-input` 从 READY generation 的关键帧相机登记生成不可变输入，不硬编码图片数量。`--verify-files` 逐张检查真实 JPEG 的安全路径、大小、格式与 SHA-256；失败保留独立报告。登记清单生成成功不表示文件核验通过。

构建是显式操作：`embedding-prepare` 下载锁定的 pgvector 0.8.7 源码，核对来源摘要，在现有 PostgreSQL 16.15 arm64 镜像之上构建。源码在外部产物根；仓库仅保存构建规则及锁。正常服务启动不隐式安装扩展或构建镜像。无新增 Python 依赖。

单项命令另写 `embedding/reports/task-<ID>.json`，包含输入、阶段、耗时、结果身份及结构化失败，不混入既有 pipeline 报告。

真实验证每轮独立写 `reports/embedding-<运行ID>/report.json`，统一入口另写轮次日志。验证使用真实图片、正式库只读备份及独立恢复库；`--faults` 允许在独立试验库注入装载前失败与提交确认丢失。数据库竞争仅为短时控制正确性核验，不是持续压测。

重复验证时可复用报告中的 trial，避免累积完整恢复卷：

```sh
./platform.sh verify embedding --trial <32位trial> --faults
```

指定 trial 意味着允许重建该试验库：先保存完整备份，再串行重建恢复，核对全表指纹。该行为仅用于本项目明确登记的独立试验库，不修改正式库或其他试验卷。恢复副本不同时保留两套完整业务数据；备份、指纹、镜像和命名卷保留。

## 文件及身份

外部产物目录：

- `embedding/inputs/<input_id>/input.json`：指定业务 generation、生产清单摘要及按媒体 ID 排序的图片登记。
- `embedding/artifacts/<artifact_id>/`：`input.json`、`space.json`、`vectors.jsonl` 和 `complete.json`。
- `embedding/trials/<trial>/`：独立数据库连接信息，目录 0700、凭据文件 0600，不进入仓库。
- `embedding/build/`：扩展源码及构建上下文。

输入条目包含媒体 ID、关键帧 ID、通道、相对路径、大小和图片摘要。集合不允许空、重复、缺失或多余记录；完整向量必须一一对应完整输入，首版不发布部分成功批次。

空间契约包含模型来源、固定 revision、模型和视觉投影文件摘要、llama 运行版本、量化、预处理、文本/图片指令、pooling、实际维度、float32、L2 归一化及 cosine 距离。规范化契约摘要形成 `space_id`。相同维度并不代表相同空间；更换配置或运行后端需重新验证。模型未就绪时使用[待确认配置](../infra/embedding/provider.example.json)，不填写伪造身份，不注册正式空间。

向量产物完成清单记录输入、空间、业务 generation、数量、加工版本、代码及文件摘要；规范化完成清单摘要形成 artifact ID，数据库版本为 `e_<artifact_id>`。文件原子提交、目录不可覆盖；只接受固定目录内的身份定位，拒绝符号链接和越界，单文件读取上限 256 MiB。未完成暂存目录不参与装载。

Python 入口：`embedding.artifacts.capture/create_artifact/verify_artifact`。`create_artifact` 接受实际编码输出，本身不执行推理。临时图片查询通过编码获得查询向量，不加入媒体清单。

## 数据库操作

```sh
./platform.sh embedding-trial --action up --trial <32位trial>
./platform.sh embedding-migrate --trial <trial> --test-database
./platform.sh embedding-load --trial <trial> --artifact-id <64位artifact_id>
./platform.sh embedding-show --trial <trial> --embedding-generation <e_ID>
./platform.sh embedding-publish --trial <trial> --embedding-generation <e_ID> --expected-revision <revision>
./platform.sh embedding-rollback --trial <trial> --embedding-generation <e_ID> --expected-revision <revision>
./platform.sh embedding-trial --action stop --trial <trial>
```

`trial-up` 仅创建或恢复运行隔离实例，不复制正式数据。首次迁移必须已有恢复的业务 schema；推荐先使用完整验证入口完成准备，不把空库初始化误当作图片登记已就绪。独立实例使用 127.0.0.1:55434；不改变正式端口 55433。trial-stop 只移除核对过身份的容器，保留卷。

连接优先使用显式 `--trial`；其次使用 `DDP_EMBEDDING_TRIAL`；未指定 trial 时读取 `DDP_EMBEDDING_DSN` 或外部应用配置 `embedding.database_dsn`，绝不回退到正式数据库 DSN。连接字符串不打印到报告。

迁移仅允许 `ddp_embedding_trial_<32位身份>` 数据库，并要求 `--test-database`，记录 SQL 摘要，重复执行幂等，摘要漂移阻断。正式部署采用独立环境登记与恢复流程，见[部署说明](embedding-deployment.md)。跨名称恢复后，须显式提供 `--restore-from-database <备份中的原试验库名>` 重登记，不能直接发布测试数据。

新增空间、向量 generation、按业务 generation 的活动指针、发布事件和图片向量表；迁移及环境登记另有元数据表。向量关联同 generation 的媒体外键。数据库触发器拒绝跨身份、非法维度、零向量/非归一化、READY 修改以及未登记环境中的测试空间和测试发布。LOADING 装载与回读核对在同一事务，成功才转 READY。

同产物装载复用；连接确认丢失时，通过确定版本身份重新查询并重跑装载，不推测提交结果。发布与回滚都要求预期 revision，CAS 冲突不得自动刷新重试。业务活动指针不受向量发布影响。没有对应向量版本时明确报“尚未建立检索数据”，不借用其他 generation。

`embedding.store.search` 是内部 Python 函数，输入 generation、space_id、已生成查询向量及可选向量版本；复用已有地点、时间、类别和必需通道筛选，另支持命中图片通道筛选。限定候选后按余弦距离精确排序，同分按媒体 ID 排序。首版无近似索引，不截断维度，不将相似度当作概率。

## 推理与验收边界

编码接口包含能力查询、运行配置读取、文本编码和图片编码。本地 llama 已接入真实多模态协议及进程/空间身份核对，详见[真实编码说明](embedding-encoding.md)；禁止重定向及环境代理。健康成功不代表空间兼容。模型或空间未就绪时编码明确失败。外部服务适配见[后端说明](embedding-backends.md)，无配置或空间不匹配时明确失败，不生成占位向量。

本地与外部后端均须核验实际协议、输出维度、归一化和图文空间一致性；相关性样本与延迟分别验收。实现与验收不得混为一谈。

本轮备份核对覆盖新增表和原表；HMS 使用导出快照对应备份与全表指纹，其正常后台锁/housekeeping 变化单独记录，不用来掩盖静态元数据变化。

实现状态、真实结果、失败轮次和未验证范围统一见[当前状态](current-status.md)。

真实编码与可恢复加工的后续实现及命令见[操作说明](embedding-encoding.md)，最新验收统一见[当前状态](current-status.md)。
