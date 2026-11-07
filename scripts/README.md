统一使用 `./platform.sh --help` 和 `./verify.sh`；参见 [运行说明](../docs/deployment.md)。

`./platform.sh verify observability --faults` 串行验证运行状态与血缘；范围及证据见[说明](../docs/run-observability.md)。

`./platform.sh embedding-input --generation <ID> --verify-files` 生成并核验真实图片输入；`./platform.sh embedding-prepare` 显式构建扩展镜像；`./platform.sh verify embedding --faults` 执行隔离数据库验收。操作与边界见[说明](../docs/embedding-foundation.md)。

`./platform.sh diagnose --target model --model-dir <目录> --metadata <上游清单JSON>` 校验已下载模型；增加 `--gguf` 和 `--mmproj` 可执行本地文本/图片探针。范围与当前坏图限制见[说明](../docs/model-verification.md)。

真实编码与复制文件恢复验证：`./platform.sh diagnose --target encoding --input-id <input_id> [--faults]`，需先启动已选中空间的本项目模型，详见[说明](../docs/embedding-encoding.md)。

真实向量入库与文本 CLI 验收：`./platform.sh verify embedding-search --trial <指定ID> --artifact-id <主产物ID> --faults`。输入、备份和恢复边界见[操作说明](../docs/embedding-search.md)。

多模态图文检索 HTTP 验收：`./platform.sh verify embedding-web --trial <指定ID> --artifact-id <主产物ID> --faults`，参见[操作说明](../docs/embedding-web.md)。脚本不代替真实浏览器验收。

正式多模态图文检索部署、恢复演练与维护回退使用 `embedding-deploy-rehearse/verify/apply/rollback`，日常使用 `embedding-service-up/status/stop`。命令、维护边界和证据位置见[正式部署说明](../docs/embedding-deployment.md)。

本次图片查询验收：`./platform.sh verify embedding-image --trial <指定ID> --artifact-id <主产物ID> --faults`。含图片与文本探针的组合验收：`./platform.sh verify integration --duration 1800`。前者不重建或装载试验库，详情见[图片查询说明](../docs/embedding-image.md)。
