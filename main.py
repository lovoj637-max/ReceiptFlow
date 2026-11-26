from datetime import datetime, timedelta
import csv
import hashlib
import io
import logging
import os
import shutil
import uuid
from typing import List
from urllib.parse import urlsplit

import stripe
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import Base, engine, ensure_database_schema, get_db
from model import Document, PaymentMethod, User
from parser import process_receipt_image
from schames import (
    DocumentResponse,
    DocumentUpdate,
    EXPENSE_CATEGORIES,
    PaymentMethodCreate,
    PaymentMethodResponse,
    UserCreate,
    UserResponse,
)

app = FastAPI(title="ReceiptFlow")
logger = logging.getLogger(__name__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set in the environment")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 1440
UPLOAD_DIR = os.getenv("UPLOAD_DIR", os.path.join(os.getcwd(), "uploads"))

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/token")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ensure_database_schema()
Base.metadata.create_all(bind=engine)


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str | None = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise credentials_exception
    return user


@app.post("/api/v1/signup", response_model=UserResponse)
def create_user(user: UserCreate, db: Session = Depends(get_db)):
    existing_user = db.query(User).filter(User.email == user.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")

    new_user = User(email=user.email, hashed_password=get_password_hash(user.password))
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user


@app.post("/api/v1/token")
def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=401,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.email}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/api/v1/profile")
def get_profile(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    receipt_count = db.query(Document).filter(Document.user_id == current_user.id).count()
    plan = "Pro" if current_user.is_pro else "Starter"
    status_text = "Active" if current_user.is_pro else "Pending"
    default_method = (
        db.query(PaymentMethod)
        .filter(PaymentMethod.user_id == current_user.id, PaymentMethod.is_default.is_(True))
        .first()
    )

    return {
        "id": current_user.id,
        "name": current_user.email.split("@", 1)[0].title(),
        "email": current_user.email,
        "plan": plan,
        "usage": f"{receipt_count} / 20",
        "status": status_text,
        "billing_portal_available": bool(current_user.stripe_customer_id),
        "payment_method": (
            f"{default_method.brand} •••• {default_method.last4}"
            if default_method
            else None
        ),
    }


@app.post("/api/v1/payment-methods", response_model=PaymentMethodResponse)
def add_payment_method(
    method: PaymentMethodCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    last4 = method.last4.strip()
    if not last4.isdigit() or len(last4) != 4:
        raise HTTPException(status_code=400, detail="Card last 4 digits must be 4 numbers")

    if not 1 <= method.expiry_month <= 12:
        raise HTTPException(status_code=400, detail="Expiry month must be between 1 and 12")

    if len(str(method.expiry_year)) != 4:
        raise HTTPException(status_code=400, detail="Expiry year must be a 4-digit year")

    if method.is_default:
        db.query(PaymentMethod).filter(
            PaymentMethod.user_id == current_user.id,
            PaymentMethod.is_default.is_(True),
        ).update({"is_default": False})
    elif not db.query(PaymentMethod).filter(PaymentMethod.user_id == current_user.id).first():
        method.is_default = True

    payment_method = PaymentMethod(
        user_id=current_user.id,
        brand=method.brand.strip() or "Card",
        last4=last4,
        expiry_month=method.expiry_month,
        expiry_year=method.expiry_year,
        card_holder_name=method.card_holder_name.strip(),
        billing_zip=(method.billing_zip or "").strip() or None,
        is_default=method.is_default,
    )
    db.add(payment_method)
    db.commit()
    db.refresh(payment_method)
    return payment_method


@app.get("/api/v1/payment-methods", response_model=List[PaymentMethodResponse])
def get_payment_methods(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(PaymentMethod)
        .filter(PaymentMethod.user_id == current_user.id)
        .order_by(PaymentMethod.is_default.desc(), PaymentMethod.created_at.desc())
        .all()
    )


def _upload_sha256(file: UploadFile) -> str:
    digest = hashlib.sha256()
    file.file.seek(0)
    while True:
        chunk = file.file.read(64 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    file.file.seek(0)
    return digest.hexdigest()


def _file_sha256(file_path: str) -> str:
    digest = hashlib.sha256()
    with open(file_path, "rb") as source_file:
        while True:
            chunk = source_file.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _find_existing_receipt(
    db: Session,
    user_id: int,
    file_sha256: str,
) -> Document | None:
    existing_document = (
        db.query(Document)
        .filter(
            Document.user_id == user_id,
            Document.file_sha256 == file_sha256,
        )
        .first()
    )
    if existing_document is not None:
        return existing_document

    legacy_documents = (
        db.query(Document)
        .filter(
            Document.user_id == user_id,
            Document.file_sha256.is_(None),
        )
        .order_by(Document.id)
        .all()
    )
    for document in legacy_documents:
        try:
            existing_sha256 = _file_sha256(document.file_path)
        except FileNotFoundError:
            continue
        if existing_sha256 != file_sha256:
            continue

        document.file_sha256 = file_sha256
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing_document = (
                db.query(Document)
                .filter(
                    Document.user_id == user_id,
                    Document.file_sha256 == file_sha256,
                )
                .first()
            )
            if existing_document is None:
                raise
            return existing_document
        return document

    return None


def _receipt_response(document: Document) -> dict:
    return {
        "id": document.id,
        "filename": document.filename,
        "merchant": document.merchant,
        "total_amount": document.total_amount,
        "invoice_date": document.invoice_date,
        "raw_text": document.raw_text,
        "expense_category": document.expense_category,
        "business_purpose": document.business_purpose,
        "created_at": document.created_at,
    }


@app.post("/api/v1/upload-receipt")
def upload_receipt(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    file_sha256 = _upload_sha256(file)
    existing_document = _find_existing_receipt(
        db,
        current_user.id,
        file_sha256,
    )
    if existing_document is not None:
        return _receipt_response(existing_document)

    if not current_user.is_pro:
        receipt_count = db.query(Document).filter(Document.user_id == current_user.id).count()
        if receipt_count >= 5:
            raise HTTPException(
                status_code=403,
                detail="Free tier limit reached (5 receipts). Upgrade to Pro for unlimited uploads!",
            )

    os.makedirs(UPLOAD_DIR, exist_ok=True)

    filename = f"{uuid.uuid4()}_{file.filename or 'receipt'}"
    file_path = os.path.join(UPLOAD_DIR, filename)

    with open(file_path, "wb") as output_file:
        shutil.copyfileobj(file.file, output_file)

    try:
        parsed = process_receipt_image(file_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    document = Document(
        user_id=current_user.id,
        filename=file.filename or "receipt",
        file_path=file_path,
        file_sha256=file_sha256,
        total_amount=parsed.get("total_amount"),
        invoice_date=parsed.get("invoice_date"),
        raw_text=parsed.get("raw_text"),
    )
    db.add(document)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        try:
            os.remove(file_path)
        except FileNotFoundError:
            pass
        existing_document = (
            db.query(Document)
            .filter(
                Document.user_id == current_user.id,
                Document.file_sha256 == file_sha256,
            )
            .first()
        )
        if existing_document is None:
            raise
        return _receipt_response(existing_document)
    db.refresh(document)

    return _receipt_response(document)


@app.get("/api/v1/receipts", response_model=List[DocumentResponse])
def get_user_receipts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    documents = db.query(Document).filter(Document.user_id == current_user.id).all()
    return documents


@app.patch("/api/v1/receipts/{receipt_id}", response_model=DocumentResponse)
def update_receipt(
    receipt_id: int,
    updates: DocumentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    receipt = (
        db.query(Document)
        .filter(Document.id == receipt_id, Document.user_id == current_user.id)
        .first()
    )
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")

    fields = updates.model_dump(exclude_unset=True)
    if "expense_category" in fields:
        category = (fields["expense_category"] or "").strip() or "Uncategorized"
        if category not in EXPENSE_CATEGORIES:
            raise HTTPException(status_code=422, detail="Invalid expense category")
        fields["expense_category"] = category

    for field in ("merchant", "invoice_date", "business_purpose"):
        if field in fields and fields[field] is not None:
            fields[field] = fields[field].strip() or None

    for field, value in fields.items():
        setattr(receipt, field, value)

    db.commit()
    db.refresh(receipt)
    return receipt


def _parse_receipt_date(invoice_date: str | None) -> datetime | None:
    if not invoice_date:
        return None
    for date_format in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(invoice_date.strip(), date_format)
        except ValueError:
            continue
    return None


def _safe_csv_text(value: str | None) -> str:
    text_value = value or ""
    if text_value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + text_value
    return text_value


@app.get("/api/v1/receipts/export.csv")
def export_receipts_csv(
    tax_year: int = Query(..., ge=2000, le=2100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    documents = (
        db.query(Document)
        .filter(Document.user_id == current_user.id)
        .order_by(Document.id)
        .all()
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "Tax year",
            "Receipt date",
            "Merchant",
            "Amount",
            "Expense category",
            "Business purpose",
            "Filename",
            "Review needed",
        ]
    )

    for document in documents:
        parsed_date = _parse_receipt_date(document.invoice_date)
        if parsed_date is not None and parsed_date.year != tax_year:
            continue
        review_needed = (
            "" if parsed_date is not None
            else "Receipt date missing or unrecognized; assign a tax year"
        )
        writer.writerow(
            [
                str(tax_year) if parsed_date is not None else "",
                _safe_csv_text(document.invoice_date),
                _safe_csv_text(document.merchant),
                document.total_amount if document.total_amount is not None else "",
                _safe_csv_text(document.expense_category),
                _safe_csv_text(document.business_purpose),
                _safe_csv_text(document.filename),
                review_needed,
            ]
        )

    filename = f"receiptflow-expenses-{tax_year}.csv"
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/v1/receipts/{receipt_id}", response_model=DocumentResponse)
def get_receipt_by_id(
    receipt_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    receipt = (
        db.query(Document)
        .filter(Document.id == receipt_id, Document.user_id == current_user.id)
        .first()
    )
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return receipt


def _stripe_client() -> stripe.StripeClient:
    secret_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not secret_key:
        raise HTTPException(
            status_code=503,
            detail="Stripe payments are not configured",
        )
    return stripe.StripeClient(secret_key)


def _stripe_price_id() -> str:
    price_id = os.getenv("STRIPE_PRICE_ID", "").strip()
    if not price_id.startswith("price_"):
        raise HTTPException(
            status_code=503,
            detail="A valid Stripe subscription price is not configured",
        )
    return price_id


def _app_base_url() -> str:
    base_url = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
    parsed_url = urlsplit(base_url)
    if (
        not parsed_url.netloc
        or parsed_url.scheme not in {"http", "https"}
        or (
            parsed_url.scheme != "https"
            and parsed_url.hostname not in {"localhost", "127.0.0.1"}
        )
    ):
        raise HTTPException(
            status_code=503,
            detail="APP_BASE_URL must be a public HTTPS URL",
        )
    return base_url


@app.post("/api/v1/checkout")
def create_checkout(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.is_pro:
        raise HTTPException(status_code=409, detail="Subscription is already active")

    price_id = _stripe_price_id()
    client = _stripe_client()
    base_url = _app_base_url()

    try:
        customer_id = current_user.stripe_customer_id
        if not customer_id:
            customer = client.v1.customers.create(
                {
                    "email": current_user.email,
                    "metadata": {"user_id": str(current_user.id)},
                },
                {"idempotency_key": f"receiptflow-user-{current_user.id}"},
            )
            customer_id = customer.id
            current_user.stripe_customer_id = customer_id
            db.commit()

        session = client.v1.checkout.sessions.create(
            {
                "mode": "subscription",
                "customer": customer_id,
                "line_items": [{"price": price_id, "quantity": 1}],
                "client_reference_id": str(current_user.id),
                "metadata": {"user_id": str(current_user.id)},
                "subscription_data": {
                    "metadata": {"user_id": str(current_user.id)}
                },
                "success_url": (
                    f"{base_url}/?checkout=success"
                    "&session_id={CHECKOUT_SESSION_ID}"
                ),
                "cancel_url": f"{base_url}/?checkout=cancelled",
            },
            {"idempotency_key": f"receiptflow-checkout-{uuid.uuid4()}"},
        )
    except stripe.StripeError as exc:
        logger.exception("Stripe checkout session creation failed")
        raise HTTPException(
            status_code=502,
            detail="Stripe could not start checkout",
        ) from exc

    if not session.url:
        raise HTTPException(status_code=502, detail="Stripe returned no checkout URL")
    return {"checkout_url": session.url}


@app.post("/api/v1/billing-portal")
def create_billing_portal(
    current_user: User = Depends(get_current_user),
):
    if not current_user.stripe_customer_id:
        raise HTTPException(status_code=404, detail="No Stripe billing account found")

    client = _stripe_client()
    base_url = _app_base_url()
    try:
        session = client.v1.billing_portal.sessions.create(
            {
                "customer": current_user.stripe_customer_id,
                "return_url": base_url,
            }
        )
    except stripe.StripeError as exc:
        logger.exception("Stripe billing portal session creation failed")
        raise HTTPException(
            status_code=502,
            detail="Stripe could not open billing management",
        ) from exc

    return {"url": session.url}


def _stripe_object_id(value) -> str | None:
    if isinstance(value, str):
        return value
    return getattr(value, "id", None)


def _stripe_field(stripe_object, field: str, default=None):
    try:
        return stripe_object[field]
    except (KeyError, TypeError):
        return default


def _stripe_event_user(
    db: Session,
    stripe_object,
) -> User | None:
    metadata = _stripe_field(stripe_object, "metadata") or {}
    user_id = _stripe_field(metadata, "user_id")
    if not user_id:
        return None
    try:
        return db.query(User).filter(User.id == int(user_id)).first()
    except (TypeError, ValueError):
        return None


@app.post("/api/v1/stripe/webhook")
async def stripe_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        raise HTTPException(
            status_code=503,
            detail="Stripe webhooks are not configured",
        )

    signature = request.headers.get("stripe-signature")
    if not signature:
        raise HTTPException(status_code=400, detail="Missing Stripe signature")

    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, signature, webhook_secret)
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid Stripe webhook signature or payload",
        ) from exc

    event_type = event["type"]
    stripe_object = event["data"]["object"]
    customer_id = _stripe_object_id(_stripe_field(stripe_object, "customer"))

    if event_type == "checkout.session.completed":
        if _stripe_field(stripe_object, "mode") == "subscription":
            user = _stripe_event_user(db, stripe_object)
            if user is None:
                logger.warning("Completed Stripe checkout has no matching user")
            elif customer_id:
                if user.stripe_customer_id not in {None, customer_id}:
                    logger.error("Stripe customer does not match the checkout user")
                else:
                    user.stripe_customer_id = customer_id
                    db.commit()
    elif event_type in {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }:
        user = _stripe_event_user(db, stripe_object)
        if user is None:
            logger.warning("Stripe subscription event has no matching user")
        elif customer_id and user.stripe_customer_id not in {None, customer_id}:
            logger.error("Stripe customer does not match the subscription user")
        else:
            if customer_id:
                user.stripe_customer_id = customer_id
            user.is_pro = _stripe_field(stripe_object, "status") in {
                "active",
                "trialing",
            }
            db.commit()

    return {"received": True}


@app.get("/api/v1/me")
def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "email": current_user.email,
        "is_pro": current_user.is_pro,
    }
