#!/usr/bin/env bash
# Stop: pytest を実行し、実装の途中で壊れていれば exit 2 で作業終了を差し戻す。
#
# - pytest が通る → 止まってよい
# - pytest が失敗し、src/ に未コミットの変更がある → 実装の途中で壊れているので差し戻す
# - pytest が失敗し、src/ に未コミットの変更が無い → テストだけを書いた TDD の Red の段階
#   （承認待ちなど）とみなし、差し戻さない
set -u
unset VIRTUAL_ENV

# 差し戻し後の再停止では、無限ループを避けるため再チェックしない
if [[ $(jq -r '.stop_hook_active // false') == "true" ]]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR" || exit 0
if out=$(uv run --quiet pytest -q 2>&1); then
  exit 0
fi

# 未追跡のファイルも含めて、src/ に未コミットの変更があるか
if [[ -z $(git status --porcelain --untracked-files=all -- src/) ]]; then
  exit 0
fi

printf "pytest が失敗しています（src/ に未コミットの変更あり）。修正してから完了してください。\n%s\n" \
  "$(echo "$out" | tail -40)" >&2
exit 2
