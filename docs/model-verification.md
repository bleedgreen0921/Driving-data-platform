# 已下载模型校验与最小功能测试

仅校验文件：

```sh
./platform.sh diagnose --target model --model-dir <原始模型目录> --metadata <本轮保存的ModelScope官方清单JSON>
```

校验并启动临时本地 llama 测试：

```sh
./platform.sh diagnose --target model \
  --model-dir <原始模型目录> \
  --metadata <官方文件清单JSON> \
  --gguf <F16主模型GGUF> \
  --mmproj <F16视觉投影GGUF> \
  --text-reference <可选原始权重文本参考JSON>
```

入口支持 `DDP_PYTHON`、`DDP_OUTPUT_ROOT`；不使用现有服务端口，默认 127.0.0.1:18092，结束时关闭自己启动的进程。结果独立保存在外部 `reports/model-probe-<ID>/`，包含报告、原始 llama 日志及真实探针向量。仅完整通过返回 0；坏图错误分类等限制返回 `partial`、退出码 2；其他失败保存报告并非零退出。报告不发布为业务向量产物。

校验逐文件比较 ModelScope 官方清单的大小、SHA-256，并检查 Safetensors 张量形状、字节数及文件区间。清单须来自本轮保留的官方响应，来源及模型固定 revision 见验收证据，不以文件能打开代替下载完整性。

llama.cpp 不能直接加载原始 Safetensors；本轮使用与已装 llama-server 相同 commit 的官方 `convert_hf_to_gguf.py`，分别用 `--outtype f16 --outfile <主模型>` 和 `--mmproj --outtype f16 --outfile <视觉投影>` 转换。脚本、版本依赖和转换日志保存在外部 `reports/model-check-1223f2fa98a4440e8f8fc6183fc43b85/`；原始文件不改写。

工具依赖只在仓库外独立环境安装。复建环境可使用：

```sh
"$DDP_PYTHON" -m venv <独立工具环境>
uv pip install --python <独立工具环境>/bin/python \
  torch==2.14.1 transformers==5.18.0 safetensors==0.8.0 \
  Pillow==13.0.0 sentencepiece==0.2.1 protobuf
```

完整解析版本以本轮外部 `tools-requirements.txt` 为准；转换器的 gguf Python 代码来自固定 commit 的官方源码，未安装到原有项目环境。

测试范围为：中文文本、两张真实登记图片、2048 维/有限值/归一化、重复性、图片输入变化、原始权重文本对照、坏图及请求后恢复。图片通过既有媒体登记及安全读取获得，原始媒体不复制、不上传外部服务。只对本地服务发请求。

当前发现坏图返回 HTTP 500，因此不是全项通过。正常编码已通过，但调用端已校验图片；独立原始模型探针仍应记录坏图行为，，避免把解码失败当作模型可用性故障。微量样本不能替代检索质量和全栈验收；原始模型诊断不替代应用编码、检索及全组件验收。最新事实统一见[当前状态](current-status.md)。
