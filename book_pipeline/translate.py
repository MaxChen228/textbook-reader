"""Translate-book pipeline.

Five sub-commands wired into the `/translate-book` skill:

    prep <slug>     [--chapters …] [--appendices …] [--force] [--batch-size N]
        Read parsed/<stem>.json, filter translatable blocks, write batches
        to mineru_data/<slug>/batches/. In gap mode (default when .zh.json
        exists), only emit blocks that aren't already translated.

    validate <slug> <batch_file>
        Check a single batch's _out.json: parsable, top-level array,
        |out| == |in|, i set matches. Prints diagnosis. Exit 0 ok, 1 retry, 2 fatal.

    merge <slug> [stem]
        For each stem (or just one), concat all validated _out.json into
        parsed/<stem>.zh.json. Refuses to merge if any batch hasn't validated.

    audit <slug>
        Coverage report: translatable / translated / real-content-missing
        (we know how much of the gap is because the source itself was empty).

Block format passed to agents (flat list, single contract):

    body block:     {"i": <int>, "t": "<p|section|…>", <md|title|caption|footnote>: "…"}
    problem block:  {"prob": "<num>", "i": <int>, "t": …, <field>: "…"}

Agent contract: input N blocks → output N items, each with the same i (and
prob when present), with the translated field. Top-level JSON array, nothing
else. See dispatch template in the skill spec.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
MINERU = ROOT / "book_pipeline" / "mineru_data"

TRANSLATABLE_TYPES = {"p", "section", "subsection", "example", "fig", "table"}
TRANSLATABLE_FIELDS = ("md", "title", "caption", "footnote")
ALLOWED_FIELDS_BY_TYPE = {
    "p": {"md"},
    "section": {"title"},
    "subsection": {"title"},
    "example": {"title"},
    "fig": {"caption"},
    "table": {"caption", "footnote"},
}


def _norm(text: str) -> str:
    """Whitespace-collapsed source text — the unit both anchoring and realign match on."""
    return " ".join((text or "").split())


def overlay_anchor(source_fields: dict[str, str]) -> str:
    """Stable 8-hex digest of a block's *source* (untranslated) field texts.

    Stored as `a` on each overlay patch at merge/realign time; corpus.py recomputes
    it from the current source block and only applies the translation when it matches.
    Survives index drift after a parser re-run: a stale patch simply stops applying
    (graceful English-only) instead of attaching its translation to the wrong block.

    Keep this the single source of truth — corpus.py imports it.
    """
    parts = [f"{k}\x1f{_norm(source_fields[k])}" for k in sorted(source_fields)]
    return hashlib.sha1("\x1e".join(parts).encode("utf-8")).hexdigest()[:8]


def _strip_controls(text: str) -> str:
    """Drop stray ASCII control chars (agents occasionally emit 0x05 etc.)."""
    return "".join(c for c in text if c >= " " or c in "\n\r\t")


def _read_json(path: Path):
    return json.loads(_strip_controls(path.read_text()))


def _book_dir(slug: str) -> Path:
    d = MINERU / slug
    if not d.exists():
        raise SystemExit(f"slug not found: {d}")
    return d


def _parsed_dir(slug: str) -> Path:
    p = _book_dir(slug) / "parsed"
    if not p.exists():
        raise SystemExit(f"parsed/ missing under {slug}; run mineru_ingest first")
    return p


def _batches_dir(slug: str) -> Path:
    p = _book_dir(slug) / "batches"
    p.mkdir(exist_ok=True)
    return p


def _stem_list(slug: str, chapters: list[str] | None, appendices: list[str] | None) -> list[str]:
    """Resolve which file stems (ch01, appA, …) we operate on."""
    book = _read_json(_parsed_dir(slug) / "book.json")
    stems: list[str] = []
    for ch in book.get("chapters", []):
        if chapters and str(ch["num"]) not in chapters:
            continue
        stems.append(Path(ch["file"]).stem)
    for ap in book.get("appendices", []):
        if appendices and str(ap["id"]) not in appendices:
            continue
        stems.append(Path(ap["file"]).stem)
    return stems


def _content_fields(block: dict) -> dict:
    """Pull non-empty translatable fields out of a source block."""
    out = {}
    for k in ("md", "title", "caption", "footnote"):
        v = block.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v
    return out


def _existing_translation_keys(zh: dict) -> tuple[set[int], set[tuple[str, str, int]]]:
    """Return (body_i_set, {(prob_num, sec, i)}) of already-translated keys.

    sec ∈ {"body","solution"} separates a problem's statement from its solution
    (two parallel block lists, each indexed from 0, that would otherwise collide)."""
    body_keys = {b.get("i") for b in zh.get("body", []) if isinstance(b.get("i"), int)}
    prob_keys: set[tuple[str, str, int]] = set()
    for pr in zh.get("problems", []):
        num = pr.get("num")
        for sec in ("body", "solution"):
            for b in pr.get(sec, []):
                if isinstance(b.get("i"), int):
                    prob_keys.add((num, sec, b["i"]))
    return body_keys, prob_keys


_GUIDE_SOP = """\
你是專業物理教科書譯者。Read 本 in.json，翻譯 `units` 陣列中每個單元的 `text` 成繁體中文（zh-TW，台灣物理慣用語）。

## 輸出格式（純文字檔，不是 JSON）
對每個單元，先寫一行 `<<<n>>>`（n 為該單元的 n 值），接著下一行起寫該單元 `text` 的譯文。例：

<<<0>>>
第 0 單元的譯文，$E=mc^2$ 之類 LaTeX 原樣保留。
<<<3>>>
第 3 單元的譯文。

- 你**只需**：複製 `<<<n>>>` 標記、寫譯文。**不要碰 i / prob / field，不要輸出 JSON、不要自己數數量、不要寫說明文字**
- 每個單元都要有對應的 `<<<n>>>` 段；段的順序隨意（程式靠標記對應）
- LaTeX（`$...$`、`$$...$$`、`\\(...\\)`、`\\[...\\]`）整段原樣，**直接照打、不要跳脫、不要改動反斜線**
- 引用編號 `Figure 1.1` / `Fig. 1.2` / `Eq. (1.1)` / `Section 2.3` / `Chapter 1` / `Problem 1.5` **保留英文原字 + 編號**，不要翻成「圖／方程式／章／節」

## 譯名與規範
"""


def _build_guide(slug: str) -> str:
    """SOP + glossary, inlined into every in.json so the agent reads one file.

    The agent's whole job is then: Read <batch>_in.json → translate each unit's
    `text` → write `<<<n>>>`-marked plain text to <batch>_out.txt. No JSON authoring
    (kills escape bugs), no counting (program reconciles markers).
    """
    gloss_path = _book_dir(slug) / "glossary.md"
    gloss = gloss_path.read_text() if gloss_path.exists() else "(無 glossary)"
    return _GUIDE_SOP + gloss


# ---------------------------------------------------------------------------
# prep
# ---------------------------------------------------------------------------

def cmd_prep(args):
    slug = args.slug
    parsed = _parsed_dir(slug)
    batches = _batches_dir(slug)
    chapters = args.chapters.split(",") if args.chapters else None
    appendices = args.appendices.split(",") if args.appendices else None
    force = args.force
    batch_size = args.batch_size

    summary: list[dict] = []
    guide = _build_guide(slug)
    for stem in _stem_list(slug, chapters, appendices):
        src_path = parsed / f"{stem}.json"
        if not src_path.exists():
            print(f"[skip] {stem}: source missing", file=sys.stderr)
            continue
        src = _read_json(src_path)
        zh_path = parsed / f"{stem}.zh.json"
        zh = _read_json(zh_path) if (zh_path.exists() and not force) else {}
        body_done, prob_done = _existing_translation_keys(zh)

        # Collect translation units — one per (block, field). Skip eq/empty/already-done.
        # Flattening to fields means the agent never tracks block structure or counts.
        units: list[dict] = []
        for idx, b in enumerate(src.get("body", [])):
            if b.get("t") not in TRANSLATABLE_TYPES or idx in body_done:
                continue
            for f, text in _content_fields(b).items():
                units.append({"prob": None, "sec": "body", "i": idx, "field": f, "text": text})
        for pr in src.get("problems", []):
            num = pr.get("num")
            # A problem has two parallel block lists: its statement (body) and its
            # merged-in solution. Flatten both; sec keeps them apart downstream.
            for sec in ("body", "solution"):
                for pi, pb in enumerate(pr.get(sec, [])):
                    if pb.get("t") not in TRANSLATABLE_TYPES or (num, sec, pi) in prob_done:
                        continue
                    for f, text in _content_fields(pb).items():
                        units.append({"prob": num, "sec": sec, "i": pi, "field": f, "text": text})

        # Clean stale batch files for this stem (old _in.json / _out.txt / legacy _out.json)
        for f in list(batches.glob(f"{stem}_*.json")) + list(batches.glob(f"{stem}_*.txt")):
            f.unlink()

        if not units:
            summary.append({"stem": stem, "batches": 0, "units": 0})
            continue

        n_batches = (len(units) + batch_size - 1) // batch_size
        for i in range(n_batches):
            chunk = units[i * batch_size : (i + 1) * batch_size]
            payload = [{"n": n, "prob": u["prob"], "sec": u["sec"], "i": u["i"], "field": u["field"], "text": u["text"]}
                       for n, u in enumerate(chunk)]
            (batches / f"{stem}_{i:02d}_in.json").write_text(
                json.dumps({"slug": slug, "stem": stem, "guide": guide, "units": payload}, ensure_ascii=False, indent=2)
            )
        summary.append({"stem": stem, "batches": n_batches, "units": len(units)})

    print(json.dumps({"slug": slug, "stems": summary}, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# validate (per-batch)
# ---------------------------------------------------------------------------

_MARKER_RE = re.compile(r'(?m)^<<<(\d+)>>>[ \t]*$')


def _parse_marked(text: str) -> dict[int, str]:
    """Parse `<<<n>>>`-delimited plain text into {n: translation}.

    re.split yields [preamble, n0, body0, n1, body1, ...]; odd entries are the
    captured marker numbers, even ones (after the first) are the bodies.
    """
    parts = _MARKER_RE.split(_strip_controls(text))
    out: dict[int, str] = {}
    for k in range(1, len(parts), 2):
        out[int(parts[k])] = parts[k + 1].strip()
    return out


def _validate_batch(in_path: Path, out_path: Path) -> tuple[bool, str]:
    if not out_path.exists():
        return False, f"missing output: {out_path.name}"
    expected = {u["n"] for u in _read_json(in_path)["units"]}
    got = _parse_marked(out_path.read_text())
    miss = sorted(expected - set(got))
    if miss:
        return False, f"missing markers: {miss[:15]}"
    extra = sorted(set(got) - expected)
    if extra:
        return False, f"unexpected markers: {extra[:15]}"
    empty = sorted(n for n in expected if not got[n])
    if empty:
        return False, f"empty translation at: {empty[:15]}"
    return True, f"ok ({len(expected)} units)"


def cmd_validate(args):
    slug = args.slug
    batches = _batches_dir(slug)
    target_in = batches / args.batch
    if not target_in.exists():
        # allow passing stem_NN
        candidate = batches / f"{args.batch}_in.json"
        if candidate.exists():
            target_in = candidate
        else:
            raise SystemExit(f"input batch missing: {args.batch}")
    out_path = target_in.with_name(target_in.name.replace("_in.json", "_out.txt"))
    ok, msg = _validate_batch(target_in, out_path)
    print(f"{target_in.name}: {msg}")
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

def cmd_merge(args):
    slug = args.slug
    parsed = _parsed_dir(slug)
    batches = _batches_dir(slug)
    stems = [args.stem] if args.stem else sorted({p.name.split("_")[0] for p in batches.glob("*_in.json")})

    for stem in stems:
        batch_ins = sorted(batches.glob(f"{stem}_*_in.json"))
        if not batch_ins:
            continue
        # Validate all first
        per_batch = []
        all_ok = True
        for bi in batch_ins:
            out_path = bi.with_name(bi.name.replace("_in.json", "_out.txt"))
            ok, msg = _validate_batch(bi, out_path)
            per_batch.append((bi.name, ok, msg))
            if not ok:
                all_ok = False
        if not all_ok:
            print(f"[{stem}] REFUSE merge — some batches invalid:", file=sys.stderr)
            for n, ok, msg in per_batch:
                if not ok:
                    print(f"  ✗ {n}: {msg}", file=sys.stderr)
            continue

        # Load existing overlay (so we extend, not overwrite)
        zh_path = parsed / f"{stem}.zh.json"
        zh = _read_json(zh_path) if zh_path.exists() else {}
        body_map: dict[int, dict] = {b["i"]: b for b in zh.get("body", []) if "i" in b}
        # prob_map[num][sec] = {i: block}; sec ∈ {"body","solution"}
        prob_map: dict[str, dict[str, dict[int, dict]]] = {}
        for pr in zh.get("problems", []):
            num = pr.get("num")
            prob_map[num] = {sec: {b["i"]: b for b in pr.get(sec, []) if "i" in b}
                             for sec in ("body", "solution")}

        # Pull title from book.zh.json if present
        book_zh_path = parsed / "book.zh.json"
        zh_title = zh.get("title")
        if book_zh_path.exists():
            book_zh = _read_json(book_zh_path)
            num_str = stem.removeprefix("ch").lstrip("0") or None
            id_str = stem.removeprefix("app") if stem.startswith("app") else None
            for ch in book_zh.get("chapters", []):
                if str(ch.get("num")) == num_str:
                    zh_title = ch.get("title")
            for ap in book_zh.get("appendices", []):
                if ap.get("id") == id_str:
                    zh_title = ap.get("title")

        # Reassemble: marker n → unit (prob,i,field) → translation. Aggregate fields
        # per (prob,i); keep the matching source text for the drift-proof anchor.
        trans: dict[tuple, dict[str, str]] = {}   # (prob,i) -> {field: zh}
        src_txt: dict[tuple, dict[str, str]] = {}  # (prob,i) -> {field: english}
        for bi in batch_ins:
            out_path = bi.with_name(bi.name.replace("_in.json", "_out.txt"))
            units = {u["n"]: u for u in _read_json(bi)["units"]}
            for n, zh_text in _parse_marked(out_path.read_text()).items():
                if not zh_text.strip():
                    continue
                u = units[n]
                key = (u.get("prob"), u.get("sec", "body"), u["i"])
                trans.setdefault(key, {})[u["field"]] = zh_text
                src_txt.setdefault(key, {})[u["field"]] = u["text"]

        added_body, added_prob = 0, 0
        for (prob, sec, i), fields_zh in trans.items():
            clean = {"i": i, **fields_zh, "a": overlay_anchor(src_txt[(prob, sec, i)])}
            if prob is not None:
                pm = prob_map.setdefault(prob, {"body": {}, "solution": {}}).setdefault(sec, {})
                if i not in pm:
                    pm[i] = clean
                    added_prob += 1
            else:
                if i not in body_map:
                    body_map[i] = clean
                    added_body += 1

        zh_out = {"title": zh_title} if zh_title else {}
        zh_out["body"] = sorted(body_map.values(), key=lambda x: x["i"])
        if prob_map:
            problems_out = []
            for num, secs in sorted(prob_map.items(), key=lambda kv: kv[0]):
                pr_out = {"num": num}
                for sec in ("body", "solution"):
                    blocks = secs.get(sec, {})
                    if blocks:
                        pr_out[sec] = sorted(blocks.values(), key=lambda x: x["i"])
                if len(pr_out) > 1:
                    problems_out.append(pr_out)
            zh_out["problems"] = problems_out
        zh_path.write_text(json.dumps(zh_out, ensure_ascii=False, indent=2))
        print(f"[{stem}] +body={added_body} +problem-blocks={added_prob} → {zh_path.relative_to(ROOT)}")


# ---------------------------------------------------------------------------
# realign — repair stale .zh.json after a parser re-run shifted body indices
# ---------------------------------------------------------------------------

def _english_by_key(stem: str, batches: Path) -> dict[tuple[str | None, str, int], dict[str, str]]:
    """From surviving batches/<stem>_*_in.json: (prob, sec, i) -> {source_field: english}."""
    out: dict[tuple[str | None, str, int], dict[str, str]] = {}
    for bi in sorted(batches.glob(f"{stem}_*_in.json")):
        data = _read_json(bi)
        for u in data.get("units", []):  # new units format
            out.setdefault((u.get("prob"), u.get("sec", "body"), u["i"]), {})[u["field"]] = u["text"]
        for b in data.get("blocks", []):  # legacy blocks format (pre-refactor)
            key = (b.get("prob"), "body", b["i"])
            out[key] = {f: b[f] for f in ("md", "title", "caption", "footnote")
                        if isinstance(b.get(f), str) and b[f].strip()}
    return out


def _match_index(eng_fields: dict[str, str], blocks: list[dict]) -> int | None:
    """Index of the unique current block whose source fields all match eng_fields; else None."""
    cand: set[int] | None = None
    for f, txt in eng_fields.items():
        n = _norm(txt)
        hits = {j for j, b in enumerate(blocks)
                if isinstance(b.get(f), str) and _norm(b[f]) == n}
        cand = hits if cand is None else (cand & hits)
        if not cand:
            return None
    return next(iter(cand)) if cand and len(cand) == 1 else None


def _match_anchor(anchor: str, fields: list[str], blocks: list[dict]) -> int | None:
    """Index of the unique current block whose source-field anchor equals `anchor`; else None."""
    hits = [j for j, b in enumerate(blocks)
            if overlay_anchor({f: b.get(f, "") for f in fields}) == anchor]
    return hits[0] if len(hits) == 1 else None


def _realign_patches(patches: list[dict], eng_lookup, src_blocks: list[dict],
                     stat: dict) -> list[dict]:
    out: dict[int, dict] = {}
    for patch in patches:
        old_i = patch.get("i")
        zh_fields = {k: v for k, v in patch.items() if k in TRANSLATABLE_FIELDS}
        if not isinstance(old_i, int) or not zh_fields:
            stat["dropped"] += 1
            continue
        # 1) anchor-based（batches 不需在場）：拿 patch 自帶錨點掃當前源檔找回正確 block
        j = anchor = None
        if isinstance(patch.get("a"), str):
            j = _match_anchor(patch["a"], list(zh_fields), src_blocks)
            if j is not None:
                anchor = patch["a"]
        # 2) fallback：用殘存 batches 的英文原文做內容比對
        if j is None:
            eng = eng_lookup(old_i)
            eng_present = {f: eng[f] for f in zh_fields if eng and f in eng}
            if not eng_present:
                stat["no_english"] += 1
                continue
            j = _match_index(eng_present, src_blocks)
            if j is None:
                stat["unmatched"] += 1
                continue
            anchor = overlay_anchor(eng_present)
        if j in out:  # two translations claim the same block — keep first, drop dup
            stat["dup"] += 1
            continue
        out[j] = {"i": j, "a": anchor, **zh_fields}
        stat["resolved"] += 1
    return sorted(out.values(), key=lambda x: x["i"])


def cmd_realign(args):
    slug = args.slug
    parsed = _parsed_dir(slug)
    batches = _book_dir(slug) / "batches"
    for stem in _stem_list(slug, None, None):
        src_path = parsed / f"{stem}.json"
        zh_path = parsed / f"{stem}.zh.json"
        if not src_path.exists() or not zh_path.exists():
            continue
        src = _read_json(src_path)
        zh = _read_json(zh_path)
        # anchor-based 不需 batches；無錨點的舊 overlay 才回退到 batches 英文比對
        has_anchor = any(isinstance(p.get("a"), str) for p in zh.get("body", []))
        has_batches = bool(list(batches.glob(f"{stem}_*_in.json")))
        if not has_anchor and not has_batches:
            print(f"[{stem}] no anchors and no batches — left untouched (need prep --force)")
            continue
        eng_map = _english_by_key(stem, batches) if has_batches else {}
        src_body = src.get("body", [])
        stat = {"resolved": 0, "unmatched": 0, "no_english": 0, "dup": 0, "dropped": 0}

        new_body = _realign_patches(
            zh.get("body", []),
            lambda i: eng_map.get((None, "body", i)),
            src_body, stat,
        )

        src_prob_body = {p.get("num"): p.get("body", []) for p in src.get("problems", [])}
        src_prob_sol = {p.get("num"): p.get("solution", []) for p in src.get("problems", [])}
        new_problems = []
        for pr in zh.get("problems", []):
            num = pr.get("num")
            body = _realign_patches(
                pr.get("body", []),
                lambda i, num=num: eng_map.get((num, "body", i)),
                src_prob_body.get(num, []), stat,
            )
            solution = _realign_patches(
                pr.get("solution", []),
                lambda i, num=num: eng_map.get((num, "solution", i)),
                src_prob_sol.get(num, []), stat,
            )
            pr_out = {"num": num}
            if body:
                pr_out["body"] = body
            if solution:
                pr_out["solution"] = solution
            if len(pr_out) > 1:
                new_problems.append(pr_out)

        zh_out: dict = {}
        if zh.get("title"):
            zh_out["title"] = zh["title"]
        zh_out["body"] = new_body
        if new_problems:
            zh_out["problems"] = new_problems
        zh_path.write_text(json.dumps(zh_out, ensure_ascii=False, indent=2))
        print(f"[{stem}] resolved={stat['resolved']} unmatched={stat['unmatched']} "
              f"no_eng={stat['no_english']} dup={stat['dup']} → {zh_path.relative_to(ROOT)}")


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

def cmd_audit(args):
    report = audit_coverage(args.slug)
    rows = report["stems"]

    print(f'{"stem":<8} {"done":>6} {"trans":>6} {"%":>5}  {"real_miss":>9}  {"empty":>6}')
    for row in rows:
        print(f"{row['stem']:<8} {row['translated']:>6} {row['translatable']:>6} {row['ratio']*100:>4.0f}%  "
              f"{row['real_miss']:>9}  {row['empty_miss']:>6}")
    print(f"\nTOTAL {report['translated']}/{report['translatable']} = {report['ratio']*100:.1f}% "
          f"| real_miss={report['real_miss']} empty={report['empty_miss']}")


def audit_coverage(slug: str) -> dict:
    """Return translation coverage for one book without parsing CLI output."""
    parsed = _parsed_dir(slug)
    rows = []
    total_translatable, total_translated, total_real_miss, total_empty_miss = 0, 0, 0, 0
    for stem in _stem_list(slug, None, None):
        src_path = parsed / f"{stem}.json"
        zh_path = parsed / f"{stem}.zh.json"
        if not src_path.exists():
            continue
        src = _read_json(src_path)
        zh = _read_json(zh_path) if zh_path.exists() else {}
        body_done, prob_done = _existing_translation_keys(zh)

        translatable = 0
        translated = 0
        real_miss = 0
        empty_miss = 0
        for i, b in enumerate(src.get("body", [])):
            if b.get("t") not in TRANSLATABLE_TYPES:
                continue
            translatable += 1
            if i in body_done:
                translated += 1
                continue
            has_content = any(
                isinstance(b.get(k), str) and b[k].strip()
                for k in ("md", "title", "caption", "footnote")
            )
            if has_content:
                real_miss += 1
            else:
                empty_miss += 1
        for pr in src.get("problems", []):
            num = pr.get("num")
            for sec in ("body", "solution"):
                for pi, pb in enumerate(pr.get(sec, [])):
                    if pb.get("t") not in TRANSLATABLE_TYPES:
                        continue
                    translatable += 1
                    if (num, sec, pi) in prob_done:
                        translated += 1
                        continue
                    has_content = any(
                        isinstance(pb.get(k), str) and pb[k].strip()
                        for k in ("md", "title", "caption", "footnote")
                    )
                    if has_content:
                        real_miss += 1
                    else:
                        empty_miss += 1

        rows.append({
            "stem": stem,
            "translated": translated,
            "translatable": translatable,
            "ratio": translated / translatable if translatable else 1.0,
            "real_miss": real_miss,
            "empty_miss": empty_miss,
        })
        total_translatable += translatable
        total_translated += translated
        total_real_miss += real_miss
        total_empty_miss += empty_miss

    return {
        "slug": slug,
        "stems": rows,
        "translated": total_translated,
        "translatable": total_translatable,
        "ratio": total_translated / total_translatable if total_translatable else 1.0,
        "real_miss": total_real_miss,
        "empty_miss": total_empty_miss,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None):
    import argparse
    parser = argparse.ArgumentParser(prog="translate.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("prep")
    pp.add_argument("slug")
    pp.add_argument("--chapters")
    pp.add_argument("--appendices")
    pp.add_argument("--force", action="store_true")
    pp.add_argument("--batch-size", type=int, default=200)
    pp.set_defaults(func=cmd_prep)

    pv = sub.add_parser("validate")
    pv.add_argument("slug")
    pv.add_argument("batch", help="batch input filename like ch04_01_in.json (or ch04_01)")
    pv.set_defaults(func=cmd_validate)

    pm = sub.add_parser("merge")
    pm.add_argument("slug")
    pm.add_argument("stem", nargs="?")
    pm.set_defaults(func=cmd_merge)

    pr = sub.add_parser("realign")
    pr.add_argument("slug")
    pr.set_defaults(func=cmd_realign)

    pa = sub.add_parser("audit")
    pa.add_argument("slug")
    pa.set_defaults(func=cmd_audit)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
