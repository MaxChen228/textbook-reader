#!/usr/bin/env python3
"""Apply tracked math-fix overrides to ignored parsed JSON (Phase 2 sweep handoff).

數學 sweep 的 reviewable 交付格式，比照 catalog_overrides：parsed/*.json 是
generated/ignored，每筆修復寫成 git 追蹤的 override，parser 重建後可重播。

與 catalog_overrides 的關鍵差別 = **guard 失配即 skip（不 raise）**，對齊 corpus overlay
哲學：源頭一漂移（重 OCR/重 audit 改了式子），舊修復自動停用、其餘照常套，而非炸掉整批。
冪等：已套用者（new 在、old 不在）直接 noop。

actions：
  fix_eq_tex      — 換 eq block 的 tex；expect=舊 tex 精確 guard（必填）。
  fix_inline_math — 換 md/caption/footnote/title 內一段數學子字串；anchor=該欄內容指紋 guard。

selector 複用 catalog 文法（apply_catalog_overrides）：`body[N]` / `problem:NUM:field[N]`；
另加 `title`（chunk 頂層 title 欄，catalog 文法沒有）。locator→selector 由
math_validate.locator_to_target 產（report findings 已附 targets，agent 直接抄）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from book_pipeline.apply_catalog_overrides import (
    DATA_DIR,
    _backup_once,
    _chunk_path,
    _load_json,
    _select_block,
    _write_json,
)
from book_pipeline.build_catalogs import build_catalogs
from book_pipeline.translate import overlay_anchor

ROOT = Path(__file__).resolve().parent.parent
OVERRIDE_DIR = ROOT / 'book_pipeline' / 'math_overrides'

_INLINE_FIELDS = {'md', 'caption', 'footnote', 'title'}
# selector → block 解析可能丟的例外（漂移），一律吞成 skip-drift。
_DRIFT_ERRORS = (LookupError, ValueError, IndexError, KeyError, TypeError)


def _resolve_field(data: dict[str, Any], selector: str, field: str) -> tuple[dict[str, Any], str]:
    """(holder, key)：title → (chunk, 'title')；其餘 → (block, field)。"""
    if selector == 'title':
        return data, 'title'
    return _select_block(data, selector), field


def _apply_fix_eq_tex(slug: str, ov: dict[str, Any], backup_dir: Path, backed_up: set[Path]) -> str:
    path = _chunk_path(slug, ov['chunk'])
    data = _load_json(path)
    try:
        block = _select_block(data, ov['selector'])
    except _DRIFT_ERRORS:
        return 'skip-drift'
    if not isinstance(block, dict) or block.get('t') != 'eq':
        return 'skip-drift'
    expect = ov.get('expect')
    if expect is None:
        raise ValueError(f"{ov.get('id', '<ov>')}: fix_eq_tex 需 expect（舊 tex guard）")
    cur = block.get('tex')
    if not isinstance(cur, str):
        return 'skip-drift'
    new = ov['new']
    if cur.strip() == new.strip():
        return 'noop'  # 已套用（重 parse 後若源頭已等於 new）
    if cur.strip() != expect.strip():
        return 'skip-drift'  # 源頭漂移 → 停用本筆
    block['tex'] = new
    _backup_once(path, backup_dir, backed_up)
    _write_json(path, data)
    return 'applied'


def _apply_fix_inline_math(slug: str, ov: dict[str, Any], backup_dir: Path, backed_up: set[Path]) -> str:
    field = ov.get('field', 'md')
    if field not in _INLINE_FIELDS:
        raise ValueError(f"{ov.get('id', '<ov>')}: unsupported field {field!r}")
    path = _chunk_path(slug, ov['chunk'])
    data = _load_json(path)
    try:
        holder, key = _resolve_field(data, ov['selector'], field)
    except _DRIFT_ERRORS:
        return 'skip-drift'
    value = holder.get(key)
    if not isinstance(value, str):
        return 'skip-drift'
    anchor = ov.get('anchor')
    if anchor and overlay_anchor({key: value}) != anchor:
        return 'skip-drift'  # 該欄內容指紋漂移 → 停用本筆
    old, new = ov['old'], ov['new']
    if old == new:
        return 'noop'  # 無實質改變（對齊 fix_eq_tex 的 cur==new 前置判斷，免無謂 write/build_catalogs）
    if old not in value:
        return 'noop' if new in value else 'skip-drift'  # 已套用 / 子字串漂移
    # all=true → 同欄全部同式一次換（occ>1：同一壞式在一欄出現多次，預設只換首處清不掉其餘）。
    holder[key] = value.replace(old, new) if ov.get('all') else value.replace(old, new, 1)
    _backup_once(path, backup_dir, backed_up)
    _write_json(path, data)
    return 'applied'


_ACTIONS = {
    'fix_eq_tex': _apply_fix_eq_tex,
    'fix_inline_math': _apply_fix_inline_math,
}


def apply_overrides(slug: str) -> dict[str, int]:
    """套用 math_overrides/<slug>.json。回各 action×結果（applied/noop/skip-drift）計數。"""
    path = OVERRIDE_DIR / f'{slug}.json'
    if not path.is_file():
        raise FileNotFoundError(path)
    spec = _load_json(path)
    backup_dir = (DATA_DIR / slug / 'parsed' / '_override_backups'
                  / ('math-' + datetime.now().strftime('%Y%m%d-%H%M%S')))
    backed_up: set[Path] = set()
    stats: Counter[str] = Counter()
    for ov in spec.get('overrides') or []:
        action = ov.get('action')
        fn = _ACTIONS.get(action)
        if fn is None:
            raise ValueError(f'unsupported action: {action!r}')
        stats[f'{action}:{fn(slug, ov, backup_dir, backed_up)}'] += 1
    if backed_up:  # tex/md 改動極少動 catalog，但比照 catalog override 保持一致
        build_catalogs(slug)
    return dict(stats)


def _ov_id(slug: str, chunk: str, selector: str, tex: str) -> str:
    """穩定唯一 id：<slug>-<chunk>-<sanitized selector>-<tex hash6>。
    tex hash 確保同一 selector 上多條相異壞式不撞 id。"""
    sel = re.sub(r"[^a-z0-9]+", "-", selector.lower()).strip("-")
    h = hashlib.sha1((tex or "").encode("utf-8")).hexdigest()[:6]
    return re.sub(r"-+", "-", f"{slug}-{chunk}-{sel}-{h}").strip("-")


def _exact_inline_region(field_value: str, field: str, tex: str) -> tuple[str, str] | None:
    """在欄位完整字串裡找 inner.strip()==tex 的數學區，回 (old 精確段, _math_regions 的 inner)。
    複用 math_audit._math_regions（與 reader / collect_formulas 同一套定界文法）。_math_regions 的
    inner 已 strip，old=field_value[start:end] 仍含原樣定界符與外圍空白；inner 在 old 內唯一出現，
    故 old.replace(inner, new, 1) 既保留定界又必命中。找不到回 None（交呼叫端 fallback）。"""
    from book_pipeline.math_audit import _math_regions
    for start, end, inner in _math_regions(field_value, field):
        if inner.strip() == tex:
            return field_value[start:end], inner
    return None


def finding_to_override(slug: str, finding: dict[str, Any], new: str, *,
                        target: dict[str, str] | None = None,
                        field_value: str | None = None) -> dict[str, Any]:
    r"""一條 math_validate finding + LLM 給的 new(正確 tex) → 一條 override dict。
    把「手填 id/action/chunk/selector/field/expect/anchor/old」全自動化，**只剩 new 要 LLM 判**
    （消滅開環的機械填欄斷點）。

    fix_eq_tex（target.field=='tex'）：完全自足——expect=finding.tex（apply guard 用 strip 比對）、
      new=原始 new tex（eq block 存裸 tex）。
    fix_inline_math（md/caption/footnote/title）：old 是欄內的數學「子字串」、anchor 是整欄指紋。
      給 field_value（該欄完整字串）→ 用 _math_regions 精確取 old 與原樣定界、new 同定界包覆、附 anchor。
      沒給 → best-effort 僅用 $tex$/$$tex$$ 重建（不覆蓋 \(..\)/\[..\] 定界）且不附 anchor（apply 端
      old 對不上只會 skip-drift，絕不誤改）。
    注意：occ>1 同一欄重複同式時，apply 端預設只換首處——若 finding 確為「單欄多次」需 agent 自行
      在產出的 dict 補 all=True（occ 也可能跨欄，每 target 各一條才對，故不自動設）。"""
    tgt = target or next(iter(finding.get("targets") or []), None)
    if not tgt:
        raise ValueError(f"{slug}: finding 無 targets，無法定位 override（tex={finding.get('tex')!r}）")
    chunk, selector = tgt["chunk"], tgt["selector"]
    field = tgt.get("field", "md")
    tex = finding.get("tex") or ""
    if not tex.strip():
        # 空 tex 無法安全定位：inline fallback 會產 old="$$"（命中任何含 $$ 的欄 → 誤改）。
        raise ValueError(f"{slug}: finding.tex 空，無法產 override（會誤改）")
    ov: dict[str, Any] = {"id": _ov_id(slug, chunk, selector, tex)}
    if field == "tex":
        ov.update(action="fix_eq_tex", chunk=chunk, selector=selector, expect=tex, new=new)
        return ov
    ov.update(action="fix_inline_math", chunk=chunk, selector=selector, field=field)
    region = _exact_inline_region(field_value, field, tex) if field_value is not None else None
    if region is not None:
        old, inner = region
        ov["old"] = old
        ov["new"] = old.replace(inner, new, 1)          # 保留原樣定界與空白
        ov["anchor"] = overlay_anchor({field: field_value})
    else:
        wrap = "$$" if finding.get("display") else "$"   # fallback：無 field_value
        ov["old"] = f"{wrap}{tex}{wrap}"
        ov["new"] = f"{wrap}{new}{wrap}"
    return ov


def _live_field_value(slug: str, tgt: dict[str, str]) -> str | None:
    """讀某 target 的 live 欄位完整字串（inline 精確定位 old + anchor 用）。
    tex target、或 selector/欄位漂移 → None（呼叫端 fallback）。"""
    if tgt.get("field") == "tex":
        return None
    try:
        data = _load_json(_chunk_path(slug, tgt["chunk"]))
        holder, key = _resolve_field(data, tgt["selector"], tgt.get("field", "md"))
        v = holder.get(key)
        return v if isinstance(v, str) else None
    except _DRIFT_ERRORS:
        return None


def finding_to_overrides(slug: str, finding: dict[str, Any], new: str) -> list[dict[str, Any]]:
    """一條 finding 的**所有** targets → 多條 override（每 target 一條，共用同一 new）。

    finding_to_override 是單 target；本函式對 finding 的每個出現位置各產一條（inline target
    自動讀 live 欄位精確定位）→ math_sweep fix 一鍵清掉該壞式的所有 occ。"""
    targets = finding.get("targets") or []
    if not targets:
        raise ValueError(f"{slug}: finding 無 targets，無法定位 override（tex={finding.get('tex')!r}）")
    return [
        finding_to_override(slug, finding, new, target=tgt, field_value=_live_field_value(slug, tgt))
        for tgt in targets
    ]


def merge_overrides(slug: str, new_ovs: list[dict[str, Any]]) -> dict[str, int]:
    """併入 math_overrides/<slug>.json，按 id 去重（同 id 覆蓋為新）。回 {added, replaced}。"""
    path = OVERRIDE_DIR / f'{slug}.json'
    spec = _load_json(path) if path.is_file() else {"overrides": []}
    by_id: dict[str, dict] = {o["id"]: o for o in spec.get("overrides") or []}
    added = replaced = 0
    for ov in new_ovs:
        replaced += ov["id"] in by_id
        added += ov["id"] not in by_id
        by_id[ov["id"]] = ov
    spec["overrides"] = list(by_id.values())
    OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(path, spec)
    return {"added": added, "replaced": replaced}


def _make_override_main(argv: list[str]) -> int:
    """make-override CLI：讀 live report 的某條 finding，產 override JSON 條目（印出，agent 自行併入
    math_overrides/<slug>.json）。需 live 資料（parsed/_math_report.json + parsed chunk）。"""
    from book_pipeline.math_validate import read_report
    ap = argparse.ArgumentParser(prog='python -m book_pipeline.apply_math_overrides make-override')
    ap.add_argument('--slug', required=True)
    ap.add_argument('--index', type=int, required=True, help='_math_report.json findings 的索引')
    ap.add_argument('--new', required=True, help='LLM 判定的正確 tex（inline 給裸 inner、eq 給裸 tex）')
    args = ap.parse_args(argv)
    rep = read_report(args.slug)
    if not rep:
        ap.error(f"無 report：{args.slug}")
    findings = rep.get("findings") or []
    if not 0 <= args.index < len(findings):
        ap.error(f"index 越界（0..{len(findings) - 1}）")
    finding = findings[args.index]
    tgt = next(iter(finding.get("targets") or []), None)
    field_value = _live_field_value(args.slug, tgt) if tgt else None
    ov = finding_to_override(args.slug, finding, args.new, target=tgt, field_value=field_value)
    print(json.dumps(ov, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == 'make-override':
        return _make_override_main(argv[1:])
    ap = argparse.ArgumentParser(prog='python -m book_pipeline.apply_math_overrides')
    ap.add_argument('slug')
    args = ap.parse_args(argv)
    stats = apply_overrides(args.slug)
    print(f"[math-overrides] {args.slug}: {stats}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
