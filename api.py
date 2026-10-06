"""Supplier HTTP API (ProdSeller model).

Exposes this bot's catalog/stock to reseller bots:

    GET  /products            - list active products with stock
    GET  /products/{id}/price - price + stock for one product
    POST /orders              - reserve stock, returns serials
    GET  /orders/{id}         - order status

Auth: X-API-Key header. Keys are issued as `psk_...` from the Telegram
admin panel (API Keys) and stored as SHA-256 hashes.

Run:
    uvicorn api:app --host 0.0.0.0 --port 8000
"""
import hashlib
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from config import config
from db import Database, InsufficientStock

log = logging.getLogger(__name__)

db = Database(config.db_path, turso_url=config.turso_url, turso_token=config.turso_token)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    log.info("Supplier API ready (db=%s)", config.db_path)
    yield
    await db.close()


app = FastAPI(title="NYStore Supplier API", version="1.0", lifespan=lifespan)


# ---------------- auth ----------------
async def require_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict:
    if not x_api_key or not x_api_key.startswith("psk_"):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key")
    digest = hashlib.sha256(x_api_key.encode()).hexdigest()
    key = await db.verify_api_key(digest)
    if not key:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    return key


# ---------------- models ----------------
class ProductOut(BaseModel):
    id: int
    name: str
    price: float
    stock: int
    active: bool


class PriceOut(BaseModel):
    id: int
    name: str
    price: float
    stock: int


class OrderIn(BaseModel):
    product_id: int = Field(gt=0)
    qty: int = Field(gt=0, le=10000)
    external_ref: str | None = Field(default=None, max_length=100)


class OrderOut(BaseModel):
    order_id: int
    product_id: int
    qty: int
    total: float
    serials: list[str]
    status: str


class OrderStatusOut(BaseModel):
    order_id: int
    product_id: int
    qty: int
    total: float
    status: str
    external_ref: str | None
    created_at: str | None


# ---------------- routes ----------------
@app.get("/products", response_model=list[ProductOut])
async def list_products(key: dict = Depends(require_key)):
    return await db.list_products_with_stock(active_only=True)


@app.get("/products/{product_id}/price", response_model=PriceOut)
async def get_price(product_id: int, key: dict = Depends(require_key)):
    p = await db.get_product(product_id)
    if not p or not p["active"]:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"id": p["id"], "name": p["name"], "price": p["price"],
            "stock": await db.count_stock(product_id)}


@app.post("/orders", response_model=OrderOut, status_code=201)
async def place_order(body: OrderIn, key: dict = Depends(require_key)):
    try:
        order_id, serials, total = await db.api_place_order(
            api_key_id=key["id"], product_id=body.product_id,
            qty=body.qty, external_ref=body.external_ref,
        )
    except InsufficientStock as e:
        raise HTTPException(status_code=409,
                            detail=f"Insufficient stock (available: {e.available})")
    return {"order_id": order_id, "product_id": body.product_id, "qty": body.qty,
            "total": total, "serials": serials, "status": "paid"}


@app.get("/orders/{order_id}", response_model=OrderStatusOut)
async def order_status(order_id: int, key: dict = Depends(require_key)):
    o = await db.get_order(order_id)
    if not o or o["api_key_id"] != key["id"]:
        raise HTTPException(status_code=404, detail="Order not found")
    return {"order_id": o["id"], "product_id": o.get("product_id", 0),
            "qty": o["qty"], "total": o["total"], "status": o["status"],
            "external_ref": o["external_ref"], "created_at": o["created_at"]}


@app.get("/health")
async def health():
    return {"ok": True}
