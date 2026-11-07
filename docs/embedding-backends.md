# 多模态图文检索后端

本地 llama 使用已有 v2 空间；外部服务使用独立 v3 空间。正式图片向量与查询向量必须属于已验证的同一空间，不能因维度相同混用后端。适配代码、协议测试与真实服务验收分开记录，见[当前状态](current-status.md)。

## 配置与登记

外部应用配置中设置 `embedding.provider_file`，指向仓库外JSON配置。相对路径以应用配置文件所在目录为基准。模板见[连接示例](../infra/embedding/external-provider.example.json)和[空间示例](../infra/embedding/external-space.example.json)。示例模型、版本和维度为空，不能直接登记；必须以真实服务的固定模型资料及探针结果填写（implementation_sha256 留空时由登记命令计算当前适配器摘要），不生成虚假模型文件摘要。

```sh
./platform.sh embedding-space-register --space-file <仓库外空间JSON>
./platform.sh diagnose --target encoding --backend external_api \
  --space-id <登记返回的空间ID> --image <本次探针图片>
./platform.sh embedding-evaluate --input-id <图片输入ID> --space-id <外部空间ID>
./platform.sh embedding-build --input-id <图片输入ID> --space-id <外部空间ID>
```

登记只保存不可变空间，不进行网络请求，也不代表通过评测。`test_only: true` 必须显式加 `--target-test-only`，只能进入允许试验空间的独立库。正式真实空间使用 `test_only: false`，仍须完成实际评测、独立建库和真实服务验收后放行。

评测沿用预先审阅的图片及查询正例清单，至少六条有效查询，两轮真实图片编码；重复余弦至少0.9999、Hit@5至少80%。外部结果保存独立评测报告，选中记录位于 `embedding/selections/<space_id>.json`，不覆盖本地选参记录。样本通过不代表所有语言、完整相关性或最大容量。

完整加工后使用已有 `embedding-load/show/publish/rollback`，显式选择 `--trial` 或 `--deployment`。外部服务不需要本地PID或模型文件；统一服务启动会探测远端，停止不会请求关闭远端服务。首次正式启用数据库仍需受控部署，不用更改连接字符串代替身份登记。

## 支持的协议

`endpoint` 为服务的API基础地址，如 `https://service.example.invalid/v1`，适配器追加 `/embeddings`。远程必须HTTPS，本机可HTTP；不自动重定向、不重试、不隐式使用环境代理。

文本请求包含 `model`、字符串 `input`、`encoding_format: "float"`。`text_instruction` 原样前置到文本，指令属于空间身份。

图片必须显式匹配 `content_parts_v1` 扩展：

```json
{
  "model": "<固定模型标识>",
  "input": [{"content": [
    {"type": "text", "text": "<image_instruction>"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,<原图字节>"}}
  ]}],
  "encoding_format": "float"
}
```

该图片格式是显式扩展，并非所有OpenAI风格服务通用。服务不匹配时明确报错，不自动降级。图片沿用既有完整解码保护，保持原字节，不旋转EXIF、缩放或重编码；临时查询不落盘、不登记。

响应要求单条 `data[0]`、整数 `index: 0`、维度一致且有限的非零 `embedding`。客户端进行稳定L2归一化及float32转换。适配器实现摘要也属于空间身份，代码变化须重新评测新空间。空间中的 `revision_field` 指定响应顶层版本字段，其值必须严格等于登记的固定 `revision`；不能核验版本的服务不得正式使用。地址与密钥不参与空间身份。

## 凭据、限制与证据

`api_key_env` 只记录凭据环境变量名；默认Bearer认证，也可配置独立认证头名。JSON不接受密钥值或任意请求头。报告和错误不输出凭据、服务响应正文、查询正文、上传文件名或向量。

请求超时上限60秒，响应上限2 MiB，文本默认最多4096字符、图片最多25 MiB且可配置更小限额。鉴权、限流、重定向、超时、版本冲突及非法输出分别报错；请求失败不自动追加重试。

协议探针报告在外部 `embedding/reports/diagnostic-*.json`，只说明协议结果。实际服务尚未选定，目前真实外部编码、建库、检索与最终组合验收均待完成。

真实产物已在明确登记的独立试验库READY后，运行 `./platform.sh verify embedding-external --trial <ID> --artifact-id <ID>`：固定查询及原图/衍生PNG、独立余弦排序与分数、空筛选、Top1/20/100、端到端版本身份及前后全表指纹核对。此入口不迁移、不装载、不发布、不重建库，不故障注入；报告独立保存在 reports/embedding-external-*/。故障恢复、浏览器、正式发布及长期组合仍须另外完成，不能用本入口通过替代。
