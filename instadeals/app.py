import argparse
import asyncio
import contextlib
import logging
import math
import random
import sqlite3
import secrets
import time
from collections import defaultdict
from datetime import timedelta, datetime
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters
from .config import Settings
from .db import Database
from .locations import maps_location
from .deal_actions import deal_actions
from .source import connect, failure_status, SourceError
from .accounts import Accounts, OAuthServer
from .ui import MenuUI, HOME, keyboard
from .offers import rank_products

log = logging.getLogger("instadeals")
HELP = """Instamart deal monitor
/connect — connect your own Swiggy account
/disconnect — disconnect Swiggy and pause alerts
/addresses — list your saved Swiggy addresses
/link LOCATION_ID ADDRESS_NUMBER — select the matching delivery address
/add NAME LAT LON — save a location (quote names with spaces)
/update ID NAME LAT LON — replace a location
/remove ID — delete a location
/threshold 50 — minimum % off MRP (1–99)
/pause or /resume — notifications
/settings — locations and preferences
/status — monitor health
/help — this menu
Alerts require repeated observations, minimum savings and price-history checks. Search coverage is limited to configured queries. Prices may change before checkout."""


def retry_delay(error):
    """Preserve Retry-After even when SDK task groups wrap transport errors."""
    delay = 0
    response = getattr(error, "response", None)
    if response is not None:
        value = response.headers.get("Retry-After", "0")
        try:
            delay = max(0, float(value))
        except ValueError:
            try:
                delay = max(0, parsedate_to_datetime(value).timestamp()-time.time())
            except (ValueError, TypeError):
                pass
    for child in getattr(error, "exceptions", ()):
        delay = max(delay, retry_delay(child))
    return delay


class Bot(MenuUI):
    def __init__(self, settings, db):
        self.s, self.db = settings, db
        self.health = "Waiting for first scan"
        self.task = None
        self.workers = {}
        self.address_choices = {}
        self.address_menus = {}
        self.ui_inputs = {}
        self.user_health = {}
        self.accounts = Accounts(db,settings.encryption_key) if settings.encryption_key else None
        self.oauth = OAuthServer(self.accounts,settings.oauth_host,settings.oauth_port) if self.accounts else None

    async def command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.effective_chat or update.effective_chat.type != "private":
            return
        uid = update.effective_user.id
        if not self.s.public_access and uid not in self.s.users:
            await update.message.reply_text("This bot is private. Ask its operator for access.")
            return
        self.db.user(uid)
        import shlex
        try:
            parts = shlex.split(update.message.text)
            cmd, args = parts[0].split('@')[0], parts[1:]
            if cmd == "/start" and args and args[0].startswith("confirm_"):
                cmd,args="/confirm",[args[0][8:]]
            message = "Saved."
            if cmd == "/start":
                await update.message.reply_text("Use the buttons below to manage your deal monitor.",reply_markup=HOME)
                await self.menu(uid,update.message,intro=True)
                return
            elif cmd == "/connect":
                if not self.s.callback_url:
                    message = "Swiggy login is awaiting the operator's public HTTPS callback setup. Your preferences can still be saved."
                else:
                    try:
                        link = await self.accounts.begin(uid,self.s.callback_url)
                        message = "Sign in to YOUR Swiggy account here (link expires in 15 minutes):\n"+link+"\n\nThen return here with the /confirm code shown by the callback page. Do not forward this link."
                        if self.s.callback_url.startswith("http://localhost:"):
                            message += "\n\nLocal test: open this link in Brave on the Mac running the bot, not on your phone."
                    except ValueError as e:
                        message = str(e)
                    except Exception:
                        message = "Couldn't create the Swiggy login link. Please try /connect later."
            elif cmd == "/addresses":
                await self.show_addresses(uid,update.message)
                return
            elif cmd == "/link":
                if len(args)!=2:
                    raise ValueError("Use /link LOCATION_ID ADDRESS_NUMBER after /addresses")
                generation,expires,rows=self.address_choices.get(uid,(None,0,[]))
                index=int(args[1])-1
                if generation!=self.accounts.generation(uid) or expires<time.time() or not 0<=index<len(rows):
                    raise ValueError("Run /addresses again and select a listed address number")
                address_id=rows[index].get('id')
                if not address_id:
                    raise ValueError("Swiggy returned an address without an ID")
                changed=self.db.run("UPDATE locations SET swiggy_address_id=?,swiggy_generation=? WHERE id=? AND user_id=?",(address_id,generation,int(args[0]),uid)).rowcount
                if not changed:
                    raise ValueError("Location not found")
                await self.cancel_worker(uid)
                message="Saved Swiggy delivery address linked. Monitoring will retry with this address."
            elif cmd == "/confirm":
                if len(args)!=1:
                    raise ValueError("Use /confirm CODE from your completed login page")
                self.accounts.confirm(uid,args[0])
                await self.cancel_worker(uid)
                self.db.run("UPDATE users SET enabled=1 WHERE id=?",(uid,))
                message = "Your Swiggy account is connected. Choose a saved delivery address below to start monitoring."
            elif cmd == "/disconnect":
                self.db.run("UPDATE users SET enabled=0 WHERE id=?",(uid,))
                await self.cancel_worker(uid)
                revoked = await self.accounts.disconnect(uid)
                self.db.run("UPDATE users SET enabled=0 WHERE id=?",(uid,))
                self.user_health.pop(uid,None)
                message = "Swiggy disconnected locally and alerts paused. Saved locations remain. Use /connect to reconnect."
                if not revoked:
                    message += " Swiggy could not confirm remote session revocation; sign out of the session in Swiggy if needed."
            elif cmd == "/help":
                message = HELP
            elif cmd in ("/add", "/update"):
                expected = 3 if cmd == "/add" else 4
                if len(args) != expected:
                    raise ValueError("Use /add NAME LAT LON or /update ID NAME LAT LON")
                name, lat, lon = args[-3], float(args[-2]), float(args[-1])
                if not name.strip() or len(name) > 60 or not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                    raise ValueError("Use a name up to 60 characters and valid latitude/longitude")
                lat, lon = round(lat,6), round(lon,6)
                if cmd == "/add":
                    if len(self.db.locations(uid)) >= 10:
                        raise ValueError("Maximum 10 locations per user")
                    self.db.run("INSERT INTO locations(user_id,name,lat,lon) VALUES(?,?,?,?)", (uid,name,lat,lon))
                else:
                    changed = self.db.run("UPDATE locations SET swiggy_address_id=NULL,swiggy_generation=NULL,name=?,lat=?,lon=? WHERE id=? AND user_id=?", (name,lat,lon,int(args[0]),uid)).rowcount
                    if not changed:
                        raise ValueError("Location not found")
            elif cmd == "/remove":
                if len(args) != 1:
                    raise ValueError("Use /remove ID")
                if not self.db.run("DELETE FROM locations WHERE id=? AND user_id=?", (int(args[0]),uid)).rowcount:
                    raise ValueError("Location not found")
            elif cmd == "/threshold":
                if len(args) != 1 or not 1 <= int(args[0]) <= 99:
                    raise ValueError("Use /threshold 1–99")
                self.db.run("UPDATE users SET threshold=? WHERE id=?", (int(args[0]),uid))
            elif cmd in ("/pause", "/resume"):
                self.db.run("UPDATE users SET enabled=? WHERE id=?", (int(cmd == "/resume"),uid))
            elif cmd == "/settings":
                u = self.db.user(uid)
                message = f"Notifications: {'on' if u['enabled'] else 'paused'} | Minimum: {u['threshold']}%\n"
                message += '\n'.join(f"{r['id']}: {r['name']}" + (f" ({r['lat']}, {r['lon']})" if r['lat'] is not None else " (saved Swiggy address)") for r in self.db.locations(uid)) or "No locations."
                message += f"\nOffer mode: {u['offer_mode']} | Sort: {u['sort_order']}"
                message += "\nSwiggy: " + ("connected" if self.accounts and self.accounts.generation(uid) else "not connected — /connect")
                message += f"\nQueries: {', '.join(self.s.queries)}\nPolling: {self.s.poll}s; confirmations: {self.s.confirmations}; min savings: ₹{self.s.savings/100:.0f}"
            elif cmd == "/status":
                message = self.user_health.get(uid, "Connect Swiggy with /connect, add a location, and enable alerts with /resume.")
            await update.message.reply_text(message)
            if cmd == "/confirm":
                await self.show_addresses(uid,update.message)
        except (ValueError, OverflowError) as e:
            await update.message.reply_text(str(e))
        except sqlite3.IntegrityError:
            await update.message.reply_text("That location name already exists. Use /update.")

    async def show_addresses(self, uid, message):
        generation=self.accounts.generation(uid)
        if not generation:
            await message.reply_text("Use /connect first to sign in to your Swiggy account.")
            return
        try:
            async with connect(self.s,self.accounts,uid,generation) as source:
                rows=await source.addresses()
            if self.accounts.generation(uid)!=generation:
                return
            rows=[r for r in rows if isinstance(r,dict) and r.get('id')]
            self.address_choices[uid]=(generation,time.time()+600,rows)
            nonce=secrets.token_hex(6)
            self.address_menus[uid]=nonce
            if not rows:
                await message.reply_text("No saved addresses found. Add your delivery address in Swiggy, then send /addresses again.")
                return
            for start in range(0,len(rows),8):
                lines=["Choose a saved Swiggy address to monitor:"]
                buttons=[]
                for i,r in enumerate(rows[start:start+8],start):
                    name=str(r.get('addressTag') or r.get('addressCategory') or 'Address')[:60]
                    lines.append(f"{i+1}. {name} — {str(r.get('addressLine',''))[:160]}")
                    buttons.append([InlineKeyboardButton(f"{i+1}. {name}",callback_data=f"address:{nonce}:{i}")])
                buttons.append([InlineKeyboardButton("Back to menu",callback_data="ui:home")])
                await message.reply_text('\n\n'.join(lines)+"\n\nTap an address below. You can select more than one. Choices expire in 10 minutes.",reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            await message.reply_text(failure_status(e)[1])

    async def select_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query=update.callback_query
        uid=update.effective_user.id
        if not update.effective_chat or update.effective_chat.type!='private' or (not self.s.public_access and uid not in self.s.users):
            await query.answer("Not available in this chat.",show_alert=True)
            return
        try:
            _,nonce,index=query.data.split(':')
            generation,expires,rows=self.address_choices.get(uid,(None,0,[]))
            index=int(index)
            if nonce!=self.address_menus.get(uid) or expires<time.time() or generation!=self.accounts.generation(uid) or not 0<=index<len(rows):
                raise ValueError("These choices expired. Send /addresses again.")
            row=rows[index]
            address_id=row['id']
            locations=self.db.locations(uid)
            if any(r['swiggy_address_id']==address_id and r['swiggy_generation']==generation for r in locations):
                await query.answer("Already selected.")
                return
            if len(locations)>=10:
                raise ValueError("Maximum 10 locations. Remove one using /remove ID.")
            name=str(row.get('addressTag') or row.get('addressCategory') or 'Swiggy address')[:60]
            base=name
            number=2
            while name in {r['name'] for r in locations}:
                name=f"{base[:50]} ({number})"
                number+=1
            self.db.user(uid)
            self.db.run('INSERT INTO locations(user_id,name,lat,lon,swiggy_address_id,swiggy_generation) VALUES(?,?,NULL,NULL,?,?)',(uid,name,address_id,generation))
            await query.answer("Address saved")
            await self.cancel_worker(uid)
            enabled=self.db.user(uid)['enabled']
            self.user_health[uid]=f"{name} selected. A fresh scan is queued." if enabled else "Alerts paused. Tap Resume alerts to start monitoring."
            await query.message.reply_text(f"Selected {name}. " + ("Monitoring will check this saved Swiggy address." if enabled else "Alerts are paused; use /resume to monitor.") + " You can add more addresses without replacing this one.",reply_markup=keyboard([[('Add another address','addresses')],[('My locations','locations'),('Back to menu','home')]]))
        except (ValueError,KeyError):
            await query.answer("Choices unavailable. Send /addresses again, or remove a location if you reached the limit.",show_alert=True)

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.effective_chat or update.effective_chat.type != "private":
            return
        if not self.s.public_access and update.effective_user.id not in self.s.users:
            await update.message.reply_text("This bot is private. Ask its operator for access.")
            return
        value = (update.message.text or "").strip()
        uid=update.effective_user.id
        actions={'Menu':'home','My locations':'locations','Discount settings':'discount','Monitoring status':'status'}
        if value in actions:
            self.ui_inputs.pop(uid,None)
            await self.ui_action(update,context,actions[value])
            return
        if self.ui_inputs.get(uid,0)>time.time():
            if value.isascii() and value.isdigit() and 1<=int(value)<=99:
                self.db.user(uid)
                self.db.run('UPDATE users SET threshold=? WHERE id=?',(int(value),uid))
                self.ui_inputs.pop(uid,None)
                await update.message.reply_text(f'Minimum discount saved: {int(value)}%.',reply_markup=keyboard([[('Back to menu','home')]]))
            else:
                await update.message.reply_text('Please send a whole number from 1 to 99.',reply_markup=keyboard([[('Cancel','cancel')]]))
            return
        mapped = maps_location(value)
        if mapped:
            uid = update.effective_user.id
            self.db.user(uid)
            name, lat, lon = mapped
            rows = self.db.locations(uid)
            if any(r['lat']==lat and r['lon']==lon for r in rows):
                message = "This delivery location is already saved. Use /settings to review it."
            elif len(rows)>=10:
                message = "Maximum 10 locations. Remove one with /remove ID."
            else:
                names={r['name'] for r in rows}
                base=name
                number=2
                while name in names:
                    name=f"{base[:50]} ({number})"
                    number+=1
                self.db.run("INSERT INTO locations(user_id,name,lat,lon) VALUES(?,?,?,?)",(uid,name,lat,lon))
                message=f"Saved {name} ({lat}, {lon}). Use /settings to review your locations. Monitoring runs when Swiggy is connected and alerts are on."
        elif value.startswith('https://'):
            message = "I couldn't extract a delivery pin from that link. Send a full Google Maps place link with coordinates, a Telegram location pin, or /add Home LATITUDE LONGITUDE."
        elif len(value) == 6 and value.isascii() and value.isdigit():
            message = (
                f"{value} looks like a postal PIN code. It hasn't been saved as a location. "
                "A PIN code covers an area; I need your delivery pin to check local stock and prices.\n\n"
                "On Telegram mobile, tap 📎 → Location and send a pin for your delivery address. "
                "You can choose a pin on the map instead of sharing your current location.\n\n"
                "Or send /add Home LATITUDE LONGITUDE."
            )
        else:
            message = ("To add a delivery location, tap 📎 → Location and send a map pin, "
                       "or use /add Home LATITUDE LONGITUDE. Send /help for all commands.")
        await update.message.reply_text(message)

    async def location(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.effective_chat or update.effective_chat.type != "private":
            return
        uid = update.effective_user.id
        if not self.s.public_access and uid not in self.s.users:
            await update.message.reply_text("Open the setup link and press Start first.")
            return
        self.db.user(uid)
        pin = update.message.location
        lat, lon = round(pin.latitude, 6), round(pin.longitude, 6)
        if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            await update.message.reply_text("That location is invalid.")
            return
        rows = self.db.locations(uid)
        if any(r['lat'] == lat and r['lon'] == lon for r in rows):
            await update.message.reply_text("This location is already saved. Use /settings to view it.")
            return
        if len(rows) >= 10:
            await update.message.reply_text("Maximum 10 locations. Remove one with /remove ID.")
            return
        names = {r['name'] for r in rows}
        number = 1
        while f"Pinned location {number}" in names:
            number += 1
        name = f"Pinned location {number}"
        self.db.run("INSERT INTO locations(user_id,name,lat,lon) VALUES(?,?,?,?)", (uid,name,lat,lon))
        await update.message.reply_text(f"Saved {name} ({lat}, {lon}). Use /settings to view it or /update to rename it.")

    async def notify(self, bot, uid, area, name, p):
        if not self.db.reserve(uid,area,p):
            return
        text = f"{p.name}\n₹{p.price/100:.2f} · MRP ₹{p.mrp/100:.2f} · {p.discount:.1f}% off\n📍 {name}\nObserved across {self.s.confirmations}+ scans. Price/stock may change."
        if p.offers:
            text += "\nOffer badge: " + "; ".join(p.offers) + "\nCheck required quantities and terms in Instamart. Badge savings are not included above."
        actions,hint=deal_actions(p)
        text+=hint
        try:
            if p.image and len(text) <= 1024:
                try:
                    await bot.send_photo(uid, p.image, caption=text,reply_markup=actions)
                except BadRequest:
                    await bot.send_message(uid, text,reply_markup=actions)
            else:
                await bot.send_message(uid, text,reply_markup=actions)
        except RetryAfter as e:
            # Telegram explicitly rejected delivery; safe to permit a future retry.
            self.db.run("DELETE FROM notifications WHERE user_id=? AND area=? AND sku=?", (uid,area,p.sku))
            delay = e.retry_after.total_seconds() if isinstance(e.retry_after,timedelta) else e.retry_after
            await asyncio.sleep(delay)
            return
        except Forbidden:
            self.db.run("UPDATE users SET enabled=0 WHERE id=?", (uid,))
            return
        except TelegramError:
            # Timeouts are ambiguous: keep reservation to avoid duplicate alerts.
            self.db.run("UPDATE notifications SET status='uncertain' WHERE user_id=? AND area=? AND sku=?", (uid,area,p.sku))
            log.warning("Telegram delivery uncertain; reservation retained")
            return
        self.db.run("UPDATE notifications SET status='sent' WHERE user_id=? AND area=? AND sku=?", (uid,area,p.sku))

    async def scan(self, source, bot, account_uid=None, generation=None):
        scan_started = time.monotonic()
        groups = defaultdict(list)
        for row in self.db.targets():
            if account_uid is None or row['user_id'] == account_uid:
                scope = f"{row['user_id']}:{generation or 'test'}:{self.db.area(row['lat'], row['lon'])}:{row['swiggy_address_id'] or ''}"
                groups[scope].append(row)
        self.db.cleanup(self.s.history_days)
        skipped=[]
        checked=[]
        product_count=0
        ordered=sorted(groups.items(),key=lambda item: not bool(item[1][0]['swiggy_address_id']))
        for area, rows in ordered:
            self.user_health[account_uid]=f"Scanning {rows[0]['name']}…\n{len(checked)} of {len(groups)} locations checked. This may take a moment."
            address_id=rows[0]['swiggy_address_id'] if rows[0]['swiggy_generation']==generation else None
            try:
                if address_id:
                    products=await source.search(rows[0]['lat'],rows[0]['lon'],address_id=address_id)
                elif rows[0]['lat'] is None:
                    skipped.append(rows[0]['name'])
                    continue
                else:
                    products=await source.search(rows[0]['lat'],rows[0]['lon'])
            except SourceError as e:
                if e.kind!='address_required':
                    raise
                skipped.append(rows[0]['name'])
                continue
            if account_uid is not None and self.accounts.generation(account_uid) != generation:
                return
            checked.append(rows[0]['name'])
            product_count+=len(products)
            now = time.time()
            present = {p.sku for p in products}
            for previous in self.db.run('SELECT sku FROM observations WHERE area=?', (area,)).fetchall():
                if previous['sku'] not in present:
                    self.db.run('UPDATE observations SET streak=0 WHERE area=? AND sku=?', (area,previous['sku']))
            # Rank across all fetched queries/pages for this location. Prefer
            # larger rupee savings on equal percentages; SKU keeps ties stable.
            ranked = rank_products(products, self.db.user(rows[0]['user_id'])['sort_order'])
            for p in ranked:
                # Broad searches can take longer than the idle polling interval.
                # Include scan time so normal full scans do not reset confirmations.
                observation_window = 3 * (self.s.poll + time.monotonic() - scan_started)
                if not self.db.observe(area,p,now,observation_window,self.s.confirmations,0):
                    continue
                notified = set()
                for row in rows:
                    uid = row['user_id']
                    # Commands may run during network awaits; re-check ownership/preferences.
                    u = self.db.user(uid)
                    current = self.db.run("SELECT * FROM locations WHERE id=? AND user_id=?", (row['id'],uid)).fetchone()
                    if uid in notified or not u['enabled'] or not current or self.db.area(current['lat'],current['lon']) != self.db.area(rows[0]['lat'],rows[0]['lon']):
                        continue
                    if account_uid is not None and self.accounts.generation(account_uid) != generation:
                        return
                    qualifies = ((p.discount > 0 or bool(p.offers)) if u['offer_mode']=='all'
                                 else p.discount >= u['threshold'] and p.mrp-p.price >= self.s.savings)
                    if qualifies:
                        await self.notify(bot,uid,area,current['name'],p)
                        notified.add(uid)
        finished=datetime.now(ZoneInfo('Asia/Dubai')).strftime('%d %b, %I:%M %p')
        self.user_health[account_uid] = (
            f"Last scan: {finished} (Dubai time)\n"
            f"Checked: {', '.join(checked) if checked else 'No locations'}\n"
            f"Products checked: {product_count}\nNext scan in about {self.s.poll//60} minutes."
        )
        if skipped:
            self.user_health[account_uid]+="\n\nNeeds a saved Swiggy address: "+', '.join(skipped)+". Open My locations to manage these entries. Other selected addresses continue monitoring."

    async def cancel_worker(self, uid):
        worker = self.workers.pop(uid,None)
        if worker:
            worker[1].cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker[1]

    async def monitor_user(self, bot, uid, generation):
        failures = 0
        while self.accounts.generation(uid)==generation:
            try:
                self.user_health[uid]="Preparing a fresh scan of your selected locations…"
                async with connect(self.s,self.accounts,uid,generation) as source:
                    while self.accounts.generation(uid)==generation:
                        await self.scan(source,bot,uid,generation)
                        failures=0
                        await asyncio.sleep(self.s.poll)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                delay=max(retry_delay(e),min(3600,self.s.poll*2**min(failures-1,5)))+random.uniform(0,10)
                kind,detail=failure_status(e)
                self.user_health[uid]=detail+f" Retry in {int(delay)}s."
                log.warning("User monitor paused (%s)",type(e).__name__)
                await asyncio.sleep(delay)

    async def monitor(self, bot):
        try:
            while True:
                self.accounts.prune()
                wanted={r['user_id']: self.accounts.generation(r['user_id']) for r in self.db.targets()}
                wanted={uid:g for uid,g in wanted.items() if g and (self.s.public_access or uid in self.s.users)}
                for uid,(generation,task) in list(self.workers.items()):
                    if wanted.get(uid)!=generation or task.done():
                        await self.cancel_worker(uid)
                for uid,generation in wanted.items():
                    if uid not in self.workers:
                        self.workers[uid]=(generation,asyncio.create_task(self.monitor_user(bot,uid,generation)))
                await asyncio.sleep(2)
        finally:
            for uid in list(self.workers):
                await self.cancel_worker(uid)

    async def start(self, app):
        self.oauth.bot_username=app.bot.username
        await self.oauth.start()
        self.task=asyncio.create_task(self.monitor(app.bot))
        log.warning("Bot started; per-user Swiggy authentication enabled")

    async def stop(self, app):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        await self.oauth.stop()
        self.db.conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    # HTTP request logs can contain the Telegram token.
    logging.getLogger('httpx').setLevel(logging.CRITICAL)
    settings = Settings.env()
    service = Bot(settings,Database(settings.database))
    app = Application.builder().token(settings.telegram_token).post_init(service.start).post_stop(service.stop).build()
    for cmd in ('start','help','connect','confirm','disconnect','addresses','link','add','update','remove','threshold','pause','resume','settings','status'):
        app.add_handler(CommandHandler(cmd,service.command))
    app.add_handler(MessageHandler(filters.LOCATION, service.location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, service.text))
    app.add_handler(CallbackQueryHandler(service.ui,pattern=r'^ui:'))
    app.add_handler(CallbackQueryHandler(service.select_address,pattern=r'^address:'))
    app.run_polling(allowed_updates=['message','callback_query'])

if __name__ == '__main__':
    main()
