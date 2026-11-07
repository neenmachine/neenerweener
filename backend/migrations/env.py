from logging.config import fileConfig
import os

from alembic import context

from timeline import database_engine, metadata

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)


def run_migrations_offline() -> None:
    context.configure(
        url=os.environ["DATABASE_URL"],
        target_metadata=metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = database_engine(os.environ["DATABASE_URL"])
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
