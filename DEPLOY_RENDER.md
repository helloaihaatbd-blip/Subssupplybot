# Deploying the shop bot on Render (FREE tier)

Everything here costs $0: Render free web services + Turso free cloud database.
The bot runs in **webhook mode** (Render free can't do 24/7 long polling) and
stores all data in **Turso** (Render's disk is ephemeral — local SQLite would
be wiped on every restart).

Do the steps in order. Nothing here needs a credit card or USD payment.

---

## Step 1 — Create the free Turso database (5 min)

1. Go to **https://turso.tech** and sign up (GitHub login works).
2. Create a database:
   - **Dashboard way:** app.turso.tech → *Create Database* → name it `subssupply` →
     pick the closest region (e.g. Singapore `sin`) → Create.
   - **CLI way:**
     ```bash
     curl -sSfL https://get.tur.so/install.sh | bash
     turso auth login
     turso db create subssupply --location sin
     ```
3. Copy two values and keep them safe (you'll paste them into Render):
   - **Database URL** — looks like `libsql://subssupply-username.turso.io`
     (dashboard: database → *Connect*; CLI: `turso db show subssupply --url`)
   - **Auth token** — dashboard: database → *Data* → *Create token*;
     CLI: `turso db tokens create subssupply`
4. That's it — no tables to create. The bot builds its own schema on first start.

## Step 2 — Push the code to GitHub (5 min)

1. Create a new **private** GitHub repo, e.g. `subssupply-bot`.
2. Push the **contents** of the `nystore_bot/` folder as the repo root, so that
   `render.yaml`, `bot.py`, `api.py`, `requirements.txt` are at the top level.
   (If you push a parent folder instead, set that subfolder as *Root Directory*
   in the Render service settings later.)
3. **Never commit `.env`** — it holds the real bot token. It is already in
   `.gitignore`. Double-check with `git status` before pushing.

## Step 3 — Create the Render Blueprint (5 min)

1. Go to **https://dashboard.render.com** and sign up (GitHub login works).
2. Click **New → Blueprint**, connect your GitHub account, select the repo.
3. Render reads `render.yaml` and shows **two** free web services:
   - `subssupply-bot` — the Telegram bot (`python bot.py --webhook`)
   - `subssupply-api` — the supplier API (`uvicorn api:app`)
4. Click **Apply**. Render will ask for the secret values — fill them in:

| Variable | Service | Value |
|---|---|---|
| `BOT_TOKEN` | both | your Telegram bot token (same one in `.env`) |
| `TURSO_DATABASE_URL` | both | `libsql://…` URL from Step 1 |
| `TURSO_AUTH_TOKEN` | both | token from Step 1 |

`ADMIN_IDS` is pre-filled (`7383329076`), `HMAC_SECRET` auto-generates,
and `WEBHOOK_URL` is detected automatically from Render's own URL —
you don't need to set those.

5. Wait for both services to show **Live** (first build takes a few minutes).

## Step 4 — Verify (2 min)

1. Open **@subssupplybot** on Telegram and send `/start`.
   - You should see the shop home menu (the force-join check for
     https://t.me/subsmaart applies as before).
2. Send `/admin` — the admin panel should open.
3. In the admin panel → **Payment settings**: set your bKash/Nagad/Rocket
   numbers, Binance/Bybit usernames, USD→BDT rate and cash-out %.
4. Add a product + upload a stock `.txt` file, then do a test purchase with
   a second Telegram account to confirm serials are delivered.
5. Check the API: open `https://subssupply-api.onrender.com/health`
   (use your actual Render URL) — it should return `{"ok":true}`.

The webhook is registered with Telegram automatically on startup
(`setWebhook`), so there is nothing to configure on Telegram's side.
If you ever redeploy, the bot re-registers it.

## Step 5 — Going live checklist

- [ ] Payment numbers / rates set in `/admin` → Payment settings
- [ ] Products added with stock files
- [ ] Test purchase works end-to-end (wallet → buy → serials delivered)
- [ ] Support username correct (`@subsmartbd` by default)

---

## Known free-tier limitations (accepted)

- **Sleep:** Render free services sleep after ~15 minutes with no traffic.
  The next Telegram message wakes the bot; the first reply after waking takes
  **~30 seconds**, then everything is instant again. (Telegram holds and
  redelivers the update, so no message is lost.)
- **No local backups:** the daily `backups/*.db` files are disabled in Turso
  mode — Turso itself replicates your data, so this is safe.
- **Cold DB:** every query is a network round-trip to Turso (~50–150 ms).
  For this shop's traffic that's unnoticeable.

## If something breaks

- **Bot doesn't reply at all:** Render dashboard → `subssupply-bot` → *Logs*.
  Common causes: `BOT_TOKEN` typo, or `TURSO_*` vars missing (the service
  would fall back to local SQLite and lose data on restart — don't run like
  that; set the Turso vars).
- **`/start` works but buttons say "Invalid button":** `HMAC_SECRET` changed
  (happens if you manually reset env vars). Just open a fresh menu with
  `/start` — old buttons expire by design.
- **Switching back to polling/VPS later:** just run `python bot.py` on any
  server with the same Turso vars (or unset them to use local SQLite again).
  To point Telegram back at polling, delete the webhook:
  `https://api.telegram.org/bot<TOKEN>/deleteWebhook`, then start polling.
