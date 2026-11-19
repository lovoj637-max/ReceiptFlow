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
