"""Path-boundary guardrails for textbooks.corpus."""

import json
from pathlib import Path

from textbooks import corpus


def _write_book(root: Path, dirname: str, slug: str) -> None:
    parsed = root / dirname / 'parsed'
    parsed.mkdir(parents=True)
    (parsed / 'book.json').write_text(json.dumps({
        'slug': slug,
        'title': f'Title {slug}',
        'chapters': [{'num': 1, 'title': 'One'}],
        'appendices': [],
    }), encoding='utf-8')
    (parsed / 'ch01.json').write_text(json.dumps({
        'num': 1,
        'title': 'One',
        'body': [],
        'problems': [],
    }), encoding='utf-8')


def test_corpus_rejects_invalid_or_mismatched_slugs(tmp_path):
    orig_dir = corpus.DATA_DIR
    orig_books = corpus._books_cache
    orig_book_cache = dict(corpus._book_cache)
    orig_chunk_cache = dict(corpus._chunk_cache)
    try:
        corpus.DATA_DIR = tmp_path
        corpus._books_cache = None
        corpus._book_cache.clear()
        corpus._chunk_cache.clear()
        _write_book(tmp_path, 'good_slug', 'good_slug')
        _write_book(tmp_path, 'bad-dir', 'bad-dir')
        _write_book(tmp_path, 'mismatch', 'other_slug')

        books = corpus.list_books()
        assert [b['slug'] for b in books] == ['good_slug']
        assert corpus.load_book('../escape') is None
        assert corpus.load_chapter('../escape', 1) is None
        assert corpus.load_appendix('good_slug', '../escape') is None
        assert corpus.load_catalogs('../escape') is None
        assert corpus.has_image('../escape', 'x.jpg') is False
        assert corpus.has_image('good_slug', '../x.jpg') is False
        assert '__invalid_slug__' in corpus.cover_path('../escape').parts
    finally:
        corpus.DATA_DIR = orig_dir
        corpus._books_cache = orig_books
        corpus._book_cache.clear(); corpus._book_cache.update(orig_book_cache)
        corpus._chunk_cache.clear(); corpus._chunk_cache.update(orig_chunk_cache)
