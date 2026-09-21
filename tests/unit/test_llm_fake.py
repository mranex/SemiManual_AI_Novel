"""Kiểm tra interface LLM và fake client offline (T07)."""

from __future__ import annotations

import pytest

from novel_ai.core.llm import (
    ChatMessage,
    FakeLLMClient,
    LLMClient,
    LLMError,
    LLMRequest,
)


def _request(content: str = "viết thử một đoạn") -> LLMRequest:
    return LLMRequest(messages=(ChatMessage(role="user", content=content),))


def test_fake_client_returns_scripted_responses_in_order_and_records_calls() -> None:
    client = FakeLLMClient(responses=["phản hồi một", "phản hồi hai"])

    first = client.complete(_request("a"))
    second = client.complete(_request("b"))

    assert (first.text, second.text) == ("phản hồi một", "phản hồi hai")
    assert [call.messages[0].content for call in client.calls] == ["a", "b"]


def test_exhausted_queue_raises_llm_error_instead_of_calling_network() -> None:
    with pytest.raises(LLMError):
        FakeLLMClient().complete(_request())


def test_scripted_failure_is_raised_and_still_recorded() -> None:
    client = FakeLLMClient(responses=LLMError("timeout"))

    with pytest.raises(LLMError):
        client.complete(_request())

    assert len(client.calls) == 1


def test_queue_appends_after_constructor_responses() -> None:
    client = FakeLLMClient(responses=["một"])
    client.queue("hai")

    assert client.complete(_request()).text == "một"
    assert client.complete(_request()).text == "hai"


def test_completed_response_carries_model_and_finish_reason() -> None:
    response = FakeLLMClient(responses=["x"], model="fake-model").complete(_request())
    assert response.model == "fake-model"
    assert response.finish_reason == "stop"


def test_fake_client_satisfies_llm_client_protocol() -> None:
    assert isinstance(FakeLLMClient(), LLMClient)


def test_chat_message_as_dict_is_provider_ready() -> None:
    assert ChatMessage(role="system", content="luật").as_dict() == {
        "role": "system",
        "content": "luật",
    }
