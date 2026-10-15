#!/usr/bin/env bash
# PostToolUse: 編集された .py ファイルに ruff format / ruff check / pyright を実行する。
# エラーがあれば exit 2 で Claude に差し戻す。
set -u
unset VIRTUAL_ENV

file_path=$(jq -r '.tool_input.file_path // empty')
[[ "$file_path" == *.py ]] || exit 0
[[ -f "$file_path" ]] || exit 0

cd "$CLAUDE_PROJECT_DIR" || exit 0

errors=""
uv run --quiet ruff format --quiet "$file_path" >/dev/null 2>&1
if ! out=$(uv run --quiet ruff check "$file_path" 2>&1); then
  errors+="[ruff check]\n$out\n"
fi
if ! out=$(uv run --quiet pyright "$file_path" 2>&1); then
  errors+="[pyright]\n$out\n"
fi

if [[ -n "$errors" ]]; then
  printf "%b" "$errors" >&2
  exit 2
fi
exit 0
