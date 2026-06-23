from pathlib import Path

import pytest

from build import build_all, convert_images
from book_pipeline import apply_catalog_overrides, build_catalogs, extract_cover, parser, smoke, validate_rules


def test_convert_images_rejects_invalid_slug(tmp_path):
    orig_data = convert_images.DATA_DIR
    orig_out = convert_images.OUT
    try:
        convert_images.DATA_DIR = tmp_path / 'mineru_data'
        convert_images.OUT = tmp_path / 'img'
        assert convert_images._jobs_for('../escape') == []
        with pytest.raises(SystemExit):
            convert_images.main(['../escape'])
        assert not (tmp_path.parent / 'img').exists()
    finally:
        convert_images.DATA_DIR = orig_data
        convert_images.OUT = orig_out


def test_extract_cover_rejects_invalid_slug(tmp_path):
    orig_data = extract_cover.DATA_DIR
    try:
        extract_cover.DATA_DIR = tmp_path / 'mineru_data'
        assert extract_cover.find_pdf_for_slug('../escape') is None
        assert extract_cover.extract_one('../escape', tmp_path / 'missing.pdf') is None
        assert not (tmp_path / 'cover.jpg').exists()
    finally:
        extract_cover.DATA_DIR = orig_data


def test_extract_cover_audited_slugs_filters_invalid_dirs(tmp_path):
    orig_data = extract_cover.DATA_DIR
    try:
        extract_cover.DATA_DIR = tmp_path
        good = tmp_path / 'good_slug' / 'unified'
        bad = tmp_path / 'bad-slug' / 'unified'
        good.mkdir(parents=True)
        bad.mkdir(parents=True)
        (good / 'content_list.json').write_text('[]', encoding='utf-8')
        (bad / 'content_list.json').write_text('[]', encoding='utf-8')
        assert extract_cover._audited_slugs() == ['good_slug']
    finally:
        extract_cover.DATA_DIR = orig_data


def test_build_all_skips_invalid_slug_before_cover_path(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(build_all.ec, '_valid_slug', lambda slug: slug == 'good_slug')
    monkeypatch.setattr(build_all.ec, 'find_pdf_for_slug', lambda slug: seen.append(slug) or None)
    build_all._ensure_covers(['../escape', 'good_slug'])
    assert seen == ['good_slug']


def test_parser_rejects_invalid_slug_before_writing(tmp_path):
    orig_data = parser.DATA_DIR
    try:
        parser.DATA_DIR = tmp_path / 'mineru_data'
        with pytest.raises(SystemExit):
            parser.parse_book('../escape')
        assert not (tmp_path / 'escape').exists()
    finally:
        parser.DATA_DIR = orig_data


def test_validate_rules_rejects_invalid_slug_before_loading(monkeypatch, tmp_path):
    orig_data = validate_rules.DATA_DIR
    try:
        validate_rules.DATA_DIR = tmp_path / 'mineru_data'
        monkeypatch.setattr(validate_rules, 'load_unified', lambda slug: pytest.fail('should not load unified'))
        assert validate_rules.validate('../escape') == 1
    finally:
        validate_rules.DATA_DIR = orig_data


def test_smoke_rejects_invalid_slug_before_writing(tmp_path):
    orig_data = smoke.DATA_DIR
    try:
        smoke.DATA_DIR = tmp_path / 'mineru_data'
        assert smoke.smoke('../escape') == 1
        assert not (tmp_path / 'escape').exists()
    finally:
        smoke.DATA_DIR = orig_data


def test_build_catalogs_rejects_invalid_slug_before_writing(tmp_path):
    orig_data = build_catalogs.DATA_DIR
    try:
        build_catalogs.DATA_DIR = tmp_path / 'mineru_data'
        with pytest.raises(ValueError):
            build_catalogs.build_catalogs('../escape')
        assert not (tmp_path / 'escape').exists()
        assert build_catalogs._scan_chunk('good_slug', '../escape') == []
    finally:
        build_catalogs.DATA_DIR = orig_data


def test_apply_catalog_overrides_rejects_path_escape_inputs(tmp_path):
    orig_data = apply_catalog_overrides.DATA_DIR
    orig_override = apply_catalog_overrides.OVERRIDE_DIR
    try:
        apply_catalog_overrides.DATA_DIR = tmp_path / 'mineru_data'
        apply_catalog_overrides.OVERRIDE_DIR = tmp_path / 'catalog_overrides'
        with pytest.raises(ValueError):
            apply_catalog_overrides.apply_overrides('../escape')
        with pytest.raises(ValueError):
            apply_catalog_overrides._chunk_path('good_slug', '../escape')
        with pytest.raises(ValueError):
            apply_catalog_overrides._safe_image_filename('../escape.png')
        with pytest.raises(ValueError):
            apply_catalog_overrides._copy_solution_images('good_slug', {'from_slug': '../escape'})
        assert not (tmp_path / 'escape').exists()
    finally:
        apply_catalog_overrides.DATA_DIR = orig_data
        apply_catalog_overrides.OVERRIDE_DIR = orig_override
