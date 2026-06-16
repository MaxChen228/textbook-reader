#!/bin/zsh
# book_pipeline 自動化迴圈 — launchd 觸發的單次 tick wrapper。
# launchd 環境極簡，需顯式補 PATH（uv 在 ~/.local/bin，claude/homebrew 在 /opt/homebrew/bin）。
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
# MinerU token 不入 plist/git（plist world-readable）；從 ~/.secrets 載入進程環境。
[ -f "$HOME/.secrets/mineru.env" ] && source "$HOME/.secrets/mineru.env"
# LLM 派工供應商：claude=Claude Max 訂閱（原生，預設）；kimi=導到 Kimi Code 端點
# （harness 仍是 claude CLI，僅換後端，key 由 _llm_env() 讀 ~/.secrets/kimi.env）。
# 實測 kimi-for-coding 在 audit 易陷「漫遊 content_list 反覆重讀」迴圈卡死，故預設回
# claude；要省訂閱額度再把這行改 kimi 即切換。
export BOOK_PIPELINE_PROVIDER=claude
cd "$HOME/project/textbook-reader" || exit 1
mkdir -p book_pipeline/reports
# max-llm 2：crawl 是最上游、每 tick 優先吃 1 個 LLM 名額；給 2 才能同時推進
# 1 個下游階段（qc/audit），否則下載的書一直積著不被處理（全自動會塞在 crawl）。
# 2 個 headless claude 序列跑約 30min < 45min tick 間隔，不超時。
exec uv run python -m book_pipeline.pipeline_tick \
    --once --max-llm 2 \
    >> book_pipeline/reports/daemon.stdout.log 2>&1
