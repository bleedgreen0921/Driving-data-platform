# 运行证据与契约

默认新处理版本为 `warehouse-v6`，ODS 版本为 `nuscenes-ods-v2`。数据语义契约见 [JSON 契约](../contracts/nuscenes-mini-v2.json)，实际规则由现有检查代码执行；质量与指标口径不增加第二套规则引擎。契约规范化摘要随新数仓、消费和 generation 清单传递。

每个快照的 `complete.json` 记录 profile、dataset version、asset policy、assets、capabilities、代码/输入摘要和全部产物摘要。服务 generation 的 manifest 继承这些字段，发布差异按 profile、能力、资产策略和指标身份比较。

运行报告独立于不可变产物，写入仓库外 `reports/`。`run-show` 只读取已保存报告；`run-list` 和 `lineage` 不生成业务产物。失败报告保留阶段和结构化错误，不能用历史成功抵消当前失败。
