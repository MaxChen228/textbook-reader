# textbook-reader

自包含的教科書 pipeline + 靜態 reader 站。整條鏈 crawl→ingest(MinerU)→parse→audit→bake→serve 全住在本 repo;常駐主機 standby 24hr 自動跑 daemon,經 Cloudflare tunnel 自託管於 **books.wordnexus.lol**(不再用 GitHub Pages)。

## build（烤靜態站）
```bash
uv run python -m build.build_all [slug ...]   # 不帶 slug = 全部書
```
- `build/bake_json.py`：本地 `textbooks.corpus` 即時轉換 → `data/` 靜態 JSON（含 .jpg→.webp 改寫）
- `build/convert_images.py`：`book_pipeline/mineru_data/<slug>/unified/images` + cover → `img/<slug>/*.webp`（cwebp q80，冪等增量）

`data/`、`img/` 是本地產物（不入 git），由 build 從 `book_pipeline/mineru_data/` 重生。

## pipeline
```bash
uv run python -m book_pipeline.status                    # 全書 stage 儀表板
uv run python -m book_pipeline.pipeline_tick --dry-run   # daemon 單 tick 計畫（不執行）
```

## 本機預覽
`uv run python -m http.server 8001`
