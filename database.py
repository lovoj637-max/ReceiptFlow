import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

# Railway injects DATABASE_URL for linked PostgreSQL services. Keep SQLite as
# a local-development fallback.
SQLALCHEMY_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./tracker.db",
).strip()
if SQLALCHEMY_DATABASE_URL.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace(
        "postgres://",
        "postgresql://",
        1,
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
    existing_tables = set(inspector.get_table_names())
    if not existing_tables:
        return

    with engine.begin() as connection:
        connection_inspector = inspect(connection)
        if "users" in existing_tables:
            existing_user_columns = {
                column["name"]
                for column in connection_inspector.get_columns("users")
            }
            user_migrations = {
                "stripe_customer_id": "VARCHAR(255)",
                "is_pro": "BOOLEAN DEFAULT FALSE",
            }
            for column, definition in user_migrations.items():
                if column not in existing_user_columns:
                    connection.execute(
                        text(f"ALTER TABLE users ADD COLUMN {column} {definition}")
                    )

        if "documents" in existing_tables:
            existing_document_columns = {
                column["name"]
                for column in connection_inspector.get_columns("documents")
            }
            document_migrations = {
                "file_sha256": "VARCHAR(64)",
                "merchant": "VARCHAR(255)",
                "expense_category": (
                    "VARCHAR(64) NOT NULL DEFAULT 'Uncategorized'"
                ),
                "business_purpose": "TEXT",
            }
            for column, definition in document_migrations.items():
                if column not in existing_document_columns:
                    connection.execute(
                        text(
                            f"ALTER TABLE documents ADD COLUMN "
                            f"{column} {definition}"
                        )
                    )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_documents_user_file_sha256 "
                    "ON documents (user_id, file_sha256)"
                )
            )


# 5. Dependency to get DB session in FastAPI routes
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()