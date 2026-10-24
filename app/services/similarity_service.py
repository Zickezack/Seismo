"""Similar articles: near-duplicates and syndicated copies linked to each other, never merged (userstories.md,
Normalisierung, Deduplizierung und Clustering, story 3).

A text is compared by its word 5-grams - character 5-grams for a script without spaces between words (Chinese,
Japanese, Thai) - and MinHash estimates the share two texts have in common (Jaccard). Two originals are linked from
MIN_SIMILARITY on, when published within LINK_WINDOW of each other - a weekly report on the same template is no copy -
and never two identical texts on one host (an error page, see duplicate_service). Every link is a measured pair: a
chain A~B~C does not link A and C. A pair the admin separated is never linked again.

The signature and its bands stay with the article's metadata, so a text is compared after its raw text is gone.
"""
import hashlib
import random
import re
from collections.abc import Iterable
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import Connection, Engine

from app.repositories import article_link_repository
from app.services.bulk import BulkResult
from app.services.duplicate_service import MIN_TEXT_FOR_HASH

SHINGLE_LENGTH = 5
SIGNATURE_SIZE = 128
# 32 bands of 4: a pair at 60 % shares a band - becomes a candidate - with 99 % probability, one at 30 % with 23 %.
BAND_ROWS = 4
MIN_SIMILARITY = 0.6
LINK_WINDOW = timedelta(days=7)
_WORD = re.compile(r"\w+")
# Scripts written without spaces between words: Thai, Lao, Myanmar, Khmer, Japanese kana, CJK ideographs.
_UNSPACED = re.compile("[\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_PRIME = (1 << 61) - 1
# Fixed for good: a stored signature is only comparable with one made by the same permutations.
_random = random.Random(20261024)
_PERMUTATIONS = [(_random.randrange(1, _PRIME), _random.randrange(0, _PRIME)) for _ in range(SIGNATURE_SIZE)]


def unspaced(text: str) -> bool:
    """Whether most of the text's letters are of a script without spaces between words: one of them says about as
    much as a word."""
    characters = "".join(_WORD.findall(text))
    return len(_UNSPACED.findall(characters)) * 2 > len(characters)


def shingles(text: str) -> set[str]:
    """The text's word 5-grams in lower case, without punctuation; character 5-grams when most of its letters are of
    a script without spaces between words."""
    words = _WORD.findall(text.lower())
    characters = "".join(words)
    if unspaced(characters):
        return {characters[i : i + SHINGLE_LENGTH] for i in range(len(characters) - SHINGLE_LENGTH + 1)}
    return {" ".join(words[i : i + SHINGLE_LENGTH]) for i in range(len(words) - SHINGLE_LENGTH + 1)}


def signature(text: str | None) -> list[int] | None:
    """The text's MinHash: per permutation the least hash of its shingles, 31 bits (a PostgreSQL integer). None for a
    text shorter than a fingerprint's (duplicate_service.MIN_TEXT_FOR_HASH)."""
    if not text or len(text) < MIN_TEXT_FOR_HASH:
        return None
    hashes = [_hash(gram.encode()) for gram in shingles(text)]
    if not hashes:
        return None
    return [min((a * value + b) % _PRIME for value in hashes) & 0x7FFFFFFF for a, b in _PERMUTATIONS]


def bands(text_signature: list[int]) -> list[int]:
    """A hash per band of BAND_ROWS values, with the band's number in it, 63 bits (a PostgreSQL bigint)."""
    return [
        _hash(repr((start, text_signature[start : start + BAND_ROWS])).encode()) >> 1
        for start in range(0, len(text_signature), BAND_ROWS)
    ]


def similarity(first: list[int], second: list[int]) -> float:
    """The estimated share of shingles two texts have in common: the share of equal signature values."""
    return sum(a == b for a, b in zip(first, second, strict=True)) / len(first)


def _hash(data: bytes) -> int:
    return int.from_bytes(hashlib.blake2b(data, digest_size=8).digest())


def fingerprint(text: str | None) -> dict:
    """The article's columns for its text: signature and bands, both empty for a text too short to compare."""
    text_signature = signature(text)
    return {"text_signature": text_signature, "text_bands": bands(text_signature) if text_signature else None}


def link(connection: Connection, article_id: int) -> int:
    """After normalization and its duplicate check, in their transaction: link the article to the originals with a
    similar text. A duplicate gets none, its original has them. Returns how many links were added."""
    article = article_link_repository.get_for_links(connection, article_id)
    if article.duplicate_of_id is not None or not article.text_bands:
        return 0
    start, end = article.moment - LINK_WINDOW, article.moment + LINK_WINDOW
    host = urlsplit(article.canonical_url or "").hostname
    added = 0
    for other in article_link_repository.candidates(connection, article_id, article.text_bands, start, end):
        same_text = article.content_hash and other.content_hash == article.content_hash
        if same_text and urlsplit(other.canonical_url or "").hostname == host:
            continue  # the same text on one host under another canonical URL: no report (duplicate_service.check)
        value = similarity(article.text_signature, other.text_signature)
        if value >= MIN_SIMILARITY:
            article_link_repository.insert_link(connection, article_id, other.id, value)
            added += 1
    return added


def separate(engine: Engine, article_id: int, other_ids: Iterable[int]) -> BulkResult:
    """The admin's word: these articles are unrelated to this one, never linked again and never duplicates of each
    other. All or nothing (requirements.md, "Destructive and Bulk Actions"): one not linked any more blocks it."""
    other_ids = sorted(set(other_ids))
    if not other_ids:
        return BulkResult(processed=0, blocked=[])
    with engine.begin() as connection:
        linked = article_link_repository.lock_linked(connection, article_id, other_ids)
        blocked = [f"Artikel #{other_id} (nicht verknüpft)" for other_id in other_ids if other_id not in linked]
        if blocked:
            return BulkResult(processed=0, blocked=blocked)
        for other_id in other_ids:
            article_link_repository.separate(connection, article_id, other_id)
    return BulkResult(processed=len(other_ids), blocked=[])
