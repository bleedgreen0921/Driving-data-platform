# 多模态图文检索依赖

`./platform.sh embedding-prepare` 显式构建锁定的 pgvector 镜像；日常启动不构建、不下载。正式环境首次启用使用受控部署与备份恢复流程，隔离验证使用 `./platform.sh verify embedding --faults`。

本地 llama 已有编码实现，外部服务协议适配另见本地 `../../docs/embedding-backends.md`。[provider.example.json](provider.example.json) 是不含模型身份的示例，不能直接注册正式空间。当前真实验收状态见本地 `../../docs/current-status.md`。
