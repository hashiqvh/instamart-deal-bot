from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from instadeals.db import Database
from instadeals.products import Product, parse_products
from instadeals.config import Settings
from instadeals.app import Bot, retry_delay
from instadeals.source import Source

@pytest.fixture
def db():
    d = Database(':memory:')
    yield d
    d.conn.close()

@pytest.fixture
def product():
    return Product('sku-1','Coffee 500g',100000,40000,True)

def test_confirmations_and_staleness(db,product):
    assert not db.observe('a',product,1000,900,2,10000)
    assert db.observe('a',product,1300,900,2,10000)
    assert not db.observe('a',product,3000,900,2,10000)

def test_inflated_mrp_and_price_rebound(db,product):
    db.observe('a',product,1000,900,2,10000)
    inflated = replace(product,mrp=200000)
    assert not db.observe('a',inflated,1300,900,2,10000)
    assert not db.observe('a',inflated,1600,900,2,10000)
    rebound = replace(product,price=45000)
    db.observe('a',rebound,1900,900,2,10000)
    assert not db.observe('a',rebound,2200,900,2,10000)

def test_unavailable_resets(db,product):
    db.observe('a',product,1000,900,2,0)
    assert not db.observe('a',replace(product,available=False),1300,900,2,0)
    assert not db.observe('a',product,1600,900,2,0)

def test_dedup_scope_and_improvement(db,product):
    assert db.reserve(1,'a',product)
    assert not db.reserve(1,'a',product)
    assert not db.reserve(1,'a',replace(product,price=39900))
    assert db.reserve(1,'a',replace(product,price=38000))
    assert not db.reserve(1,'a',product)
    assert db.reserve(2,'a',product)
    assert db.reserve(1,'b',product)

def payload(price=40):
    return {'products':[{'productId':'p','displayName':'Coffee','inStock':True,'isAvail':True,'variations':[
        {'skuId':'sku','displayName':'Coffee','price':{'mrp':100,'offerPrice':price},'isInStockAndAvailable':True}]}]}

@pytest.mark.parametrize('price',[None,0,-10,True,'NaN','Infinity',101])
def test_invalid_prices(price):
    assert parse_products(payload(price)) == []

def test_missing_stock_fails_closed():
    data = payload()
    del data['products'][0]['isAvail']
    assert not parse_products(data)[0].available

def test_duplicate_conflicting_variants():
    data = payload()
    data['products'] += payload(50)['products']
    assert parse_products(data) == []

async def test_scan_isolates_accounts_and_notifies_once(db,product):
    s = Settings('fake',frozenset({1,2}),savings=0)
    service = Bot(s,db)
    for uid in (1,2):
        db.user(uid)
        db.run('INSERT INTO locations(user_id,name,lat,lon) VALUES(?,?,?,?)',(uid,'Home',12,77))
    source = SimpleNamespace(search=AsyncMock(return_value=[product]))
    telegram = SimpleNamespace(send_message=AsyncMock())
    await service.scan(source,telegram)
    assert telegram.send_message.await_count == 0
    await service.scan(source,telegram)
    assert source.search.await_count == 4
    assert telegram.send_message.await_count == 2
    await service.scan(source,telegram)
    assert telegram.send_message.await_count == 2

async def test_scan_ranks_qualifying_deals_and_preserves_confirmation_and_dedup(db):
    service = Bot(Settings('fake',frozenset({1}),savings=10000),db)
    db.user(1)
    db.run('INSERT INTO locations(user_id,name,lat,lon) VALUES(?,?,?,?)',(1,'Home',12,77))
    products = [
        Product('low','Low',100000,50000,True),
        Product('tie-small','Tie small',100000,40000,True),
        Product('unavailable','Unavailable',100000,10000,False),
        Product('highest','Highest',100000,20000,True),
        Product('tie-large','Tie large',200000,80000,True),
        Product('below','Below threshold',100000,60000,True),
        Product('tiny','Tiny savings',1000,100,True),
    ]
    source = SimpleNamespace(search=AsyncMock(return_value=products))
    telegram = SimpleNamespace(send_message=AsyncMock())
    await service.scan(source,telegram)
    assert telegram.send_message.await_count == 0
    await service.scan(source,telegram)
    messages = [call.args[1] for call in telegram.send_message.await_args_list]
    assert len(messages) == 4
    for message, name in zip(messages, ['Highest','Tie large','Tie small','Low']):
        assert name in message
    await service.scan(source,telegram)
    assert telegram.send_message.await_count == 4

async def test_pagination_and_coordinates():
    calls = []
    async def call(name,args):
        calls.append(args)
        data = payload()
        data['nextOffset'] = '10' if len(calls)==1 else '-1'
        return SimpleNamespace(isError=False,structuredContent={'success':True,'data':data})
    s = Settings('fake',frozenset({1}),queries=('coffee',),gap=0)
    source = Source(SimpleNamespace(call_tool=call),s)
    source.schema = {'type':'object'}
    result = await source.search(12,77)
    assert len(result)==1
    assert [c['offset'] for c in calls]==[0,10]
    assert all(c['latitude']==12 and c['longitude']==77 for c in calls)

async def test_uncertain_delivery_is_not_repeated(db,product):
    from telegram.error import TimedOut
    service = Bot(Settings('fake',frozenset({1})),db)
    telegram = SimpleNamespace(send_message=AsyncMock(side_effect=TimedOut()))
    await service.notify(telegram,1,'a','Home',product)
    await service.notify(telegram,1,'a','Home',product)
    assert telegram.send_message.await_count == 1
    assert db.run('SELECT status FROM notifications').fetchone()[0]=='uncertain'

def test_retry_after_nested():
    import httpx
    response = httpx.Response(429,headers={'Retry-After':'600'})
    error = httpx.HTTPStatusError('limited',request=httpx.Request('POST','https://example.com'),response=response)
    assert retry_delay(ExceptionGroup('wrapped',[error]))==600

def test_persistence(tmp_path,product):
    path = str(tmp_path/'test.db')
    d = Database(path)
    d.user(123)
    d.reserve(123,'a',product)
    d.conn.close()
    d = Database(path)
    assert not d.reserve(123,'a',product)
    d.conn.close()

async def test_absent_product_breaks_confirmation(db,product):
    service = Bot(Settings('fake',frozenset({1}),savings=0),db)
    db.user(1)
    db.run('INSERT INTO locations(user_id,name,lat,lon) VALUES(1,\'Home\',12,77)')
    source = SimpleNamespace(search=AsyncMock(side_effect=[[product],[],[product],[product]]))
    telegram = SimpleNamespace(send_message=AsyncMock())
    for _ in range(3):
        await service.scan(source,telegram)
    assert telegram.send_message.await_count==0
    await service.scan(source,telegram)
    assert telegram.send_message.await_count==1

async def test_commands_scope_and_validation(db):
    service = Bot(Settings('fake',frozenset({1,2})),db)
    async def command(uid,text):
        message = SimpleNamespace(text=text,reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=uid),effective_chat=SimpleNamespace(type='private'),message=message)
        await service.command(update,None)
        return message.reply_text.call_args.args[0]
    await command(1,'/add Home 12 77')
    loc = db.locations(1)[0]['id']
    assert await command(2,f'/remove {loc}')=='Location not found'
    assert await command(2,f'/update {loc} Elsewhere 10 70')=='Location not found'
    assert len(db.locations(1))==1
    await command(1,'/add Broken nan 77')
    assert len(db.locations(1))==1
    await command(1,'/threshold 60')
    assert db.user(1)['threshold']==60
    await command(1,'/pause')
    assert not db.user(1)['enabled']
    await command(1,'/resume')
    assert db.user(1)['enabled']
    await command(1,f'/remove {loc}')
    assert not db.locations(1)

async def test_shared_location_is_private_and_deduplicated(db):
    service = Bot(Settings('fake',frozenset({1})),db)
    message = SimpleNamespace(location=SimpleNamespace(latitude=9.97,longitude=76.28),reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1),effective_chat=SimpleNamespace(type='private'),message=message)
    await service.location(update,None)
    await service.location(update,None)
    assert len(db.locations(1))==1
    update.effective_user.id=2
    await service.location(update,None)
    assert not db.locations(2)

async def test_start_explains_setup_with_current_preferences(db):
    service = Bot(Settings('fake',frozenset({1})),db)
    db.user(1)
    db.run('UPDATE users SET threshold=60,enabled=0 WHERE id=1')
    message = SimpleNamespace(text='/start',reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1),effective_chat=SimpleNamespace(type='private'),message=message)
    await service.command(update,None)
    text = message.reply_text.call_args.args[0]
    assert 'Welcome' in text and 'saved delivery address' in text
    assert 'Minimum discount: 60%' in text and 'Alerts: Paused' in text
    labels=[b.text for row in message.reply_text.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
    assert 'Connect Swiggy' in labels and 'My locations' in labels
    assert not db.user(1)['enabled']

async def test_postal_code_gets_guidance_not_guessed_coordinates(db):
    service = Bot(Settings('fake',frozenset({1})),db)
    message = SimpleNamespace(text='682037',reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1),effective_chat=SimpleNamespace(type='private'),message=message)
    await service.text(update,None)
    text = message.reply_text.call_args.args[0]
    assert '682037' in text and "hasn't been saved" in text and '📎 → Location' in text
    assert not db.locations(1)

async def test_status_names_checked_and_unlinked_locations(db,product):
    from instadeals.source import SourceError
    s=Settings('fake',frozenset({1}),savings=0)
    service=Bot(s,db)
    db.user(1)
    db.run("INSERT INTO locations(user_id,name,lat,lon) VALUES(1,'Unlinked pin',10,76)")
    db.run("INSERT INTO locations(user_id,name,swiggy_address_id) VALUES(1,'Saved home','address')")
    async def search(lat,lon,address_id=None):
        assert 'Scanning' in service.user_health[None]
        if address_id:
            return [product]
        raise SourceError('address_required')
    await service.scan(SimpleNamespace(search=search),SimpleNamespace(send_message=AsyncMock()))
    status=service.user_health[None]
    assert 'Checked: Saved home' in status
    assert 'Products checked: 1' in status
    assert 'Needs a saved Swiggy address: Unlinked pin' in status
