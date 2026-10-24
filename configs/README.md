# 数据配置与资产预检

已实现安全 YAML、显式环境变量覆盖及只读数据预检。数据库、大数据和调度字段均为**设计约定，尚未实现**；示例不是部署脚本。

```bash
PYTHONPATH=src /home/qjj/.venvs/py312/bin/python -m driving_data_platform.common.preflight --config configs/application.local.yaml
```

默认且唯一任务为 dataset；`--task dataset` 可保留，历史 `all`/`model` 已移除。成功输出 JSON 并退出 0；数据/配置门禁失败退出 2，输出不泄露凭据或连接信息。预检不创建输出、不隐式下载、不导入模型、不连接服务。

复制[应用示例](application.example.yaml)至忽略的本地配置，填写仓库外绝对 dataset.root、dataset.version、storage.output_root、assets.evidence_file。加载器不自动读取 `.env`；环境变量显式空值也覆盖文件值。模型环境变量已不再识别，多余历史证据不会被数据任务消费。

路径解析符号链接后必须在仓库外且不能包含仓库；输入输出不得重叠。数据根必须含版本目录、samples、sweeps、maps，13 张元数据表必须是记录数组。核心关系和媒体全量对账由[ODS 接入](../docs/ingestion.md)执行。

[证据示例](asset-evidence.example.json)仅保留 dataset。填写 source_url、license、license_basis、acquired_at（ISO 日期）、version、checksums；未知值不能当作确认。checksums 至少覆盖 13 张元数据表，为根目录相对路径到 SHA-256 的映射，不允许越界。真实证据留在仓库外，示例空值不可作为实际资产确认。

数据库连接和外部服务配置仅为后续设计占位，秘密由部署环境注入；不得填入版本库。向量维度、模型路径、revision 等字段已从公共示例删除。

阶段 0 新增独立的 [部署配置示例](platform.example.yaml)：部署根、阶段 1 已核验镜像摘要、后续候选版本、资源限额、磁盘预算及秘密环境变量名。该文件不合并进数据身份计算；自动回归验证附加部署字段也不会改变 ODS 身份。示例磁盘预算留空，未核验的组件镜像留空；不得将示例视为全栈可部署配置。第一次部署前填入经盘点的预算，使用 common.resources CLI 做只读静态门禁，随后按 [基础设施说明](../infra/docker/README.md)准备项目资源。当前 resources 检查为“尚未分配全部预算”的初次门禁；后续扩展须扣除已分配项目占用后另行核算剩余预算，不能重复计算已下载镜像，也不能把其他项目占用当作本项目预算额度。
