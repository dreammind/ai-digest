#!/usr/bin/env bash
# Stop: pytest を実行し、失敗していれば exit 2 で作業終了を差し戻す。
set -u
unset VIRTUAL_ENV

# 差し戻し後の再停止では、無限ループを避けるため再チェックしない
if [[ $(jq -r '.stop_hook_active // false') == "true" ]]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR" || exit 0
if ! out=$(uv run --quiet pytest -q 2>&1); then
  printf "pytest が失敗しています。修正してから完了してください。\n%s\n" "$(echo "$out" | tail -40)" >&2
  exit 2
fi
exit 0
