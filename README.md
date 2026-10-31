# Instadeals

Telegram deal monitoring with a separate Swiggy OAuth account for every user. Python 3.11+, SQLite, official MCP SDK, python-telegram-bot. No LLM API needed.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
cp .env.example .env
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the generated key in `TOKEN_ENCRYPTION_KEY`, your BotFather token in `TELEGRAM_BOT_TOKEN`, and protect `.env` with `chmod 600 .env`. Keep the key stable and back it up separately from SQLite. Set `ALLOWED_USER_IDS` for invite-only use, or explicitly set `PUBLIC_ACCESS=true` to admit any Telegram user. Public mode does not grant access to anyone else's Swiggy session.

Set `OAUTH_CALLBACK_URL=https://bot.yourdomain.com/callback`. The domain needs valid HTTPS and a reverse proxy to this process on `127.0.0.1:8765`. A domain alone is insufficient: the bot must run on a reachable, always-on machine. Swiggy must accept the exact callback URI during dynamic client registration; coordinate production access with Swiggy if registration rejects it. For same-machine local testing, set `OAUTH_CALLBACK_URL=http://localhost:8765/callback` and keep `OAUTH_BIND_HOST=127.0.0.1`. Open login links in Brave on the Mac running the bot; opening them on a phone will not work. The bot labels local login links accordingly. Without the callback setting the bot still runs, explains the setup requirement, and saves preferences, but cannot connect accounts.

Example Caddy reverse proxy on the same host:

```caddyfile
bot.yourdomain.com {
    @callback path /callback
    reverse_proxy @callback 127.0.0.1:8765
    respond "Not found" 404
}
```

Use your real hostname, point its DNS at your server and allow HTTPS traffic. Keep the callback port private. Avoid proxy access logs that retain OAuth query parameters. Run exactly one process against the database; multiple replicas are unsupported.

```sh
.venv/bin/instadeals
```

## User flow

Press Start once to reveal the persistent menu. Daily use is button-based:

- **Connect Swiggy** opens a sign-in button. Complete phone/OTP on Swiggy, tap **Return to Telegram**, and press Start to confirm the same-account login.
- **My locations** lets you choose saved Swiggy addresses, review selections, and remove locations.
- **Discount settings** offers 40–90% presets and a custom percentage prompt.
- **Monitoring status** shows the latest scan and a refresh button.
- **Pause alerts / Resume alerts** controls monitoring.
- **Account** offers reconnect and a confirmed disconnect action.
- **How it works** explains coverage and alert timing.

Commands remain available as optional shortcuts. Full Google Maps links and Telegram pins are still accepted, but signed-in search may require a saved Swiggy address. Select saved addresses for the simplest setup.

Disconnect stops monitoring, deletes local credentials and pending logins, and attempts remote session revocation. Locations remain and alerts pause. The response reports remote revocation failures.

A new `/connect` invalidates that user's earlier pending links. Pending state lives only in memory, expires, is single-use and is invalidated by restart. PKCE protects code exchange. Callback success does not activate credentials until the originating Telegram user confirms. Do not forward login links or confirmation codes.

## Account isolation

Access and rotating refresh tokens are encrypted with Fernet in the `accounts` SQLite table. The encryption key comes only from the environment. Each account gets its own persistent MCP session and refresh lock. A generation marker rejects stale writes after disconnect/reconnect. Independent worker backoff prevents an expired account from stopping others; all search calls share pacing to avoid bursts.

Locations, thresholds and delivery history belong to Telegram users. Search results and price history are scoped to user + connection generation + coordinates. Even identical coordinates across accounts are fetched independently; identical coordinates within one account share a scan. Reconnecting starts a fresh comparison/deduplication scope because the Swiggy account may have changed.

**Migration:** the old shared `.swiggy-oauth.json` and `SWIGGY_ACCESS_TOKEN` are never read by this version. No shared credentials are automatically assigned to any user. Existing locations/preferences remain, but every user must `/connect`. The retired `--login`/`--inspect` modes have been removed. Old local credential files are left untouched for the operator to retire.

## Deal checks and limitations

Default: 50% off MRP, ₹100 minimum savings and two stable observations within three polling intervals. Invalid prices, unknown stock and unavailable variants don't qualify. Discount is `(MRP - offerPrice) / MRP × 100`; basket, bank, membership and coupon discounts aren't added. History rejects increased MRPs and prices above a recently observed low. First-seen MRP cannot independently be verified as market value.

Configure `SEARCH_QUERIES` and `MAX_PAGES`. For each location, results from all fetched queries and pages are ranked by discount percentage, highest first, with larger rupee savings breaking ties. Qualifying alerts follow that order. This ranks only fetched results: coverage is bounded search, not a full-catalog feed. Default scans wait 300 seconds after completion; confirmations add latency. Global request spacing and backoff increase latency with load. Notifications contain product, price, discount, location, and optional image/link.

A new alert needs a 5% improvement over the last notified price in the same account scope. A reservation before sending avoids duplicates after ambiguous delivery failures, but can lose an alert on timeout/crash. Explicit Telegram rate-limit rejections allow later retry. Blocked chats pause. Images rejected by Telegram fall back to text.

Price history expires after `HISTORY_DAYS`; delivery history persists. SQLite also contains precise locations, so restrict filesystem access and backups. Serve production traffic only after confirming rate/callback arrangements with Swiggy. This implementation has automated multi-user tests; a real two-account end-to-end login awaits a configured public callback.

## Development

```sh
.venv/bin/pytest -q
```

`accounts.py`: encrypted credentials, OAuth callback and refresh. `source.py`: MCP schema discovery, pagination and pacing. `products.py`: normalization. `db.py`: persistence and deal qualification. `app.py`: Telegram commands and account-scoped workers. [MCP discovery notes](docs/mcp-discovery.md).

## Signed-in address requirement

Live testing found that some authenticated searches reject guest coordinates with `addressId is required` despite the advertised schema. This is not an expired login. Use `/addresses` to retrieve your own saved Swiggy addresses, then `/link LOCATION_ID ADDRESS_NUMBER` to explicitly select the matching address. If absent, add it in Swiggy first. Choices expire after ten minutes; reconnecting invalidates links. Editing a location clears its address selection. The bot never creates Swiggy addresses or substitutes another location automatically.

## Saved address picker

After `/confirm`, the bot fetches saved Swiggy addresses and shows inline buttons. `/addresses` also opens the picker. Selection adds an account-scoped monitored location using the returned address ID; coordinates remain unknown rather than being invented. Buttons expire after ten minutes and cannot be used by another Telegram account or after reconnecting. Existing coordinate-only locations are retained; use `/remove ID` to remove unwanted ones. A location requiring an address no longer blocks scanning other selected addresses.

## Buy and Share buttons

New deal alerts include Buy now and Share in both photo and text formats. Buy now uses an HTTPS product URL supplied by the provider when available; it does not place an order. Telegram/OS app-link handling determines whether it opens Swiggy/Instamart or a browser. When MCP omits product URLs, the bot uses `https://instamart.in/item/{productId}?share=true`. This productId mapping was verified against a product link shared from the Instamart app and its matching live MCP search result. SKU/SPIN identifiers are never substituted. Several pack variants may share a product page, so generated-link alerts ask users to check the pack size and delivery address. If both URL and valid productId are missing, the button explicitly falls back to the Instamart homepage. Share opens Telegram’s recipient picker with product name, prices and a public link; it excludes private saved-location labels, coordinates and account data. Previously sent messages are not retroactively changed.
