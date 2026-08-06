from __future__ import annotations

import json

import httpx
import pytest

from mr_review_platform.config import Settings
from mr_review_platform.errors import ExternalServiceError
from mr_review_platform.llm import OpenAICompatibleLLM
from mr_review_platform.prompting import (
    PromptBudgetError,
    assemble_prompt_envelope,
    request_hash,
)
from mr_review_platform.review_plan import build_effective_review_plan


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


def _envelope(*, max_input_tokens: int = 3_000):
    plan = build_effective_review_plan(
        {
            "config_version": 1,
            "review_enabled": True,
            "review_strategy": "simple_context",
            "prompt_profile": "default",
            "llm_provider": "openai-compatible",
            "llm_model": "test-model",
            "llm_credential_ref": "env://TEST_LLM_KEY",
            "data_policy": "internal_only",
            "publish_mode": "shadow",
            "supported_extensions": [".py"],
            "ignore_paths": [],
            "enabled_rules": ["correctness"],
            "path_rules": {},
            "max_input_tokens": max_input_tokens,
            "max_transport_attempts_per_call": 2,
            "min_confidence": 70,
        }
    )
    return assemble_prompt_envelope(plan, {"changes": []})


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
    actual_request_hashes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        actual_request_hashes.append(request_hash(json.loads(request.content)))
        content = "not json" if call_count == 1 else json.dumps(_valid_review())
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    with OpenAICompatibleLLM(
        _settings(), api_key="key", transport=httpx.MockTransport(handler)
    ) as client:
        output = client.review({"changes": []})

    assert call_count == 2
    assert len(output.calls) == 2
    assert [call.request_hash for call in output.calls] == actual_request_hashes
    assert output.request_hash == actual_request_hashes[0]
    assert actual_request_hashes[0] != actual_request_hashes[1]


def test_llm_adapter_classifies_non_retryable_request_error() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(400, json={"error": "bad"}))

    with OpenAICompatibleLLM(_settings(), api_key="key", transport=transport) as client:
        with pytest.raises(ExternalServiceError) as captured:
            client.review({"changes": []})

    assert captured.value.code == "LLM_REQUEST_ERROR"
    assert captured.value.retryable is False


def test_each_real_transport_attempt_is_audited(monkeypatch) -> None:
    monkeypatch.setattr("mr_review_platform.llm.time.sleep", lambda _seconds: None)
    response_count = 0
    events = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal response_count
        response_count += 1
        if response_count == 1:
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_valid_review())}}]},
        )

    with OpenAICompatibleLLM(
        _settings(), api_key="key", transport=httpx.MockTransport(handler)
    ) as client:
        output = client.review(_envelope(), call_auditor=events.append)

    assert [(event.call_no, event.status) for event in events] == [
        (1, "running"),
        (1, "failed"),
        (2, "running"),
        (2, "succeeded"),
    ]
    assert output.calls[0].call_no == 2
    assert output.calls[0].transport_attempt_no == 2


def test_repair_request_is_rejected_when_it_exceeds_input_budget() -> None:
    request_count = 0
    events = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "x" * 12_000}}]},
        )

    with OpenAICompatibleLLM(
        _settings(), api_key="key", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(PromptBudgetError):
            client.review(_envelope(), call_auditor=events.append)

    assert request_count == 1
    assert [(event.call_no, event.status) for event in events] == [
        (1, "running"),
        (1, "succeeded"),
    ]
