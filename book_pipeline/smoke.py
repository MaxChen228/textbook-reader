"""對 parser 跑出的 parsed/ 結果做啟發式語義檢測。

audit-book skill 的 §5 在 schema validator 之後 invoke 本 module，
用「parser smoke」識別 audit yaml 的語義問題（不是 schema bug 是判斷錯）。

啟發式（critical = ❌ 需回 audit-book §3 修；warning = ⚠ 留紀錄）：
  H1 inline 漏設：章 body < 20 + problems > 30
                  → pbi=null + inline_problems=true
  H2 namespace 漏設：章內 problem.num 重複
                    → problem_num_namespace_by_section=true（必須 inline mode）
  H3 problem body 全空：該章每個 problem 都 body=[]
                       → 通常是 OCR list_items 全漏或 parser bug
  H4 appendix 沒切：appendix body > 1500
                   → 補 index_start_page / bibliography_start_page (⚠)
  H5 章 body 鄰近差太大：相鄰章 body ratio > 5x 且都 > 50
                       → anchor 飄移可疑 (⚠)
  H6 catalog 結構破洞：圖檔缺失、正文 Figure/Table ref 無 catalog entry
                       → 不可完成，回 audit/agent 修資料
  H7 catalog 語義破洞：figure/table 使用 fallback id 或 caption 空白
                       → 不可完成，回 audit/agent 補可索引圖表目錄

退出碼：
  0 = 全綠或僅 warning
  1 = 有 critical anomaly

產出：parsed/_smoke.md
"""
from __future__ import annotations

import json
import argparse
import re
import sys
from collections import Counter
from pathlib import Path

from book_pipeline.catalog_audit import audit_catalog

DATA_DIR = Path('book_pipeline/mineru_data')
SLUG_RE = re.compile(r'^[a-z0-9_]{1,64}$')

H1_BODY_THRESHOLD = 20
H1_PROBLEMS_THRESHOLD = 30
H4_APPENDIX_BODY_THRESHOLD = 1500
H5_BODY_RATIO = 5.0
H5_BODY_MIN = 50


def smoke_findings(slug: str) -> tuple[list[str], list[str]]:
    """純結構啟發式（H1-H5），無 I/O（不寫檔、不 print）→ (critical, warnings)。

    catalog gate（H6/H7）刻意**不在**本函式：那是 audit_catalog 的職責、且會寫 _catalog_audit.md。
    proposals verify 的 engine 結構路徑用本純核取 len(critical)，caption/catalog 路徑另走
    _verify_catalog（audit_catalog write_report=False）→ verify 全程零寫檔污染。
    book.json 不存在 → 拋 FileNotFoundError（呼叫端決定處置；smoke() 轉 sys.exit）。
    """
    if not isinstance(slug, str) or SLUG_RE.fullmatch(slug) is None:
        raise ValueError(f'invalid slug: {slug}')
    parsed_dir = DATA_DIR / slug / 'parsed'
    book_path = parsed_dir / 'book.json'
    if not book_path.exists():
        raise FileNotFoundError(f'{book_path} 不存在，先跑 parser')

    book = json.loads(book_path.read_text())
    critical: list[str] = []
    warnings: list[str] = []

    # 章節掃描
    ch_body_counts: list[tuple[int, int]] = []  # (num, body_count)
    for ch_info in book.get('chapters', []):
        ch_path = parsed_dir / ch_info['file']
        ch = json.loads(ch_path.read_text())
        body = ch.get('body') or []
        problems = ch.get('problems') or []
        num = ch.get('num')
        title = (ch.get('title') or '')[:30]
        ch_body_counts.append((num, len(body)))

        # H1 inline 漏設
        if len(body) < H1_BODY_THRESHOLD and len(problems) > H1_PROBLEMS_THRESHOLD:
            critical.append(
                f'[H1] ch{num:02d} {title!r}: body={len(body)} problems={len(problems)} '
                f'— inline 模式可能漏設：請把該章 problems_block_idx 改為 null，'
                f'並設 top-level inline_problems=true'
            )

        # H2 namespace 漏設
        nums = [p.get('num') for p in problems]
        cnt = Counter(nums)
        dups = sorted([n for n, c in cnt.items() if c > 1])
        if dups:
            critical.append(
                f'[H2] ch{num:02d} {title!r}: problem num 重複 {dups[:5]} '
                f'— 每節 Problem Set 題號可能重置：設 '
                f'top-level problem_num_namespace_by_section=true'
            )

        # H3 全部 problems body=[]
        if problems and all(not p.get('body') for p in problems):
            critical.append(
                f'[H3] ch{num:02d} {title!r}: 全部 {len(problems)} 題 body=[] '
                f'— 檢查 parser type=list 是否讀 list_items，或該章 OCR 已死'
            )

    # H5 鄰章 body ratio
    for i in range(1, len(ch_body_counts)):
        prev_num, prev_b = ch_body_counts[i - 1]
        cur_num, cur_b = ch_body_counts[i]
        if prev_b < H5_BODY_MIN or cur_b < H5_BODY_MIN:
            continue
        ratio = max(prev_b, cur_b) / min(prev_b, cur_b)
        if ratio > H5_BODY_RATIO:
            warnings.append(
                f'[H5] ch{prev_num:02d}/ch{cur_num:02d} body {prev_b}/{cur_b} '
                f'(ratio {ratio:.1f}x) — anchor 飄移可疑'
            )

    # 附錄掃描
    for ap_info in book.get('appendices', []) or []:
        ap_path = parsed_dir / ap_info['file']
        ap = json.loads(ap_path.read_text())
        body_count = len(ap.get('body') or [])
        if body_count > H4_APPENDIX_BODY_THRESHOLD:
            warnings.append(
                f"[H4] app{ap_info['id']} {ap.get('title','')[:30]!r}: "
                f'body={body_count} — 可能吞 Index/Bibliography，'
                f'補 index_start_page 或 bibliography_start_page'
            )

    return critical, warnings


def smoke(slug: str) -> int:
    """smoke_findings 純核（H1-H5）+ catalog gate（H6/H7，寫 _catalog_audit.md）+ 寫 _smoke.md + print。"""
    if not isinstance(slug, str) or SLUG_RE.fullmatch(slug) is None:
        print(f'❌ {slug}: invalid slug')
        return 1
    parsed_dir = DATA_DIR / slug / 'parsed'
    try:
        critical, warnings = smoke_findings(slug)
    except FileNotFoundError as e:
        sys.exit(f'❌ {e}')

    # Catalog semantic gate. This also writes parsed/_catalog_audit.md as the
    # concrete repair queue for agent/LLM/manual follow-up.
    try:
        catalog_summary = audit_catalog(slug, write_report=True)
        unresolved_refs = (
            catalog_summary['missing_figure_refs'] + catalog_summary['missing_table_refs']
        )
        if unresolved_refs:
            critical.append(
                f"[H6] catalog unresolved refs={unresolved_refs} "
                f"(Figure={catalog_summary['missing_figure_refs']} "
                f"Table={catalog_summary['missing_table_refs']}) "
                f"— 正文引用沒有對應可索引 catalog entry；看 parsed/_catalog_audit.md work queue"
            )
        if catalog_summary['fallback_ids'] or catalog_summary['empty_captions']:
            critical.append(
                f"[H7] catalog fallback_ids={catalog_summary['fallback_ids']} "
                f"empty_captions={catalog_summary['empty_captions']} "
                f"— 圖表目錄仍缺可索引圖號/標題；看 parsed/_catalog_audit.md work queue"
            )
    except FileNotFoundError as e:
        critical.append(
            f'[H6] catalog audit 無法執行：{e} — 先跑 build_catalogs/parser 產出 catalogs.json'
        )

    # 寫 _smoke.md
    lines = [f'# Parser smoke — {slug}', '']
    if critical:
        lines.append('## ❌ CRITICAL（須回 audit-book §3 修 yaml）')
        for c in critical:
            lines.append(f'- {c}')
        lines.append('')
    if warnings:
        lines.append('## ⚠ WARNING（紀錄、可選修）')
        for w in warnings:
            lines.append(f'- {w}')
        lines.append('')
    if not critical and not warnings:
        lines.append('## ✅ 全綠')
        lines.append('')
    (parsed_dir / '_smoke.md').write_text('\n'.join(lines))

    # Console summary
    print(f'[smoke] {slug}: critical={len(critical)} warning={len(warnings)}')
    for c in critical:
        print(f'  ❌ {c}')
    for w in warnings:
        print(f'  ⚠ {w}')

    return 1 if critical else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='對 parser 跑出的 parsed/ 結果做啟發式語義檢測')
    ap.add_argument('slug')
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    return smoke(args.slug)


if __name__ == '__main__':
    sys.exit(main())
