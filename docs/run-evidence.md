# 契约、运行报告与发布差异

```sh
./platform.sh run
./platform.sh run-show --run-id <32位运行ID>
./platform.sh publish-diff --generation <READY版本>
./platform.sh publish-diff --generation <READY版本> --against-generation <基线版本>
./verify.sh evidence
```

默认新处理版本为 `warehouse-v5`。数据语义契约见 [JSON 契约](../contracts/nuscenes-mini-v1.json)，实际规则继续由现有检查代码执行；质量与指标口径不增加第二套规则引擎。契约规范化摘要随新数仓、消费和 generation 清单传递。旧快照不补写契约，旧服务版本缺失信息显示 `legacy_unrecorded`。

运行报告在外部产物根 `reports/pipeline-<run_id>.json`。`report_schema_version=1`，记录固定的起始活动指针、契约、ODS、数仓、HMS、generation、attempt、阶段时间及错误。运行身份不同不影响产物复用。`run-show` 原样读取新旧报告，不恢复任务。SIGTERM/键盘中断尽力保存终态，SIGKILL 只能留下未完成报告；重跑前检查实际子进程、容器和已提交产物。

发布差异报告位于 `reports/publish-diff-<随机ID>.json`，记录比较基线及 revision、候选清单身份、五项全局指标和地点/类别/通道分布。首次发布无基线，零基数比例为 null。只比较已有历史元数据，不推断旧版本运行环境。差异不自动审批，也不使用任意变化阈值；报告生成失败阻断发布，生成后仍按原预期指针执行 CAS。

错误含 category、code、message、stage、advice。输入错误先修数据，契约冲突先核对版本，依赖失败先恢复并核实提交。发布竞争不能靠自动刷新指针重试。未识别子进程失败只引用阶段日志，不推测原因。CLI 成功退出码 0、失败 2。报告不保存连接密码或完整环境变量。

原证据补强轮次交付 Python、CLI 和报告；新增只读 API、运行状态和血缘页面见[操作说明](run-observability.md)。未来可通过媒体 ID、内容 SHA-256、generation 关联向量任务，模型及向量版本另行设计。

最新实现、验收范围及外部证据统一见[当前状态](current-status.md)。`run-show` 可在任务持锁时查询；读取失败运行的报告本身成功时退出码仍为 0。
