# NYStore Bot (V2)

Telegram digital-products shop bot in the NY Store style: product list with
live stock indicators, wallet, manual deposits (bKash / Nagad / Rocket /
Binance / Bybit), referral program, product reviews, force-join channel gate,
full admin panel, and a supplier HTTP API (ProdSeller model).

## 1. Setup

```bash
cd ~/workspace/nystore_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and set BOT_TOKEN (from @BotFather -> /newbot)
```

### Admin access

Only **7383329076** can open `/admin` by default. To use a different (or
additional) admin, set in `.env`:

```
ADMIN_IDS=123456789,987654321
```

To find your Telegram user ID, message **@userinfobot** on Telegram — it
replies with your numeric ID.

`HMAC_SECRET` is generated automatically on first run and saved to `.env`
(it signs all inline-button callbacks).

### Payment settings (in-bot)

Open `/admin` → **⚙️ Payment settings** and set:

| Setting | What it does |
|---|---|
| USD → BDT rate | Used by the deposit BDT calculator |
| Min / Max deposit | $0.20 – $100 defaults, changeable |
| bKash / Nagad / Rocket number | Shown to buyers on deposit |
| Binance / Bybit username/UID | Shown to buyers on deposit |
| Cash-out % (per mobile method) | Deducted in the BDT shown to the buyer |
| Force-join channel link | Default `https://t.me/subsmaart` |

Deposit flow: buyer picks a method → enters USD amount → bot shows the exact
BDT to send (rate minus cash-out %) and the number → buyer submits the
Transaction ID → request lands in **Admin → 💰 Deposits** → approve credits
the wallet instantly, reject notifies the buyer. Duplicate Transaction IDs
are rejected automatically.

### Force join

Every protected action checks channel membership via `getChatMember`.
Users who haven't joined see **📢 Join Community** + **✅ I Have Joined**.
Change the channel in Admin → Payment settings → force-join link.

### Support

The 🆘 menu links to **@subsmartbd** (change via `SUPPORT_USERNAME` in `.env`).

## 2. Run the bot

```bash
source venv/bin/activate
python bot.py
```

Self-test (verifies the token, polls 15s, exits cleanly):

```bash
python bot.py --selftest
```

Offline end-to-end test (spins up a local mock Telegram server — useful
where the real API is unreachable; exercises /start, force-join and menus):

```bash
python bot.py --selftest-mock
```

## 3. Supplier API (for reseller bots)

The bot can act as a **supplier** (like ProdSeller). Start the API server:

```bash
source venv/bin/activate
uvicorn api:app --host 0.0.0.0 --port 8000
```

Create a key in Telegram: `/admin` → **🔑 API keys** → Generate. The full
`psk_...` key is shown once — resellers send it as the `X-API-Key` header.

### Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/products` | Active products with live stock |
| GET | `/products/{id}/price` | Price + stock for one product |
| POST | `/orders` | Reserve stock. Body: `{"product_id": 1, "qty": 5, "external_ref": "optional"}`. Returns `order_id`, `serials[]`, `total`. `409` if stock is insufficient |
| GET | `/orders/{id}` | Order status (only your own key's orders) |
| GET | `/health` | Liveness check |

Example:

```bash
curl -H "X-API-Key: psk_..." http://localhost:8000/products
curl -X POST -H "X-API-Key: psk_..." -H "Content-Type: application/json" \
  -d '{"product_id":1,"qty":2}' http://localhost:8000/orders
```

### Pulling from an upstream supplier

`/admin` → **⚙️ Payment settings** → set **Supplier base URL** and
**Supplier API key** (point them at another bot running this same `api.py`).
Then **🔄 Sync supplier** pulls that catalog and updates local prices/stock
for products that have a matching **Supplier SKU**. With nothing configured,
the built-in stub is used and the bot simply runs on local stock
(`supplier.py`).

## 4. Features

- **Shop:** single product list with stock emoji (🔴 out / 🟠 1–10 / 🟢 11+),
  detail page, quantity picker, confirm page with balance/shortfall,
  atomic pay (exactly one buyer wins the last item), `.txt` serial delivery,
  re-download via `/recover` or 📦 Orders.
- **Wallet:** balance, referral earnings, total spent, deposits, deposit
  history with status (🟡/✅/❌). No withdrawals.
- **Referral:** unique link per user, $0.01 to the referrer only after the
  invitee's first `/start` **and** channel join. Self/duplicate referrals blocked.
- **Reviews:** buyers only, 1–5 stars + comment + anonymous option, admin
  approval before going public, one review per user per product.
- **Admin:** users list/search, add/remove balance (never negative),
  ban/unban, deposit approve/reject, review approve/reject, product
  add/edit/delete, stock upload via `.txt` / clear stock, payment settings,
  API keys, stats, supplier sync, text & photo broadcast (failed chats skipped).
- **Security:** HMAC-signed callbacks, admin ID re-checked server-side,
  per-user rate limiting, idempotent pay (double-tap = one order), atomic
  stock reservation, parameterized SQL, strict input validation, force-join
  on every protected action, banned users blocked everywhere.
- **Backups:** automatic daily SQLite backup to `backups/` (keeps 7).

## 5. Deploy

### Render / Railway
- New **Worker** (not web) service, repo with this folder.
- Build: `pip install -r requirements.txt` · Start: `python bot.py`
- Add env vars from `.env` in the dashboard (never commit `.env`).
- For the supplier API, add a second **Web** service: start
  `uvicorn api:app --host 0.0.0.0 --port $PORT`.

### VPS (systemd)
```ini
# /etc/systemd/system/nystore-bot.service
[Unit]
Description=NYStore Telegram bot
After=network.target

[Service]
WorkingDirectory=/opt/nystore_bot
ExecStart=/opt/nystore_bot/venv/bin/python bot.py
Restart=always
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now nystore-bot
```

## 6. Project layout

```
bot.py            # entry point, routers, guards, backup job
config.py         # .env config (token only in .env)
db.py             # async SQLite layer, atomic transactions
utils.py          # HMAC signing, rate limit, validation
supplier.py       # supplier adapter interface + stub + HTTP impl
api.py            # FastAPI supplier API
handlers/
  common.py       # ban / force-join guards, admin notify
  shop.py         # menu, products, buy flow, orders, reviews
  wallet.py       # wallet, manual deposits, referral page
  admin.py        # full admin panel
backups/          # daily SQLite backups (auto)
data/             # bot.db lives here (gitignored)
```
