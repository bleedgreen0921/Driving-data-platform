# 入口迁移

旧命令已退出，不保留转发别名；请更新本地运维脚本。运行配置和路径解析见[部署说明](deployment.md)。

| 原入口 | 当前入口 |
| --- | --- |
| `embedding-trial-up/stop` | `embedding-trial --action up/stop --trial <ID>` |
| `embedding-api-up/status/stop` | `embedding-trial --action api-up/api-status/api-stop --trial <ID>` |
| 隔离数据库状态 | `embedding-trial --action status --trial <ID>` |
| `hms-register`、`hms-consume` | 正式使用 `run/load`；隔离验证使用 `verify metastore --faults` |
| 独立模型校验Shell脚本 | `diagnose --target model`，沿用 `--model-dir`、`--metadata` 等参数 |
| 独立编码校验Shell脚本 | `diagnose --target encoding`，沿用 `--input-id`、`--faults` 等参数 |
| 外部服务编码诊断 | `diagnose --target encoding --backend external_api --space-id <ID> --image <图片>` |

首次准备、模型管理、图片清单、向量构建装载发布、正式部署、统一启停及verify能力保留。诊断中的本地模型文件参数不适用于外部模式。

当前运行查询要求显式报告schema v1；无schema旧报告仍在原处，列表跳过并返回警告，详情明确报不支持。历史证据不补写、不伪装成当前报告。
