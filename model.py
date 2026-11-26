from database import Base
from sqlalchemy.orm import relationship

from sqlalchemy import (
  Boolean,
  Column,
  DateTime,
  Float,
  ForeignKey,
  Integer,
  String,
  Text,
  UniqueConstraint,
  func,
)
# ... existing imports ...


class User(Base):
  __tablename__ = "users"

  id = Column(Integer, primary_key=True, index=True)
  email = Column(String(255), unique=True, index=True, nullable=False)
  hashed_password = Column(String(255), nullable=False)

  # NEW Stripe fields
  stripe_customer_id = Column(String(255), nullable=True)
  is_pro = Column(Boolean, default=False)

  created_at = Column(DateTime(timezone=True), server_default=func.now())
  documents = relationship(
      "Document", back_populates="owner", cascade="all, delete-orphan"
  )
  payment_methods = relationship(
      "PaymentMethod", back_populates="owner", cascade="all, delete-orphan"
  )
  __table_args__={"extend_existing": True}

class Document(Base):
  __tablename__ = "documents"

  id = Column(Integer, primary_key=True, index=True)
  user_id = Column(
      Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
  )

  filename = Column(String(255), nullable=False)
  file_path = Column(String(500), nullable=False)
  file_sha256 = Column(String(64), nullable=True)
  merchant = Column(String(255), nullable=True)
  total_amount = Column(Float, nullable=True)
  invoice_date = Column(String(50), nullable=True)
  raw_text = Column(Text, nullable=True)
  expense_category = Column(
      String(64),
      nullable=False,
      default="Uncategorized",
      server_default="Uncategorized",
  )
  business_purpose = Column(Text, nullable=True)
  created_at = Column(DateTime(timezone=True), server_default=func.now())
  __table_args__ = (
      UniqueConstraint(
          "user_id",
          "file_sha256",
          name="uq_documents_user_file_sha256",
      ),
  )

  # Relationship back to user
  owner = relationship("User", back_populates="documents")


class PaymentMethod(Base):
  __tablename__ = "payment_methods"

  id = Column(Integer, primary_key=True, index=True)
  user_id = Column(
      Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
  )
  brand = Column(String(50), nullable=False)
  last4 = Column(String(4), nullable=False)
  expiry_month = Column(Integer, nullable=False)
  expiry_year = Column(Integer, nullable=False)
  card_holder_name = Column(String(255), nullable=False)
  billing_zip = Column(String(20), nullable=True)
  is_default = Column(Boolean, default=False, nullable=False)
  created_at = Column(DateTime(timezone=True), server_default=func.now())

  owner = relationship("User", back_populates="payment_methods")