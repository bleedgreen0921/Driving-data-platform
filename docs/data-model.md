# 数据与发布模型

ODS 保存全部 13 张原表及带 entity_id、source_token、整数 timestamp_us 的 JSONL，附质量报告、31,223 个引用文件的摘要清单和完成标记。身份由输入文件摘要决定。地图范围外 log 引用单独记录，不豁免其他核心外键。

Spark 使用显式 schema 和独立 SQL，输出如下 Parquet 与 JSON 服务交换文件：

| 产物 | 粒度与用途 |
| --- | --- |
| dim_scene/category/sensor/location | 场景、类别、传感器、地点维度 |
| fact_frame | 每个关键帧一行，保留整数微秒、实体身份与源 token |
| fact_media | 每条 sample_data 一行，区分关键帧媒体与 sweep |
| fact_annotation | 每条标注一行，保留对象、类别、位置、尺寸、旋转及点数 |
| frame_category/channel | 去重的帧与类别/已校验关键帧媒体通道关联 |
| statistics | 地点、类别、通道分布；类别分别计帧、标注、对象 |
| metrics | 全局帧、标注、对象、sweep 和关键帧媒体数量 |

Parquet 回读与原 DataFrame 对比；宿主独立 Python 基准逐行核对全部服务交换数据。暂存目录对账后才生成完成清单，同步文件与目录并原子重命名。清单记录输入、schema、处理版本、SQL/加工代码摘要、数量及全部文件摘要。下游拒绝缺标记、篡改、缺失及多余文件；同处理版本不能静默接受不同代码。

PostgreSQL `control` 保存 task、attempt、generation、active、publication。装载 generation 由完成清单摘要确定，事务内先 LOADING、写入并回读核对，再进入 READY。失败回滚全部服务写入；确定身份防止重复装载。注入提交后确认丢失时，重新连接核实已提交版本，随后重跑复用。

`serving` 保存 scene、frame、media、annotation、frame_category、frame_channel、statistics；小维度及全局指标放在 dimension。主键包含 generation 与业务身份，关系含同 generation 外键。READY 版本的服务数据与版本记录由数据库触发器拒绝增删改。媒体记录同时保存 ODS 登记的相对路径、大小和 SHA-256。

唯一 active 行保存 generation 和递增 revision。READY 才能发布；CAS 更新与事件插入在同一事务中，两个相同预期指针的发布仅一个成功。回滚也使用同一比较协议，目标必须是已验证 READY 版本。旧 generation 不自动删除。

默认服务装载先通过 Hive Metastore 外部表读取 Parquet。正式目录身份位于 `metastore/generations/`，与处理版本、生产清单、消费代码和运行镜像绑定；同版本不改变身份，不将旧 JSON 校验副本作为默认装载源。详见[正式目录消费](metastore-validation.md)。

新 `warehouse-v4` 产物携带 `data_contract`（ID、版本、规范化 SHA-256），随 HMS 消费清单传入 generation；旧 ODS 与旧 generation 不回填。运行报告独立于不可变产物身份，发布差异基于 READY 服务数据生成，切换仍使用原预期指针。详见[运行证据](run-evidence.md)。

独立的向量空间、向量产物及按业务 generation 发布的向量版本见[说明](embedding-foundation.md)。当前新增表只在隔离试验库验证，未迁移正式库。
