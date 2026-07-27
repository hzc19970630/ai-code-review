from __future__ import annotations


class PlatformError(RuntimeError):
    code = "PLATFORM_ERROR"
    retryable = False


class ExternalServiceError(PlatformError):
    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ReviewOutputError(PlatformError):
    code = "LLM_OUTPUT_INVALID"
    retryable = True
