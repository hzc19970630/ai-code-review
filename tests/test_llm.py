from __future__ import annotations

import json

import httpx
import pytest

from mr_review_platform.config import Settings
from mr_review_platform.errors import ExternalServiceError
from mr_review_platform.llm import OpenAICompatibleLLM


def _settings() -> Settings:
    return Settings(
        app_env="test",
        llm_base_url="https://llm.example.com/v1",
        llm_model="test-model",
        llm_max_retries=1,
    )


def _valid_review() -> dict:
    return {
        "summary": "No verified issues.",
        "coverage": {"reviewed_files": 1, "skipped_files": 0, "limitations": []},
        "findings": [],
    }


def test_llm_adapter_parses_structured_result_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(_valid_review())}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    with OpenAICompatibleLLM(
        _settings(), api_key="key", transport=httpx.MockTransport(handler)
    ) as client:
        output = client.review({"changes": []})

    assert output.result.summary == "No verified issues."
    assert output.calls[0].total_tokens == 15
    assert len(output.request_hash) == 64


def test_llm_adapter_repairs_invalid_first_response() -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        content = "not json" if call_count == 1 else json.dumps(_valid_review())
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    with OpenAICompatibleLLM(
        _settings(), api_key="key", transport=httpx.MockTransport(handler)
    ) as client:
        output = client.review({"changes": []})

    assert call_count == 2
    assert len(output.calls) == 2


def test_llm_adapter_classifies_non_retryable_request_error() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(400, json={"error": "bad"}))

    with OpenAICompatibleLLM(_settings(), api_key="key", transport=transport) as client:
        with pytest.raises(ExternalServiceError) as captured:
            client.review({"changes": []})

    assert captured.value.code == "LLM_REQUEST_ERROR"
    assert captured.value.retryable is False
