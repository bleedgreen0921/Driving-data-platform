"""本地与外部多模态编码接口；明确核对空间，不生成占位向量。"""

import base64
import json
import math
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from driving_data_platform.common.errors import PlatformError
from driving_data_platform.embedding.artifacts import validate_space
from driving_data_platform.embedding.external import ExternalAPI
from driving_data_platform.embedding.transport import NoRedirect


class Encoder(Protocol):
    def capabilities(self) -> dict: ...
    def runtime_config(self) -> dict: ...
    def encode_text(self, text: str) -> list[float]: ...
    def encode_image(self, image: bytes) -> list[float]: ...


def unavailable(code, message):
    return PlatformError("DEPENDENCY_UNAVAILABLE", code, message)


class LocalLlama:
    def __init__(self, config):
        endpoint = config.get("endpoint")
        timeout = config.get("timeout_seconds", 60)
        try:
            parsed = urlsplit(endpoint)
            port = parsed.port
            valid = (
                parsed.scheme == "http"
                and parsed.hostname in ("127.0.0.1", "::1", "localhost")
                and port is not None
                and not parsed.username
                and not parsed.password
                and parsed.path in ("", "/")
                and not parsed.query
                and not parsed.fragment
            )
        except (ValueError, TypeError, AttributeError):
            valid = False
        if (
            not valid
            or type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 60
        ):
            raise ValueError("llama endpoint 必须为显式本机 HTTP 端口，超时为 0–60 秒")
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.config = config.copy()
        for key in ("model_path", "projector_path"):
            path = config.get(key)
            if path is not None:
                from driving_data_platform.common.config import external_path

                if not isinstance(path, str):
                    raise ValueError("模型文件必须通过仓库外绝对路径配置")
                external_path(path, Path(__file__).resolve().parents[3], key)
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def capabilities(self):
        return {
            "backend": "local_llama",
            "health_probe": True,
            "text_encoding": self.config.get("space", {}).get("schema_version") == 2,
            "image_encoding": self.config.get("space", {}).get("schema_version") == 2,
            "compatibility": "requires-runtime-identity-check",
        }

    def runtime_config(self):
        # 可供本机加工层使用；禁止直接作为 API 响应。
        return self.config.copy()

    def probe(self):
        try:
            request = Request(
                self.endpoint + "/health", headers={"Accept": "application/json"}
            )
            with self.opener.open(request, timeout=self.timeout) as response:
                data = response.read(65537)
                if len(data) > 65536:
                    raise ValueError()
                value = json.loads(data)
                if not isinstance(value, dict) or value.get("status") != "ok":
                    raise ValueError()
            return {
                "status": "passed",
                "scope": "health-only",
                "model_compatibility": "not_verified",
            }
        except (HTTPError, URLError, TimeoutError, OSError):
            raise unavailable(
                "llama-unavailable", "本地 llama 服务连接失败或超时；未执行推理"
            ) from None
        except (ValueError, UnicodeError):
            raise unavailable(
                "llama-invalid-response", "本地 llama 健康响应不合法；未执行推理"
            ) from None

    def _request(self, path, body=None):
        try:
            payload = (
                None
                if body is None
                else json.dumps(body, ensure_ascii=False, allow_nan=False).encode(
                    "utf-8"
                )
            )
            request = Request(
                self.endpoint + path,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with self.opener.open(request, timeout=self.timeout) as response:
                data = response.read(2 * 1024**2 + 1)
                if len(data) > 2 * 1024**2:
                    raise ValueError("模型响应超限")
                return json.loads(data)
        except HTTPError as error:
            category = (
                "DEPENDENCY_UNAVAILABLE" if error.code >= 500 else "EXECUTION_FAILED"
            )
            raise PlatformError(
                category, "llama-http-error", f"模型请求失败 HTTP {error.code}"
            ) from None
        except (URLError, TimeoutError, OSError):
            raise unavailable("llama-unavailable", "本地模型连接失败或超时") from None
        except (ValueError, UnicodeError):
            raise PlatformError(
                "CONTRACT_CONFLICT", "llama-response", "模型响应格式非法或超限"
            ) from None

    def check_identity(self):
        space = self.config.get("space")
        if space is None:
            raise unavailable("embedding-model-not-ready", "模型及向量空间尚未就绪")
        validate_space(space)
        if space["schema_version"] != 2 or space["dimension"] != 2048:
            raise PlatformError(
                "CONTRACT_CONFLICT",
                "space-version",
                "真实编码要求已核验的 v2 / 2048 维空间",
            )
        checker = self.config.get("identity_check")
        if checker is None:
            raise PlatformError(
                "CONTRACT_CONFLICT", "identity-required", "缺少项目模型进程身份核对"
            )
        checker()

    def _template(self, text):
        response = self._request(
            "/apply-template",
            {
                "messages": [
                    {"role": "system", "content": "Represent the user's input."},
                    {"role": "user", "content": text},
                ]
            },
        )
        if not isinstance(response, dict) or not isinstance(
            response.get("prompt"), str
        ):
            raise PlatformError(
                "CONTRACT_CONFLICT", "template-response", "模型模板响应非法"
            )
        return response["prompt"]

    def _embed(self, value):
        from driving_data_platform.embedding.artifacts import vector32

        response = self._request(
            "/v1/embeddings", {"input": value, "encoding_format": "float"}
        )
        try:
            records = response["data"]
            if (
                len(records) != 1
                or type(records[0]["index"]) is not int
                or records[0]["index"] != 0
            ):
                raise ValueError()
            return vector32(records[0]["embedding"], 2048)
        except (KeyError, TypeError, ValueError, IndexError):
            raise PlatformError(
                "CONTRACT_CONFLICT",
                "vector-response",
                "模型输出记录、索引、维度或归一化不符合空间契约",
            ) from None

    def encode_text(self, text):
        try:
            if not isinstance(text, str) or not text.strip() or len(text) > 4096:
                raise ValueError()
            text.encode("utf-8", errors="strict")
        except (ValueError, UnicodeError):
            raise PlatformError(
                "INPUT_INVALID",
                "invalid-text",
                "文本必须为有效 Unicode、非空且不超过 4096 字符",
            ) from None
        self.check_identity()
        prompt = self._template(text)
        tokens = self._request("/tokenize", {"content": prompt, "add_special": False})
        if not isinstance(tokens, dict) or not isinstance(tokens.get("tokens"), list):
            raise PlatformError(
                "CONTRACT_CONFLICT", "token-response", "模型分词响应非法"
            )
        if (
            len(tokens["tokens"])
            > self.config["space"]["runtime_parameters"]["context"]
        ):
            raise PlatformError(
                "INPUT_INVALID", "context-exceeded", "文本加入模板后超过上下文限制"
            )
        return self._embed(prompt)

    def encode_image(self, image):
        from driving_data_platform.embedding.images import validate_image

        mime = validate_image(image)
        self.check_identity()
        prompt = self._template("IMAGE_PLACEHOLDER")
        if prompt.count("IMAGE_PLACEHOLDER") != 1:
            raise PlatformError(
                "CONTRACT_CONFLICT", "image-template", "图片模板占位符不唯一"
            )
        prefix, suffix = prompt.split("IMAGE_PLACEHOLDER")
        uri = "data:" + mime + ";base64," + base64.b64encode(image).decode("ascii")
        return self._embed(
            [
                {
                    "content": [
                        {"type": "text", "text": prefix},
                        {"type": "image_url", "image_url": {"url": uri}},
                        {"type": "text", "text": suffix},
                    ]
                }
            ]
        )


def provider(config):
    if config.get("backend") == "local_llama":
        return LocalLlama(config)
    if config.get("backend") == "external_api":
        return ExternalAPI(config)
    raise ValueError("未知 embedding 后端")
