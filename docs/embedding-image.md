# 本次图片查询

图片查询只用于本次检索，不登记媒体、不入库、不生成持久化查询向量。使用已有正式部署和冻结图片空间，无需迁移或重新向量化。

```sh
./platform.sh embedding-service-up
# 浏览器访问 http://127.0.0.1:18080，选择“图片搜图”并选择本地图片。
```

API：`POST /api/embedding/search/image?generation=<业务版本>&limit=20`，请求体为原始图片字节，`Content-Type` 为 `image/jpeg`、`image/png` 或 `application/octet-stream`。不使用multipart和Base64 JSON。可选参数沿用文本查询：`embedding_generation`、`location`、`start`、`end`、`category`、可重复的 `required_channels`、`image_channel`。默认Top20，范围1–100；空匹配返回200及空列表。

```sh
curl --fail-with-body -X POST \
  'http://127.0.0.1:18080/api/embedding/search/image?generation=<业务版本>&limit=20' \
  -H 'Content-Type: image/jpeg' --data-binary @query.jpg
```

仅接受单帧JPEG/PNG，最多25 MiB、单边8192像素、总像素2000万。校验原始格式并完整解码，不旋转EXIF、缩放或重编码。服务端全程内存处理，浏览器只使用本地对象URL预览；清除或切换模式时释放预览。无图片查询CLI。

图片与文本HTTP和文本CLI共用非阻塞检索锁，繁忙返回429，请手动重试。接收总时限30秒（408），请求体超限413，媒体类型不支持415，图片/参数非法422，版本不存在404，版本/空间冲突409，依赖不可用503。上传接收阶段断开释放锁；已经开始的解码/编码/SQL完成前不会因客户端断开提前放锁。模型请求60秒、SQL10秒、连接5秒，不自动重试。

结果固定到编码前解析的业务/向量版本，返回追溯图片及详情；相似度不是概率，不自动排除查询原图。耗时包括上传、编码、SQL和请求总耗时。独立报告位于仓库外 `embedding/reports/http-*.json`，保存摘要、字节数、格式和版本身份，不保存图片正文、文件名、本地路径、Base64或查询向量。

真实验收：

```sh
./platform.sh verify embedding-image \
  --trial <已登记试验ID> --artifact-id <主产物ID> --faults
./platform.sh verify integration --duration 1800
```

独立报告在外部 `reports/embedding-image-*/`。前者不重建、不装载试验库，仅对已有真实向量查询；故障只操作本轮启动的依赖及指定试验库，结束恢复原状态。12张原图和6张衍生PNG为可追溯验收输入，放在仓库外，与用户请求处理不落图片文件的规则区分。后者包含五业务客户端和额外串行文本/图片探针、两轮加工、回滚及两库独立恢复。各轮结果以[当前状态](current-status.md)为准；工程正确性与语义相关性分开评价。
