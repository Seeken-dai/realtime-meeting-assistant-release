"""无网络验证 LLM 瞬时错误重试和结构化诊断。"""

from suggest import (
    LLMRequestError,
    SuggestionEngine,
    classify_llm_error,
    format_llm_error,
    llm_error_details,
)


def _engine(retry_attempts=2):
    # 不初始化 OpenAI 客户端，只测试本地重试编排。
    engine = object.__new__(SuggestionEngine)
    engine.retry_attempts = retry_attempts
    engine.retry_backoff_seconds = 0
    engine.timeout_seconds = 12.0
    return engine


def test_connection_error_retries_once_then_succeeds():
    error_type = type("APIConnectionError", (Exception,), {})
    calls = []

    def operation():
        calls.append(True)
        if len(calls) == 1:
            raise error_type("Connection error.")
        return "ok"

    assert _engine()._run_with_retry(operation) == "ok"
    assert len(calls) == 2
    kind, retryable, stage = classify_llm_error(error_type("Connection error."))
    assert (kind, retryable, stage) == ("connection", True, None)


def test_auth_error_does_not_retry():
    error_type = type("AuthenticationError", (Exception,), {})
    calls = []

    def operation():
        calls.append(True)
        raise error_type("401 invalid api key")

    try:
        _engine()._run_with_retry(operation)
    except LLMRequestError as error:
        assert error.attempts == 1
        assert error.retryable is False
    else:
        raise AssertionError("authentication errors must fail without retry")
    assert len(calls) == 1


def test_timeout_diagnostic_is_structured_after_retries_exhausted():
    error_type = type("APITimeoutError", (Exception,), {})

    def operation():
        raise error_type("Request timed out.")

    try:
        _engine()._run_with_retry(operation)
    except LLMRequestError as error:
        details = llm_error_details(
            error,
            provider="gemini",
            model="gemini-3.6-flash",
            stage="final",
        )
    else:
        raise AssertionError("timeout should be reported after retry exhaustion")

    assert details["kind"] == "timeout"
    assert details["timeoutStage"] == "llm"
    assert details["attempts"] == 2
    assert details["retryable"] is True
    assert "gemini-3.6-flash" in format_llm_error(details, "会议纪要服务")
    assert "已尝试 2 次" in format_llm_error(details, "会议纪要服务")


def main():
    test_connection_error_retries_once_then_succeeds()
    test_auth_error_does_not_retry()
    test_timeout_diagnostic_is_structured_after_retries_exhausted()
    print("ok: LLM transient retry and diagnostics")


if __name__ == "__main__":
    main()
