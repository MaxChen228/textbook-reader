#!/bin/zsh
# book_pipeline 自動化迴圈 — launchd 觸發的單次 tick wrapper。
# launchd 環境極簡，需顯式補 PATH（uv 在 ~/.local/bin，claude/homebrew 在 /opt/homebrew/bin）。
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
# MinerU token 不入 plist/git（plist world-readable）；從 ~/.secrets 載入進程環境。
[ -f "$HOME/.secrets/mineru.env" ] && source "$HOME/.secrets/mineru.env"
# LLM 派工走 Kimi Code（harness 仍是 claude CLI，僅後端端點換）；key 由 _llm_env() 讀
# ~/.secrets/kimi.env，不進此處 env。設為 claude 或移除此行即回 Claude Max 訂閱。
export BOOK_PIPELINE_PROVIDER=kimi
cd "$HOME/project/textbook-reader" || exit 1
mkdir -p book_pipeline/reports
# max-llm 2：crawl 是最上游、每 tick 優先吃 1 個 LLM 名額；給 2 才能同時推進
# 1 個下游階段（qc/audit），否則下載的書一直積著不被處理（全自動會塞在 crawl）。
# 2 個 headless claude 序列跑約 30min < 45min tick 間隔，不超時。
exec uv run python -m book_pipeline.pipeline_tick \
    --once --max-llm 2 \
    >> book_pipeline/reports/daemon.stdout.log 2>&1
