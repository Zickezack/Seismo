"""Comparing texts without a database: shingles, the MinHash signature, its bands and the estimate."""
from app.services import similarity_service
from app.services.similarity_service import bands, shingles, signature, similarity

WORDS = [f"Wort{number}" for number in range(300)]


def text(changed=0):
    words = list(WORDS)
    for position in range(changed):
        words[(position * 37) % len(words)] = f"Anders{position}"
    return " ".join(words)


def jaccard(first, second):
    a, b = shingles(first), shingles(second)
    return len(a & b) / len(a | b)


def test_word_5_grams_in_lower_case_without_punctuation():
    assert shingles("Die EZB, laut Rat: Zins bleibt!") == {"die ezb laut rat zins", "ezb laut rat zins bleibt"}
    assert shingles("Zu kurz") == set()


def test_a_script_without_spaces_is_compared_by_its_characters():
    assert shingles("台风过境，造成停电。") == {"台风过境造", "风过境造成", "过境造成停", "境造成停电"}
    assert len(shingles("台風が通過し、停電が発生した。")) == 9  # 13 characters
    # Korean is written with spaces: by its words.
    korean = shingles("태풍이 지나가면서 많은 지역에 정전이 발생했다")
    assert korean == {"태풍이 지나가면서 많은 지역에 정전이", "지나가면서 많은 지역에 정전이 발생했다"}


def test_the_signature_is_the_same_on_every_run_and_none_for_a_short_text():
    assert signature(text()) == signature(text())
    assert len(signature(text())) == similarity_service.SIGNATURE_SIZE
    assert all(0 <= value < 2**31 for value in signature(text()))
    assert signature("x" * 199) is None and signature(None) is None


def test_the_estimate_follows_the_share_of_common_shingles():
    original = text()
    for changed in (3, 10, 40):
        copy = text(changed)
        # 128 values: an estimate within about three standard deviations.
        assert abs(similarity(signature(original), signature(copy)) - jaccard(original, copy)) < 0.13
    assert similarity(signature(original), signature(original)) == 1


def test_bands_hash_four_values_each_with_their_place():
    value = signature(text())

    assert len(bands(value)) == 32 and all(0 <= band < 2**63 for band in bands(value))
    # The same four values in another band are another band.
    shifted = value[4:8] + value[:4] + value[8:]
    assert bands(shifted)[0] != bands(value)[1]
    assert bands(value)[2:] == bands(shifted)[2:]
