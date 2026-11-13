# 多模态图文检索 CLI

```sh
./platform.sh embedding-search --trial 126a9e17a1df40cb9bdb851f8677aba1 \
  --generation <产物绑定的业务generation> --text '路口有汽车' --limit 20

./platform.sh verify embedding-search \
  --trial 126a9e17a1df40cb9bdb851f8677aba1 \
  --artifact-id 1e0a6429f23abcf5e40ed6eb6cd4a02334e60b6a3fbd653f641c0a908363759e --faults
```

查询需要指定隔离 PostgreSQL 与已登记的 256 token 模型在线。使用既有 `embedding-trial --action up --trial ...` 和 `embedding-model-up --space-id ...` 启动；模型及空间身份仍由原管理器核对。业务 generation 从产物读取，不能使用当前业务指针替代。命令不会回退到正式数据库，不自动启动模型或重试。

可选 `--embedding-generation` 固定 READY 版本，否则在编码前固定该业务版本的向量活动指针。支持 `--location`、`--start`/`--end`（UTC ISO 时间，包含边界）、`--category`、可重复的 `--required-channel`、`--image-channel`。`--limit` 默认 20，范围 1–100。条件取交集；重复必需通道去重。

标准输出为 JSON，包含业务/向量版本、空间与产物身份、规范化筛选、耗时及可追溯图片结果。分数是余弦相似度，不是概率。空列表退出 0；输入、空间或依赖错误退出 2。连接超时 5 秒、模型请求 60 秒、检索语句 10 秒，不自动重试。

每次 CLI 查询独立写入外部产物根 `embedding/reports/task-*.json`，返回 `report` 路径。报告保存查询 SHA-256、字符数、版本、结果数及耗时，不保存原文或查询向量。

组合验收使用统一操作锁，完整备份旧试验库与正式业务只读快照；只重建上述指定试验数据库。报告、日志、备份和 Top10 联系表位于外部 `reports/embedding-search-<ID>/` 及组合入口报告目录。验收后保留真实向量，恢复原有服务运行状态。失败查看本轮 `report.json` 和组合日志。`--faults` 显式允许指定库重建与本轮新启模型/指定库停机验证。

真实验收结果及尚未验证边界统一见[当前状态](current-status.md)。本页只说明文本 CLI；API和页面见[检索页面说明](embedding-web.md)。样本相关性与工程正确性分别记录。短时串行观察不代表最大容量或全服务长期联调。

正式环境通过显式 `--deployment` 使用相同检索行为，部署与统一启停见[正式部署说明](embedding-deployment.md)。本页的仅trial约束描述隔离阶段，最新验收状态见当前状态。
