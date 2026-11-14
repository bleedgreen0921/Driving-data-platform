使用 `./platform.sh init-db` 创建 schema.sql 中的控制与服务表。装载和发布见本地 `../../docs/deployment.md`，不要手工修改已就绪版本。


独立的源表镜像由 `ods.sql` 建立，摄取入口单事务装载并逐字段回读；不执行embedding迁移。pgAdmin可运行[查询脚本](inspect-metadata.sql)核对13表计数、源token关联和API活动generation。
