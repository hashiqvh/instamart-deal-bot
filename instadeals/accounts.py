"""Per-user encrypted OAuth credentials and one-use browser handoffs."""
import asyncio
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import httpx
from aiohttp import web
from cryptography.fernet import Fernet

BASE = 'https://mcp.swiggy.com'

class Accounts:
    def __init__(self, db, key):
        self.db = db
        self.cipher = Fernet(key)
        self.db.run('CREATE TABLE IF NOT EXISTS accounts(uid INTEGER PRIMARY KEY, generation TEXT NOT NULL, secret BLOB NOT NULL)')
        self.pending = {}
        self.locks = {}

    def lock(self, uid):
        return self.locks.setdefault(uid, asyncio.Lock())

    def generation(self, uid):
        row = self.db.run('SELECT generation FROM accounts WHERE uid=?', (uid,)).fetchone()
        return row[0] if row else None

    def get(self, uid, generation=None):
        row = self.db.run('SELECT * FROM accounts WHERE uid=?', (uid,)).fetchone()
        if row is None or (generation is not None and row['generation'] != generation):
            raise ValueError('Swiggy account disconnected or replaced')
        return json.loads(self.cipher.decrypt(row['secret']))

    def save(self, uid, tokens, generation=None):
        if not isinstance(tokens.get('access_token'), str) or not tokens['access_token']:
            raise ValueError('Missing OAuth token')
        data = dict(tokens)
        data['expires_at'] = time.time() + float(data.get('expires_in', 3600))
        secret = self.cipher.encrypt(json.dumps(data).encode())
        if generation is not None:
            if not self.db.run('UPDATE accounts SET secret=? WHERE uid=? AND generation=?', (secret,uid,generation)).rowcount:
                raise ValueError('Account changed during token refresh')
            return generation
        generation = secrets.token_hex(16)
        self.db.run('INSERT OR REPLACE INTO accounts VALUES(?,?,?)', (uid,generation,secret))
        return generation

    def delete(self, uid):
        self.db.run('DELETE FROM accounts WHERE uid=?', (uid,))
        for state, item in list(self.pending.items()):
            if item['uid'] == uid:
                self.pending.pop(state, None)

    def prune(self):
        for state, item in list(self.pending.items()):
            if item['expires'] < time.time():
                self.pending.pop(state, None)

    async def begin(self, uid, redirect):
        self.prune()
        if any(p['uid']==uid and p['expires']-900 > time.time()-30 for p in self.pending.values()):
            raise ValueError('Please wait 30 seconds before requesting another link')
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.post(BASE+'/auth/register', json={
                'client_name':'Instadeals', 'redirect_uris':[redirect],
                'grant_types':['authorization_code','refresh_token'], 'response_types':['code'],
                'token_endpoint_auth_method':'none', 'scope':'mcp:tools'})
            r.raise_for_status()
        client_id = r.json()['client_id']
        for state,item in list(self.pending.items()):
            if item['uid']==uid:
                del self.pending[state]
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        self.pending[state] = dict(uid=uid, verifier=verifier, client_id=client_id,
                                   redirect=redirect, expires=time.time()+900, phase='waiting')
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        return BASE+'/auth/authorize?'+urlencode(dict(response_type='code',client_id=client_id,
            redirect_uri=redirect,code_challenge=challenge,code_challenge_method='S256',state=state,scope='mcp:tools'))

    async def exchange(self, state, code):
        self.prune()
        item = self.pending.get(state)
        if not item or item['phase']!='waiting' or not code:
            raise ValueError('Login link expired or already used')
        item['phase']='exchanging'  # Claim before awaiting; concurrent callbacks cannot replay.
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                r = await http.post(BASE+'/auth/token',json=dict(grant_type='authorization_code',code=code,
                    code_verifier=item['verifier'],client_id=item['client_id'],redirect_uri=item['redirect']))
                r.raise_for_status()
            data = r.json()
            if not isinstance(data.get('access_token'), str) or not data['access_token']:
                raise ValueError('Missing token')
            if self.pending.get(state) is not item or item['expires'] < time.time():
                raise ValueError('Login cancelled')
            data['client_id']=item['client_id']
            item['tokens']=self.cipher.encrypt(json.dumps(data).encode())
            item['confirm']=secrets.token_urlsafe(18)
            item['phase']='confirm'
            item['expires']=time.time()+300
            return item['confirm']
        except Exception:
            self.pending.pop(state,None)
            raise

    def confirm(self, uid, code):
        self.prune()
        for state,item in list(self.pending.items()):
            if item['uid']==uid and item['phase']=='confirm' and secrets.compare_digest(item['confirm'],code):
                data = json.loads(self.cipher.decrypt(item['tokens']))
                generation = self.save(uid,data)
                del self.pending[state]
                return generation
        raise ValueError('Invalid or expired confirmation. Use /connect again.')

    async def disconnect(self, uid):
        async with self.lock(uid):
            try:
                tokens = self.get(uid)
            except ValueError:
                tokens = None
            self.delete(uid)  # Always remove local access, even if remote logout is unavailable.
            if tokens:
                try:
                    async with httpx.AsyncClient(timeout=20) as http:
                        r = await http.post(BASE+'/auth/logout',headers={'Authorization':'Bearer '+tokens['access_token']})
                        r.raise_for_status()
                except httpx.HTTPError:
                    return False
        return True

class AccountAuth(httpx.Auth):
    def __init__(self, accounts, uid, generation):
        self.accounts, self.uid, self.generation = accounts, uid, generation

    async def token(self, force=False, previous=None):
        async with self.accounts.lock(self.uid):
            data = self.accounts.get(self.uid,self.generation)
            if (force and data['access_token']==previous) or data.get('expires_at',0)<time.time()+60:
                if not data.get('refresh_token'):
                    raise ValueError('Reconnect Swiggy with /connect')
                async with httpx.AsyncClient(timeout=30) as http:
                    r = await http.post(BASE+'/auth/token',json=dict(grant_type='refresh_token',
                        refresh_token=data['refresh_token'],client_id=data.get('client_id','')))
                    r.raise_for_status()
                updated={**data,**r.json()}
                self.accounts.save(self.uid,updated,self.generation)
                data=self.accounts.get(self.uid,self.generation)
            return data['access_token']

    async def async_auth_flow(self, request):
        token = await self.token()
        request.headers['Authorization']='Bearer '+token
        response = yield request
        if response.status_code==401:
            await response.aread()
            request.headers['Authorization']='Bearer '+await self.token(True,token)
            yield request

class OAuthServer:
    def __init__(self, accounts, host, port):
        self.accounts, self.host, self.port = accounts, host, port
        self.runner=None
        self.bot_username=None

    async def callback(self, request):
        try:
            code = await self.accounts.exchange(request.query.get('state',''),request.query.get('code',''))
            body = ('Swiggy login received. Return to the SAME Telegram chat and send:\n\n/confirm '+code+
                    '\n\nOnly confirm if you personally started and completed this login. This code expires in 5 minutes.')
            status=200
            if self.bot_username:
                import html
                link=f"https://t.me/{self.bot_username}?start=confirm_{code}"
                body=("<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
                      "<title>Connect Swiggy</title><h1>Finish connecting Swiggy</h1>"
                      "<p>Only continue if you personally started and completed this login.</p>"
                      f"<p><a href='{html.escape(link,quote=True)}'>Return to Telegram</a></p>"
                      "<p>Press Start in Telegram to confirm. This link expires in five minutes.</p>")
        except Exception:
            body, status = 'Login failed, cancelled or expired. Return to Telegram and use /connect again.',400
        return web.Response(text=body,status=status,content_type='text/html' if status==200 and self.bot_username else 'text/plain',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer',
            'Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'"})

    async def start(self):
        app=web.Application(client_max_size=8192)
        app.router.add_get('/callback',self.callback)
        self.runner=web.AppRunner(app,access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner,self.host,self.port).start()

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
