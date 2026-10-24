"""The fingerprint of a cleaned text (duplicate_service.content_hash)."""
import hashlib

from app.services.duplicate_service import MIN_TEXT_FOR_HASH, content_hash


def test_a_text_gets_its_fingerprint_from_200_characters_on():
    text = "x" * MIN_TEXT_FOR_HASH

    assert content_hash(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert content_hash(text[:-1]) is None
    assert content_hash("") is None and content_hash(None) is None


def test_one_character_more_is_another_text():
    text = "Die Nationalbank belässt den Leitzins. " * 10

    assert content_hash(text) != content_hash(text + "!")
