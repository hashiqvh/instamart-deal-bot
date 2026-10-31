import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from instadeals.accounts import Accounts, AccountAuth, OAuthServer
from instadeals.db import Database

@pytest.fixture
def accounts():
    db=Database(':memory:')
    yield Accounts(db,Fernet.generate_key())
    db.conn.close()


def test_tokens_encrypted_and_user_isolated(accounts):
    a=accounts.save(1,{'access_token':'secret-one'})
    b=accounts.save(2,{'access_token':'secret-two'})
    assert accounts.get(1,a)['access_token']=='secret-one'
    assert accounts.get(2,b)['access_token']=='secret-two'
    assert b'secret-one' not in accounts.db.run('SELECT secret FROM accounts WHERE uid=1').fetchone()[0]
    with pytest.raises(ValueError):
        accounts.get(1,b)


def test_disconnect_invalidates_pending_and_stale_refresh(accounts):
    g=accounts.save(1,{'access_token':'old'})
    accounts.pending['state']={'uid':1,'expires':time.time()+60}
    accounts.delete(1)
    assert not accounts.pending
    with pytest.raises(ValueError):
        accounts.save(1,{'access_token':'late-refresh'},g)
    assert accounts.generation(1) is None


def test_confirmation_bound_to_telegram_user_and_single_use(accounts):
    accounts.pending['state']=dict(uid=1,phase='confirm',expires=time.time()+60,confirm='code',
        tokens=accounts.cipher.encrypt(json.dumps({'access_token':'test'}).encode()))
    with pytest.raises(ValueError):
        accounts.confirm(2,'code')
    generation=accounts.confirm(1,'code')
    assert accounts.get(1,generation)['access_token']=='test'
    with pytest.raises(ValueError):
        accounts.confirm(1,'code')


def test_expired_confirmation_rejected(accounts):
    accounts.pending['state']=dict(uid=1,phase='confirm',expires=time.time()-1,confirm='code')
    with pytest.raises(ValueError):
        accounts.confirm(1,'code')


async def test_replayed_callback_never_exchanges_again(accounts):
    accounts.pending['state']=dict(uid=1,phase='exchanging',expires=time.time()+60)
    with pytest.raises(ValueError):
        await accounts.exchange('state','code')


async def test_auth_sends_only_requested_users_token(accounts):
    g=accounts.save(1,{'access_token':'one','expires_in':3600})
    accounts.save(2,{'access_token':'two'})
    seen=[]
    def respond(request):
        seen.append(request.headers['Authorization'])
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond),auth=AccountAuth(accounts,1,g)) as client:
        await client.get('https://mcp.swiggy.com/im')
    assert seen==['Bearer one']


async def test_disconnect_cleans_local_when_remote_unavailable(accounts,monkeypatch):
    accounts.save(1,{'access_token':'one'})
    async def fail(*args,**kwargs):
        raise httpx.ConnectError('offline')
    monkeypatch.setattr(httpx.AsyncClient,'post',fail)
    assert not await accounts.disconnect(1)
    assert accounts.generation(1) is None


async def test_rotation_is_saved_to_same_user(accounts,monkeypatch):
    g=accounts.save(1,{'access_token':'old','refresh_token':'r','expires_in':0})
    accounts.save(2,{'access_token':'other'})
    async def refresh(*args,**kwargs):
        return httpx.Response(200,json={'access_token':'new','refresh_token':'r2','expires_in':3600},request=httpx.Request('POST','https://mcp.swiggy.com/auth/token'))
    monkeypatch.setattr(httpx.AsyncClient,'post',refresh)
    assert await AccountAuth(accounts,1,g).token()=='new'
    assert accounts.get(1)['refresh_token']=='r2'
    assert accounts.get(2)['access_token']=='other'

async def test_account_scan_cannot_notify_another_user(accounts):
    from instadeals.app import Bot
    from instadeals.config import Settings
    from instadeals.products import Product
    from types import SimpleNamespace
    service=Bot(Settings('fake',frozenset({1,2}),savings=0),accounts.db)
    service.accounts=accounts
    for uid in (1,2):
        accounts.db.user(uid)
        accounts.db.run('INSERT INTO locations(user_id,name,lat,lon) VALUES(?,?,12,77)',(uid,'Home'))
    generation=accounts.save(1,{'access_token':'one'})
    accounts.save(2,{'access_token':'two'})
    p=Product('sku','Coffee',100000,40000,True)
    source=SimpleNamespace(search=AsyncMock(return_value=[p]))
    telegram=SimpleNamespace(send_message=AsyncMock())
    await service.scan(source,telegram,1,generation)
    await service.scan(source,telegram,1,generation)
    assert [call.args[0] for call in telegram.send_message.call_args_list]==[1]
    assert all(row['area'].startswith('1:') for row in accounts.db.run('SELECT area FROM prices'))


async def test_disconnect_during_fetch_discards_results(accounts):
    from instadeals.app import Bot
    from instadeals.config import Settings
    from instadeals.products import Product
    from types import SimpleNamespace
    service=Bot(Settings('fake',frozenset({1}),savings=0),accounts.db)
    service.accounts=accounts
    accounts.db.user(1)
    accounts.db.run("INSERT INTO locations(user_id,name,lat,lon) VALUES(1,'Home',12,77)")
    generation=accounts.save(1,{'access_token':'one'})
    async def search(*args):
        accounts.delete(1)
        return [Product('sku','Coffee',100000,40000,True)]
    telegram=SimpleNamespace(send_message=AsyncMock())
    await service.scan(SimpleNamespace(search=search),telegram,1,generation)
    assert not accounts.db.run('SELECT * FROM prices').fetchall()
    telegram.send_message.assert_not_called()
