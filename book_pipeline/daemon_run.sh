#!/bin/zsh
# book_pipeline 自動化迴圈 — launchd 觸發的反應式 controller wrapper（非定時批次）。
# 下方 BOOK_PIPELINE_REACTIVE=1 → 一個長駐 controller 持續 observe→派工→reap→harvest→sleep，
# 可做的 transition 立即派、worker 完成即 wake 重觀測（下游秒接力）。launchd StartInterval 只是
# 「監工」：controller 自退（跑滿 walltime 或連數輪無事）後 15min 內重拉；flock 確保只有一隻。
# launchd 環境極簡，需顯式補 PATH（uv 在 ~/.local/bin，claude/homebrew 在 /opt/homebrew/bin）。
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
# MinerU token 不入 plist/git（plist world-readable）；從 ~/.secrets 載入進程環境。
[ -f "$HOME/.secrets/mineru.env" ] && source "$HOME/.secrets/mineru.env"
# LLM 派工策略＝單一真相源 pipeline_tick.py 的 DispatchSpec 配置層（DEFAULT_DISPATCH +
# STAGE_DISPATCH），**不在此 export 雙寫**。四條 provider：
#   codex-pool = codex CLI 走 ccNexus 池子（-p nexus；maxn970228 輪換、獨立額度）
#   codex      = codex CLI 原生 OAuth（~/.codex/auth.json，ChatGPT 訂閱；與 pool 不同帳號＝不同額度）
#                codex CLI 須裝 npm @openai/codex（headless），勿用 brew cask GUI 包裝（dyld 卡死）
#   kimi       = claude CLI 導 Kimi 端點（~/.secrets/kimi.env）
#   claude     = Claude Max 訂閱原生
# 預設 chain codex-pool→codex→kimi→claude（撞額度沿鏈 failover），per-stage reasoning effort
# 與模型亦在 STAGE_DISPATCH 宣告。要臨時覆寫（運維拉桿，凌駕程式）才在此 export：
#   BOOK_PIPELINE_PROVIDER_CHAIN（逗號分隔）/ _CODEX_MODEL（codex 家族含 pool 共用）
#   / _CODEX_EFFORT / _CLAUDE_MODEL / _LLM_TIMEOUT。預設不設＝走程式宣告值。
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
