"""服务探测逻辑回归测试（不联网、不读取真实凭证）。"""

import service_check
from model_catalog import FALLBACK_MODEL_CANDIDATES


class _FakeModels:
    def __init__(self, items=None, error=None):
        self.items = items or []
        self.error = error

    def list(self):
        if self.error:
            raise RuntimeError(self.error)
        return {"data": self.items}


class _FakeClient:
    def __init__(self, models):
        self.models = models

    def with_options(self, **_kwargs):
        return self


class _FakeEngine:
    label = "Fake"

    def __init__(self, items=None, error=None):
        self.model = "gemini-3.6-flash"
        self._client = _FakeClient(_FakeModels(items, error))
        self.calls = []

    def _call(self, _system, _user):
        self.calls.append(self.model)
        if self.model == "gemini-2.5-flash":
            raise RuntimeError("model unavailable")
        return "正常"


def test_catalog_is_dynamic_and_filters_non_text_models():
    engine = _FakeEngine([
        {"id": "gemini-3.6-flash"},
        {"id": "gemini-new-flash"},
        {"id": "gemini-embedding-2-preview"},
        {"id": "gemini-new-flash"},
    ])
    original_build = service_check.providers.build_llm
    original_out = service_check.out
    payloads = []
    try:
        service_check.providers.build_llm = lambda **_kwargs: engine
        service_check.out = payloads.append
        service_check.probe_llm("gemini")
    finally:
        service_check.providers.build_llm = original_build
        service_check.out = original_out

    payload = payloads[-1]
    assert payload["source"] == "catalog"
    assert [row["model"] for row in payload["results"]] == [
        "gemini-3.6-flash", "gemini-new-flash"
    ]
    assert payload["results"][0]["verified"] is True
    assert payload["results"][1]["verified"] is False
    assert engine.calls == ["gemini-3.6-flash"]


def test_catalog_failure_uses_runtime_fallback_candidates():
    engine = _FakeEngine(error="404 models endpoint")
    original_build = service_check.providers.build_llm
    original_out = service_check.out
    payloads = []
    try:
        service_check.providers.build_llm = lambda **_kwargs: engine
        service_check.out = payloads.append
        service_check.probe_llm("gemini")
    finally:
        service_check.providers.build_llm = original_build
        service_check.out = original_out

    payload = payloads[-1]
    assert payload["source"] == "fallback"
    assert payload["discoveryError"] == "404 models endpoint"
    expected = [engine.model] + [
        model for model in FALLBACK_MODEL_CANDIDATES["gemini"]
        if model != engine.model
    ]
    assert [row["model"] for row in payload["results"]] == expected
    assert all(row["source"] == "fallback" for row in payload["results"])


def main():
    test_catalog_is_dynamic_and_filters_non_text_models()
    test_catalog_failure_uses_runtime_fallback_candidates()
    print("service check tests passed")


if __name__ == "__main__":
    main()
