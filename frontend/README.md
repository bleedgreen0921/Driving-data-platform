原生 HTML/CSS/JavaScript 工作台，由 FastAPI 提供，访问 http://127.0.0.1:18080 。显示质量、分布、筛选、图片、标注及版本绑定的导出；无网页管理后台。

新增运行状态、阶段详情与轻量血缘折叠区，支持只读查询及 JSON 下载。运行区仅展开且页面可见时每 5 秒刷新；详情见[运行说明](../docs/run-observability.md)，验收缺口见[当前状态](../docs/current-status.md)。

多模态图文检索支持专用隔离API与明确登记的正式18080 API。未启用正式部署时默认禁用；正式统一启停见[操作说明](../docs/embedding-deployment.md)，隔离命令见[隔离说明](../docs/embedding-web.md)。真实HTTP与浏览器已验收，当前完整验收边界见当前状态。

工作台支持文本/图片检索模式。图片只用于本次查询，支持JPEG/PNG、本地预览与清除，共用版本、筛选及结果详情；不提供入库上传。限制与错误处理见[图片查询说明](../docs/embedding-image.md)，最新验收边界见当前状态。
