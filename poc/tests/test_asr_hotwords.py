"""Local regression for Aliyun hotword vocabulary synchronization."""

from __future__ import annotations

import sys
import types

import asr_hotwords
from asr_hotwords import ensure_aliyun_vocabulary_id


class FakeVocabularyService:
    created = None

    def list_vocabularies(self):
        return []

    def create_vocabulary(self, **kwargs):
        type(self).created = kwargs
        return "vocab-test-id"


class FailingVocabularyService:
    def list_vocabularies(self):
        raise RuntimeError("simulated quota/network failure")

    def create_vocabulary(self, **kwargs):
        raise RuntimeError("simulated quota/network failure")


class ExistingButQuotaUpdatingVocabularyService:
    def list_vocabularies(self):
        return [
            {
                "vocabulary_id": "vocab-existing",
                "prefix": "mchot",
                "target_model": "paraformer-realtime-v2",
            }
        ]

    def update_vocabulary(self, **kwargs):
        raise RuntimeError(
            "VocabularyServiceException: 429 Throttling.AllocationQuota: "
            "Free allocated quota exceeded"
        )

    def delete_vocabulary(self, _vocabulary_id):
        raise AssertionError("an existing vocabulary must not be deleted on quota errors")

    def create_vocabulary(self, **kwargs):
        raise AssertionError("an existing vocabulary must not be recreated on quota errors")


dashscope = types.ModuleType("dashscope")
audio = types.ModuleType("dashscope.audio")
asr = types.ModuleType("dashscope.audio.asr")
asr.VocabularyService = FakeVocabularyService
dashscope.audio = audio
audio.asr = asr
sys.modules["dashscope"] = dashscope
sys.modules["dashscope.audio"] = audio
sys.modules["dashscope.audio.asr"] = asr


vocabulary_id = ensure_aliyun_vocabulary_id(
    [{"text": "联想文档中台", "weight": 4}],
    "test-key",
    prefix="mc-hot 2026",
)

assert vocabulary_id == "vocab-test-id"
assert FakeVocabularyService.created is not None
assert FakeVocabularyService.created["prefix"] == "mchot2026"
assert FakeVocabularyService.created["prefix"].isalnum()
assert FakeVocabularyService.created["vocabulary"] == [
    {"text": "联想文档中台", "weight": 4}
]

asr.VocabularyService = FailingVocabularyService
assert ensure_aliyun_vocabulary_id(
    [{"text": "三快", "weight": 5}],
    "test-key",
) is None
assert "quota/network failure" in (asr_hotwords.LAST_SYNC_DIAGNOSTIC or "")

asr.VocabularyService = ExistingButQuotaUpdatingVocabularyService
assert ensure_aliyun_vocabulary_id(
    [{"text": "三快", "weight": 5}],
    "test-key",
) == "vocab-existing"
assert "继续复用已有词表" in (asr_hotwords.LAST_SYNC_DIAGNOSTIC or "")
assert "AllocationQuota" in (asr_hotwords.LAST_SYNC_DIAGNOSTIC or "")

assert ensure_aliyun_vocabulary_id(
    [{"text": "三快", "weight": 5}],
    "",
) is None
assert asr_hotwords.LAST_SYNC_DIAGNOSTIC == "阿里 ASR 凭证为空"

print("ok: Aliyun hotword prefix normalization + diagnostic downgrade")
