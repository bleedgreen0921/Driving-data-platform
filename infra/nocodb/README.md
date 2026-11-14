# NocoDB 本机表格界面

使用锁定的原生 arm64 NocoDB 2026.10.1 与 Redis 镜像，配置见[Compose](../docker/compose.nocodb.yaml)及[镜像锁](runtime.lock.json)。NocoDB、worker、Redis 独立于业务服务启停，共用本项目 Docker 网络。

- `ddp_nocodb` 是 NocoDB 必需的独立应用库，保存账号、项目、连接和视图配置。
- `ddp` 是外部业务数据源，通过 `ddp_nocodb_reader` 只读连接 `ods`、`serving`、`control`。
- 密码、JWT密钥和连接加密密钥保存在仓库外0600配置中，不进入仓库。

## 本轮接入

`Driving Data Platform` 项目下有三个外部源，均使用 `DDP Readonly PostgreSQL`：

| 数据源 | schema | 表数 |
| --- | --- | --- |
| ODS Metadata | ods | 14 |
| Serving Metadata | serving | 8 |
| Control Metadata | control | 5 |

三个源均为 schema/data read-only。NocoDB API 写请求返回 403；`ddp_nocodb_reader` 仅有 SELECT 与 schema USAGE。当前侧栏未显示 `calibrated_sensor`、`ego_pose`、`instance` 三个 ODS 表，API 仍可读取；不要据此删除或重建数据源。

## 启停

将下列路径替换为本机已有的仓库外 NocoDB 配置目录：

```sh
export DDP_NOCODB_ROOT="<仓库外NocoDB目录>"
docker compose --env-file "$DDP_NOCODB_ROOT/compose.env" -f infra/docker/compose.nocodb.yaml up -d --wait --pull never
docker compose --env-file "$DDP_NOCODB_ROOT/compose.env" -f infra/docker/compose.nocodb.yaml ps
docker compose --env-file "$DDP_NOCODB_ROOT/compose.env" -f infra/docker/compose.nocodb.yaml logs --tail 100 nocodb
# 停止NocoDB相关服务，保留数据库与卷
docker compose --env-file "$DDP_NOCODB_ROOT/compose.env" -f infra/docker/compose.nocodb.yaml stop
```

启动前确保本项目 PostgreSQL 已在线。界面默认地址为 http://127.0.0.1:18082 。失败查看本轮启动日志和容器日志；不要删除应用库或数据卷来重置故障。

`ods` 的原始字段已展开；`serving` 的部分字段存于 `payload` JSONB。服务表保留多个generation，查看特定版本时按generation筛选。媒体仍未校验，NocoDB不会补齐原图。

安装和实际验证状态见本地 `../../docs/current-status.md`。
