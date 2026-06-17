#!/bin/zsh
# textbook-reader 上線流程（一鍵）：測試 → (可選 commit) → push → 優雅 reload daemon 載新碼 → 驗證 SHA 跟上。
# daemon 跑「工作目錄」的碼，故上線 = 讓常駐 controller 重載新碼。reload 是零浪費（排空在飛 audit 才退）
# + 零空檔（退出即 detached kickstart 自動拉新碼），不必再戰戰兢兢硬殺。
#
# 用法（在 textbook-reader/，於 daemon 所在機 felix 跑）：
#   ./deploy.sh                 # 工作目錄須已乾淨（自己先 commit）→ push + reload + 驗證
#   ./deploy.sh "feat: 訊息"     # add -A + commit(此訊息) + push + reload + 驗證
#
# 註：與爬書購物清單無關（那是 ./crawl-refill.sh）。這支只管「把新碼上線到常駐 daemon」。
cd "$(dirname "$0")" || exit 1
MSG="$1"

# 1. 紅燈閘：全測試套件，任一紅燈即中止（絕不上線壞碼）
echo "▶ 跑測試套件…"
for f in book_pipeline/test_*.py; do
  m="book_pipeline.$(basename "${f%.py}")"
  if ! uv run python -m "$m" >/dev/null 2>&1; then
    echo "✗ $m 失敗 → 中止上線（跑 'uv run python -m $m' 看細節）"; exit 1
  fi
done
echo "✓ 測試全綠"

# 2. 可選 commit（給了訊息才動 git 寫入；無變更不報錯）
if [ -n "$MSG" ]; then
  git add -A
  git commit -m "$MSG" || true
fi

# 3. 乾淨樹要求：daemon 跑 working tree、但版本觀測報的是 HEAD 的 SHA。樹不乾淨 → 跑的是未提交碼、
#    SHA 卻顯示 HEAD → 驗證會「假通過」。故強制乾淨,讓 code=<sha> 誠實。
if [ -n "$(git status --porcelain)" ]; then
  echo "✗ 有未提交變更 → 請先 commit（或 ./deploy.sh \"訊息\" 自動 commit）。daemon 版本觀測需乾淨樹。"
  exit 1
fi

# 4. push（備援 + 跨機同步）。失敗不中止：本地 reload 仍會載新碼,網路恢復後再 push 即可。
echo "▶ push 到 origin…"
if git push >/dev/null 2>&1; then echo "✓ pushed"; else echo "⚠ push 失敗（reload 仍會載本地新碼；稍後再 push 備援/跨機）"; fi

HEAD="$(git rev-parse --short HEAD)"
echo "▶ 目標版本 HEAD=$HEAD"

# 5. 優雅 reload：排空在飛 worker 後退出 → detached kickstart 立即拉新碼（零浪費、零空檔）
echo "▶ reload daemon…"
uv run python -m book_pipeline.devctl reload

# 6. 驗證：輪詢 controller 的 running SHA 跟上 HEAD（單一 uv 進程內輪詢，免反覆啟動開銷）
echo "▶ 驗證新碼上線…"
uv run python - "$HEAD" <<'PY'
import sys, time
from book_pipeline import devctl
head = sys.argv[1]
for _ in range(60):  # 無在飛工作秒級;有 audit 在飛則排空後才換,故留足輪詢窗
    run = devctl.code_status().get('running') or ''
    if run == head:
        print(f'✅ 上線完成：daemon 正跑 {head}'); sys.exit(0)
    time.sleep(2)
print(f'⚠ 120s 內 SHA 未跟上 HEAD={head}（reload 可能在排空在飛 audit；稍後 devctl status 再確認）')
sys.exit(2)
PY
