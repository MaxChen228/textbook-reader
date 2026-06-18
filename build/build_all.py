#!/usr/bin/env python3
"""一鍵 build：抽封面 + convert images + bake JSON。

用法：
    uv run python -m build.build_all [slug ...]

封面為何先抽：cover.jpg 缺時 convert 不會生 cover.webp（圖庫無封面）。早期
extract_cover 沒進 daemon 管線、封面靠外部補，補在 deploy 之後就永遠不生 webp
（書牆缺封面）。故 deploy 唯一路徑（本檔）先冪等抽封面（缺才抽、需 raw PDF），
保證 convert 一定看得到 cover.jpg → 每次部署都自足產出 cover.webp。

**convert 必須先於 bake（資產先於 manifest）**：nginx 直讀工作目錄即時上線，bake 一寫
books.json（`has_cover=True`）即對外可見，但 reader 隨即去抓 `img/<slug>/cover.webp`。
若 convert 還沒生出該 webp（上千圖的書 convert 要數分鐘），這空窗內載入書牆 → cover 404
→ 瀏覽器**快取破圖**（即使稍後 webp 生成也不自動恢復，需硬重整）。故順序固定為
convert（產資產）→ bake（發佈引用資產的資料），杜絕「manifest 引用尚不存在的資產」空窗。
convert 與 bake 互相獨立（convert 讀 mineru_data→寫 img/；bake 讀 corpus→寫 data/），交換安全。
"""
import sys

from build import bake_json, convert_images, gen_macros
from book_pipeline import extract_cover as ec


def _ensure_covers(slugs: list[str]) -> None:
    """對每個 slug 冪等補 cover.jpg（已存在則跳過）。找不到 raw PDF 只警告不中斷——
    convert 仍會用既有 cover.jpg；真缺封面者 surface 待補 PDF。"""
    targets = slugs or ec._audited_slugs()
    for slug in targets:
        if (ec.DATA_DIR / slug / 'cover.jpg').is_file():
            continue
        pdf = ec.find_pdf_for_slug(slug)
        if pdf is None:
            print(f'  ⚠ {slug}: 無 cover.jpg 且 raw_pdfs/ 找不到 PDF → 書牆將缺封面')
            continue
        ec.extract_one(slug, pdf)


if __name__ == '__main__':
    args = sys.argv[1:]
    print('=== gen math macros → reader ===')
    print('  ', 'updated' if gen_macros.run() else 'unchanged', 'assets/qbank-shared.js')
    print('=== ensure covers ===')
    _ensure_covers(args)
    print('=== convert images → WebP ===')   # 先產資產（cover.webp + 圖庫 webp）…
    convert_images.main(args)
    print('=== bake JSON ===')               # …再發佈引用它們的 manifest（杜絕破圖空窗，見模組 docstring）
    bake_json.main(args)
