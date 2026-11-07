# 真实编码与可恢复加工

先读取外部产物根的 `embedding/selection.json`，取得通过评测的 `space_id`。以下命令不注册数据库空间、不装载或发布向量，不改变原六阶段 `run`。

```sh
./platform.sh embedding-evaluate --input-id <input_id>
./platform.sh embedding-model-up --space-id <space_id>
./platform.sh embedding-model-status
./platform.sh embedding-build --input-id <input_id> --space-id <space_id>
./platform.sh embedding-build-show --run-id <run_id>
./platform.sh embedding-model-stop
```

`embedding-build-show` 可在加工时读取，默认加工保持单图串行。完整报告在 `embedding/build-runs/<run_id>.json`；成功缓存位于 `embedding/cache/`，不可变产物位于 `embedding/artifacts/<artifact_id>/`。修复失败后显式重跑同一命令，成功结果会核验复用；没有逐项自动重试。任何失败或受阻都不提交新的完成标记。缓存损坏会保留原文件并报告失败，需先核对证据、隔离损坏缓存，再显式重跑。

SIGTERM / Ctrl-C 会保存 interrupted 报告；已逐张同步落盘的缓存可恢复。SIGKILL、断电及磁盘硬件故障不在本轮恢复承诺内。已有不可变产物不会被覆盖；重跑仍检查输入文件，全部缓存合法时不再推理。已完成产物同时绑定完整加工代码摘要；匹配产物在缓存缺失且模型停止时也能核验复用，坏缓存仍会单项失败。

## 模型与候选登记

模型、配置和所有运行证据都在仓库外。`embedding/model.json` 登记 `binary`、`model`、`projector` 各自的绝对 `path` 和 `sha256`，只接受已核验的模型和二进制。启动完整扫描摘要，编码期间检查文件 stat 身份、进程启动身份、稳定模型元信息及 `/props`；`/health` 不能证明空间兼容。端口被占用拒绝接管，不静默替换不同配置的项目服务。正常端口为 18090，比较端口为 18092。重复启动相同空间会复用；停止前再次核对 PID 启动身份。

候选缺失时，评测入口根据 `embedding/model-provenance.json` 中已核验的模型名称、revision 与 runtime_version 启动临时进程读取真实模板，生成候选；登记缺失或版本不一致会阻断，不猜测模型配置。

候选契约为 `embedding/spaces/<space_id>/space.json`，内容规范化摘要就是空间身份。v1 历史契约保持只读兼容，真实编码要求 v2：2048 维、F16、已登记二进制/模型/投影/实际模板/编码实现摘要，以及固定上下文 4096、批次 1024、单槽、图片最小 token 64。任务指令保持 `Represent the user's input.`。实现摘要变化须新建候选、重新评测，不修改旧空间。

图片只接受完整解码的 JPEG/PNG，最多 25 MiB、最长边 8192、2000 万像素、单帧；原字节传递，不旋转、缩放或重编码。依赖锁为 Pillow 13.0.0。文本保留空格和换行，拒绝空白、非法 Unicode、超过 4096 字符或模板后上下文超限。HTTP 禁用环境代理及重定向、单条输入、默认超时 60 秒。响应数量、索引、2048 维、有限值及 L2 归一化必须符合契约，不补齐、不截断、不重新归一化。

## 参数比较

`embedding-evaluate` 从实际输入按 scene token 排序，选择各场景最早关键帧的六路相机。评测前必须准备外部 `embedding/evaluation-labels.json`，绑定样本摘要、全部审阅媒体 ID、审阅方式、不确定项及固定文本查询的正例；不得用三维标注替代相机可见性。标签在产生结果前不可变保存。无正例查询不参与评分，至少六条有正例查询；不足时停止，需扩展样本与审阅后再评测。

每个候选预热后执行两轮图片，记录逐项延迟、P95、RSS 和系统内存压力，服务日志独立保留统一内存分配信息。先比较 256/512，仅两者均未达标时比较 1024。门槛为合法输出、重复余弦至少 0.9999、固定近义句相似度大于无关句、固定查询 Hit@5 至少 80%。达标候选按最低 P95 选取；10% 内依次比较 RSS 和 token 上限。报告及唯一选中记录位于 `embedding/evaluations/` 与 `embedding/selection.json`。小样本结果不是正式检索质量或容量结论。

## 重复验证

```sh
# 先按选中空间启动本项目模型；故障只作用于独立复制媒体和本项目模型进程。
./platform.sh diagnose --target encoding --input-id <input_id>
./platform.sh embedding-model-up --space-id <space_id> > <仓库外启动回执.json>
./platform.sh diagnose --target encoding --input-id <input_id> --faults --model-start-receipt <仓库外启动回执.json>
```

验证证据在 `reports/encoding-recovery-<id>/`。故障轮次验证坏图无请求、正常恢复、单图失败继续、缓存损坏、SIGTERM、提交前后重复执行和停服后受阻扫描；真实 SIGSTOP 使用显式短超时验证连续三次失败后的健康核对与受阻。服务恢复后再进行完整清单加工。停服故障必须提供 `reused=false` 且 PID/启动身份匹配的本轮启动回执；拒绝停止原本运行的服务。验证不管理其他模型服务，不清理缓存或既有产物。测试结束由操作者按开始时状态停止本轮启动服务。

资源预算上限为 64 GiB 磁盘与至少 12 GiB 空闲预留；模型另申报 6 GiB 宿主内存，不能从磁盘预算推导内存容量。RSS 门禁不是 GPU/统一内存峰值保证。原始权重仅逐个登记到 `embedding/model-source-files.json`，usage 连同工具环境和内容缓存计量，按设备/inode 去重，不扫描全部共享模型缓存。Docker 不在线时，usage 不能给出当前 Docker 占用核验，不以历史数字冒充本轮真实盘点。本轮不替代全套服务同时在线验收。

外部 model.json 中 binary/model/projector 的 path 可使用相对该登记文件目录的路径或部署机绝对路径；仍核对真实文件、仓库外位置和文件摘要。外部后端不需要这些本地文件，见[后端说明](embedding-backends.md)。
