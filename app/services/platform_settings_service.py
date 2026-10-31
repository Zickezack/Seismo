"""Plattform-Einstellungen: global for the instance, admin only (requirements.md, "Settings")."""
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields

from sqlalchemy import Connection, Engine

from app.repositories import platform_settings_repository
from app.services import tdm_policy


# A quality per domain, for the catalogue's publishers (admin's decision of 2026-10-24): official bodies high,
# established media medium, press release portals - a company's own word - low. Everything else stays unknown.
STARTER_SOURCE_QUALITY = {
    "hoch": (
        "admin.ch", "bankofcanada.ca", "bankofengland.co.uk", "bis.org", "bls.gov", "bundesbank.de", "destatis.de",
        "ec.europa.eu", "ecb.europa.eu", "federalreserve.gov", "noaa.gov", "sec.gov", "snb.ch", "usgs.gov", "who.int",
        "wto.org",
    ),
    "mittel": (
        "abc.net.au", "africanews.com", "allafrica.com", "bleepingcomputer.com", "dw.com", "ebc.com.br", "gcaptain.com",
        "meduza.io", "mercopress.com", "middleeasteye.net", "splash247.com", "thehindu.com", "ukrinform.net",
    ),
    "niedrig": ("cision.com", "presseportal.de"),
}
QUALITY_WORDS = {"hoch": "high", "mittel": "medium", "niedrig": "low"}


def _quality_lines(by_word: Mapping[str, tuple[str, ...]]) -> str:
    return "\n".join(sorted(f"{domain} {word}" for word, domains in by_word.items() for domain in domains))


class InvalidSetting(ValueError):
    """Invalid values from the settings form. The message is shown to the admin as is."""


@dataclass(frozen=True)
class PlatformSettings:
    # One call at a time suits the GTX 1080 of stage 1 (architecture.md, "Stages and hardware sizing").
    llm_max_parallel_calls: int = 1
    succeeded_jobs_retention_days: int = 7
    failed_jobs_retention_days: int = 30
    backup_interval_hours: int = 24
    backups_to_keep: int = 14
    article_max_age_days: int = 7
    domain_min_interval_seconds: int = 10
    # One domain per line; a full text from it or a subdomain is not fetched (tdm_policy).
    tdm_blocked_domains: str = "\n".join(tdm_policy.STARTER_BLOCKED_DOMAINS)
    # The event extraction's pre-filter and budget (extraction_service): ISO 639-1 codes, comma-separated.
    extraction_languages: str = "de,en,es,fr,it,nl,pt"
    extraction_min_text_chars: int = 500
    extraction_max_age_days: int = 3
    extractions_per_hour: int = 30
    # The reprocessing's budget (reprocessing_service), apart from the extraction's.
    reprocessings_per_hour: int = 30
    # "domain level" per line, level hoch, mittel or niedrig; a domain covers its subdomains (extraction_service).
    source_quality_domains: str = _quality_lines(STARTER_SOURCE_QUALITY)

    @property
    def blocked_domains(self) -> list[str]:
        return self.tdm_blocked_domains.split()

    @property
    def extraction_language_codes(self) -> list[str]:
        return self.extraction_languages.split(",")

    @property
    def source_quality_levels(self) -> dict[str, str]:
        """Domain to level (high, medium, low)."""
        pairs = (line.split() for line in self.source_quality_domains.splitlines() if line.strip())
        return {domain: QUALITY_WORDS[word] for domain, word in pairs}


@dataclass(frozen=True)
class NumberField:
    """A whole-number setting as the form shows and checks it."""

    name: str
    label: str
    hint: str
    minimum: int
    maximum: int
    kind = "number"

    @property
    def error(self) -> str:
        return f"{self.label}: bitte eine ganze Zahl von {self.minimum} bis {self.maximum} eingeben."

    def parse(self, raw: str) -> int:
        try:
            value = int(raw.strip())
        except ValueError:
            raise InvalidSetting(self.error) from None
        if not self.minimum <= value <= self.maximum:
            raise InvalidSetting(self.error)
        return value


@dataclass(frozen=True)
class DomainListField:
    """A list of domains, one per line, as the form shows and checks it; stored sorted, each once."""

    name: str
    label: str
    hint: str
    kind = "domains"

    def parse(self, raw: str) -> str:
        try:
            return "\n".join(tdm_policy.parse_domains(raw))
        except ValueError as error:
            raise InvalidSetting(f"{self.label}: {error}.") from None


@dataclass(frozen=True)
class LanguageListField:
    """Language codes (ISO 639-1, or 639-3 for one without), separated by commas or spaces; stored sorted, each once."""

    name: str
    label: str
    hint: str
    kind = "text"

    def parse(self, raw: str) -> str:
        codes = sorted({code.lower() for code in re.split(r"[\s,;]+", raw) if code})
        invalid = [code for code in codes if not re.fullmatch(r"[a-z]{2,3}", code)]
        if invalid or not codes:
            raise InvalidSetting(f"{self.label}: bitte Sprachcodes wie de, en, fr angeben, durch Kommas getrennt.")
        return ",".join(codes)


@dataclass(frozen=True)
class QualityListField:
    """Domains with a quality, "domain level" per line; stored sorted by domain, each once, the level as a word."""

    name: str
    label: str
    hint: str
    kind = "quality"

    def parse(self, raw: str) -> str:
        levels, invalid = {}, []
        for line in raw.splitlines():
            parts = line.split()
            if not parts:
                continue
            word = parts[-1].lower() if len(parts) == 2 else ""
            word = {english: german for german, english in QUALITY_WORDS.items()}.get(word, word)
            try:
                [domain] = tdm_policy.parse_domains(parts[0]) if word in QUALITY_WORDS else []
            except ValueError:
                domain = None
            if domain is None:
                invalid.append(line.strip())
            else:
                levels[domain] = word
        if invalid:
            shown = ", ".join(invalid[:5]) + (" …" if len(invalid) > 5 else "")
            raise InvalidSetting(f"{self.label}: je Zeile eine Domain und hoch, mittel oder niedrig, nicht: {shown}.")
        return "\n".join(f"{domain} {word}" for domain, word in sorted(levels.items()))


FORM_FIELDS = {
    field.name: field
    for field in (
        NumberField(
            "llm_max_parallel_calls",
            "Parallele LLM-Aufrufe (höchstens)",
            "Gilt für alle Worker zusammen. Eine Senkung wirkt, sobald laufende Aufrufe beendet sind.",
            1,
            32,
        ),
        NumberField(
            "article_max_age_days",
            "Höchstalter neuer Artikel (Tage)",
            "Ältere Einträge übernimmt der laufende Abruf nicht, sie gehören zum Backfill. Einträge ohne Datum kommen immer.",
            1,
            365,
        ),
        NumberField(
            "domain_min_interval_seconds",
            "Mindestabstand pro Domain (Sekunden)",
            "Zwischen zwei Anfragen an denselben Host, für Feeds und Volltexte. Ein längerer Crawl-delay aus robots.txt gilt immer.",
            1,
            600,
        ),
        DomainListField(
            "tdm_blocked_domains",
            "Domains ohne Volltext",
            "Eine Domain pro Zeile, gilt auch für ihre Subdomains: deren Bedingungen verbieten Text- und Data-Mining, "
            "ihre Artikel bleiben ohne Volltext. Ein maschinenlesbarer Vorbehalt (TDMRep) gilt zusätzlich, für jede Domain.",
        ),
        LanguageListField(
            "extraction_languages",
            "Sprachen",
            "Nur Artikel in diesen Sprachen gehen an das LLM, erkannt am Text, sonst wie die Quelle sie angibt. Codes wie de, en, fr.",
        ),
        NumberField(
            "extraction_min_text_chars",
            "Mindestlänge des Texts (Zeichen)",
            "Kürzere Texte tragen selten ein Ereignis mit Belegen.",
            0,
            100000,
        ),
        NumberField(
            "extraction_max_age_days",
            "Höchstalter (Tage)",
            "Ab der Veröffentlichung, sonst ab dem Abruf. Ältere Artikel extrahiert der Worker nicht mehr.",
            1,
            30,
        ),
        QualityListField(
            "source_quality_domains",
            "Quellenqualität nach Domain",
            "Je Zeile eine Domain und hoch, mittel oder niedrig; gilt auch für ihre Subdomains, die genaueste zuerst. "
            "Sonst gilt die Qualität der Quelle des Artikels, sonst bleibt sie unbekannt.",
        ),
        NumberField(
            "extractions_per_hour",
            "Extraktionen pro Stunde (höchstens)",
            "Die neuesten zuerst; was nicht drankommt, wartet, bis es zu alt ist. 0 hält die Extraktion an.",
            0,
            1000,
        ),
        NumberField(
            "reprocessings_per_hour",
            "Neuverarbeitungen pro Stunde (höchstens)",
            "Extraktionen und Entitätenerkennungen einer Neuverarbeitung (Datenbestand), immer nach der laufenden Arbeit. "
            "0 hält die Neuverarbeitung an.",
            0,
            1000,
        ),
        NumberField(
            "succeeded_jobs_retention_days", "Erledigte Jobs aufbewahren (Tage)", "Danach löscht der Worker sie.", 1, 3650
        ),
        NumberField(
            "failed_jobs_retention_days",
            "Fehlgeschlagene Jobs aufbewahren (Tage)",
            "Ab dem Fehlschlag. Ein erneut eingereihter Job ist davon ausgenommen.",
            1,
            3650,
        ),
        NumberField(
            "backup_interval_hours",
            "Backup-Intervall (Stunden)",
            "Zeit vom letzten Backup bis zum nächsten automatischen Backup.",
            1,
            720,
        ),
        NumberField(
            "backups_to_keep",
            "Backups aufbewahren (Anzahl)",
            "Nach jedem neuen Backup löscht der Worker die ältesten darüber hinaus.",
            1,
            365,
        ),
    )
}


def load(connection: Connection) -> PlatformSettings:
    """The stored values; every setting never saved has its default."""
    stored = platform_settings_repository.load(connection) or {}
    known = {field.name for field in fields(PlatformSettings)}
    return PlatformSettings(**{name: value for name, value in stored.items() if name in known and value is not None})


def current(engine: Engine) -> PlatformSettings:
    with engine.connect() as connection:
        return load(connection)


def save(engine: Engine, form: Mapping[str, str]) -> PlatformSettings:
    """Save the settings a form sent; other settings stay as they are. One invalid value changes nothing."""
    values = parse(form)
    with engine.begin() as connection:
        store(connection, values)
        return load(connection)


def store(connection: Connection, values: Mapping[str, object]) -> None:
    """Write checked values (from parse) inside the caller's transaction."""
    if values:
        platform_settings_repository.save(connection, dict(values))


def parse(form: Mapping[str, str]) -> dict[str, object]:
    """Check the settings among the form's fields, others are ignored; all errors together in one InvalidSetting."""
    values, errors = {}, []
    for name, raw in form.items():
        field = FORM_FIELDS.get(name)
        if field is None:
            continue
        try:
            values[name] = field.parse(raw)
        except InvalidSetting as error:
            errors.append(str(error))
    if errors:
        raise InvalidSetting(" ".join(errors))
    return values
