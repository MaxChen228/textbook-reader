"""Frontend escaping guardrails."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dynamic_attributes_use_attr_escape():
    shared = (ROOT / 'assets' / 'qbank-shared.js').read_text(encoding='utf-8')
    assert 'function escapeAttr' in shared
    assert re.search(r'window\.QBankShared = \{[\s\S]*escapeAttr,', shared)
    pages = [
        ROOT / 'dev' / 'index.html',
        ROOT / 'index.html',
        ROOT / 'problems.html',
    ]
    for page in pages:
        html = page.read_text(encoding='utf-8')
        assert (
            'QBankShared.escapeAttr' in html
            or 'S.escapeAttr' in html
            or 'escapeAttr } = QBankShared' in html
        ), page
        assert 'const escapeAttr = v =>' not in html
        unsafe = re.findall(
            r'(?:src|href|data-[\w-]+|title|id|alt)="[^"`\n]*\$\{(?:escapeHtml|esc)\(',
            html,
        )
        assert unsafe == [], (page, unsafe)
    assert unsafe == []
