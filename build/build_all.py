#!/usr/bin/env python3
"""一鍵 build：抽封面 + bake JSON + convert images。

用法：
    uv run python -m build.build_all [slug ...]

封面為何先抽：cover.jpg 缺時 convert 不會生 cover.webp（圖庫無封面）。早期
extract_cover 沒進 daemon 管線、封面靠外部補，補在 deploy 之後就永遠不生 webp
（書牆缺封面）。故 deploy 唯一路徑（本檔）先冪等抽封面（缺才抽、需 raw PDF），
保證 convert 一定看得到 cover.jpg → 每次部署都自足產出 cover.webp。
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
    print('=== bake JSON ===')
    bake_json.main(args)
    print('=== convert images → WebP ===')
    convert_images.main(args)
