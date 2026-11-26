from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr


class UserCreate(BaseModel):
  email: EmailStr
  password: str


class UserResponse(BaseModel):
  id: int
  email: str
  created_at: datetime

  class Config:
    from_attributes = True


class PaymentMethodCreate(BaseModel):
  brand: str
  last4: str
  expiry_month: int
  expiry_year: int
  card_holder_name: str
  billing_zip: Optional[str] = None
  is_default: bool = False


class PaymentMethodResponse(BaseModel):
  id: int
  brand: str
  last4: str
  expiry_month: int
  expiry_year: int
  card_holder_name: str
  billing_zip: Optional[str] = None
  is_default: bool
  created_at: Optional[datetime] = None

  class Config:
    from_attributes = True


class DocumentResponse(BaseModel):
  id: int
  filename: str
  total_amount: Optional[float] = None
  invoice_date: Optional[str] = None
  raw_text: Optional[str] = None
  created_at: Optional[datetime] = None

  class Config:
    from_attributes = True