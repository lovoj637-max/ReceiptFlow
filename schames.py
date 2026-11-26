from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, Field


EXPENSE_CATEGORIES = (
  "Uncategorized",
  "Advertising",
  "Car and travel",
  "Contractors",
  "Insurance",
  "Meals",
  "Office supplies",
  "Professional services",
  "Rent",
  "Software",
  "Supplies",
  "Utilities",
  "Other",
)


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
  merchant: Optional[str] = None
  total_amount: Optional[float] = None
  invoice_date: Optional[str] = None
  raw_text: Optional[str] = None
  expense_category: str = "Uncategorized"
  business_purpose: Optional[str] = None
  created_at: Optional[datetime] = None

  class Config:
    from_attributes = True


class DocumentUpdate(BaseModel):
  merchant: Optional[str] = Field(default=None, max_length=255)
  invoice_date: Optional[str] = Field(default=None, max_length=50)
  expense_category: Optional[str] = Field(default=None, max_length=64)
  business_purpose: Optional[str] = Field(default=None, max_length=1000)