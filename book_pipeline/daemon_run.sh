#!/bin/zsh
# book_pipeline 自動化迴圈 — launchd 觸發的單次 tick wrapper。
# launchd 環境極簡，需顯式補 PATH（uv 在 ~/.local/bin，claude/homebrew 在 /opt/homebrew/bin）。
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
# MinerU token 不入 plist/git（plist world-readable）；從 ~/.secrets 載入進程環境。
[ -f "$HOME/.secrets/mineru.env" ] && source "$HOME/.secrets/mineru.env"
# LLM 派工供應商（三選一）：
#   kimi   = claude CLI 導到 Kimi Code 端點（預設，省 Claude 訂閱額度；key 讀 ~/.secrets/kimi.env）
#   claude = Claude Max 訂閱原生（claude CLI 不換後端）
#   codex  = OpenAI codex CLI `codex exec --json`（認證走 ~/.codex/auth.json＝codex login ChatGPT
#            訂閱；模型 BOOK_PIPELINE_CODEX_MODEL 預設 gpt-5.4）。codex CLI 須裝 npm @openai/codex
#            （headless），勿用 brew cask 那份 GUI 包裝（dyld 啟動卡死、不適 launchd 無 GUI session）。
# 三者都吃 dispatch_llm 的 1h process-group timeout 護欄（卡死即殺、下個 tick 重派）。
# kimi 偶在 audit 陷「漫遊 content_list 反覆重讀」迴圈，靠 timeout 兜底。
#
# Failover 串接（BOOK_PIPELINE_PROVIDER_CHAIN，逗號分隔，優先序由左到右）：某 provider 撞額度
# 不再讓整輪停擺，而是換鏈上下一個重跑「同一任務」（不浪費派工），全鏈撞光才 defer 到下個 tick。
# 預設 kimi→codex→claude：先榨 kimi（省 Claude 額度）、撞了用 codex（ChatGPT 訂閱）、再撞回 Claude
# Max 原生。要單一 provider 就清掉 CHAIN、改用下面的 BOOK_PIPELINE_PROVIDER（向後相容）。
export BOOK_PIPELINE_PROVIDER_CHAIN=kimi,codex,claude
export BOOK_PIPELINE_PROVIDER=kimi
# 反應式控制迴圈（取代 daily 單次 tick）：一個 controller 進程跑有界 observe→非阻塞派工→
# reap→harvest→sleep 迴圈，三條件齊備（產物就緒 ∧ 資源可用 ∧ 無人在做）的 transition 立即
# 派 thread worker，OCR 一就緒即收割（延遲塌縮到一 poll cycle，免人工 kick）。launchd
# StartInterval 重拉、flock 序列化。LOOP_POLL=150：observe(build_queue 掃全書)~16s，故 poll
# 拉到 150s 把 observe 攤提到 ~10% duty（預設 75 對 16s observe 太密）。要回滾單次 tick 模型：
# 設 BOOK_PIPELINE_REACTIVE=0（程式碼預設即 0，行為退回 tick_once）。
export BOOK_PIPELINE_REACTIVE=1
export BOOK_PIPELINE_LOOP_POLL=150
cd "$HOME/project/textbook-reader" || exit 1
mkdir -p book_pipeline/reports
# 不設 --max-llm：LLM 可解的階段（crawl/qc/audit/sol_extract）每 tick 全部跑完，瓶頸
# 只交給外部額度（zlib 10/日、MinerU 預算）與 per-LLM 40min timeout 護欄。一個長 tick
# 靠 flock 擋住後續 launchd 觸發、做完即釋放，不會堆疊；卡死的子工由 timeout 殺掉重派。
exec uv run python -m book_pipeline.pipeline_tick \
    --once \
    >> book_pipeline/reports/daemon.stdout.log 2>&1
