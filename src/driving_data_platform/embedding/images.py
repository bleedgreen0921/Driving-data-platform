"""离线加工与查询共用的原字节图片校验。"""

import io
import warnings

from PIL import Image, UnidentifiedImageError

from driving_data_platform.common.errors import PlatformError

RULES = {
    "formats": ["JPEG", "PNG"],
    "max_bytes": 25 * 1024**2,
    "max_edge": 8192,
    "max_pixels": 20_000_000,
    "frames": 1,
    "transmission": "original-bytes-no-exif-rotation-resize-or-reencode",
    "pillow_version": "13.0.0",
}


def validate_image(data):
    """完整解码但不修改图片；损坏输入不得到达模型。"""
    try:
        if not isinstance(data, bytes) or not 0 < len(data) <= RULES["max_bytes"]:
            raise ValueError("图片必须为非空字节且不超过 25 MiB")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                fmt = image.format
                width, height = image.size
                if (
                    fmt not in RULES["formats"]
                    or getattr(image, "n_frames", 1) != 1
                    or max(width, height) > RULES["max_edge"]
                    or width * height > RULES["max_pixels"]
                ):
                    raise ValueError("图片格式、帧数、边长或像素数超限")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
        return "image/jpeg" if fmt == "JPEG" else "image/png"
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise PlatformError(
            "INPUT_INVALID", "invalid-image", f"图片校验失败：{error}"
        ) from None
