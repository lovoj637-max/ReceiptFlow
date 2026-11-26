import os
import csv
import tempfile
import unittest
from io import BytesIO
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "test-secret"

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import UploadFile

import database
import main
from database import Base
from model import Document, User
from schames import DocumentUpdate


class ReceiptDeduplicationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.session = sessionmaker(bind=self.engine)()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_upload_dir = main.UPLOAD_DIR
        main.UPLOAD_DIR = self.temp_dir.name
        self.parser_patch = patch.object(
            main,
            "process_receipt_image",
            return_value={
                "total_amount": 12.5,
                "invoice_date": "11/24/2026",
                "raw_text": "Sample receipt",
            },
        )
        self.parser_patch.start()

    def tearDown(self):
        self.parser_patch.stop()
        main.UPLOAD_DIR = self.original_upload_dir
        self.session.close()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def create_user(self, email):
        user = User(email=email, hashed_password="test-hash", is_pro=True)
        self.session.add(user)
        self.session.commit()
        return user

    def upload(self, user, content, filename="receipt.png"):
        file = UploadFile(file=BytesIO(content), filename=filename)
        return main.upload_receipt(file=file, current_user=user, db=self.session)

    def test_repeated_upload_returns_existing_receipt(self):
        user = self.create_user("one@example.com")
        content = b"same image bytes"

        first = self.upload(user, content)
        second = self.upload(user, content, filename="renamed.png")

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["filename"], second["filename"])
        self.assertEqual(
            self.session.query(Document)
            .filter(Document.user_id == user.id)
            .count(),
            1,
        )

    def test_same_image_is_deduplicated_against_legacy_receipt(self):
        user = self.create_user("legacy@example.com")
        content = b"previously uploaded image"
        legacy_path = os.path.join(self.temp_dir.name, "legacy.png")
        with open(legacy_path, "wb") as saved_file:
            saved_file.write(content)
        legacy_document = Document(
            user_id=user.id,
            filename="legacy.png",
            file_path=legacy_path,
        )
        self.session.add(legacy_document)
        self.session.commit()
        self.parser_patch.stop()
        self.parser_patch = patch.object(main, "process_receipt_image")
        parser_mock = self.parser_patch.start()

        result = self.upload(user, content)

        self.assertEqual(result["id"], legacy_document.id)
        self.assertIsNotNone(legacy_document.file_sha256)
        parser_mock.assert_not_called()

    def test_same_image_for_different_users_creates_separate_receipts(self):
        first_user = self.create_user("first@example.com")
        second_user = self.create_user("second@example.com")
        content = b"same image bytes"

        first = self.upload(first_user, content)
        second = self.upload(second_user, content)

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.session.query(Document).count(), 2)

    def test_schema_migration_adds_hash_column_and_unique_index(self):
        legacy_engine = create_engine("sqlite://")
        with legacy_engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE users ("
                "id INTEGER PRIMARY KEY, email VARCHAR(255) NOT NULL, "
                "hashed_password VARCHAR(255) NOT NULL, created_at DATETIME)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE documents ("
                "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                "filename VARCHAR(255) NOT NULL, file_path VARCHAR(500) NOT NULL, "
                "total_amount FLOAT, invoice_date VARCHAR(50), raw_text TEXT, "
                "created_at DATETIME)"
            )

        try:
            with patch.object(database, "engine", legacy_engine):
                database.ensure_database_schema()
            inspector = inspect(legacy_engine)
            document_columns = {
                column["name"] for column in inspector.get_columns("documents")
            }
            document_indexes = inspector.get_indexes("documents")
            self.assertIn("file_sha256", document_columns)
            self.assertIn("merchant", document_columns)
            self.assertIn("expense_category", document_columns)
            self.assertIn("business_purpose", document_columns)
            self.assertTrue(
                any(
                    index["name"] == "uq_documents_user_file_sha256"
                    and index["unique"]
                    for index in document_indexes
                )
            )
        finally:
            legacy_engine.dispose()

    def test_receipt_details_can_be_updated_by_owner(self):
        user = self.create_user("owner@example.com")
        receipt = Document(
            user_id=user.id,
            filename="receipt.png",
            file_path="receipt.png",
        )
        self.session.add(receipt)
        self.session.commit()

        updated = main.update_receipt(
            receipt_id=receipt.id,
            updates=DocumentUpdate(
                merchant="Office Store",
                invoice_date="11/24/2026",
                expense_category="Office supplies",
                business_purpose="Printer paper for work",
            ),
            db=self.session,
            current_user=user,
        )

        self.assertEqual(updated.merchant, "Office Store")
        self.assertEqual(updated.expense_category, "Office supplies")
        self.assertEqual(updated.business_purpose, "Printer paper for work")

    def test_receipt_updates_are_scoped_to_owner(self):
        owner = self.create_user("owner@example.com")
        other_user = self.create_user("other@example.com")
        receipt = Document(
            user_id=owner.id,
            filename="receipt.png",
            file_path="receipt.png",
        )
        self.session.add(receipt)
        self.session.commit()

        with self.assertRaises(main.HTTPException) as error:
            main.update_receipt(
                receipt_id=receipt.id,
                updates=DocumentUpdate(expense_category="Supplies"),
                db=self.session,
                current_user=other_user,
            )

        self.assertEqual(error.exception.status_code, 404)

    def test_tax_year_csv_filters_dates_and_marks_unreadable_dates(self):
        user = self.create_user("export@example.com")
        self.session.add_all(
            [
                Document(
                    user_id=user.id,
                    filename="2026.png",
                    file_path="2026.png",
                    merchant="=SUM(A1:A2)",
                    invoice_date="11/24/2026",
                    total_amount=12.5,
                    expense_category="Supplies",
                ),
                Document(
                    user_id=user.id,
                    filename="2025.png",
                    file_path="2025.png",
                    invoice_date="11/24/2025",
                    total_amount=8.0,
                ),
                Document(
                    user_id=user.id,
                    filename="undated.png",
                    file_path="undated.png",
                    invoice_date=None,
                ),
                Document(
                    user_id=self.create_user("other@example.com").id,
                    filename="other-user.png",
                    file_path="other-user.png",
                    invoice_date="11/24/2026",
                ),
            ]
        )
        self.session.commit()

        response = main.export_receipts_csv(
            tax_year=2026,
            db=self.session,
            current_user=user,
        )
        rows = list(csv.reader(response.body.decode("utf-8").splitlines()))

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][2], "'=SUM(A1:A2)")
        self.assertEqual(rows[2][6], "undated.png")
        self.assertIn("assign a tax year", rows[2][7])


if __name__ == "__main__":
    unittest.main()
