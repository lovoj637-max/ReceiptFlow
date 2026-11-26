from datetime import datetime, timedelta
import os
import shutil
import uuid
from typing import List

from fastapi import Body, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from database import Base, engine, ensure_database_schema, get_db
from model import Document, PaymentMethod, User
from parser import process_receipt_image
from schames import (
    DocumentResponse,
    PaymentMethodCreate,
    PaymentMethodResponse,
    UserCreate,
    UserResponse,
)

app = FastAPI(title="ReceiptFlow")

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


@app.post("/api/v1/upload-receipt")
def upload_receipt(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
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
        total_amount=parsed.get("total_amount"),
        invoice_date=parsed.get("invoice_date"),
        raw_text=parsed.get("raw_text"),
    )
    db.add(document)
    db.commit()
    db.refresh(document)

    return {
        "id": document.id,
        "filename": document.filename,
        "total_amount": document.total_amount,
        "invoice_date": document.invoice_date,
        "raw_text": document.raw_text,
        "created_at": document.created_at,
    }


@app.get("/api/v1/receipts", response_model=List[DocumentResponse])
def get_user_receipts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    documents = db.query(Document).filter(Document.user_id == current_user.id).all()
    return documents


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


@app.post("/api/v1/checkout")
def create_checkout(
    payload: dict = Body(default={"plan": "pro"}),
    current_user: User = Depends(get_current_user),
):
    plan = str(payload.get("plan", "pro")).lower()
    if plan not in {"pro", "starter"}:
        plan = "pro"

    if current_user.is_pro:
        redirect_url = "https://example.com/checkout/pro-already-active"
    else:
        redirect_url = f"https://example.com/checkout/{plan}"

    return {"checkout_url": redirect_url}


@app.get("/api/v1/me")
def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "email": current_user.email,
        "is_pro": current_user.is_pro,
    }
