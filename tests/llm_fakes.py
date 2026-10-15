"""LLM を使う機能のテストで共通に使う、`LlmClient` の偽物と応答の組み立て。"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Unpack

from anthropic.types import Message, MessageParam

from aidigest.llm import CreateOptions, Purpose

MODEL = "claude-haiku-5-5"


@dataclass(frozen=True)
class Call:
    purpose: Purpose
    messages: list[MessageParam]
    max_tokens: int
    options: CreateOptions


class FakeLlm:
    """`LlmClient` の偽物。決まった順に応答を返すか、例外を送出する。"""

    model = MODEL

    def __init__(self, outcomes: Sequence[Message | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[Call] = []

    def create(
        self,
        purpose: Purpose,
        *,
        messages: Iterable[MessageParam],
        max_tokens: int,
        **options: Unpack[CreateOptions],
    ) -> Message:
        self.calls.append(Call(purpose, list(messages), max_tokens, options))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def user_text(self, call: int = 0) -> str:
        (first,) = self.calls[call].messages
        content = first["content"]
        assert isinstance(content, str)
        return content


def message(content: Sequence[Mapping[str, object]], stop_reason: str = "tool_use") -> Message:
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }
    )


def tool_reply(
    name: str, tool_input: Mapping[str, object], stop_reason: str = "tool_use"
) -> Message:
    block = {"type": "tool_use", "id": "toolu_test", "name": name, "input": tool_input}
    return message([block], stop_reason)


def text_reply(text: str = "すみません", stop_reason: str = "end_turn") -> Message:
    return message([{"type": "text", "text": text}], stop_reason)
