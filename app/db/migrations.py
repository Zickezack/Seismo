from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


def head_revision() -> str:
    """The newest migration in the code base - the revision a ready database must be at."""
    return ScriptDirectory.from_config(alembic_config()).get_current_head()
