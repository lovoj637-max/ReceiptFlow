import asyncio
import os
import csv
import hashlib
import hmac
import json
import tempfile
import time
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "test-secret"

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request
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

    def create_user(self, email, is_pro=True):
        user = User(
            email=email,
            hashed_password="test-hash",
            is_pro=is_pro,
        )
        self.session.add(user)
        self.session.commit()
        return user

    def stripe_request(self, event, secret, valid_signature=True):
        payload = json.dumps(event, separators=(",", ":")).encode()
        timestamp = int(time.time())
        signed_payload = str(timestamp).encode() + b"." + payload
        signature = hmac.new(
            secret.encode(),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()
        if not valid_signature:
            signature = "0" * 64
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/stripe/webhook",
            "headers": [
                (
                    b"stripe-signature",
                    f"t={timestamp},v1={signature}".encode(),
                )
            ],
        }

        async def receive():
            return {
                "type": "http.request",
                "body": payload,
                "more_body": False,
            }

        return Request(scope, receive)

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

    def test_free_user_can_keep_five_unique_receipts_but_not_six(self):
        user = self.create_user("free-limit@example.com", is_pro=False)
        receipts = []
        for index in range(5):
            content = f"existing-receipt-{index}".encode()
            receipts.append(
                Document(
                    user_id=user.id,
                    filename=f"receipt-{index}.png",
                    file_path=f"receipt-{index}.png",
                    file_sha256=hashlib.sha256(content).hexdigest(),
                )
            )
        self.session.add_all(receipts)
        self.session.commit()

        duplicate = self.upload(user, b"existing-receipt-0")
        self.assertEqual(duplicate["id"], receipts[0].id)

        with self.assertRaises(main.HTTPException) as error:
            self.upload(user, b"sixth-unique-receipt")

        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(
            self.session.query(Document)
            .filter(Document.user_id == user.id)
            .count(),
            5,
        )

    def test_pro_user_can_upload_more_than_five_receipts(self):
        user = self.create_user("pro-limit@example.com", is_pro=True)
        for index in range(5):
            self.session.add(
                Document(
                    user_id=user.id,
                    filename=f"receipt-{index}.png",
                    file_path=f"receipt-{index}.png",
                    file_sha256=hashlib.sha256(
                        f"existing-{index}".encode()
                    ).hexdigest(),
                )
            )
        self.session.commit()

        self.upload(user, b"sixth-pro-receipt")

        self.assertEqual(
            self.session.query(Document)
            .filter(Document.user_id == user.id)
            .count(),
            6,
        )

    def test_profile_reports_plan_specific_receipt_limits(self):
        free_user = self.create_user("free-profile@example.com", is_pro=False)
        pro_user = self.create_user("pro-profile@example.com", is_pro=True)

        free_profile = main.get_profile(current_user=free_user, db=self.session)
        pro_profile = main.get_profile(current_user=pro_user, db=self.session)

        self.assertEqual(free_profile["usage"], "0 / 5")
        self.assertEqual(pro_profile["usage"], "0 / Unlimited")

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

    def test_checkout_creates_customer_and_subscription_session(self):
        user = self.create_user("checkout@example.com", is_pro=False)
        client = Mock()
        client.v1.customers.create.return_value = SimpleNamespace(id="cus_test")
        client.v1.checkout.sessions.create.return_value = SimpleNamespace(
            url="https://checkout.stripe.test/session"
        )
        settings = {
            "STRIPE_SECRET_KEY": "sk_test_example",
            "STRIPE_PRICE_ID": "price_test",
            "APP_BASE_URL": "https://receiptflow.example",
        }

        with patch.dict(os.environ, settings), patch(
            "main.stripe.StripeClient",
            return_value=client,
        ):
            result = main.create_checkout(current_user=user, db=self.session)

        self.assertEqual(result["checkout_url"], "https://checkout.stripe.test/session")
        self.assertEqual(user.stripe_customer_id, "cus_test")
        checkout_params = client.v1.checkout.sessions.create.call_args.args[0]
        self.assertEqual(checkout_params["mode"], "subscription")
        self.assertEqual(
            checkout_params["line_items"],
            [{"price": "price_test", "quantity": 1}],
        )
        self.assertEqual(
            checkout_params["subscription_data"]["metadata"]["user_id"],
            str(user.id),
        )

    def test_checkout_requires_stripe_configuration(self):
        user = self.create_user("unconfigured@example.com", is_pro=False)
        settings = {
            "STRIPE_SECRET_KEY": "",
            "STRIPE_PRICE_ID": "",
            "APP_BASE_URL": "",
        }

        with patch.dict(os.environ, settings):
            with self.assertRaises(main.HTTPException) as error:
                main.create_checkout(current_user=user, db=self.session)

        self.assertEqual(error.exception.status_code, 503)

    def test_billing_portal_uses_existing_customer(self):
        user = self.create_user("portal@example.com")
        user.stripe_customer_id = "cus_test"
        self.session.commit()
        client = Mock()
        client.v1.billing_portal.sessions.create.return_value = SimpleNamespace(
            url="https://billing.stripe.test/portal"
        )
        settings = {
            "STRIPE_SECRET_KEY": "sk_test_example",
            "APP_BASE_URL": "https://receiptflow.example",
        }

        with patch.dict(os.environ, settings), patch(
            "main.stripe.StripeClient",
            return_value=client,
        ):
            result = main.create_billing_portal(current_user=user)

        self.assertEqual(result["url"], "https://billing.stripe.test/portal")
        portal_params = client.v1.billing_portal.sessions.create.call_args.args[0]
        self.assertEqual(portal_params["customer"], "cus_test")

    def test_signed_subscription_webhooks_update_pro_access(self):
        user = self.create_user("webhook@example.com", is_pro=False)
        secret = "whsec_test"
        settings = {
            "STRIPE_WEBHOOK_SECRET": secret,
            "STRIPE_PRICE_ID": "price_test",
        }
        checkout_event = {
            "id": "evt_checkout",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "customer": "cus_test",
                    "mode": "subscription",
                    "metadata": {"user_id": str(user.id)},
                }
            },
        }
        subscription = {
            "customer": "cus_test",
            "status": "active",
            "metadata": {"user_id": str(user.id)},
        }
        active_event = {
            "id": "evt_active",
            "type": "customer.subscription.updated",
            "data": {"object": subscription},
        }
        canceled_event = {
            "id": "evt_canceled",
            "type": "customer.subscription.deleted",
            "data": {
                "object": {
                    **subscription,
                    "status": "canceled",
                }
            },
        }

        with patch.dict(os.environ, settings):
            asyncio.run(
                main.stripe_webhook(
                    self.stripe_request(checkout_event, secret),
                    db=self.session,
                )
            )
            self.assertEqual(user.stripe_customer_id, "cus_test")
            self.assertFalse(user.is_pro)

            asyncio.run(
                main.stripe_webhook(
                    self.stripe_request(active_event, secret),
                    db=self.session,
                )
            )
            self.assertTrue(user.is_pro)
            self.assertEqual(user.stripe_customer_id, "cus_test")

            asyncio.run(
                main.stripe_webhook(
                    self.stripe_request(canceled_event, secret),
                    db=self.session,
                )
            )

        self.assertFalse(user.is_pro)

    def test_webhook_rejects_invalid_signature(self):
        user = self.create_user("signature@example.com", is_pro=False)
        event = {
            "id": "evt_invalid",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "customer": "cus_test",
                    "status": "active",
                    "metadata": {"user_id": str(user.id)},
                }
            },
        }

        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": "whsec_test"}):
            with self.assertRaises(main.HTTPException) as error:
                asyncio.run(
                    main.stripe_webhook(
                        self.stripe_request(
                            event,
                            "whsec_test",
                            valid_signature=False,
                        ),
                        db=self.session,
                    )
                )

        self.assertEqual(error.exception.status_code, 400)
        self.assertFalse(user.is_pro)


if __name__ == "__main__":
    unittest.main()
