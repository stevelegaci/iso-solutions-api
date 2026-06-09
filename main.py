"""
ISO Solutions — Autonomous Agent Backend
FastAPI + DuckDuckGo Search + SQLite (env-configurable)
Production-ready for Railway deployment
"""
import os, uuid, json, re, asyncio, sqlite3
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from duckduckgo_search import DDGS
from stripe_routes import router as stripe_router

app = FastAPI(title="ISO Solutions API", version="1.0.0")

ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,https://localhost:3000"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(stripe_router)

_data_dir = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "/tmp")
DB_PATH = os.getenv("DATABASE_URL", os.path.join(_data_dir, "iso.db"))

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS intents (
            id TEXT PRIMARY KEY,
            raw_text TEXT,
            urgency_score REAL,
            budgets TEXT,
            source_url TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            result TEXT
        );
        CREATE TABLE IF NOT EXISTS members (
            id TEXT PRIMARY KEY,
            name TEXT,
            email TEXT,
            plan TEXT DEFAULT 'free',
            requests_used INTEGER DEFAULT 0,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS recommendations (
            id TEXT PRIMARY KEY,
            intent_id TEXT,
            items TEXT,
            best_option TEXT,
            service_fee REAL,
            created_at TEXT
        );
    """)
    existing = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
    if existing == 0:
        demo = [
            ("m1", "Alex Rivera",   "alex@example.com",   "paid",    12),
            ("m2", "Jordan Smith",  "jordan@example.com", "free",     3),
            ("m3", "Taylor Brooks", "taylor@example.com", "paid",    28),
            ("m4", "Casey Morgan",  "casey@example.com",  "free",     1),
            ("m5", "Sam Williams",  "sam@example.com",    "premium", 47),
        ]
        for d in demo:
            conn.execute(
                "INSERT INTO members VALUES (?,?,?,?,?,NULL,NULL,?)",
                (*d, datetime.utcnow().isoformat()),
            )
    conn.commit()
    conn.close()

init_db()

class IntentPayload(BaseModel):
    raw_text: str
    urgency_score: float = 0.5
    budgets: List[str] = []
    source_url: str = ""

class MatchRequest(BaseModel):
    query: str
    budget: Optional[float] = None
    member_plan: str = "free"

class MemberCreate(BaseModel):
    name: str
    email: str
    plan: str = "free"

@app.post("/v1/intent/ingest")
async def ingest_intent(payload: IntentPayload, background_tasks: BackgroundTasks):
    intent_id = f"int_{uuid.uuid4().hex[:8]}"
    conn = get_db()
    conn.execute(
        "INSERT INTO intents VALUES (?,?,?,?,?,?,?,?)",
        (intent_id, payload.raw_text[:1000], payload.urgency_score,
         json.dumps(payload.budgets), payload.source_url,
         "pending", datetime.utcnow().isoformat(), None),
    )
    conn.commit()
    conn.close()
    background_tasks.add_task(run_agent_pipeline, intent_id, payload.raw_text, payload.budgets)
    return {"intent_id": intent_id, "status": "queued"}

def classify_intent(text: str) -> dict:
    text_lower = text.lower()
    urgency_words = ["asap","urgent","desperately","need now","immediately","today",
                     "can't find","cannot find"]
    budget_pattern = re.compile(r'\$?\d+(?:\.\d{1,2})?')
    urgency = any(w in text_lower for w in urgency_words)
    budgets = budget_pattern.findall(text)
    item_patterns = [
        r"(?:looking for|searching for|need|want|find me|where can i get)\s+(.+?)(?:\.|$|,|\?)",
        r"(?:best|cheapest|good)\s+(.+?)(?:\.|$|,|\?)",
    ]
    item = None
    for p in item_patterns:
        m = re.search(p, text_lower)
        if m:
            item = m.group(1).strip()[:80]
            break
    if not item:
        item = text[:60].strip()
    return {
        "item": item,
        "urgency": urgency,
        "budgets": budgets[:3],
        "max_budget": float(budgets[-1].replace("$", "")) if budgets else None,
    }

def search_products(query: str, max_results: int = 5) -> List[dict]:
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(
                f"{query} buy price site:amazon.com OR site:ebay.com OR site:walmart.com OR site:etsy.com",
                max_results=max_results,
            ):
                price_match = re.search(r'\$[\d,]+(?:\.\d{1,2})?', r.get("body", ""))
                price = None
                if price_match:
                    try:
                        price = float(price_match.group().replace("$", "").replace(",", ""))
                    except Exception:
                        pass
                results.append({
                    "title":       r.get("title", "")[:100],
                    "url":         r.get("href", ""),
                    "description": r.get("body", "")[:200],
                    "price":       price,
                    "source":      r.get("href", "").split("/")[2] if r.get("href") else "web",
                })
    except Exception:
        try:
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=max_results):
                    results.append({
                        "title":       r.get("title", "")[:100],
                        "url":         r.get("href", ""),
                        "description": r.get("body", "")[:200],
                        "price":       None,
                        "source":      r.get("href", "").split("/")[2] if r.get("href") else "web",
                    })
        except Exception:
            pass
    return results

def rank_results(results: List[dict], budget: Optional[float] = None) -> List[dict]:
    known = ["amazon.com", "ebay.com", "walmart.com", "bestbuy.com", "etsy.com", "target.com"]

    def score(r: dict) -> float:
        s = 0.5
        if r.get("price"):
            s += 0.3 if (budget and r["price"] <= budget) else 0.15
        if any(k in r.get("source", "") for k in known):
            s += 0.2
        if r.get("title") and len(r["title"]) > 10:
            s += 0.1
        return s

    scored = sorted(results, key=score, reverse=True)
    for i, r in enumerate(scored):
        r["confidence"] = round(min(score(r), 0.99), 2)
        r["rank"] = i + 1
    return scored

def build_recommendation(intent_id: str, ranked: List[dict], item: str) -> dict:
    rec_id = f"rec_{uuid.uuid4().hex[:8]}"
    best   = ranked[0] if ranked else None
    alts   = ranked[1:3] if len(ranked) > 1 else []
    rec = {
        "id": rec_id, "intent_id": intent_id, "item_searched": item,
        "best_option": best, "alternatives": alts, "all_results": ranked,
        "service_fee": 4.99, "created_at": datetime.utcnow().isoformat(),
    }
    conn = get_db()
    conn.execute(
        "INSERT INTO recommendations VALUES (?,?,?,?,?,?)",
        (rec_id, intent_id, json.dumps(ranked), json.dumps(best), 4.99, rec["created_at"]),
    )
    conn.execute(
        "UPDATE intents SET status='completed', result=? WHERE id=?",
        (json.dumps(rec), intent_id),
    )
    conn.commit()
    conn.close()
    return rec

async def run_agent_pipeline(intent_id: str, text: str, budgets: list):
    conn = get_db()
    conn.execute("UPDATE intents SET status='processing' WHERE id=?", (intent_id,))
    conn.commit()
    conn.close()
    classified = classify_intent(text)
    results    = search_products(classified["item"], max_results=6)
    ranked     = rank_results(results, classified.get("max_budget"))
    build_recommendation(intent_id, ranked, classified["item"])

@app.post("/v1/match/find")
async def find_match(req: MatchRequest):
    results = search_products(req.query, max_results=6)
    ranked  = rank_results(results, req.budget)
    return {"query": req.query, "results": ranked,
            "best_option": ranked[0] if ranked else None, "total_found": len(ranked)}

@app.get("/v1/intents")
async def list_intents(limit: int = 20, status: Optional[str] = None):
    conn = get_db()
    if status:
        rows = conn.execute(
            "SELECT * FROM intents WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM intents ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/v1/intents/{intent_id}")
async def get_intent(intent_id: str):
    conn = get_db()
    row  = conn.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Intent not found")
    data = dict(row)
    if data.get("result"):
        data["result"] = json.loads(data["result"])
    return data

@app.get("/v1/recommendations")
async def list_recommendations(limit: int = 20):
    conn  = get_db()
    rows  = conn.execute(
        "SELECT * FROM recommendations ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["items"]       = json.loads(d["items"])       if d["items"]       else []
        d["best_option"] = json.loads(d["best_option"]) if d["best_option"] else None
        out.append(d)
    return out

@app.get("/v1/members")
async def list_members():
    conn = get_db()
    rows = conn.execute("SELECT * FROM members ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/v1/members")
async def create_member(member: MemberCreate):
    mid = f"m_{uuid.uuid4().hex[:8]}"
    conn = get_db()
    conn.execute(
        "INSERT INTO members VALUES (?,?,?,?,?,NULL,NULL,?)",
        (mid, member.name, member.email, member.plan, 0, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return {"id": mid, **member.dict(), "requests_used": 0}

@app.patch("/v1/members/{member_id}/plan")
async def update_member_plan(member_id: str, plan: str):
    conn = get_db()
    conn.execute("UPDATE members SET plan=? WHERE id=?", (plan, member_id))
    conn.commit()
    conn.close()
    return {"updated": True, "plan": plan}

@app.get("/v1/stats")
async def get_stats():
    conn     = get_db()
    total    = conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0]
    done     = conn.execute("SELECT COUNT(*) FROM intents WHERE status='completed'").fetchone()[0]
    members  = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
    paid     = conn.execute("SELECT COUNT(*) FROM members WHERE plan != 'free'").fetchone()[0]
    conn.close()
    return {
        "total_intents":  total,
        "completed":      done,
        "pending":        total - done,
        "success_rate":   round(done / max(total, 1) * 100, 1),
        "total_members":  members,
        "paid_members":   paid,
        "revenue_est":    round(paid * 49.0, 2),
    }

@app.get("/v1/health")
async def health():
    return {"status": "ok", "version": "1.0.0", "service": "ISO Solutions API",
            "env": os.getenv("RAILWAY_ENVIRONMENT", "local")}

@app.on_event("startup")
async def seed_demo():
    conn = get_db()
    existing = conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0]
    conn.close()
    if existing == 0:
        demos = [
            ("Looking for a good mechanical keyboard under $100, need it ASAP for work", 0.9, ["$100"]),
            ("Can anyone help me find best noise cancelling headphones? Budget $200",     0.7, ["$200"]),
            ("Desperately need a standing desk for home office, cant find one in stock",  0.95, []),
            ("Where can I buy a decent espresso machine under $150 today?",               0.85, ["$150"]),
            ("Looking for ergonomic office chair, urgent, my back is killing me",         0.9,  []),
        ]
        for text, score, budgets in demos:
            iid = f"int_{uuid.uuid4().hex[:8]}"
            conn = get_db()
            conn.execute(
                "INSERT INTO intents VALUES (?,?,?,?,?,?,?,?)",
                (iid, text, score, json.dumps(budgets),
                 "https://reddit.com/r/deals", "pending",
                 datetime.utcnow().isoformat(), None),
            )
            conn.commit()
            conn.close()
            asyncio.create_task(run_agent_pipeline(iid, text, budgets))
