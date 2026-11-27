# ReceiptFlow

ReceiptFlow is a FastAPI application for uploading receipt images and extracting
text, totals, and dates with Tesseract OCR.

## Deploy on Render

1. Push this repository to GitHub.
2. In Render, create a Blueprint from the repository and apply `render.yaml`.
3. Wait for the API service and PostgreSQL database to finish deploying.

The Blueprint configures the API service, health check, generated `SECRET_KEY`,
PostgreSQL connection, and a persistent disk mounted at `/var/data` for uploaded
receipts. The service plan must support persistent disks.

- API health check: `/health`
- API documentation: `/docs`
- Web app: `/`

## Run locally

Install the requirements and Tesseract OCR, then set a `SECRET_KEY` and run:

```powershell
pip install -r requirements.txt
$env:SECRET_KEY = "use-a-local-development-secret"
uvicorn main:app --reload
```

The app uses `tracker.db` locally unless `DATABASE_URL` is set. Set
`TESSERACT_CMD` if the Tesseract executable is not on `PATH`.

Receipt uploads are deduplicated per account by exact image content. Uploading
the same image again returns the existing receipt; a cropped or re-encoded
image is treated as a separate upload.

Free accounts can store up to five unique receipts. Active or trialing Pro
subscriptions remove the upload limit.

## Tax organizer

Review each receipt to edit its merchant, date, expense category, and business
purpose. Export a tax-year CSV from the app or call
`GET /api/v1/receipts/export.csv?tax_year=YYYY`. Receipts with missing or
unrecognized dates are included and flagged for review rather than assigned to
a tax year. Categories are for organizing records; users should verify tax
treatment with a qualified tax professional.

## Deploy on Railway

1. Create a PostgreSQL database service in Railway and link it to the API service.
2. Add `SECRET_KEY` to the API service variables.
3. Railway will provide `DATABASE_URL` automatically from the linked PostgreSQL
   database. Do not paste `${{receiptflow-db.DATABASE_URL}}` as a literal value.
4. Optionally set `UPLOAD_DIR` to a persistent Railway volume mount path.
5. Set `RAILPACK_DEPLOY_APT_PACKAGES` to `tesseract-ocr` on the API service so
   the runtime image includes the Tesseract executable required for OCR.

`railway.toml` configures the build, start command, and `/health` check.

### Stripe subscriptions

1. In Stripe, create the Pro product and a recurring USD $5/month Price.
2. Add `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID`, and `APP_BASE_URL` to the API
   service variables. `APP_BASE_URL` must be the public HTTPS URL of the app.
3. Add a Stripe webhook endpoint at
   `https://YOUR_APP_HOST/api/v1/stripe/webhook` and subscribe it to
   `checkout.session.completed`, `customer.subscription.created`,
   `customer.subscription.updated`, and `customer.subscription.deleted`.
4. Copy that webhook's signing secret into the `STRIPE_WEBHOOK_SECRET` Railway
   variable. Enable the Stripe customer portal if users should manage or cancel
   their subscriptions.

Test with Stripe test-mode keys and a test Price before switching to live mode.
Never commit Stripe secrets or put them in browser code. Pro access is changed
only by verified Stripe subscription webhooks.
