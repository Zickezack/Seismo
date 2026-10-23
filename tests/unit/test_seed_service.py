"""The starter catalogue of Quellen as shipped in app/seeds/sources.json."""
from urllib.parse import urlsplit

from app.services import seed_service, source_service


def test_catalogue_passes_the_rules_of_the_source_form():
    # source_catalogue() checks every item with source_service.parse and raises on the first invalid one.
    catalogue = seed_service.source_catalogue()

    assert catalogue
    assert all(item.values.is_active for item in catalogue)


def test_catalogue_keys_and_names_are_unique():
    catalogue = seed_service.source_catalogue()

    keys = [item.key for item in catalogue]
    names = [item.values.name.casefold() for item in catalogue]
    assert len(set(keys)) == len(keys)
    assert len(set(names)) == len(names)


def test_catalogue_items_use_https_and_link_their_terms_of_use():
    for item in seed_service.source_catalogue():
        assert urlsplit(item.values.url).scheme == "https", item.key
        assert item.values.terms_note and "https://" in item.values.terms_note, item.key
        # A GDELT query spans every language: unknown, never a guess (datamodel.md, rule 5).
        if item.values.adapter_type == "gdelt":
            assert item.values.query and item.values.language is None, item.key
        else:
            assert item.values.language in source_service.LANGUAGES, item.key


def test_unset_fields_get_the_defaults_of_a_new_source():
    form = seed_service._form_of({"key": "k", "name": "N", "url": "https://example.org/feed"})

    assert form == {
        "adapter_type": "rss",
        "fetch_interval_minutes": "60",
        "raw_text_retention_days": "30",
        "language": "",
        "terms_note": "",
        "name": "N",
        "url": "https://example.org/feed",
        "is_active": "on",
    }
