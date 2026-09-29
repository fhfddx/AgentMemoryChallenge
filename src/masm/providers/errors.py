"""模型 Provider 的统一、脱敏错误边界。"""


class ProviderError(RuntimeError):
    """所有模型 Provider 错误的公共基类。"""


class ProviderUnavailableError(ProviderError):
    """Provider 网络、超时或 HTTP 状态不可用。"""


class ProviderResponseError(ProviderError):
    """Provider 响应无法通过结构或数值校验。"""
