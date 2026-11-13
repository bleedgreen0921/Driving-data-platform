# 多模态图文检索 API 与页面

## 启动与使用

先读取外部 `embedding/selection.json` 的空间，以及主产物绑定的业务 generation。当前使用已验收试验库：

```sh
./platform.sh embedding-trial --action up --trial 126a9e17a1df40cb9bdb851f8677aba1
./platform.sh embedding-model-up --space-id dc64ff4f42baa501ef802e03175861c7293a8c286e16b39c839e68ab7dc10311
./platform.sh embedding-trial --action api-up --trial 126a9e17a1df40cb9bdb851f8677aba1
./platform.sh embedding-trial --action api-status --trial 126a9e17a1df40cb9bdb851f8677aba1
```

打开 `http://127.0.0.1:18081`，选择具有真实向量的业务版本，在“多模态图文检索”输入文本描述。检索复用上方地点、UTC 起止时间、类别与必需通道；图片通道和返回数量在检索区设置。点击结果的“查看关键帧”核对原图与标注。

当前主产物业务版本为 `g_607a85cd0da553202e4fcab527a94cdbb94dbc9f82613f4b7f90b4609a6b06b5`。未建立向量的版本明确显示无检索数据，不自动换版本。

独立实例固定回环 18081、单 worker；未启用正式部署时，默认业务API为18080且禁用检索；正式启用与统一启停见[正式部署说明](embedding-deployment.md)。启动命令不会自动启动模型/数据库、迁移、装载或发布。搜索、图片、详情和业务查询使用同一个已核对隔离数据库。启动拒绝容器、镜像、端口或连接身份冲突，不接管未知监听者。

```sh
./platform.sh embedding-trial --action api-stop --trial 126a9e17a1df40cb9bdb851f8677aba1
```

此命令只停止身份核对通过的专用 API；数据库、模型按各自命令管理。

## HTTP

`GET /api/embedding/status?generation=<业务版本>` 返回 `ready`、`no_data` 或 `disabled`，不编码文本；依赖错误返回 503，契约冲突返回 409。原 `/api/health` 继续只检查业务数据库。

`POST /api/embedding/search` 使用 JSON：

```json
{
  "generation": "g_607a85cd0da553202e4fcab527a94cdbb94dbc9f82613f4b7f90b4609a6b06b5",
  "text": "路口有汽车",
  "limit": 20,
  "required_channels": ["CAM_FRONT"]
}
```

可选 `embedding_generation`、`location`、`start`、`end`、`category`、`image_channel`。UTC 时间包含边界，`limit` 为严格整数 1–100，必需通道最多12项并去重，未知字段拒绝。文本原样编码，非空白且最多4096字符，还需通过真实分词上下文限制；请求体最多64 KiB。

响应包含请求编号、版本/空间/产物、实际筛选、数量、耗时和图片追溯字段；图片与详情 URL 都绑定返回的业务 generation。相似度是余弦相似度，不是概率。合法空结果返回200。

CLI 与 HTTP 共用跨进程非阻塞检索锁，一次仅一个检索，其余立即429并附 `Retry-After: 1`，不排队或重试。浏览器取消等待不提前释放仍在执行的工作锁。连接/模型请求/SQL 超时分别为5/60/10秒。

错误状态：413请求过大；422参数非法；404指定版本不存在；409无原始媒体、无检索数据或契约冲突；429繁忙；503未启用或依赖不可用；500内部错误。错误不回显输入、凭据、内部异常或宿主路径。收到429手动重试；依赖故障恢复组件后点击“检查检索状态”。

## 验证与证据

```sh
./platform.sh verify embedding-web \
  --trial 126a9e17a1df40cb9bdb851f8677aba1 \
  --artifact-id 1e0a6429f23abcf5e40ed6eb6cd4a02334e60b6a3fbd653f641c0a908363759e --faults
```

统一锁串行执行；检查真实数据、HTTP、独立余弦/筛选基准、媒体与详情、60秒五客户端、显式故障恢复和既有API回归。没有数据库重建或新向量产物。`--faults` 需要目标组件由本轮新启，不擅自停止原本运行的组件。验证结束恢复初始服务状态，保留独立报告与失败证据。

外部证据：`reports/embedding-web-<ID>/` 和组合入口目录；每次POST报告在 `embedding/reports/http-<request_id>.json`，只保存查询摘要与字符数，不保存查询原文或向量。API进程登记与运行日志在 `embedding/api-runtime/`，不返回数据库凭据。

固定负载中的成功与429分开统计；短测不是最大容量，也不替代全服务长期联调。真实结果、浏览器验收和未验证边界统一见[当前状态](current-status.md)。本阶段不含上传、图片查询、正式库切换或正式相关性结论。
