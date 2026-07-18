"""Business-domain SQLAlchemy models for the Phase 2 data layer."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, Date, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import TimestampMixin


class Employee(TimestampMixin, Base):
    __tablename__ = "employees"
    __table_args__ = (
        CheckConstraint("salary >= 0", name="ck_employees_salary_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32), unique=True)
    department: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    city: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    salary: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    employment_status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")

    permissions: Mapped[list[EmployeePermission]] = relationship(
        back_populates="employee",
        cascade="save-update, merge",
        passive_deletes=True,
    )
    owned_sales_deals: Mapped[list[SalesDeal]] = relationship(back_populates="owner_employee")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class EmployeePermission(TimestampMixin, Base):
    __tablename__ = "employee_permissions"
    __table_args__ = (
        UniqueConstraint("employee_id", "permission_code", name="uq_employee_permissions_employee_permission"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    permission_code: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    employee: Mapped[Employee] = relationship(back_populates="permissions")


class Vendor(TimestampMixin, Base):
    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    vendor_name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    contact_email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32), unique=True)
    city: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    country: Mapped[str] = mapped_column(String(100), nullable=False, default="India", server_default="India")
    category: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")

    product_mappings: Mapped[list[ProductVendorMapping]] = relationship(back_populates="vendor")


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    customer_name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    contact_email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32), unique=True)
    city: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    country: Mapped[str] = mapped_column(String(100), nullable=False, default="India", server_default="India")
    industry: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")

    sales_deals: Mapped[list[SalesDeal]] = relationship(back_populates="customer")


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("list_price >= 0", name="ck_products_list_price_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    product_name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    category: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    list_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    vendor_mappings: Mapped[list[ProductVendorMapping]] = relationship(back_populates="product")
    sales_deals: Mapped[list[SalesDeal]] = relationship(back_populates="product")


class ProductVendorMapping(TimestampMixin, Base):
    __tablename__ = "product_vendor_mappings"
    __table_args__ = (
        UniqueConstraint("product_id", "vendor_id", name="uq_product_vendor_mappings_product_vendor"),
        CheckConstraint("quoted_price >= 0", name="ck_product_vendor_mappings_quoted_price_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True)
    vendor_sku: Mapped[Optional[str]] = mapped_column(String(100))
    quoted_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    is_preferred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    product: Mapped[Product] = relationship(back_populates="vendor_mappings")
    vendor: Mapped[Vendor] = relationship(back_populates="product_mappings")


class SalesDeal(TimestampMixin, Base):
    __tablename__ = "sales_deals"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_sales_deals_amount_nonnegative"),
        CheckConstraint("probability >= 0 AND probability <= 100", name="ck_sales_deals_probability_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deal_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(250), nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True)
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    owner_employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id", ondelete="SET NULL"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    stage: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    probability: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_close_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open", server_default="open")

    customer: Mapped[Customer] = relationship(back_populates="sales_deals")
    product: Mapped[Optional[Product]] = relationship(back_populates="sales_deals")
    owner_employee: Mapped[Optional[Employee]] = relationship(back_populates="owned_sales_deals")
