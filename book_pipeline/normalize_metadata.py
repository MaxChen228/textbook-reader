"""校驗 + 規範化 book_pipeline/mineru_data/*/extract_rules.yaml metadata。

yaml 是 SoT（入 git）；book.json 是 parser 產物（gitignored）。
讀 book_pipeline/metadata_schema.yaml 為規範定義，掃所有 extract_rules.yaml 並：
1. 驗證 subject ∈ schema.subjects
2. 驗證 edition 格式（或為 null）
3. 驗證 author 分隔符
4. --fix：自動修可確定的格式（"4" → "4th"、" and " → "; "）

修完 yaml 後須**重跑 parser** 讓 book.json 對齊：
  uv run --with pyyaml python -m book_pipeline.parser <slug>

CLI:
  python -m book_pipeline.normalize_metadata          # 只驗證，列違規
  python -m book_pipeline.normalize_metadata --fix    # 自動修可確定的
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'book_pipeline' / 'mineru_data'
SCHEMA_PATH = ROOT / 'book_pipeline' / 'metadata_schema.yaml'


def load_schema() -> dict:
    return yaml.safe_load(SCHEMA_PATH.read_text())


def normalize_edition(val: str | None) -> tuple[str | None, bool]:
    """回 (normalized, was_changed)。None 留 None；'4' → '4th'；'Seventh International Edition' → '7th International'。"""
    if val is None or val == '':
        return None, val == ''
    s = val.strip()
    # 純數字 → 加序數
    if s.isdigit():
        return _add_ordinal(int(s)), True
    # "4th"、"7th International" 已合規
    if re.match(r'^\d+(st|nd|rd|th)( (International|Global|Asian|SI|Student|Instructor))?$', s):
        return s, False
    # 嘗試解 "International Seventh Edition" / "Seventh Edition"
    word_to_num = {
        'first':1, 'second':2, 'third':3, 'fourth':4, 'fifth':5,
        'sixth':6, 'seventh':7, 'eighth':8, 'ninth':9, 'tenth':10,
    }
    variants = {'international','global','asian','si','student','instructor'}
    tokens = [t.lower().rstrip(',').rstrip('.') for t in s.split()]
    tokens = [t for t in tokens if t != 'edition']
    num = None
    variant = None
    for t in tokens:
        if t in word_to_num:
            num = word_to_num[t]
        elif t.rstrip('snrdth').isdigit() and any(t.endswith(suf) for suf in ('st','nd','rd','th')):
            num = int(re.match(r'^(\d+)', t).group(1))
        elif t.isdigit():
            num = int(t)
        elif t in variants:
            variant = t.title() if t != 'si' else 'SI'
    if num:
        out = _add_ordinal(num)
        if variant:
            out += f' {variant}'
        return out, True
    return val, False  # 解不出來，留原樣讓 validate 報錯


def _add_ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f'{n}th'
    return f'{n}{ {1:"st",2:"nd",3:"rd"}.get(n%10, "th") }'


def normalize_author(val: str | None) -> tuple[str | None, bool]:
    """' and ' → '; '；保留單一逗號分隔（少數正常）但 ', ' → '; ' 若全是名字。"""
    if not val:
        return val, False
    s = val.strip()
    original = s
    # " and " → "; "
    s = re.sub(r'\s+and\s+', '; ', s)
    # ", " → "; "（簡化：作者名通常不會內部含逗號；若有 "Last, First" 風格本 normalize 會搞錯，需人工）
    if ', ' in s and '; ' not in s:
        s = s.replace(', ', '; ')
    return s, s != original


def validate(book: dict, schema: dict, slug: str) -> list[str]:
    errs: list[str] = []
    subj = book.get('subject')
    if subj is None:
        errs.append(f'{slug}: subject 缺')
    elif subj not in schema['subjects']:
        errs.append(f'{slug}: subject {subj!r} 不在 schema.subjects（請先補 metadata_schema.yaml）')
    ed = book.get('edition')
    if ed is not None and not re.match(schema['edition_format']['pattern'], ed):
        errs.append(f'{slug}: edition {ed!r} 不符 pattern')
    au = book.get('author')
    if au and ', ' in au:
        errs.append(f'{slug}: author {au!r} 含 ", "（應該 "; "）')
    if au and ' and ' in au:
        errs.append(f'{slug}: author {au!r} 含 " and "（應該 "; "）')
    return errs


def _yaml_quote(v: str | None) -> str:
    """寫回 yaml scalar：null → null；含特殊字元 → 雙引號 escape。"""
    if v is None:
        return 'null'
    if re.search(r'[:#&*!|>%@`\'"\[\]{},]', v) or v.startswith(('-', '?')):
        return '"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return v


def _patch_yaml_field(text: str, field: str, new_val: str | None) -> str:
    """把 top-level `field: ...` 那行的 value 換掉（保留同行尾註解、保留檔內其他 line）。"""
    pat = re.compile(rf'^({field}:\s*)(?:".*?"|\'.*?\'|[^\n#]*?)(\s*(?:#.*)?)$', re.M)
    repl = lambda m: f'{m.group(1)}{_yaml_quote(new_val)}{m.group(2)}'
    new_text, n = pat.subn(repl, text, count=1)
    if n != 1:
        raise RuntimeError(f'patch {field} failed')
    return new_text


def main() -> None:
    fix = '--fix' in sys.argv
    schema = load_schema()
    paths = sorted(DATA_DIR.glob('*/extract_rules.yaml'))
    all_errs: list[str] = []
    fixes: list[str] = []
    for p in paths:
        slug = p.parent.name
        text = p.read_text()
        book = yaml.safe_load(text)
        new_ed, ch_ed = normalize_edition(book.get('edition'))
        if ch_ed:
            fixes.append(f'  {slug}: edition {book.get("edition")!r} → {new_ed!r}')
            text = _patch_yaml_field(text, 'edition', new_ed)
            book['edition'] = new_ed
        new_au, ch_au = normalize_author(book.get('author'))
        if ch_au:
            fixes.append(f'  {slug}: author {book.get("author")!r} → {new_au!r}')
            text = _patch_yaml_field(text, 'author', new_au)
            book['author'] = new_au
        if fix and (ch_ed or ch_au):
            p.write_text(text)
        # validate (用 fix 後的值)
        all_errs.extend(validate(book, schema, slug))

    if fixes:
        print('## 可自動修的格式：')
        for f in fixes:
            print(f)
        print(f'  ({"已寫入" if fix else "預覽；加 --fix 寫入"})')
    if all_errs:
        print('## 規範違規（需手動處理）：')
        for e in all_errs:
            print(f'  ✗ {e}')
        sys.exit(1)
    print(f'## ✅ {len(paths)} 本 metadata 規範通過')


if __name__ == '__main__':
    main()
