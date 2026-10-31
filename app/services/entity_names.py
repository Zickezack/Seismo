"""Name forms and identifiers of entities (userstories.md, Entitäten und Wissensmodell, story 1): the key an alias is
looked up by, and the checks an identifier must pass before it names an entity.

A name's key is what two spellings of one name have in common: case, accents, punctuation, a leading "the" and a
trailing legal form left out - "The Boeing Company" and "Boeing Co." are "boeing", "U.S. Steel" and "US Steel" are
"us steel". A transliteration ("Vladimir" for "Volodymyr") is no spelling: the model names the entity by its usual
English name, and similar keys are only candidates it judges (entity_service).
"""
import re
import unicodedata

from app.db.models import AliasScheme

_WORD = re.compile(r"[^\W_]+")
# Left out before the words are split: "U.S." is "us", "Lowe's" "lowes", "S.p.A." "spa".
_JOINING = str.maketrans("", "", ".'’")
# Legal forms at a name's end, in lower case and without dots.
LEGAL_FORMS = frozenset(
    "inc incorporated corp corporation co company ltd limited plc llc llp lp ag gmbh kg kgaa sa sas sarl srl spa se nv "
    "bv ab asa as oyj kk ltda pty bhd tbk".split()
)


def name_key(name: str) -> str:
    """What a name is looked up and compared by: without accents, case, punctuation, a leading "the" and trailing legal
    forms. Empty for a name without a letter or digit."""
    text = "".join(char for char in unicodedata.normalize("NFKD", name) if not unicodedata.combining(char))
    words = _WORD.findall(text.casefold().translate(_JOINING).replace("&", " and "))
    if len(words) > 1 and words[0] == "the":
        words = words[1:]
    while len(words) > 1 and words[-1] in LEGAL_FORMS:
        words.pop()
    return " ".join(words)


# ISO 3166-1 alpha-2: the officially assigned codes, and XK for Kosovo, which the EU and the World Bank use.
COUNTRY_CODES = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF
CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH
GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW
KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG
NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN
SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT
ZA ZM ZW XK
""".split())
# Reserved for the United Kingdom and Greece, and written for them often: the assigned code instead.
_COUNTRY_SUBSTITUTES = {"UK": "GB", "EL": "GR"}
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_LEI = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")
_TICKER = re.compile(r"^([A-Z][A-Z0-9]{0,9}):([A-Z0-9][A-Z0-9.\-]{0,11})$")


def country_code(code: str | None) -> str | None:
    """The ISO 3166-1 alpha-2 code, or None for one that is none."""
    if not code:
        return None
    code = _COUNTRY_SUBSTITUTES.get(code.upper(), code.upper())
    return code if code in COUNTRY_CODES else None


def identifier_key(scheme: str, value: str) -> str | None:
    """The identifier in its one spelling - upper case, without spaces; a ticker as EXCHANGE:SYMBOL - or None when it
    is not a valid one of its scheme: an ISIN or LEI with a wrong check digit names nothing."""
    value = "".join(value.split()).upper()
    if scheme == AliasScheme.ISIN:
        return value if _ISIN.match(value) and _luhn_valid(_digits(value)) else None
    if scheme == AliasScheme.LEI:
        # ISO 17442: the number the characters make, modulo 97, is 1.
        return value if _LEI.match(value) and int(_digits(value)) % 97 == 1 else None
    if scheme == AliasScheme.TICKER:
        return value if _TICKER.match(value) else None
    return None


def stated_in(scheme: str, key: str, quote: str) -> bool:
    """Whether the quote states the identifier: an ISIN or LEI as it is, spaces aside; a ticker's exchange and symbol,
    the symbol as a word of its own - "(NYSE: BA)". An identifier the model knows but the report does not state is a
    guess (datamodel.md, rule 5)."""
    if scheme == AliasScheme.TICKER:
        exchange, symbol = key.split(":", 1)
        upper = quote.upper()
        return exchange in upper and re.search(rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])", upper) is not None
    return key in "".join(quote.split()).upper()


def _digits(value: str) -> str:
    """Letters as numbers, A 10 to Z 35, as ISIN and LEI compute their check digits."""
    return "".join(str(int(char, 36)) for char in value)


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char) * (2 if index % 2 else 1)
        total += number - 9 if number > 9 else number
    return total % 10 == 0
