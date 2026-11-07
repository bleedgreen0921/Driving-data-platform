"""Explicit OpenAI-style text and content-parts image protocol adapter."""

import base64
import json
import math
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from driving_data_platform.common.errors import PlatformError
from driving_data_platform.embedding.artifacts import validate_space, vector32
from driving_data_platform.embedding.images import RULES, validate_image
from driving_data_platform.embedding.transport import NoRedirect

FIELDS = {
    "schema_version",
    "backend",
    "model",
    "revision",
    "protocol",
    "image_protocol",
    "image_preprocessing",
    "text_instruction",
    "image_instruction",
    "dimension",
    "dtype",
    "normalization",
    "distance",
    "test_only",
    "revision_field",
    "implementation_sha256",
}


def implementation():
    import hashlib
    from pathlib import Path

    from driving_data_platform.embedding.artifacts import checksum

    return checksum(
        {
            name: hashlib.sha256(
                Path(__file__).with_name(name).read_bytes()
            ).hexdigest()
            for name in ("external.py", "images.py", "transport.py")
        }
    )


def validate_contract(value):
    if (
        set(value) != FIELDS
        or value["schema_version"] != 3
        or value["backend"] != "external_api"
    ):
        raise ValueError("外部空间字段不完整或版本不支持")
    if not isinstance(value["implementation_sha256"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", value["implementation_sha256"]
    ):
        raise ValueError("外部空间编码实现摘要未确认")
    for key in ("model", "revision", "revision_field"):
        if not isinstance(value[key], str) or not re.fullmatch(
            r"[a-zA-Z0-9_./:@+-]{1,256}", value[key]
        ):
            raise ValueError("外部空间必须登记固定模型与可核验版本字段")
    if value["revision"].lower() in {"latest", "unknown", "unverified", "todo"}:
        raise ValueError("模型版本必须固定")
    if (value["protocol"], value["image_protocol"]) != (
        "openai_embeddings",
        "content_parts_v1",
    ):
        raise ValueError("外部空间协议不支持")
    if value["image_preprocessing"] != RULES:
        raise ValueError("外部空间图片策略不匹配")
    for key in ("text_instruction", "image_instruction"):
        if not isinstance(value[key], str) or len(value[key]) > 4096:
            raise ValueError("空间指令必须是有界字符串")
    if type(value["dimension"]) is not int or not 1 <= value["dimension"] <= 16000:
        raise ValueError("外部向量维度非法")
    if type(value["test_only"]) is not bool or (
        value["dtype"],
        value["normalization"],
        value["distance"],
    ) != ("float32", "l2", "cosine"):
        raise ValueError("外部向量类型或归一化规则非法")


class ExternalAPI:
    def __init__(self, config=None):
        self.config = dict(config or {})
        self.space = self.config.get("space")
        self.endpoint = self.config.get("endpoint", "")
        try:
            parsed = urlsplit(self.endpoint)
            valid = (
                (
                    parsed.scheme == "https"
                    or (
                        parsed.scheme == "http"
                        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                    )
                )
                and parsed.hostname
                and not any(
                    (parsed.username, parsed.password, parsed.query, parsed.fragment)
                )
            )
            _ = parsed.port
        except (ValueError, TypeError, AttributeError):
            valid = False
        if self.config and not valid:
            raise ValueError("外部地址必须为无凭据的 HTTPS 或本机 HTTP URL")
        self.timeout = self.config.get("timeout_seconds", 60)
        if (
            type(self.timeout) not in (int, float)
            or not math.isfinite(self.timeout)
            or not 0 < self.timeout <= 60
        ):
            raise ValueError("外部请求超时必须在 0–60 秒之间")
        self.max_text = self.config.get("max_text_characters", 4096)
        self.max_image = self.config.get("max_image_bytes", RULES["max_bytes"])
        if (
            type(self.max_text) is not int
            or not 1 <= self.max_text <= 4096
            or type(self.max_image) is not int
            or not 1 <= self.max_image <= RULES["max_bytes"]
        ):
            raise ValueError("外部输入限制非法")
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def capabilities(self):
        return {
            "backend": "external_api",
            "text_encoding": bool(self.space),
            "image_encoding": bool(self.space),
            "compatibility": "requires-protocol-and-space-verification",
        }

    def runtime_config(self):
        return {
            "backend": "external_api",
            "configured": bool(self.endpoint),
            "space_id": validate_space(self.space) if self.space else None,
        }

    def check_identity(self):
        if not self.endpoint or self.space is None:
            raise PlatformError(
                "DEPENDENCY_UNAVAILABLE",
                "external-not-configured",
                "外部服务及空间尚未配置",
            )
        validate_space(self.space)
        if (
            self.space.get("backend") != "external_api"
            or self.space.get("schema_version") != 3
        ):
            raise PlatformError(
                "CONTRACT_CONFLICT", "external-space", "外部编码要求独立 v3 空间"
            )
        if self.space["implementation_sha256"] != implementation():
            raise PlatformError(
                "CONTRACT_CONFLICT",
                "external-implementation",
                "外部编码实现已变化，须重新评测新空间",
            )
        if self.config.get("model") != self.space["model"]:
            raise PlatformError(
                "CONTRACT_CONFLICT", "external-model", "外部模型与空间登记不一致"
            )

    def _headers(self):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        name = self.config.get("api_key_env")
        if name:
            if not isinstance(name, str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", name
            ):
                raise ValueError("凭据必须通过环境变量名称指定")
            secret = os.environ.get(name)
            if not secret or any(c in secret for c in "\r\n"):
                raise PlatformError(
                    "DEPENDENCY_UNAVAILABLE",
                    "external-credentials",
                    "外部服务凭据未设置或非法",
                )
            header = self.config.get("auth_header", "Authorization")
            if (
                not isinstance(header, str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", header)
                or header.lower()
                in {
                    "host",
                    "content-length",
                    "content-type",
                    "accept",
                    "transfer-encoding",
                }
            ):
                raise ValueError("认证头名称非法")
            headers[header] = (
                "Bearer " + secret if header.lower() == "authorization" else secret
            )
        return headers

    def _encode(self, value):
        self.check_identity()
        payload = json.dumps(
            {"model": self.space["model"], "input": value, "encoding_format": "float"},
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        try:
            request = Request(
                self.endpoint.rstrip("/") + "/embeddings",
                data=payload,
                headers=self._headers(),
            )
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2 * 1024**2 + 1)
            if len(raw) > 2 * 1024**2:
                raise ValueError()
            result = json.loads(raw)
            if result.get(self.space["revision_field"]) != self.space["revision"]:
                raise PlatformError(
                    "CONTRACT_CONFLICT",
                    "external-revision",
                    "外部响应模型版本与空间不一致",
                )
            rows = result["data"]
            if (
                not isinstance(rows, list)
                or len(rows) != 1
                or type(rows[0].get("index")) is not int
                or rows[0]["index"] != 0
            ):
                raise ValueError()
            values = rows[0]["embedding"]
            if (
                not isinstance(values, list)
                or len(values) != self.space["dimension"]
                or any(
                    type(x) not in (int, float) or not math.isfinite(x) for x in values
                )
            ):
                raise ValueError()
            # Stable normalization avoids overflow on finite but large values.
            scale = max(abs(x) for x in values)
            if scale == 0:
                raise ValueError()
            norm = math.sqrt(math.fsum((x / scale) ** 2 for x in values))
            return vector32(
                [(x / scale) / norm for x in values], self.space["dimension"]
            )
        except HTTPError as error:
            code = (
                "external-auth"
                if error.code in (401, 403)
                else "external-rate-limit"
                if error.code == 429
                else "external-redirect"
                if 300 <= error.code < 400
                else "external-http"
            )
            raise PlatformError(
                "DEPENDENCY_UNAVAILABLE", code, f"外部编码请求失败 HTTP {error.code}"
            ) from None
        except (URLError, TimeoutError, OSError):
            raise PlatformError(
                "DEPENDENCY_UNAVAILABLE",
                "external-unavailable",
                "外部编码连接失败或超时",
            ) from None
        except PlatformError:
            raise
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            IndexError,
            OverflowError,
        ):
            raise PlatformError(
                "CONTRACT_CONFLICT",
                "external-response",
                "外部编码响应格式、维度或数值非法",
            ) from None

    def encode_text(self, text):
        self.check_identity()
        if not isinstance(text, str) or not text.strip() or len(text) > self.max_text:
            raise PlatformError("INPUT_INVALID", "external-text", "文本为空或超限")
        try:
            value = self.space["text_instruction"] + text
            value.encode("utf-8", errors="strict")
        except UnicodeError:
            raise PlatformError(
                "INPUT_INVALID", "external-text", "文本编码非法"
            ) from None
        return self._encode(value)

    def encode_image(self, image):
        mime = validate_image(image)
        if len(image) > self.max_image:
            raise PlatformError(
                "INPUT_INVALID", "external-image-limit", "图片超过外部服务输入上限"
            )
        self.check_identity()
        uri = "data:" + mime + ";base64," + base64.b64encode(image).decode("ascii")
        return self._encode(
            [
                {
                    "content": [
                        {"type": "text", "text": self.space["image_instruction"]},
                        {"type": "image_url", "image_url": {"url": uri}},
                    ]
                }
            ]
        )

    def probe(self):
        self.encode_text("embedding availability probe")
        return {
            "status": "passed",
            "scope": "text-protocol-only",
            "backend": "external_api",
        }
