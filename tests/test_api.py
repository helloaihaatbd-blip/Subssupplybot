"""Supplier API tests (FastAPI + httpx ASGI transport).

Run:  venv/bin/python tests/run_tests.py
"""
import hashlib
import os
import sys
import tempfile
from functools import wraps

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import api as api_module  # noqa: E402
from db import Database  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append((fn.__name__, fn))
    return fn


def _headers(key: str):
    return {"X-API-Key": key}


async def fresh_client():
    """Fresh API app bound to a fresh temp DB; yields (client, db, api_key)."""
    fd, path = tempfile.mkstemp(prefix="nystore_api_", suffix=".db")
    os.close(fd)
    os.unlink(path)
    db = Database(path)
    await db.init()
    raw_key = "psk_testkey123456"
    digest = hashlib.sha256(raw_key.encode()).hexdigest()
    await db.create_api_key("testshop", digest, "psk_test")
    old_db = api_module.db
    api_module.db = db
    transport = httpx.ASGITransport(app=api_module.app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    try:
        yield client, db, raw_key
    finally:
        await client.aclose()
        api_module.db = old_db
        await db.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass


def run(coro_fn):
    @wraps(coro_fn)
    async def wrapper():
        gen = fresh_client()
        client, db, raw_key = await gen.__anext__()
        try:
            await coro_fn(client, db, raw_key)
        finally:
            try:
                await gen.__anext__()
            except StopAsyncIteration:
                pass
    return wrapper


@test
@run
async def test_products_no_key_401(client, db, key):
    r = await client.get("/products")
    assert r.status_code == 401, r.status_code


@test
@run
async def test_products_bad_key_401(client, db, key):
    r = await client.get("/products", headers=_headers("psk_wrong"))
    assert r.status_code == 401


@test
@run
async def test_products_empty_200(client, db, key):
    r = await client.get("/products", headers=_headers(key))
    assert r.status_code == 200 and r.json() == []


@test
@run
async def test_products_lists_stock(client, db, key):
    pid = await db.add_product("Netflix", 2.5, "d", "h", "t")
    await db.add_stock(pid, ["a", "b"])
    await db.add_product("Hidden", 1.0, "d", "h", "t")
    await db.update_product(2, active=0)
    r = await client.get("/products", headers=_headers(key))
    items = r.json()
    assert len(items) == 1 and items[0]["stock"] == 2 and items[0]["name"] == "Netflix"


@test
@run
async def test_price_ok(client, db, key):
    pid = await db.add_product("Spotify", 3.0, "d", "h", "t")
    await db.add_stock(pid, ["x"])
    r = await client.get(f"/products/{pid}/price", headers=_headers(key))
    assert r.status_code == 200
    body = r.json()
    assert body["price"] == 3.0 and body["stock"] == 1


@test
@run
async def test_price_404(client, db, key):
    r = await client.get("/products/999/price", headers=_headers(key))
    assert r.status_code == 404


@test
@run
async def test_place_order_201(client, db, key):
    pid = await db.add_product("Disney", 4.0, "d", "h", "t")
    await db.add_stock(pid, ["k1", "k2"])
    r = await client.post("/orders", headers=_headers(key),
                          json={"product_id": pid, "qty": 2, "external_ref": "ext-9"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["serials"] == ["k1", "k2"] and body["total"] == 8.0
    assert body["status"] == "paid"


@test
@run
async def test_place_order_insufficient_409(client, db, key):
    pid = await db.add_product("Disney", 4.0, "d", "h", "t")
    await db.add_stock(pid, ["only"])
    r = await client.post("/orders", headers=_headers(key),
                          json={"product_id": pid, "qty": 5})
    assert r.status_code == 409
    assert "available" in r.json()["detail"]


@test
@run
async def test_place_order_invalid_qty_422(client, db, key):
    pid = await db.add_product("Disney", 4.0, "d", "h", "t")
    r = await client.post("/orders", headers=_headers(key),
                          json={"product_id": pid, "qty": 0})
    assert r.status_code == 422
    r = await client.post("/orders", headers=_headers(key),
                          json={"product_id": pid, "qty": 20000})
    assert r.status_code == 422


@test
@run
async def test_order_status_ok(client, db, key):
    pid = await db.add_product("X", 1.0, "d", "h", "t")
    await db.add_stock(pid, ["s1"])
    r = await client.post("/orders", headers=_headers(key),
                          json={"product_id": pid, "qty": 1, "external_ref": "r1"})
    oid = r.json()["order_id"]
    r = await client.get(f"/orders/{oid}", headers=_headers(key))
    assert r.status_code == 200
    body = r.json()
    assert body["order_id"] == oid and body["external_ref"] == "r1"


@test
@run
async def test_order_status_other_key_404(client, db, key):
    pid = await db.add_product("X", 1.0, "d", "h", "t")
    await db.add_stock(pid, ["s1"])
    r = await client.post("/orders", headers=_headers(key),
                          json={"product_id": pid, "qty": 1})
    oid = r.json()["order_id"]
    assert (await client.get(f"/orders/{oid}")).status_code == 401
    # a different valid key must not see this order
    other = "psk_otherkey999"
    await db.create_api_key("other", hashlib.sha256(other.encode()).hexdigest(), "psk_oth")
    r = await client.get(f"/orders/{oid}", headers=_headers(other))
    assert r.status_code == 404
    assert (await client.get("/orders/99999", headers=_headers(key))).status_code == 404


@test
@run
async def test_revoked_key_401(client, db, key):
    keys = await db.list_api_keys()
    await db.revoke_api_key(keys[0]["id"])
    r = await client.get("/products", headers=_headers(key))
    assert r.status_code == 401


@test
@run
async def test_health_200(client, db, key):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json() == {"ok": True}
