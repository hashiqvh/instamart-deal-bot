from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from instadeals.app import Bot
from instadeals.config import Settings
from instadeals.db import Database
from instadeals.products import Product, parse_products
from instadeals.offers import multibuy_badges, rank_products


def test_badges_are_explicit_not_inferred_and_do_not_change_price():
    assert multibuy_badges({'badges':[{'text':'Sponsored'},{'text':'Price drop'}]}) == ()
    assert multibuy_badges({'badges':[{'text':'Buy 2 Get 1 free'}]}) == ('Buy 2 Get 1 free',)
    assert multibuy_badges({'badges':None}) == ()
    rows=parse_products({'products':[{'displayName':'Soap','badges':[{'text':'Buy one get one free'}],
        'inStock':True,'isAvail':True,'variations':[{'skuId':'s','price':{'mrp':100,'offerPrice':100},'isInStockAndAvailable':True}]}]})
    assert rows[0].offers == ('Buy one get one free',)
    assert rows[0].discount == 0


def test_price_sorting():
    rows=[Product('a','a',10000,3000,True),Product('b','b',100000,20000,True)]
    assert [p.sku for p in rank_products(rows,'discount')] == ['b','a']
    assert [p.sku for p in rank_products(rows,'price_low')] == ['a','b']
    assert [p.sku for p in rank_products(rows,'price_high')] == ['b','a']


async def test_all_offers_filters_and_location_isolation():
    db=Database(':memory:')
    try:
        db.user(1)
        db.run("UPDATE users SET offer_mode='all',sort_order='price_low' WHERE id=1")
        for name,lat in [('Home',12),('Office',13)]:
            db.run('INSERT INTO locations(user_id,name,lat,lon) VALUES(?,?,?,?)',(1,name,lat,77))
        service=Bot(Settings('fake',frozenset({1})),db)
        def products(lat,lon):
            return [Product('combo','Soap combo',10000,10000,True,offers=('Buy 1 Get 1 free',)),
                    Product('small','Small discount',1000,900 if lat==12 else 800,True),
                    Product('regular','Regular',10000,10000,True),
                    Product('stock','No stock',10000,100,False)]
        source=SimpleNamespace(search=AsyncMock(side_effect=products))
        telegram=SimpleNamespace(send_message=AsyncMock())
        await service.scan(source,telegram)
        assert telegram.send_message.await_count==0
        await service.scan(source,telegram)
        messages=[c.args[1] for c in telegram.send_message.await_args_list]
        assert len(messages)==4
        assert 'Small discount' in messages[0] and '₹9.00' in messages[0] and 'Home' in messages[0]
        assert 'Soap combo' in messages[1] and '0.0% off' in messages[1] and 'Check required quantities' in messages[1]
        assert 'Small discount' in messages[2] and '₹8.00' in messages[2] and 'Office' in messages[2]
        await service.scan(source,telegram)
        assert telegram.send_message.await_count==4
    finally:
        db.conn.close()


def test_offer_change_requires_fresh_confirmation():
    db=Database(':memory:')
    try:
        a=Product('s','Soap',10000,10000,True,offers=('Buy 1 Get 1 free',))
        b=Product('s','Soap',10000,10000,True,offers=('Buy 2 Get 1 free',))
        assert not db.observe('a',a,100,900,2,0)
        assert not db.observe('a',b,200,900,2,0)
        assert db.observe('a',b,300,900,2,0)
    finally:
        db.conn.close()
