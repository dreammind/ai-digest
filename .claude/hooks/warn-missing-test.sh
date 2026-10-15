#!/usr/bin/env bash
# PostToolUse: src/aidigest/ のモジュールに対応する tests/**/test_<name>.py が無ければ警告する。
# 作業は止めない（警告のみ）。
set -u

file_path=$(jq -r '.tool_input.file_path // empty')
cd "$CLAUDE_PROJECT_DIR" || exit 0
rel="${file_path#"$CLAUDE_PROJECT_DIR"/}"

[[ "$rel" == src/aidigest/*.py ]] || exit 0
name=$(basename "$rel" .py)
[[ "$name" == "__init__" || "$name" == "__main__" ]] && exit 0

if [[ -z $(find tests -name "test_${name}.py" -print -quit 2>/dev/null) ]]; then
  jq -n --arg msg "警告: $rel に対応するテスト tests/**/test_${name}.py がありません。TDD ではテストを先に書いてください。" \
    '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $msg}}'
fi
exit 0
