import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./tracker.db")
if SQLALCHEMY_DATABASE_URL.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace(
        "postgres://", "postgresql://", 1
    )

engine_options = {"pool_pre_ping": True}
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}
engine = create_engine(SQLALCHEMY_DATABASE_URL, **engine_options)

# 3. Session local factory for handling database transactions
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def ensure_database_schema():
    inspector = inspect(engine)
    if not inspector.has_table("users"):
        return

    with engine.begin() as connection:
        existing_user_columns = {
            column["name"] for column in inspect(connection).get_columns("users")
        }
        migrations = {
            "stripe_customer_id": "VARCHAR(255)",
            "is_pro": "BOOLEAN DEFAULT FALSE",
        }
        for column, definition in migrations.items():
            if column not in existing_user_columns:
                connection.execute(
                    text(f"ALTER TABLE users ADD COLUMN {column} {definition}")
                )


# 5. Dependency to get DB session in FastAPI routes
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()