# 查询与导出协议

接口统一以 `/api` 开头：`health`、`versions`、`quality`、`analytics`、`scenes`、`frames`、`frames/{entity_id}`、`media/{entity_id}`、`export`；可交互查看 `/api/docs`。

首次查询可以省略 generation，响应给出确定版本。详情、媒体及导出必须显式传入 generation；页面将后续请求绑定到所选版本。历史 READY 数据不可修改，因此发布时进行中的分页不会混版。

响应携带 `profile_id`、`profile_version`、`asset_policy`、`assets` 和 `capabilities`，不返回输入模式。下游按能力判断：

```json
{
  "capabilities": {
    "structured_query": true,
    "annotation_review": true,
    "original_media": false,
    "image_search": false
  }
}
```

`frames` 接受 location、UTC ISO 格式 start/end（含边界）、单个精确 category 和重复 channel 参数。各条件取交集，多个通道必须全部满足；通道表示关键帧登记媒体。关联使用 EXISTS，避免连接放大。按 timestamp_us、entity_id 排序，分页默认 50、上限 200。SQL 值均参数化。

图片只按登记 entity_id 取出关键帧相机媒体，逐级 O_NOFOLLOW 打开相对路径，拒绝越界、符号链接、非普通文件、不符类型/大小/摘要。读取同一文件描述符后返回内容，避免校验路径后再次打开的竞态。图片上限 25 MiB。资产状态不是 `verified` 或 `capabilities.original_media` 为 false 时返回 409。

`export?generation=...&format=jsonl|csv` 返回 ZIP，其中每个数据行对应一个关键帧，附 generation、snapshot_id、processing_version、profile、asset policy 和能力。manifest.json 保存筛选条件、数量、字节、排序与数据 SHA-256。超过 100000 行或未压缩数据与清单合计 100 MiB 时返回 413，不发送部分成功。CSV 对公式前缀文本转义，JSONL 可无损获取原值。

工作台提供质量、分布、筛选、分页、相机图片、标注明细和导出。媒体未校验时显示登记信息并禁用原图；检索依赖向量状态。标注为世界坐标，首版不做图片投影或三维渲染。没有网页管理后台，装载、发布和回滚使用 Shell 入口。
