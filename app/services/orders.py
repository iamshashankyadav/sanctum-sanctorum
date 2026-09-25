"""Order operations: placing, paying and cancelling purchases."""
from datetime import datetime
from typing import Dict, List, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Book, Member, MemberTier, Order, OrderItem, OrderStatus
from app.schemas import OrderCreate
from app.services.members import RESTRICTED_MIN_TIER, tier_at_least

# Percentage discount granted by each membership tier.
TIER_DISCOUNT_PERCENT: Dict[str, int] = {
    MemberTier.APPRENTICE.value: 0,
    MemberTier.ADEPT.value: 5,
    MemberTier.MASTER.value: 10,
    MemberTier.SUPREME.value: 15,
}

# Extra discount when the total quantity across all items reaches the threshold.
BULK_QUANTITY_THRESHOLD = 10
BULK_DISCOUNT_PERCENT = 5


def calculate_discount_percent(member: Member, total_quantity: int) -> int:
    """Tier discount, plus the bulk discount when total quantity >= threshold."""
    tier_discount = TIER_DISCOUNT_PERCENT.get(member.tier, 0)
    bulk_discount = BULK_DISCOUNT_PERCENT if total_quantity >= BULK_QUANTITY_THRESHOLD else 0
    return tier_discount + bulk_discount


def create_order(db: Session, data: OrderCreate, now: datetime) -> Order:
    """Place a pending order and reserve stock.

    Checks, in order (422 for empty items / bad quantity / duplicate books is done by the schema):
    1. 404 member not found; 404 any book not found
    2. 403 any book restricted and member tier below master
    3. 409 any book has insufficient stock (all-or-nothing: nothing is changed)
    Then stock is decremented for every item and prices are snapshotted.
    Pricing: discount_cents = subtotal * percent // 100; total = subtotal - discount.
    """
    # 1. Load the member (404) and every book (404).
    member = db.get(Member, data.member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")

    book_items: List[Tuple[Book, int]] = []
    for item in data.items:
        book = db.get(Book, item.book_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"Book {item.book_id} not found")
        book_items.append((book, item.quantity))

    # 2. Check restricted access per book (403)
    for book, _ in book_items:
        if book.restricted and not tier_at_least(member.tier, RESTRICTED_MIN_TIER):
            raise HTTPException(
                status_code=403,
                detail=f"Member tier '{member.tier}' not permitted to purchase restricted book",
            )

    # 3. Check stock for every item before changing anything (409)
    for book, quantity in book_items:
        if book.stock < quantity:
            raise HTTPException(
                status_code=409,
                detail=f"Insufficient stock for book '{book.title}' (requested: {quantity}, available: {book.stock})",
            )

    # 4. Decrement stock and build OrderItems with price snapshot
    order_items: List[OrderItem] = []
    subtotal_cents = 0
    total_quantity = 0

    for book, quantity in book_items:
        book.stock -= quantity
        line_total = book.price_cents * quantity
        subtotal_cents += line_total
        total_quantity += quantity
        order_items.append(
            OrderItem(
                book_id=book.id,
                quantity=quantity,
                unit_price_cents=book.price_cents,
            )
        )

    # 5. Compute subtotal, discount_percent, discount_cents, total
    discount_percent = calculate_discount_percent(member, total_quantity)
    discount_cents = subtotal_cents * discount_percent // 100
    total_cents = subtotal_cents - discount_cents

    # 6. Save the pending Order with created_at = now
    order = Order(
        member_id=member.id,
        status=OrderStatus.PENDING.value,
        items=order_items,
        subtotal_cents=subtotal_cents,
        discount_percent=discount_percent,
        discount_cents=discount_cents,
        total_cents=total_cents,
        created_at=now,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def get_order(db: Session, order_id: int) -> Order:
    """Return an order by id, or raise 404."""
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


def pay_order(db: Session, order_id: int) -> Order:
    """Mark a pending order as paid. 404 if missing; 409 if not pending."""
    order = get_order(db, order_id)
    if order.status != OrderStatus.PENDING.value:
        raise HTTPException(status_code=409, detail=f"Cannot pay an order that is {order.status}")
    order.status = OrderStatus.PAID.value
    db.commit()
    db.refresh(order)
    return order


def cancel_order(db: Session, order_id: int) -> Order:
    """Cancel a pending order and restore the reserved stock. 404 if missing; 409 if not pending."""
    order = get_order(db, order_id)
    if order.status != OrderStatus.PENDING.value:
        raise HTTPException(status_code=409, detail=f"Cannot cancel an order that is {order.status}")
    order.status = OrderStatus.CANCELLED.value
    for item in order.items:
        item.book.stock += item.quantity
    db.commit()
    db.refresh(order)
    return order

