from sqlalchemy import Engine, create_engine

from app.config import Settings

CONNECT_TIMEOUT_SECONDS = 5


def create_db_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url(),
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )
