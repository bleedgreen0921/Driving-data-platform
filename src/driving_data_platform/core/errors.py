"""跨数据域共享的输入错误类型。"""


class IngestionError(ValueError):
    """输入或快照不满足契约；消息只包含稳定的错误代码。"""
