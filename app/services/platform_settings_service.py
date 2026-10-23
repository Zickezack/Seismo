"""Plattform-Einstellungen: global for the instance, admin only (requirements.md, "Settings")."""
from collections.abc import Mapping
from dataclasses import dataclass, fields

from sqlalchemy import Connection, Engine

from app.repositories import platform_settings_repository
from app.services import tdm_policy


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

    @property
    def blocked_domains(self) -> list[str]:
        return self.tdm_blocked_domains.split()


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
    with engine.begin() as connection:
        if values:
            platform_settings_repository.save(connection, values)
        return load(connection)
