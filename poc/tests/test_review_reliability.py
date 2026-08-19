"""无网络验证会后复盘增强的超时配置和安全诊断。"""

import json

import generate_review as review


class FakeEngine:
    provider = "gemini"
    model = "gemini-3.6-flash"

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def _call(self, _system, _prompt):
        if self.error:
            raise self.error("Request timed out.")
        return self.response


def _payload():
    return {
        "title": "复盘增强测试",
        "scene": "requirements",
        "startedAt": 1_000_000,
        "transcript": [
            {
                "id": "review-test-1",
                "speaker": "我",
                "text": "确认采用这个方案。",
                "isFinal": True,
                "at": 1_001_000,
            }
        ],
    }


def test_review_uses_a_longer_offline_budget():
    assert review.REVIEW_LLM_TIMEOUT_SECONDS >= 30
    assert review.REVIEW_LLM_RETRY_ATTEMPTS == 2


def test_generate_review_passes_timeout_and_retry_to_provider():
    response = json.dumps(
        {
            "memoryItems": [
                {
                    "kind": "decision",
                    "content": "采用这个方案",
                    "owner": None,
                    "dueAt": None,
                    "evidenceTranscriptId": "review-test-1",
                    "evidenceText": "确认采用这个方案。",
                }
            ],
            "glossaryCandidates": [],
        },
        ensure_ascii=False,
    )
    captured = {}
    original = review.providers.build_llm

    def fake_build_llm(_kb, **kwargs):
        captured.update(kwargs)
        return FakeEngine(response=response)

    review.providers.build_llm = fake_build_llm
    try:
        result = review.generate_review(
            _payload(),
            provider="gemini",
            model="gemini-3.6-flash",
            timeout_seconds=30,
            retry_attempts=2,
        )
    finally:
        review.providers.build_llm = original

    assert captured["timeout_seconds"] == 30
    assert captured["retry_attempts"] == 2
    assert result["ok"] is True
    assert result["timeoutSeconds"] == 30
    assert result["retryAttempts"] == 2


def test_timeout_returns_structured_review_diagnostic():
    error_type = type("APITimeoutError", (Exception,), {})
    original = review.providers.build_llm
    review.providers.build_llm = lambda _kb, **_kwargs: FakeEngine(error=error_type)
    try:
        try:
            review.generate_review(
                _payload(),
                provider="gemini",
                model="gemini-3.6-flash",
                timeout_seconds=30,
                retry_attempts=2,
            )
        except review.ReviewGenerationError as error:
            assert error.diagnostic["kind"] == "timeout"
            assert error.diagnostic["timeoutStage"] == "llm"
            assert error.diagnostic["provider"] == "gemini"
            assert error.diagnostic["model"] == "gemini-3.6-flash"
            assert error.diagnostic["timeoutSeconds"] == 30
            assert "请求超时" in str(error)
        else:
            raise AssertionError("timeout should produce a structured review error")
    finally:
        review.providers.build_llm = original


def main():
    test_review_uses_a_longer_offline_budget()
    test_generate_review_passes_timeout_and_retry_to_provider()
    test_timeout_returns_structured_review_diagnostic()
    print("ok: review enhancement timeout and diagnostics")


if __name__ == "__main__":
    main()
