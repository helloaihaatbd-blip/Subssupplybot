"""DB / security / utility tests for the shop bot (local SQLite path).

Run:  venv/bin/python tests/run_tests.py
"""
import asyncio
import os
from functools import wraps
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import Database, InsufficientFunds, InsufficientStock  # noqa: E402
from utils import RateLimiter, sign_cb, verify_cb  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append((fn.__name__, fn))
    return fn


async def fresh_db():
    fd, path = tempfile.mkstemp(prefix="nystore_test_", suffix=".db")
    os.close(fd)
    os.unlink(path)
    db = Database(path)
    await db.init()
    try:
        yield db
    finally:
        await db.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass


def run(coro_fn):
    @wraps(coro_fn)
    async def wrapper():
        gen = fresh_db()
        db = await gen.__anext__()
        try:
            await coro_fn(db)
        finally:
            try:
                await gen.__anext__()
            except StopAsyncIteration:
                pass
    return wrapper


# ---------------- settings ----------------
@test
@run
async def test_settings_seeded_defaults(db):
    assert await db.get_setting("usd_rate") == "120"
    assert await db.get_setting("force_join_link") == "https://t.me/subsmaart"
    assert await db.get_setting("min_deposit") == "0.20"


@test
@run
async def test_set_get_setting(db):
    await db.set_setting("usd_rate", "117.5")
    assert await db.get_setting("usd_rate") == "117.5"
    await db.set_setting("usd_rate", "120")
    assert await db.get_setting("usd_rate") == "120"


@test
@run
async def test_all_settings(db):
    s = await db.all_settings()
    for k in ("usd_rate", "bkash_number", "cashout_bkash", "supplier_base_url"):
        assert k in s, k


@test
@run
async def test_get_setting_missing_default(db):
    assert await db.get_setting("nope") == ""
    assert await db.get_setting("nope", "dflt") == "dflt"


# ---------------- users ----------------
@test
@run
async def test_ensure_user_creates(db):
    await db.ensure_user(1, "alice")
    u = await db.get_user(1)
    assert u and u["username"] == "alice" and u["wallet_balance"] == 0
    assert u["banned"] is False and u["joined"] is False


@test
@run
async def test_ensure_user_updates_username(db):
    await db.ensure_user(1, "alice")
    await db.ensure_user(1, "alice2")
    assert (await db.get_user(1))["username"] == "alice2"


@test
@run
async def test_get_user_missing_none(db):
    assert await db.get_user(999) is None


@test
@run
async def test_get_balance_missing_zero(db):
    assert await db.get_balance(999) == 0.0


@test
@run
async def test_add_balance(db):
    await db.ensure_user(1, "a")
    assert await db.add_balance(1, 5.0) == 5.0
    assert await db.add_balance(1, 2.5) == 7.5


@test
@run
async def test_add_balance_negative_floors_zero(db):
    await db.ensure_user(1, "a")
    await db.add_balance(1, 3.0)
    assert await db.add_balance(1, -10.0) == 0.0
    assert await db.get_balance(1) == 0.0


@test
@run
async def test_add_balance_missing_user_creates(db):
    assert await db.add_balance(42, 1.5) == 1.5


@test
@run
async def test_ban_unban(db):
    await db.ensure_user(1, "a")
    await db.set_banned(1, True)
    assert (await db.get_user(1))["banned"] is True
    await db.set_banned(1, False)
    assert (await db.get_user(1))["banned"] is False


# ---------------- referrals ----------------
@test
@run
async def test_set_referred_by_new_true(db):
    await db.ensure_user(1, "a")
    await db.ensure_user(2, "b")
    assert await db.set_referred_by(2, 1) is True


@test
@run
async def test_set_referred_by_again_false(db):
    await db.ensure_user(1, "a")
    await db.ensure_user(2, "b")
    await db.set_referred_by(2, 1)
    assert await db.set_referred_by(2, 1) is False
    assert await db.set_referred_by(2, 3) is False


@test
@run
async def test_reward_referral_pays(db):
    await db.ensure_user(1, "a")
    await db.ensure_user(2, "b")
    await db.set_referred_by(2, 1)
    assert await db.reward_referral(2) == 1
    assert await db.get_balance(1) == 0.01
    stats = await db.referral_stats(1)
    assert stats["invited"] == 1 and stats["rewarded"] == 1 and stats["earnings"] == 0.01


@test
@run
async def test_reward_referral_idempotent(db):
    await db.ensure_user(1, "a")
    await db.ensure_user(2, "b")
    await db.set_referred_by(2, 1)
    assert await db.reward_referral(2) == 1
    assert await db.reward_referral(2) is None
    assert await db.get_balance(1) == 0.01


@test
@run
async def test_reward_referral_self_none(db):
    await db.ensure_user(1, "a")
    assert await db.reward_referral(1) is None


@test
@run
async def test_reward_referral_no_referrer_none(db):
    await db.ensure_user(2, "b")
    assert await db.reward_referral(2) is None


@test
@run
async def test_referral_stats_empty(db):
    await db.ensure_user(1, "a")
    s = await db.referral_stats(1)
    assert s == {"invited": 0, "rewarded": 0, "earnings": 0.0}


# ---------------- products ----------------
async def _product(db, name="Netflix", price=2.5, active=True):
    pid = await db.add_product(name, price, "desc", "how", "terms", supplier_sku="SKU1")
    if not active:
        await db.update_product(pid, active=0)
    return pid


@test
@run
async def test_add_get_product(db):
    pid = await _product(db)
    p = await db.get_product(pid)
    assert p["name"] == "Netflix" and p["price"] == 2.5 and p["active"] is True
    assert p["supplier_sku"] == "SKU1"


@test
@run
async def test_list_products_active_only(db):
    await _product(db, "A")
    await _product(db, "B", active=False)
    assert [p["name"] for p in await db.list_products()] == ["A"]
    assert len(await db.list_products(active_only=False)) == 2


@test
@run
async def test_update_product(db):
    pid = await _product(db)
    await db.update_product(pid, name="N2", price=3.0)
    p = await db.get_product(pid)
    assert p["name"] == "N2" and p["price"] == 3.0


@test
@run
async def test_update_product_ignores_unknown_fields(db):
    pid = await _product(db)
    await db.update_product(pid, nope="x")  # must not raise
    assert (await db.get_product(pid))["name"] == "Netflix"
    await db.update_product(pid)  # no fields -> no-op


@test
@run
async def test_delete_product_removes_stock(db):
    pid = await _product(db)
    await db.add_stock(pid, ["s1", "s2"])
    await db.delete_product(pid)
    assert await db.get_product(pid) is None
    assert await db.count_stock(pid) == 0


@test
@run
async def test_get_product_missing_none(db):
    assert await db.get_product(12345) is None


@test
@run
async def test_list_products_with_stock_counts(db):
    pid = await _product(db)
    await db.add_stock(pid, ["a", "b", "c"])
    rows = await db.list_products_with_stock()
    assert rows[0]["stock"] == 3


@test
@run
async def test_product_default_terms(db):
    pid = await db.add_product("X", 1.0, "", "", "")
    p = await db.get_product(pid)
    assert p["terms"] == ""  # explicitly passed; default only when omitted


# ---------------- stock ----------------
@test
@run
async def test_add_stock_dedupe_blank(db):
    pid = await _product(db)
    n = await db.add_stock(pid, ["a", "a", " ", "b", ""])
    assert n == 2
    assert await db.count_stock(pid) == 2


@test
@run
async def test_count_stock(db):
    pid = await _product(db)
    assert await db.count_stock(pid) == 0
    await db.add_stock(pid, ["x"])
    assert await db.count_stock(pid) == 1


@test
@run
async def test_clear_stock_keeps_used(db):
    pid = await _product(db)
    await db.add_stock(pid, ["u1", "free1"])
    await db.ensure_user(1, "a")
    await db.add_balance(1, 100)
    await db.purchase(1, pid, 1)  # consumes u1
    cleared = await db.clear_stock(pid)
    assert cleared == 1
    assert await db.count_stock(pid) == 0


@test
@run
async def test_add_stock_empty_zero(db):
    pid = await _product(db)
    assert await db.add_stock(pid, []) == 0
    assert await db.add_stock(pid, [" ", ""]) == 0


@test
@run
async def test_get_order_serials(db):
    pid = await _product(db)
    await db.add_stock(pid, ["k1", "k2"])
    await db.ensure_user(1, "a")
    await db.add_balance(1, 100)
    oid, serials, _, _ = await db.purchase(1, pid, 2)
    assert serials == ["k1", "k2"]
    assert await db.get_order_serials(oid) == ["k1", "k2"]


# ---------------- purchase ----------------
@test
@run
async def test_purchase_happy_path(db):
    pid = await _product(db, price=2.5)
    await db.add_stock(pid, ["s1", "s2", "s3"])
    await db.ensure_user(7, "buyer")
    await db.add_balance(7, 10.0)
    oid, serials, bal, remaining = await db.purchase(7, pid, 2)
    assert serials == ["s1", "s2"]
    assert bal == 5.0 and remaining == 1
    assert isinstance(oid, int)


@test
@run
async def test_purchase_insufficient_funds(db):
    pid = await _product(db, price=5.0)
    await db.add_stock(pid, ["s1"])
    await db.ensure_user(7, "b")
    await db.add_balance(7, 4.99)
    try:
        await db.purchase(7, pid, 1)
        assert False, "should raise"
    except InsufficientFunds:
        pass
    assert await db.count_stock(pid) == 1  # untouched
    assert await db.get_balance(7) == 4.99


@test
@run
async def test_purchase_insufficient_stock(db):
    pid = await _product(db, price=1.0)
    await db.add_stock(pid, ["only"])
    await db.ensure_user(7, "b")
    await db.add_balance(7, 100)
    try:
        await db.purchase(7, pid, 2)
        assert False, "should raise"
    except InsufficientStock as e:
        assert e.available == 1
    assert await db.get_balance(7) == 100


@test
@run
async def test_purchase_zero_qty_valueerror(db):
    pid = await _product(db)
    await db.ensure_user(7, "b")
    try:
        await db.purchase(7, pid, 0)
        assert False, "should raise"
    except ValueError:
        pass


@test
@run
async def test_purchase_inactive_product(db):
    pid = await _product(db, active=False)
    await db.add_stock(pid, ["s1"])
    await db.ensure_user(7, "b")
    await db.add_balance(7, 100)
    try:
        await db.purchase(7, pid, 1)
        assert False, "should raise"
    except InsufficientStock:
        pass


@test
@run
async def test_purchase_exact_balance(db):
    pid = await _product(db, price=2.5)
    await db.add_stock(pid, ["s1"])
    await db.ensure_user(7, "b")
    await db.add_balance(7, 2.5)
    _, _, bal, _ = await db.purchase(7, pid, 1)
    assert bal == 0.0


@test
@run
async def test_purchase_race_single_winner(db):
    """5 concurrent buyers, 1 unit of stock: exactly one wins."""
    pid = await _product(db, price=1.0)
    await db.add_stock(pid, ["last-one"])
    for i in range(5):
        await db.ensure_user(100 + i, f"u{i}")
        await db.add_balance(100 + i, 10.0)
    results = await asyncio.gather(
        *[db.purchase(100 + i, pid, 1) for i in range(5)],
        return_exceptions=True,
    )
    wins = [r for r in results if not isinstance(r, Exception)]
    losses = [r for r in results if isinstance(r, InsufficientStock)]
    assert len(wins) == 1, results
    assert len(losses) == 4
    assert await db.count_stock(pid) == 0


@test
@run
async def test_purchase_race_balance_never_negative(db):
    pid = await _product(db, price=3.0)
    await db.add_stock(pid, [f"s{i}" for i in range(10)])
    await db.ensure_user(7, "b")
    await db.add_balance(7, 10.0)
    # 5 concurrent 2-unit purchases at $3 each ($6 each); balance is $10,
    # so only the first one can afford it — the rest get InsufficientFunds.
    results = await asyncio.gather(
        *[db.purchase(7, pid, 2) for _ in range(5)],
        return_exceptions=True,
    )
    wins = [r for r in results if not isinstance(r, Exception)]
    assert len(wins) == 1
    bal = await db.get_balance(7)
    assert bal >= 0, bal


@test
@run
async def test_list_orders_and_get_order(db):
    pid = await _product(db, name="P1", price=1.0)
    await db.add_stock(pid, ["s1"])
    await db.ensure_user(7, "b")
    await db.add_balance(7, 5)
    oid, _, _, _ = await db.purchase(7, pid, 1)
    orders = await db.list_orders(7)
    assert len(orders) == 1 and orders[0]["product"] == "P1"
    o = await db.get_order(oid)
    assert o["qty"] == 1 and o["status"] == "paid" and o["user_id"] == 7


@test
@run
async def test_recent_orders(db):
    pid = await _product(db, price=1.0)
    await db.add_stock(pid, ["s1", "s2"])
    await db.ensure_user(7, "b")
    await db.add_balance(7, 5)
    await db.purchase(7, pid, 1)
    await db.purchase(7, pid, 1)
    assert len(await db.recent_orders()) == 2


# ---------------- supplier API orders ----------------
@test
@run
async def test_api_place_order(db):
    pid = await _product(db, price=4.0)
    await db.add_stock(pid, ["k1", "k2"])
    kid = await db.create_api_key("r1", "hash1", "psk_ab")
    oid, serials, total = await db.api_place_order(kid, pid, 2, external_ref="ext-1")
    assert serials == ["k1", "k2"] and total == 8.0
    o = await db.get_order(oid)
    assert o["api_key_id"] == kid and o["external_ref"] == "ext-1" and o["user_id"] == 0


@test
@run
async def test_api_place_order_insufficient_stock(db):
    pid = await _product(db)
    kid = await db.create_api_key("r1", "hash1", "psk_ab")
    try:
        await db.api_place_order(kid, pid, 1)
        assert False, "should raise"
    except InsufficientStock:
        pass


@test
@run
async def test_api_place_order_bad_qty(db):
    pid = await _product(db)
    kid = await db.create_api_key("r1", "hash1", "psk_ab")
    try:
        await db.api_place_order(kid, pid, 0)
        assert False, "should raise"
    except ValueError:
        pass


# ---------------- deposits ----------------
@test
@run
async def test_create_deposit_pending(db):
    await db.ensure_user(1, "a")
    did = await db.create_deposit(1, "bKash", 5.0, 600.0, "TRX1")
    pend = await db.list_pending_deposits()
    assert len(pend) == 1 and pend[0]["id"] == did and pend[0]["status"] == "pending"


@test
@run
async def test_trxid_exists(db):
    await db.ensure_user(1, "a")
    assert await db.trxid_exists("NOPE") is False
    assert await db.trxid_exists("") is False
    await db.create_deposit(1, "bKash", 5.0, None, "DUP1")
    assert await db.trxid_exists("DUP1") is True


@test
@run
async def test_trxid_exists_rejected_excluded(db):
    await db.ensure_user(1, "a")
    did = await db.create_deposit(1, "bKash", 5.0, None, "REJ1")
    await db.reject_deposit(did)
    assert await db.trxid_exists("REJ1") is False


@test
@run
async def test_list_pending_deposits(db):
    await db.ensure_user(1, "a")
    d1 = await db.create_deposit(1, "bKash", 1.0, None, "T1")
    d2 = await db.create_deposit(1, "Nagad", 2.0, None, "T2")
    await db.approve_deposit(d1)
    pend = await db.list_pending_deposits()
    assert [d["id"] for d in pend] == [d2]


@test
@run
async def test_user_deposits(db):
    await db.ensure_user(1, "a")
    await db.ensure_user(2, "b")
    await db.create_deposit(1, "bKash", 1.0, None, "U1")
    await db.create_deposit(2, "Nagad", 2.0, None, "U2")
    mine = await db.user_deposits(1)
    assert len(mine) == 1 and mine[0]["trxid"] == "U1"


@test
@run
async def test_approve_deposit_credits_and_idempotent(db):
    await db.ensure_user(1, "a")
    did = await db.create_deposit(1, "bKash", 7.5, 900.0, "AP1")
    r = await db.approve_deposit(did)
    assert r["new_balance"] == 7.5 and r["user_id"] == 1
    assert await db.approve_deposit(did) is None  # already approved
    assert await db.get_balance(1) == 7.5  # not double-credited
    assert await db.approve_deposit(99999) is None


@test
@run
async def test_reject_deposit(db):
    await db.ensure_user(1, "a")
    did = await db.create_deposit(1, "bKash", 7.5, None, "RJ1")
    r = await db.reject_deposit(did)
    assert r["id"] == did
    assert await db.get_balance(1) == 0.0
    assert await db.reject_deposit(did) is None  # already rejected


# ---------------- reviews ----------------
async def _buyer_with_purchase(db, uid=7, pid=None):
    if pid is None:
        pid = await _product(db, price=1.0)
        await db.add_stock(pid, ["s1"])
    await db.ensure_user(uid, f"u{uid}")
    await db.add_balance(uid, 10)
    await db.purchase(uid, pid, 1)
    return pid


@test
@run
async def test_has_purchased(db):
    pid = await _buyer_with_purchase(db)
    assert await db.has_purchased(7, pid) is True
    assert await db.has_purchased(7, 99999) is False
    await db.ensure_user(8, "x")
    assert await db.has_purchased(8, pid) is False


@test
@run
async def test_add_review_and_duplicate_none(db):
    pid = await _buyer_with_purchase(db)
    rid = await db.add_review(7, pid, 5, "great", False)
    assert isinstance(rid, int)
    assert await db.has_reviewed(7, pid) is True
    assert await db.add_review(7, pid, 4, "again", False) is None  # UNIQUE


@test
@run
async def test_approved_reviews_empty_then_visible(db):
    pid = await _buyer_with_purchase(db)
    await db.add_review(7, pid, 5, "nice", True)
    assert await db.approved_reviews(pid) == []
    assert await db.set_review_status(1, "approved") is True
    revs = await db.approved_reviews(pid)
    assert len(revs) == 1 and revs[0]["anonymous"] is True


@test
@run
async def test_avg_stars(db):
    pid = await _buyer_with_purchase(db, uid=7)
    pid2 = await _product(db, name="P2", price=1.0)
    await db.add_stock(pid2, ["z1"])
    await db.ensure_user(8, "u8")
    await db.add_balance(8, 10)
    await db.purchase(8, pid2, 1)
    await db.add_review(7, pid, 5, "", False)
    await db.add_review(8, pid2, 3, "", False)
    await db.set_review_status(1, "approved")
    await db.set_review_status(2, "approved")
    avg, cnt = await db.avg_stars(pid)
    assert (avg, cnt) == (5.0, 1)
    avg2, cnt2 = await db.avg_stars(pid2)
    assert (avg2, cnt2) == (3.0, 1)
    assert await db.avg_stars(99999) == (0.0, 0)


@test
@run
async def test_set_review_status_reject(db):
    pid = await _buyer_with_purchase(db)
    await db.add_review(7, pid, 1, "bad", False)
    assert await db.set_review_status(1, "rejected") is True
    assert await db.approved_reviews(pid) == []
    assert await db.set_review_status(1, "approved") is False  # no longer pending


@test
@run
async def test_pending_reviews(db):
    pid = await _buyer_with_purchase(db)
    await db.add_review(7, pid, 4, "ok", False)
    pend = await db.pending_reviews()
    assert len(pend) == 1 and pend[0]["product"] == "Netflix"


# ---------------- API keys ----------------
@test
@run
async def test_create_list_api_keys(db):
    kid = await db.create_api_key("shop1", "h1", "psk_abc")
    keys = await db.list_api_keys()
    assert len(keys) == 1 and keys[0]["id"] == kid and keys[0]["active"] is True


@test
@run
async def test_verify_api_key(db):
    await db.create_api_key("shop1", "h1", "psk_abc")
    k = await db.verify_api_key("h1")
    assert k and k["name"] == "shop1"
    assert await db.verify_api_key("nope") is None


@test
@run
async def test_verify_api_key_revoked_none(db):
    kid = await db.create_api_key("shop1", "h1", "psk_abc")
    assert await db.revoke_api_key(kid) is True
    assert await db.verify_api_key("h1") is None
    assert await db.revoke_api_key(99999) is False


# ---------------- stats / misc ----------------
@test
@run
async def test_shop_stats(db):
    pid = await _buyer_with_purchase(db)
    await db.ensure_user(9, "banned")
    await db.set_banned(9, True)
    await db.create_deposit(7, "bKash", 1.0, None, "ST1")
    s = await db.shop_stats()
    assert s["orders"] == 1 and s["revenue"] == 1.0
    assert s["users"] == 2 and s["banned"] == 1
    assert s["pending_deposits"] == 1 and s["stock_units"] == 0


@test
@run
async def test_all_user_ids_excludes_banned(db):
    await db.ensure_user(1, "a")
    await db.ensure_user(2, "b")
    await db.set_banned(2, True)
    assert await db.all_user_ids() == [1]


@test
@run
async def test_list_users_and_count(db):
    await db.ensure_user(1, "a")
    await db.ensure_user(2, "b")
    assert await db.count_users() == 2
    users = await db.list_users()
    assert [u["id"] for u in users] == [2, 1]


@test
@run
async def test_search_users(db):
    await db.ensure_user(111, "alice_shop")
    await db.ensure_user(222, "bob")
    assert [u["id"] for u in await db.search_users("alice")] == [111]
    assert [u["id"] for u in await db.search_users("111")] == [111]
    assert await db.search_users("zzz") == []


# ---------------- utils ----------------
@test
async def test_sign_verify_cb_roundtrip():
    secret = "test-secret"
    assert verify_cb(secret, sign_cb(secret, "m:products")) == "m:products"


@test
async def test_verify_cb_tamper():
    secret = "test-secret"
    good = sign_cb(secret, "pay:1")
    bad = good[:-2] + ("ab" if not good.endswith("ab") else "cd")
    assert verify_cb(secret, bad) is None
    assert verify_cb("other-secret", good) is None
    assert verify_cb(secret, "") is None
    assert verify_cb(secret, "garbage-no-separator") is None


@test
async def test_rate_limiter():
    rl = RateLimiter(max_calls=2, window_seconds=60)
    assert rl.hit(1) is False
    assert rl.hit(1) is False
    assert rl.hit(1) is True
    assert rl.hit(2) is False  # per-user buckets
