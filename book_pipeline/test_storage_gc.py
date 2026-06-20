"""storage_gc 單元測試（無 pytest 亦可跑）：uv run python -m book_pipeline.test_storage_gc

storage_gc 是會「真刪 / 真搬」磁碟產物的治理工具，安全全押在四組判斷上——本檔逐一鎖死：
  ① collect_prune 安全閘：只刪「已上站 ∧ 非在飛」書的可重生產物；未上站/在飛一律保留。
     （誤判 = 刪掉 daemon 正在 assemble/harvest 回讀的中間產物 → 整鏈崩。）
  ② can_archive_raw_pdf 邊界：主書未上站 / _sol 未 ingest / legacy 名無對應 → 一律保留源 PDF。
  ③ reassemble ranges fallback 鏈：_run.json→unified/chunks.json→_assembly.json→origin.pdf 反推，
     任一在即可重組（斷除對 gitignore 單檔的單點依賴）。
  ④ _deployed 強化：壞/null/缺 chapters 的 book.json 視為未上站（保守保留 raw，不誤刪）。
  另含 gc_book（刪除核心只動可重生、留 unified/parsed）與 _safe_move/sentinel（事務 + 掛載安全）。

hermetic：把 storage_gc 的全部 module 全域（DATA/BAKED/RAW_PDFS/QUARANTINE/ARCHIVE_ROOT/SIDECAR/
TICK_LOCK）＋ status.DATA ＋ booklists.SLUG_MAP 重導 temp、patch mb.in_flight，finally 全還原，
絕不污染真實 mineru_data / 真實狀態 / 真實冷藏。
"""
import contextlib
import json
import os
import shutil
import tempfile

import fitz  # pymupdf（pyproject 已宣告）

from book_pipeline import jsonio
from book_pipeline import storage_gc as g


# ── 共用 fixture ─────────────────────────────────────────────────────────────
@contextlib.contextmanager
def _env():
    """重導全域到 temp；yield (tmp, flying)——flying 是可變 set，測試往裡塞 slug 即模擬在飛。"""
    import book_pipeline.mineru_budget as mb
    from book_pipeline import booklists as bl
    from book_pipeline import status as st
    keys = ('DATA', 'BAKED', 'RAW_PDFS', 'QUARANTINE', 'ARCHIVE_ROOT', 'SIDECAR', 'TICK_LOCK')
    saved = {k: getattr(g, k) for k in keys}
    saved_inflight, saved_stdata, saved_slugmap = mb.in_flight, st.DATA, bl.SLUG_MAP
    tmp = tempfile.mkdtemp()
    flying: set[str] = set()
    try:
        g.DATA = st.DATA = os.path.join(tmp, 'mineru_data')
        g.BAKED = os.path.join(tmp, 'data')
        g.RAW_PDFS = os.path.join(tmp, 'raw_pdfs')
        g.QUARANTINE = os.path.join(tmp, 'q')
        g.ARCHIVE_ROOT = os.path.join(tmp, 'cold')
        g.SIDECAR = os.path.join(tmp, '.sc.json')
        g.TICK_LOCK = os.path.join(tmp, '.tick.lock')
        bl.SLUG_MAP = os.path.join(tmp, 'slug_map.json')
        os.makedirs(g.DATA)
        os.makedirs(g.BAKED)
        mb.in_flight = lambda: set(flying)
        yield tmp, flying
    finally:
        for k, v in saved.items():
            setattr(g, k, v)
        mb.in_flight, st.DATA, bl.SLUG_MAP = saved_inflight, saved_stdata, saved_slugmap
        shutil.rmtree(tmp, ignore_errors=True)


def _make_pdf(path: str, pages: int) -> None:
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(path)
    doc.close()


def _deploy(slug: str, *, valid: bool = True, raw: str | None = None) -> None:
    """烤一個 book.json 模擬上站。valid=False → 寫缺 chapters 的壞檔；raw='null'/'corrupt' 注入。"""
    d = os.path.join(g.BAKED, slug)
    os.makedirs(d, exist_ok=True)
    bj = os.path.join(d, 'book.json')
    if raw == 'null':
        open(bj, 'w').write('null')
    elif raw == 'corrupt':
        open(bj, 'w').write('{not json')
    elif valid:
        jsonio.atomic_write_json(bj, {'chapters': []})
    else:
        jsonio.atomic_write_json(bj, {'no_chapters_key': 1})


def _book_with_chunks(slug: str, chunk_pages: list[int], *, overlap: int = 1,
                      run=False, ck=False, asm=False, unified=False, parsed=False) -> list[list[int]]:
    """建一本含 raw/chunk_i/（真 origin.pdf + content_list）+ chunks/ 的書；可選 sidecar 來源。
    回該書真實 ranges（逆 plan_chunks 算）。"""
    book = os.path.join(g.DATA, slug)
    raw = os.path.join(book, 'raw')
    os.makedirs(raw)
    ranges, s = [], 1
    for i, pc in enumerate(chunk_pages):
        cdir = os.path.join(raw, f'chunk_{i}')
        os.makedirs(cdir)
        _make_pdf(os.path.join(cdir, f'uuid{i}_origin.pdf'), pc)
        json.dump([{'page_idx': p, 'type': 'text', 'text': f'p{p}'} for p in range(pc)],
                  open(os.path.join(cdir, f'uuid{i}_content_list.json'), 'w'))
        e = s + pc - 1
        ranges.append([s, e])
        s = e - overlap + 1
    os.makedirs(os.path.join(book, 'chunks'))
    open(os.path.join(book, 'chunks', 'p0001-0003.pdf'), 'wb').write(b'x' * 100)
    if run:
        json.dump({'ranges': ranges, 'overlap': overlap}, open(os.path.join(book, '_run.json'), 'w'))
    if ck or unified:
        os.makedirs(os.path.join(book, 'unified'), exist_ok=True)
    if ck:
        json.dump({'ranges': ranges, 'overlap': overlap},
                  open(os.path.join(book, 'unified', 'chunks.json'), 'w'))
    if asm:
        json.dump({'ranges': ranges, 'overlap': overlap},
                  open(os.path.join(book, '_assembly.json'), 'w'))
    if parsed:
        os.makedirs(os.path.join(book, 'parsed'), exist_ok=True)
        open(os.path.join(book, 'parsed', 'book.json'), 'w').write('{}')
    return ranges


# ── ① collect_prune 安全閘 ────────────────────────────────────────────────────
def test_collect_prune_safety_gate():
    with _env() as (_tmp, flying):
        _book_with_chunks('aaa', [3]); _deploy('aaa')                  # 上站、非在飛 → 應入選
        _book_with_chunks('bbb', [3])                                  # 未上站 → skip
        _book_with_chunks('ccc', [3]); _deploy('ccc'); flying.add('ccc')        # 上站但在飛 → skip
        _book_with_chunks('ddd', [3]); _deploy('ddd'); flying.add('ddd_sol')    # _sol 在飛 → skip
        targets, deployed, skipped = g.collect_prune()
        sk = dict(skipped)
        assert deployed == ['aaa'], f'只 aaa 該入選，得 {deployed}'
        assert 'bbb' in sk and '未上站' in sk['bbb']
        assert 'ccc' in sk and '在飛' in sk['ccc']
        assert 'ddd' in sk and '在飛' in sk['ddd']
        # 目標只含 aaa 的產物，絕不含 bbb/ccc/ddd 任何路徑
        joined = ' '.join(targets)
        assert '/aaa/' in joined
        for bad in ('/bbb/', '/ccc/', '/ddd/'):
            assert bad not in joined, f'{bad} 不該出現在刪除目標'
    print('✓ ① collect_prune 安全閘：未上站/在飛/sol 在飛全擋')


# ── ② can_archive_raw_pdf 邊界 ────────────────────────────────────────────────
def test_can_archive_raw_pdf_boundaries():
    with _env() as (tmp, _flying):
        from book_pipeline import booklists as bl
        json.dump({'map': {'Weird Name.pdf': 'realbook'}}, open(bl.SLUG_MAP, 'w'))
        # 主書經 slug_map 對應、已上站 → 可冷藏
        _deploy('realbook')
        ok, _ = g.can_archive_raw_pdf('Weird Name.pdf')
        assert ok, '已上站主書應可冷藏'
        # 主書未上站 → 保留
        json.dump({'map': {'X.pdf': 'undeployed'}}, open(bl.SLUG_MAP, 'w'))
        ok, why = g.can_archive_raw_pdf('X.pdf')
        assert not ok and '未上站' in why
        # legacy 名：無 map 對應 ∧ stem 不match SLUG_RE（含空白）→ 保留
        json.dump({'map': {}}, open(bl.SLUG_MAP, 'w'))
        ok, why = g.can_archive_raw_pdf('Has Spaces 2020.pdf')
        assert not ok and 'slug_map' in why
        # _sol：母書上站但 sol 未 ingest（無 unified/content_list）→ 保留
        json.dump({'map': {'sol.pdf': 'mybook_sol'}}, open(bl.SLUG_MAP, 'w'))
        _deploy('mybook')  # 母書上站
        ok, why = g.can_archive_raw_pdf('sol.pdf')
        assert not ok and 'sol 未 ingest' in why
        # _sol：母書上站 ∧ sol 已 ingest（unified/content_list.json 在）→ 可冷藏
        soldir = os.path.join(g.DATA, 'mybook_sol', 'unified')
        os.makedirs(soldir)
        open(os.path.join(soldir, 'content_list.json'), 'w').write('[]')
        ok, _ = g.can_archive_raw_pdf('sol.pdf')
        assert ok, 'sol 已 ingest 應可冷藏'
        # _sol：母書未上站 → 保留
        json.dump({'map': {'sol2.pdf': 'nodep_sol'}}, open(bl.SLUG_MAP, 'w'))
        ok, why = g.can_archive_raw_pdf('sol2.pdf')
        assert not ok and '未上站' in why
    print('✓ ② can_archive_raw_pdf：主書/sol/legacy 邊界全保守')


# ── ③ reassemble ranges fallback 鏈 ──────────────────────────────────────────
def test_reassemble_fallback_chain():
    expected = [(1, 3), (3, 4)]  # chunk_pages=[3,2], overlap=1：逆 plan_chunks
    cases = [
        (dict(run=True), '_run.json'),
        (dict(ck=True), 'unified/chunks.json'),
        (dict(asm=True), '_assembly.json(冷藏)'),
        (dict(), 'origin.pdf 頁數反推(overlap=1)'),  # 無任何 sidecar → 反推
    ]
    for kw, want_src in cases:
        with _env() as (_tmp, _flying):
            _book_with_chunks('zzz', [3, 2], **kw)
            book = os.path.join(g.DATA, 'zzz')
            ranges, overlap, src = g._resolve_ranges_overlap(book, os.path.join(book, 'raw'))
            assert [tuple(r) for r in ranges] == expected, f'{want_src}: ranges={ranges}'
            assert overlap == 1
            assert src == want_src, f'來源應 {want_src}，得 {src}'
    # e2e：純反推 → assemble 真產 unified（3 + (2-overlap1) = 4 blocks）
    with _env() as (_tmp, _flying):
        _book_with_chunks('zzz', [3, 2])
        book = os.path.join(g.DATA, 'zzz')
        out = os.path.join(book, 'unified')
        from book_pipeline.mineru_ingest import assemble
        from pathlib import Path
        ranges, overlap, _ = g._resolve_ranges_overlap(book, os.path.join(book, 'raw'))
        summary = assemble(Path(os.path.join(book, 'raw')), [tuple(r) for r in ranges],
                           overlap, Path(out))
        assert summary['total_blocks'] == 4, f"blocks={summary['total_blocks']}（期望 4）"
    print('✓ ③ reassemble fallback：4 來源優先序正確 + 純反推 e2e assemble 對')


# ── ④ _deployed 強化 ─────────────────────────────────────────────────────────
def test_deployed_hardened():
    with _env() as (_tmp, _flying):
        _deploy('good')
        assert g._deployed('good') is True
        _deploy('nullbook', raw='null')
        assert g._deployed('nullbook') is False, 'null book.json 應視為未上站'
        _deploy('corruptbook', raw='corrupt')
        assert g._deployed('corruptbook') is False, '壞 JSON 應視為未上站'
        _deploy('nochap', valid=False)
        assert g._deployed('nochap') is False, '缺 chapters 鍵應視為未上站'
        assert g._deployed('missing') is False, '無 book.json 應未上站'
    print('✓ ④ _deployed：null/壞/缺chapters/缺檔 全判未上站（保守保留 raw）')


# ── gc_book：刪除核心只動可重生、留 unified/parsed ────────────────────────────
def test_gc_book_preserves_hot():
    with _env() as (_tmp, _flying):
        _book_with_chunks('hhh', [3, 2], unified=True, parsed=True)
        _deploy('hhh')
        book = os.path.join(g.DATA, 'hhh')
        freed, warns = g.gc_book('hhh')
        assert freed > 0 and not warns
        assert not os.path.exists(os.path.join(book, 'chunks')), 'chunks/ 應刪'
        assert not g._raw_extracted_dirs('hhh'), 'raw/chunk_*/ 應刪'
        assert os.path.isdir(os.path.join(book, 'unified')), 'unified/ 須留'
        assert os.path.isdir(os.path.join(book, 'parsed')), 'parsed/ 須留'
    print('✓ gc_book：刪可重生（chunks/raw解壓檔）、留 unified/parsed')


# ── _safe_move + sentinel：事務性 + 掛載安全 ──────────────────────────────────
def test_safe_move_and_sentinel():
    with _env() as (tmp, _flying):
        a, b = os.path.join(tmp, 'a'), os.path.join(tmp, 'b')
        open(a, 'wb').write(b'Z' * 4096)
        assert g._safe_move(a, b) == 4096
        assert not os.path.exists(a) and os.path.exists(b)
        open(a, 'wb').write(b'Q' * 10)
        try:
            g._safe_move(a, b); raise AssertionError('應拒絕覆蓋')
        except FileExistsError:
            assert os.path.exists(a), '拒絕覆蓋時源須保留'
        # sentinel：初始化 → 不符拒絕 → 空內容拒絕
        os.makedirs(g.ARCHIVE_ROOT, exist_ok=True)
        g._ensure_sentinel(for_write=True)
        sid = g._read_sidecar()['archive_id']
        assert sid and open(g._sentinel_path()).read().strip() == sid
        open(g._sentinel_path(), 'w').write('OTHER\n')
        try:
            g._ensure_sentinel(for_write=True); raise AssertionError('應擋身分不符')
        except SystemExit:
            pass
        open(g._sentinel_path(), 'w').write('')
        try:
            g._ensure_sentinel(for_write=True); raise AssertionError('應擋空 sentinel')
        except SystemExit:
            pass
    print('✓ _safe_move 事務+拒覆蓋；sentinel 初始化/不符/空 全擋')


_TESTS = [
    test_collect_prune_safety_gate,
    test_can_archive_raw_pdf_boundaries,
    test_reassemble_fallback_chain,
    test_deployed_hardened,
    test_gc_book_preserves_hot,
    test_safe_move_and_sentinel,
]


def main() -> int:
    fails = 0
    for t in _TESTS:
        try:
            t()
        except Exception as e:
            fails += 1
            print(f'✗ {t.__name__}: {e}')
    print(f'\n{len(_TESTS) - fails}/{len(_TESTS)} 通過')
    return 1 if fails else 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
