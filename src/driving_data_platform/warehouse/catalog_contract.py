"""校验目录 schema；非空语义由数据校验补充，不依赖文件目录保留 nullable。"""


def check_schema(actual: dict, expected: dict) -> list[str]:
    def types(value):
        if isinstance(value, dict):
            return {
                key: types(item)
                for key, item in value.items()
                if key not in {"metadata", "nullable"}
            }
        if isinstance(value, list):
            return [types(item) for item in value]
        return value

    if types(actual) != types(expected):
        raise ValueError("目录字段名称、顺序或数据类型不一致")
    return [
        field["name"] for field in expected["fields"] if field.get("nullable") is False
    ]
