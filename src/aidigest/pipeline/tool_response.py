"""tool use で出力を受け取る LLM 呼び出しの、応答の共通の検証（specs/008、010）。"""

from anthropic.types import Message


def single_tool_input(response: Message, tool_name: str) -> tuple[object | None, str]:
    """`tool_name` の tool_use がちょうど 1 つあれば、その入力を返す。

    出力が途中で切れた（`max_tokens`）・断られた（`refusal`）・ツールを使わなかった・
    複数回使ったときは、(None, 理由) を返す。入力の中身の検証は呼び出し側で行う。
    """
    if response.stop_reason in ("max_tokens", "refusal"):
        return None, f"stop_reason={response.stop_reason}"
    calls = [b for b in response.content if b.type == "tool_use" and b.name == tool_name]
    if len(calls) != 1:
        return None, f"{tool_name} の呼び出しが {len(calls)} 回"
    return calls[0].input, ""
