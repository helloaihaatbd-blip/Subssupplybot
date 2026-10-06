"""Turso (libsql) backend adapter tests.

These exercise the _TursoConnection async wrapper against a local libsql
file — no network or Turso credentials needed. They prove the Turso code
path (init, CRUD, explicit transactions, concurrency) behaves identically
to the aiosqlite path.

Run:  venv/bin/python tests/run_tests.py
"""
import asyncio
import os
from functools import wraps
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import Database, InsufficientFunds, InsufficientStock  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append((fn.__name__, fn))
    return fn


async def fresh_turso_db():
    fd, path = tempfile.mkstemp(prefix="nystore_turso_", suffix=".db")
    os.close(fd)
    os.unlink(path)
    # libsql local file as the "remote" stand-in; token is ignored for files.
    db = Database(":memory:", turso_url=f"file:{path}", turso_token="dummy")
    assert db.uses_turso is True
    await db.init()
    try:
        yield db, path
    finally:
        await db.close()
        try:
            os.unlink(path)
        except OSError:
            pass


def run(coro_fn):
    @wraps(coro_fn)
    async def wrapper():
        gen = fresh_turso_db()
        db, path = await gen.__anext__()
        try:
            await coro_fn(db)
        finally:
            try:
                await gen.__anext__()
            except StopAsyncIteration:
                pass
    return wrapper


@test
async def test_turso_flag_off_without_creds():
    db = Database("/tmp/x.db")
    assert db.uses_turso is False
    db2 = Database("/tmp/x.db", turso_url="libsql://x.turso.io", turso_token="")
    assert db2.uses_turso is False  # token missing -> local mode


@test
@run
async def test_turso_init_schema_seeded(db):
    assert await db.get_setting("usd_rate") == "120"
    assert await db.count_users() == 0


@test
@run
async def test_turso_crud(db):
    await db.ensure_user(1, "turso_user")
    assert await db.add_balance(1, 9.99) == 9.99
    pid = await db.add_product("TursoProd", 3.0, "d", "h", "t")
    assert await db.add_stock(pid, ["a", "b"]) == 2
    assert await db.count_stock(pid) == 2
    assert (await db.get_product(pid))["name"] == "TursoProd"


@test
@run
async def test_turso_purchase_atomic(db):
    pid = await db.add_product("P", 5.0, "d", "h", "t")
    await db.add_stock(pid, ["k1"])
    await db.ensure_user(1, "u")
    await db.add_balance(1, 4.0)
    try:
        await db.purchase(1, pid, 1)
        assert False, "should raise"
    except InsufficientFunds:
        pass
    # failed txn rolled back: stock untouched, balance intact
    assert await db.count_stock(pid) == 1
    assert await db.get_balance(1) == 4.0


@test
@run
async def test_turso_purchase_race_single_winner(db):
    pid = await db.add_product("P", 1.0, "d", "h", "t")
    await db.add_stock(pid, ["last"])
    for i in range(3):
        await db.ensure_user(50 + i, f"u{i}")
        await db.add_balance(50 + i, 10.0)
    results = await asyncio.gather(
        *[db.purchase(50 + i, pid, 1) for i in range(3)],
        return_exceptions=True,
    )
    wins = [r for r in results if not isinstance(r, Exception)]
    assert len(wins) == 1, results
    assert await db.count_stock(pid) == 0


@test
@run
async def test_turso_referral_and_deposit_flow(db):
    await db.ensure_user(1, "a")
    await db.ensure_user(2, "b")
    assert await db.set_referred_by(2, 1) is True
    assert await db.reward_referral(2) == 1
    assert await db.reward_referral(2) is None
    did = await db.create_deposit(2, "Nagad", 2.0, 240.0, "TT1")
    r = await db.approve_deposit(did)
    assert r["new_balance"] == 2.0
