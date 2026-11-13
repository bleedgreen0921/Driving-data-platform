# 配置示例

仓库只保留示例和契约配置，真实配置、凭据、模型和数据位于仓库外。应用示例见 [application.example.yaml](application.example.yaml)，选择与优先级见[部署说明](../docs/deployment.md)。

宿主路径可使用相对路径或部署机绝对路径，不硬编码原机器目录；服务端口可覆盖。备份位置可单独配置，不允许与原始数据重叠。Python通过DDP_PYTHON或已激活的Python 3.12环境选择。输入完整性通过 `dataset.assets.policy` 选择 `strict` 或 `registered`，不再使用输入模式切换代码路径。

本地 llama 示例见 [provider.example.json](../infra/embedding/provider.example.json)，外部服务及独立空间示例见[后端说明](../docs/embedding-backends.md)。空模型/版本/维度不能直接注册，密钥只通过部署环境注入。
