"""
ISO Solutions — Stripe Payment Routes
Checkout Sessions · Webhooks · Customer Portal
"""
import os, json, sqlite3
import stripe
from fastapi import APIRouter, HTTPException, Request, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

router = APIRouter(prefix="/v1/stripe", tags=["stripe"])

# All secrets come from Railway environment variables
STRIPE_SECRET_KEY      = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET  = os.getenv("STRIPE_WEBHOOK_SECRET", "")
DASHBOARD_URL          = os.getenv("DASHBOARD_URL", "http://localhost:3000")

PRICE_MAP = {
    "paid":    os.getenv("STRIPE_PRICE_PAID",    "price_1TgPCh3wAwm7WSvkQNyajoEe"),
    "premium": os.getenv("STRIPE_PRICE_PREMIUM", "price_1TgPCi3wAwm7WSvk838rKU8b"),
}
PLAN_FROM_PRICE = {v: k for k, v in PRICE_MAP.items()}

stripe.api_key = STRIPE_SECRET_KEY
_data_dir = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "/tmp")
DB_PATH = os.getenv("DATABASE_URL", os.path.join(_data_dir, "iso.db"))

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

class CheckoutRequest(BaseModel):
    plan: str
    member_id: Optional[str]  = None
    email:     Optional[str]  = None
    name:      Optional[str]  = None

class PortalRequest(BaseModel):
    member_id: str

def _ensure_stripe_columns():
    conn = get_db()
    try:
        conn.execute("ALTER TABLE members ADD COLUMN stripe_customer_id TEXT")
        conn.execute("ALTER TABLE members ADD COLUMN stripe_subscription_id TEXT")
        conn.commit()
    except Exception:
        pass
    conn.close()

_ensure_stripe_columns()

def _get_or_create_customer(member_id: str, email: str, name: str) -> str:
    conn = get_db()
    row = conn.execute("SELECT stripe_customer_id, email, name FROM members WHERE id=?", (member_id,)).fetchone()
    conn.close()
    if row and row["stripe_customer_id"]:
        return row["stripe_customer_id"]
    customer = stripe.Customer.create(
        email=email or (row["email"] if row else None),
        name=name  or (row["name"]  if row else None),
        metadata={"member_id": member_id},
    )
    conn = get_db()
    conn.execute("UPDATE members SET stripe_customer_id=? WHERE id=?", (customer.id, member_id))
    conn.commit()
    conn.close()
    return customer.id

@router.post("/checkout")
async def create_checkout(req: CheckoutRequest):
    if req.plan not in PRICE_MAP:
        raise HTTPException(400, f"Invalid plan: {req.plan}. Choose 'paid' or 'premium'.")
    price_id = PRICE_MAP[req.plan]
    customer_id = None
    if req.member_id:
        customer_id = _get_or_create_customer(req.member_id, req.email or "", req.name or "")
    session_kwargs = dict(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{DASHBOARD_URL}/pricing/success?session_id={{CHECKOUT_SESSION_ID}}&plan={req.plan}&member_id={req.member_id or ''}",
        cancel_url=f"{DASHBOARD_URL}/pricing/cancel",
        allow_promotion_codes=True,
        billing_address_collection="auto",
        metadata={"member_id": req.member_id or "", "plan": req.plan},
    )
    if customer_id:
        session_kwargs["customer"] = customer_id
    elif req.email:
        session_kwargs["customer_email"] = req.email
    session = stripe.checkout.Session.create(**session_kwargs)
    return {"checkout_url": session.url, "session_id": session.id}

@router.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: Optional[str] = Header(None)):
    payload = await request.body()
    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WEBHOOK_SECRET)
        except stripe.error.SignatureVerificationError:
            raise HTTPException(400, "Invalid webhook signature")
    else:
        event = stripe.Event.construct_from(json.loads(payload), stripe.api_key)
    etype = event["type"]
    if etype in ("customer.subscription.created", "customer.subscription.updated"):
        sub = event["data"]["object"]
        plan = PLAN_FROM_PRICE.get(sub["items"]["data"][0]["price"]["id"], "paid")
        conn = get_db()
        conn.execute("UPDATE members SET plan=?, stripe_subscription_id=? WHERE stripe_customer_id=?",
                     (plan, sub["id"], sub["customer"]))
        conn.commit()
        conn.close()
    elif etype == "customer.subscription.deleted":
        conn = get_db()
        conn.execute("UPDATE members SET plan='free', stripe_subscription_id=NULL WHERE stripe_customer_id=?",
                     (event["data"]["object"]["customer"],))
        conn.commit()
        conn.close()
    elif etype == "checkout.session.completed":
        sess = event["data"]["object"]
        member_id = sess.get("metadata", {}).get("member_id")
        if member_id:
            conn = get_db()
            conn.execute("UPDATE members SET plan=?, stripe_customer_id=? WHERE id=?",
                         (sess.get("metadata", {}).get("plan", "paid"), sess.get("customer"), member_id))
            conn.commit()
            conn.close()
    return {"received": True, "type": etype}

@router.post("/portal")
async def customer_portal(req: PortalRequest):
    conn = get_db()
    row = conn.execute("SELECT stripe_customer_id FROM members WHERE id=?", (req.member_id,)).fetchone()
    conn.close()
    if not row or not row["stripe_customer_id"]:
        raise HTTPException(404, "No Stripe customer found for this member. Upgrade first.")
    session = stripe.billing_portal.Session.create(
        customer=row["stripe_customer_id"],
        return_url=f"{DASHBOARD_URL}/members",
    )
    return {"portal_url": session.url}

@router.get("/verify-session")
async def verify_session(session_id: str, member_id: Optional[str] = None):
    """Called by the success page to confirm payment and update the member plan."""
    session = stripe.checkout.Session.retrieve(session_id, expand=["subscription"])
    plan = session.metadata.get("plan", "paid")
    if session.payment_status in ("paid", "no_payment_required") and member_id:
        sub_id = session.subscription.id if session.subscription else None
        conn = get_db()
        conn.execute("UPDATE members SET plan=?, stripe_customer_id=?, stripe_subscription_id=? WHERE id=?",
                     (plan, session.customer, sub_id, member_id))
        conn.commit()
        conn.close()
    return {
        "status":      session.payment_status,
        "plan":        plan,
        "customer_id": session.customer,
        "confirmed":   session.payment_status in ("paid", "no_payment_required"),
    }

@router.get("/prices")
async def get_prices():
    return {
        "paid":    {"price_id": PRICE_MAP["paid"],    "amount": 4900, "currency": "usd", "interval": "month"},
        "premium": {"price_id": PRICE_MAP["premium"], "amount": 9900, "currency": "usd", "interval": "month"},
    }
