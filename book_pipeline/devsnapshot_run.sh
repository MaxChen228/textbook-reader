#!/bin/zsh
# devctl snapshot wrapper — launchd 每 ~2 分觸發，重生 dev/status.json 供監控頁讀取。
# 純檔案讀取、無 LLM、無對外計費 → 高頻安全。PATH 補 uv（~/.local/bin）。
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd "$HOME/project/textbook-reader" || exit 1
exec uv run python -m book_pipeline.devctl snapshot >/dev/null 2>&1
