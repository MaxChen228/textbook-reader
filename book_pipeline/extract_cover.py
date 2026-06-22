"""抽 raw_pdfs/<file>.pdf 第一頁 → book_pipeline/mineru_data/<slug>/cover.jpg。

CLI:
  python -m book_pipeline.extract_cover                # 掃所有 audited book 補缺
  python -m book_pipeline.extract_cover <slug>         # 自動探測 raw PDF（推薦）
  python -m book_pipeline.extract_cover <slug> <pdf>   # 明確指定 PDF

預設輸出 ~720px 寬 JPG（quality=85），~50–120KB。入 git。
旗標：--force 覆蓋既有 cover.jpg。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import fitz  # pymupdf

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / 'raw_pdfs'
DATA_DIR = ROOT / 'book_pipeline' / 'mineru_data'

# pymupdf/MuPDF 對某些病態 JPEG2000(JPX) 圖會「無限慢解」≈死循環（實測曾凍住 daemon 主迴圈
# 28 分鐘 96% CPU）。故 daemon 鉤子 ensure_covers 一律把 render 隔離進子進程 + 硬 timeout +
# killpg：逾時殺整組、寫 cover.skip marker，下個 cycle 不再重試卡死。CLI 直跑 extract_one 不受影響。
COVER_TIMEOUT_S = int(os.environ.get('BOOK_PIPELINE_COVER_TIMEOUT', '45'))

# 只放「無法從 slug 直推檔名」的 alias overrides（z-library 雜訊檔名等）。
# 新書若 raw_pdfs/ 檔名是 `{Slug}.pdf` / `{slug}.pdf` 直接被 find_pdf_for_slug 探測到，免補。
SLUG_TO_PDF = {
    'alexander_circuits': 'Fundamentals Of Electric Circuits (Charles K. Alexander  Matthew N. O. Sadiku) (z-library.sk, 1lib.sk, z-lib.sk).pdf',
    'cheng_em': 'Field and wave electromagnetics (Cheng, David K. (David Keun), 1917-) (z-library.sk, 1lib.sk, z-lib.sk).pdf',
    'griffiths_qm3': 'griffiths_qm_3ed.pdf',
    'kittel_thermal': 'Kittel-ThermalPhysics.80.pdf',
    'sedra_microe': 'Microelectronic circuits (Sedra, Adel S., author, Smith etc.) (z-library.sk, 1lib.sk, z-lib.sk).pdf',
    'strang_linalg': 'Linear Algebra and Its Applications, 4th Edition (Gilbert Strang) (z-library.sk, 1lib.sk, z-lib.sk).pdf',
}

TARGET_WIDTH = 720
JPG_QUALITY = 85

# 解答本後綴（探測時跳過）
_SOL_SUFFIXES = ('_solutions', '_sol', '_solution', '-solutions', '-sol', '_answers', '-answers')


def _is_solution_pdf(stem: str) -> bool:
    s = stem.lower()
    return any(s.endswith(suf) for suf in _SOL_SUFFIXES) or 'solution' in s or 'answer' in s


def find_pdf_for_slug(slug: str) -> Path | None:
    """依序嘗試：alias map → 規範化後 stem 比對 → slug 各段全出現於 stem。"""
    if slug in SLUG_TO_PDF:
        p = RAW / SLUG_TO_PDF[slug]
        return p if p.is_file() else None

    norm_slug = slug.lower().replace('_', '').replace('-', '').replace(' ', '')
    fuzzy: list[Path] = []
    for p in sorted(RAW.glob('*.pdf')):
        stem = p.stem
        if _is_solution_pdf(stem):
            continue
        s_lower = stem.lower()
        norm_stem = s_lower.replace('_', '').replace('-', '').replace(' ', '')
        if norm_stem == norm_slug:
            return p
        parts = [x for x in slug.lower().split('_') if x]
        if len(parts) >= 2 and all(part in s_lower for part in parts):
            fuzzy.append(p)
    if len(fuzzy) == 1:
        return fuzzy[0]
    return None


def extract_one(slug: str, pdf_path: Path, *, force: bool = False) -> Path | None:
    if not pdf_path.is_file():
        print(f'  ✗ {slug}: PDF 不存在 ({pdf_path.name})')
        return None
    out = DATA_DIR / slug / 'cover.jpg'
    if out.is_file() and not force:
        print(f'  · {slug}: 已存在 cover.jpg（--force 覆蓋）')
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf_path) as doc:
        page = doc[0]
        rect = page.rect
        zoom = TARGET_WIDTH / rect.width
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        pix.pil_save(out, format='JPEG', quality=JPG_QUALITY, optimize=True)
    print(f'  ✓ {slug}: {out.relative_to(ROOT)} ({out.stat().st_size // 1024} KB)')
    return out


def _mark_cover_skip(sdir: Path, reason: str) -> None:
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / 'cover.skip').write_text(reason, encoding='utf-8')
    except Exception:
        pass


def _render_cover_isolated(slug: str) -> int:
    """子進程跑 extract_cover CLI（render 在子進程內），硬 timeout 到就 killpg 整組。
    回 0=成功 / 1=失敗(rc!=0) / 2=逾時(病態圖)。start_new_session 使子進程自成 group，
    pymupdf 卡在 C 層 SIGTERM 未必停 → 直接 SIGKILL group。"""
    cmd = ['uv', 'run', 'python', '-m', 'book_pipeline.extract_cover', slug]
    p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        return 0 if p.wait(timeout=COVER_TIMEOUT_S) == 0 else 1
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return 2


def ensure_covers(slugs) -> int:
    """pipeline 自動鉤子（daemon 主迴圈呼叫）：給一批 slug，凡缺 cover.jpg 且能在 raw_pdfs/
    探到對應 PDF 者，抽第一頁補上。冪等（已有則跳過），回實際新抽的張數。

    **render 一律隔離進子進程 + 硬 timeout**：pymupdf 對病態 JPX 圖會無限卡死，絕不可 inline
    凍住 daemon 主迴圈。逾時/失敗 → 寫 cover.skip marker，下個 cycle 不再重試（防反覆卡死）。
    封面源頭 = raw PDF 第一頁，triage 完成即可生；解答本（_sol）跳過；任何單本失敗不連坐。"""
    made = 0
    for slug in slugs:
        if _is_solution_pdf(slug):
            continue
        sdir = DATA_DIR / slug
        if (sdir / 'cover.jpg').is_file() or (sdir / 'cover.skip').is_file():
            continue  # 已有封面 或 先前 render 逾時/失敗已標記 → 跳過
        if find_pdf_for_slug(slug) is None:
            continue  # 還沒下載到 PDF（crawl 中）→ 下個 cycle 再試
        rc = _render_cover_isolated(slug)
        if rc == 0:
            made += 1
        elif rc == 2:
            print(f'  ⏱ {slug}: 抽封面 render 逾時 >{COVER_TIMEOUT_S}s（病態圖？）→ 標記 cover.skip 略過')
            _mark_cover_skip(sdir, f'render timeout >{COVER_TIMEOUT_S}s')
        else:
            print(f'  ✗ {slug}: 抽封面失敗（rc=1）→ 標記 cover.skip 略過')
            _mark_cover_skip(sdir, 'extract rc=1')
    return made


def _audited_slugs() -> list[str]:
    """掃 mineru_data/ 找已 ingest（unified/content_list.json 存在）且非解答本的 slug。"""
    out: list[str] = []
    if not DATA_DIR.is_dir():
        return out
    for d in sorted(DATA_DIR.iterdir()):
        if not d.is_dir() or _is_solution_pdf(d.name):
            continue
        if (d / 'unified' / 'content_list.json').is_file():
            out.append(d.name)
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if a != '--force']
    force = '--force' in sys.argv

    if len(args) == 2:
        ok = extract_one(args[0], Path(args[1]), force=force)
        sys.exit(0 if ok else 1)

    if len(args) == 1:
        slug = args[0]
        pdf = find_pdf_for_slug(slug)
        if pdf is None:
            print(f'  ✗ {slug}: raw_pdfs/ 找不到對應 PDF（試過 alias map / stem 比對 / 模糊匹配）')
            print(f'    候選：{sorted(p.name for p in RAW.glob("*.pdf"))}')
            sys.exit(1)
        ok = extract_one(slug, pdf, force=force)
        sys.exit(0 if ok else 1)

    if len(args) != 0:
        sys.exit('usage: python -m book_pipeline.extract_cover [<slug> [<pdf>]] [--force]')

    # 零參：掃所有 audited slug，缺 cover.jpg 才跑（force 則一律跑）
    slugs = _audited_slugs()
    missing = [s for s in slugs if force or not (DATA_DIR / s / 'cover.jpg').is_file()]
    if not missing:
        print(f'  · 全部 {len(slugs)} 本 audited book 已有 cover.jpg')
        return
    print(f'  → 待補封面 {len(missing)}/{len(slugs)} 本')
    fail = 0
    for slug in missing:
        pdf = find_pdf_for_slug(slug)
        if pdf is None:
            print(f'  ✗ {slug}: raw_pdfs/ 找不到對應 PDF（補 SLUG_TO_PDF alias 或改檔名）')
            fail += 1
            continue
        if extract_one(slug, pdf, force=force) is None:
            fail += 1
    sys.exit(0 if fail == 0 else 1)


if __name__ == '__main__':
    main()
