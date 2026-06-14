# textbook-reader

教科書 reader 靜態站。資料由 qbank 預烤產出（`build/`），公開部署到 GitHub Pages。

## 重新 build
```bash
QBANK_ROOT=~/project/qbank python3 build/build_all.py [slug ...]
```
- `bake_json.py`：把 `textbooks.corpus` 的即時轉換烤成 `data/` 靜態 JSON（含 .jpg→.webp 改寫）
- `convert_images.py`：`unified/images` + cover → `img/<slug>/*.webp`（cwebp q80）

本機預覽：`python3 -m http.server 8001`
