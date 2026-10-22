import pytest

from app.repositories import platform_settings_repository
from app.services import platform_settings_service
from app.services.platform_settings_service import FORM_FIELDS, InvalidSetting, PlatformSettings


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("llm_max_parallel_calls", "1", 1),
        ("llm_max_parallel_calls", " 4 ", 4),
        ("llm_max_parallel_calls", "32", 32),
        ("succeeded_jobs_retention_days", "1", 1),
        ("failed_jobs_retention_days", "3650", 3650),
    ],
)
def test_valid_values(name, raw, expected):
    assert FORM_FIELDS[name].parse(raw) == expected


@pytest.mark.parametrize("raw", ["", "0", "33", "-1", "1.5", "zwei", "1e2"])
def test_invalid_values_are_rejected_with_a_message_naming_the_setting(raw):
    with pytest.raises(InvalidSetting) as error:
        FORM_FIELDS["llm_max_parallel_calls"].parse(raw)

    assert str(error.value) == "Parallele LLM-Aufrufe (höchstens): bitte eine ganze Zahl von 1 bis 32 eingeben."


def test_every_setting_has_a_form_field_and_every_default_fits_its_range():
    defaults = PlatformSettings()

    assert set(FORM_FIELDS) == set(defaults.__dict__)
    for name, field in FORM_FIELDS.items():
        assert field.minimum <= getattr(defaults, name) <= field.maximum


def test_save_names_every_invalid_value_and_writes_nothing(monkeypatch):
    monkeypatch.setattr(platform_settings_repository, "save", lambda *_: pytest.fail("saved despite an invalid value"))

    with pytest.raises(InvalidSetting) as error:
        platform_settings_service.save(None, {"succeeded_jobs_retention_days": "0", "failed_jobs_retention_days": "x"})

    assert str(error.value) == (
        "Erledigte Jobs aufbewahren (Tage): bitte eine ganze Zahl von 1 bis 3650 eingeben. "
        "Fehlgeschlagene Jobs aufbewahren (Tage): bitte eine ganze Zahl von 1 bis 3650 eingeben."
    )


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, PlatformSettings()),
        ({"llm_max_parallel_calls": 3}, PlatformSettings(llm_max_parallel_calls=3)),
        # A column of a newer or older code version must not break loading.
        ({"llm_max_parallel_calls": 2, "removed_setting": "x"}, PlatformSettings(llm_max_parallel_calls=2)),
    ],
)
def test_load_uses_the_default_for_every_setting_never_saved(monkeypatch, stored, expected):
    monkeypatch.setattr(platform_settings_repository, "load", lambda _connection: stored)

    assert platform_settings_service.load(None) == expected
