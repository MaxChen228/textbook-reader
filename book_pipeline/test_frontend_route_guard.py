"""Static guardrails for reader hash routing."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_reader_hash_route_validates_chunk_before_fetch():
    html = (ROOT / 'index.html').read_text(encoding='utf-8')
    assert 'function validChunkRef(kind, key)' in html
    parse_hash = html.split('async function parseHash', 1)[1].split('window.addEventListener', 1)[0]
    assert 'if (!validChunkRef(kind, key))' in parse_hash
    assert parse_hash.index('if (!validChunkRef(kind, key))') < parse_hash.index('await showChunk(kind, key)')
