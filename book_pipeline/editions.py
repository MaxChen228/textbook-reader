#!/usr/bin/env python3
"""book_pipeline.editions — LLM 親查版本結論的持久層（git 追蹤的「貴重成果」，per-book）。

[architect note — 為何與 crawl_resolution.json 分立]

查證左移後，每本書「是哪一版、解答本是否對齊母書、憑什麼這樣判」由書單管理 agent 多源 LLM 親查
得出——這是燒 token 換來的判斷，必須持久化、換機/重建 resolution 時不該丟。故落
editions/<slug>.json 並**入 git**（比照 catalog_overrides/：daemon LLM 寫的 git 追蹤貴重成果，
由定期「data artifacts 快照」commit 帶上跨機）。與機器連結快取按「寫入頻率 × 持久化需求」分層：
  crawl_resolution.json（gitignore，高頻 enrich：href/cover/at 每 cycle 變）= 態 + z-lib 連結，可重生。
  editions/<slug>.json（git 追蹤，低頻穩定）                              = 版本判斷，貴重不可重生。
（resolution 高頻寫、若入 git 會讓 daemon working-tree 永遠 dirty + 跨機 merge 衝突，故維持 gitignore。）

鐵律：version / sol_alignment 一律 **LLM 親判**（禁 regex 抽 title、禁確定性版本比對）。本模組只存
判斷結果與其證據軌跡，**自身不做任何判斷**——它是純讀寫層。版本「字串值」原樣保留（'3rd'、'Revised'、
'10th Anniversary' 等非序數值皆可），不做正規化/int 化。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from datetime import datetime, timezone

from book_pipeline import jsonio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDITIONS_DIR = os.path.join(ROOT, 'book_pipeline', 'editions')

SOL_SUFFIX = '_sol'  # 解答本 slug 尾綴（與 booklists 同；本地定義避免 import 環）

# 一筆 edition 結論的 schema。「合格存在」四維模型（2026-06 重構）把 editions 從「只存版本判斷」
# 升級為「每本合格書的完整持久記錄」——身份 + 分類 + 四維驗證結論皆落此（取代舊 booklists 條目）。
# 三維「未驗」一律 None（沿用既有慣例 = lazy 回填觸發點）：
#   version        {label,year,publisher,isbn,matches_pref} —— 維③ LLM 親查的版次事實
#   sol_alignment  {aligned,parent_version,sol_version,basis} —— 維④ 僅 <slug>_sol 用（解答↔母書對齊）
#   confidence     high/medium/low（**非數字**，避機械閘）
#   evidence       1-3 條多 haiku 共識理由（重量級全文另落 .verify_log/，本檔只留 digest）
#   sources        [{type,ref,note}] 查證來源
#   by / checked_at 戳記（誰查、何時查；與 resolution 的下載落盤 at 區分）
#   identity       {title,author,edition_pref,has_solution,promoted_from} —— 取代 booklists book{} 條目
#   classification {field_id,subject,order:[field_order,subject_rank,book_rank]} —— 取代 booklists 嵌套
#   qualification  {eligible:true|false|None, verified_at} —— 維① 夠格收錄結論（合格存在核心）
# 維②（z-lib 有可下載連結）**不存此**——衍生自 crawl_resolution.json[slug].status=='found'（連結可重生）。
FIELDS = ('version', 'sol_alignment', 'confidence', 'evidence', 'sources', 'by', 'checked_at',
          'identity', 'classification', 'qualification')


def _path(slug: str) -> str:
    return os.path.join(EDITIONS_DIR, f'{slug}.json')


def blank() -> dict:
    """空骨架（新書初始化 / 補骨架用）。三維判斷欄 + 身份/分類/合格結論初始 None = 尚未 LLM 親查。"""
    return {'version': None, 'sol_alignment': None, 'confidence': None,
            'evidence': [], 'sources': [], 'by': None, 'checked_at': None,
            'identity': None, 'classification': None, 'qualification': None}


# ── 四維「合格存在」判定（純函式，無 I/O——booklists shim 與 catalog 衍生用）─────────────────────
def dims(slug: str, ed: dict | None, resolution: dict, have: set | None = None) -> dict:
    """一本書的四維驗證布林。**純判定、不做任何 LLM**——只讀已落盤的親查結論。
      維① eligible       qualification.eligible is True（人工/agent 判夠格收錄）
      維② link           resolution[slug].status=='found'（或已 owned，實體在手即視同有連結）
      維③ version        version 親查完成 **且** matches_pref is True（確認且是符合偏好的版次）
      維④ sol_alignment  僅 <slug>_sol：sol_alignment.aligned is True；非解答本 N/A → True
    回 {eligible, link, version, sol_alignment}（全 bool）。"""
    ed = ed or {}
    have = have or set()
    qual = ed.get('qualification') or {}
    eligible = qual.get('eligible') is True
    r = resolution.get(slug) or {}
    link = (slug in have) or (r.get('status') == 'found')
    ver = ed.get('version') or {}
    version = bool(ver) and ver.get('matches_pref') is True
    if slug.endswith(SOL_SUFFIX):
        sa = ed.get('sol_alignment') or {}
        sol_alignment = bool(sa) and sa.get('aligned') is True
    else:
        sol_alignment = True  # 非解答本：維④ N/A → 視為通過
    return {'eligible': eligible, 'link': link, 'version': version, 'sol_alignment': sol_alignment}


def qualifies(slug: str, ed: dict | None, resolution: dict, have: set | None = None) -> bool:
    """四維全過 = 「合格存在」（QUALIFIED：可交買書員下載 / reader 收錄）。"""
    return all(dims(slug, ed, resolution, have).values())


def load(slug: str) -> dict | None:
    """讀單本 edition 結論；無檔回 None（= 尚未查證 → lazy 回填觸發點）。"""
    return jsonio.read_json(_path(slug), None)


def load_all() -> dict:
    """{slug: edition} 全表（dev / 稽核 / 跨母子書版本比對用）。"""
    out = {}
    for p in sorted(glob.glob(os.path.join(EDITIONS_DIR, '*.json'))):
        d = jsonio.read_json(p, None)
        if isinstance(d, dict):
            out[os.path.basename(p)[:-5]] = d
    return out


def save(slug: str, fields: dict) -> dict:
    """merge 寫入（更新/新增欄位、覆蓋同名舊值）——LLM 親查落盤用。flock per-file 並發安全 + 原子寫。
    回合併後全筆。"""
    import fcntl
    os.makedirs(EDITIONS_DIR, exist_ok=True)
    p = _path(slug)
    with open(p + '.lock', 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        cur = jsonio.read_json(p, None) or blank()
        cur.update(fields)
        jsonio.atomic_write_json(p, cur, indent=1)
        return cur


def ensure(slug: str, defaults: dict | None = None) -> dict:
    """冪等補骨架：只補**缺少**的欄位、不蓋已有值（migration / 機械推導骨架用）。已存在的 edition
    值原樣保留 → 重跑安全。回補齊後全筆。"""
    import fcntl
    os.makedirs(EDITIONS_DIR, exist_ok=True)
    p = _path(slug)
    base = blank()
    base.update(defaults or {})
    with open(p + '.lock', 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        cur = jsonio.read_json(p, None) or {}
        changed = False
        for k, v in base.items():
            if k not in cur:
                cur[k] = v
                changed = True
        if changed:
            jsonio.atomic_write_json(p, cur, indent=1)
        return cur


# ── CLI（書單管理 skill 落盤版本判斷用；版本/解答對齊一律 LLM 親查後寫入，本 CLI 不做判斷）────────
def cmd_set(args) -> int:
    """寫單本版本結論。version 子欄（label/year/publisher/isbn/matches_pref）merge 既有（不蓋未給者）；
    evidence/source **append** 既有（多次查證累積軌跡、不丟前次）。by 預設 booklist-manager、checked_at 自動戳。"""
    cur = load(args.slug) or {}                  # 一次讀既有，供 version merge 與 evidence/source append
    fields = {'by': args.by, 'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    ver = {}
    for k in ('label', 'year', 'publisher', 'isbn'):
        v = getattr(args, k, None)
        if v is not None:
            ver[k] = v
    if args.matches_pref is not None:
        ver['matches_pref'] = args.matches_pref
    if ver:
        merged = dict(cur.get('version') or {})
        merged.update(ver)                       # version 子 dict 也 merge（保留未給的舊欄）
        fields['version'] = merged
    if args.confidence:
        fields['confidence'] = args.confidence
    sa = {}                                       # 解答本與母書版次對齊（LLM 親判；僅 <slug>_sol 用）
    if getattr(args, 'sol_aligned', None) is not None:
        sa['aligned'] = args.sol_aligned
    for k in ('parent_version', 'sol_version', 'basis'):
        v = getattr(args, k, None)
        if v is not None:
            sa[k] = v
    if sa:
        merged = dict(cur.get('sol_alignment') or {})
        merged.update(sa)                         # sol_alignment 子 dict 也 merge（保留未給的舊欄）
        fields['sol_alignment'] = merged
    cls = {}                                       # 分類（領域歸類；agent 查證時順手歸，子 dict merge）
    if getattr(args, 'field_id', None) is not None:
        cls['field_id'] = args.field_id
    if getattr(args, 'subject', None) is not None:
        cls['subject'] = args.subject
    if cls:
        merged = dict(cur.get('classification') or {})
        merged.update(cls)
        fields['classification'] = merged
    if getattr(args, 'eligible', None) is not None:  # 維① 夠格收錄結論 + 親查戳
        q = dict(cur.get('qualification') or {})
        q['eligible'] = args.eligible
        q['verified_at'] = fields['checked_at']
        fields['qualification'] = q
    if args.evidence:
        fields['evidence'] = (cur.get('evidence') or []) + args.evidence              # 真 append（不蓋前次）
    if args.source:
        fields['sources'] = (cur.get('sources') or []) + [{'note': s} for s in args.source]  # 真 append
    e = save(args.slug, fields)
    print(json.dumps({'ok': True, 'slug': args.slug, 'edition': e}, ensure_ascii=False, indent=2))
    return 0


def cmd_show(args) -> int:
    e = load(args.slug)
    print(json.dumps({'slug': args.slug, 'edition': e}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='editions：LLM 親查版本結論的讀寫（書單管理 skill 用）')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('set', help='寫版本結論（version 子欄 merge、evidence/source append）')
    p.add_argument('slug')
    p.add_argument('--label', help="版次字串原樣（'3rd'/'Revised'/'10th Anniversary'…，不正規化）")
    p.add_argument('--year', type=int)
    p.add_argument('--publisher')
    p.add_argument('--isbn')
    p.add_argument('--matches-pref', dest='matches_pref', action=argparse.BooleanOptionalAction,
                   default=None, help='LLM 判定此版是否符合 booklists.edition_pref')
    p.add_argument('--confidence', choices=('high', 'medium', 'low'))
    p.add_argument('--sol-aligned', dest='sol_aligned', action=argparse.BooleanOptionalAction,
                   default=None, help='（解答本）LLM 親判解答本版次是否對齊母書')
    p.add_argument('--parent-version', dest='parent_version', help='（解答本）母書版次')
    p.add_argument('--sol-version', dest='sol_version', help='（解答本）解答本版次')
    p.add_argument('--basis', help='（解答本）對齊判斷依據')
    p.add_argument('--field-id', dest='field_id', help='分類領域 id（join fields.json 取顯示名/排序）')
    p.add_argument('--subject', help='分類子科目（領域內細分）')
    p.add_argument('--eligible', dest='eligible', action=argparse.BooleanOptionalAction,
                   default=None, help='維① LLM/人工判此書是否夠格收錄（--no-eligible=判不夠格）')
    p.add_argument('--evidence', action='append', help='共識理由（可重複；重量級全文另落 .verify_log/）')
    p.add_argument('--source', action='append', help='查證來源（可重複，自由文字）')
    p.add_argument('--by', default='booklist-manager')
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser('show', help='看單本版本結論')
    p.add_argument('slug')
    p.set_defaults(fn=cmd_show)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == '__main__':
    raise SystemExit(main())
