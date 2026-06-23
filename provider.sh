#!/usr/bin/env bash
# provider.sh — 從「任意目錄」偵測現在跑 agent 會落在三層 provider fallback 的哪一層。
# 對 _probe_provider.py 的薄包裝：自我定位本 repo 根（即使經 symlink/從別處呼叫）→ 在根用 uv
# 跑、原樣透傳所有參數。故可 `./provider.sh`、絕對路徑、或 symlink 進 PATH/任何 bin，皆正確。
#
#   ./provider.sh                     現在會用哪層（早停，最省額度）
#   ./provider.sh --all               三層健康全景
#   ./provider.sh --skip claude       關掉某層看 fallback 改落哪（--skip codex / --skip codex-pool …）
#   ./provider.sh --only codex        只測單層本身可用否
#   ./provider.sh -h                  完整參數
set -euo pipefail

# 解析本 script 的真實絕對路徑（逐層解 symlink；macOS BSD readlink 無 -f 故手動迴圈、可移植）。
src="${BASH_SOURCE[0]}"
while [ -h "$src" ]; do
  dir="$(cd -P "$(dirname "$src")" && pwd)"
  src="$(readlink "$src")"
  [[ "$src" != /* ]] && src="$dir/$src"
done
ROOT="$(cd -P "$(dirname "$src")" && pwd)"

# 在 repo 根跑：uv 抓到 ROOT/pyproject.toml 的環境，python 以 ROOT 為 sys.path[0]（`from
# book_pipeline import …` 才解析得到）。_probe_provider 用 pipeline_tick.ROOT 跑子工，與 cwd 無關。
cd "$ROOT"
exec uv run python _probe_provider.py "$@"
