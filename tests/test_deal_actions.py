from dataclasses import replace
from urllib.parse import parse_qs,urlparse
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from telegram.error import BadRequest
from instadeals.deal_actions import deal_actions,public_product_link,INSTAMART_HOME
from instadeals.products import Product
from instadeals.app import Bot
from instadeals.config import Settings
from instadeals.db import Database

P=Product('sku','Coffee & Cocoa 500g',100000,40000,True,link='https://www.swiggy.com/instamart/item/example')

def test_buy_and_share_same_product():
    markup,hint=deal_actions(P)
    buy,share=markup.inline_keyboard[0]
    assert buy.text=='Buy now' and buy.url==P.link
    assert share.text=='Share'
    params=parse_qs(urlparse(share.url).query)
    assert params['url']==[P.link]
    assert 'Coffee & Cocoa' in params['text'][0]
    assert '₹400.00' in params['text'][0]
    assert not hint

def test_fallback_is_explicit():
    markup,hint=deal_actions(replace(P,link=None))
    assert markup.inline_keyboard[0][0].url==INSTAMART_HOME
    assert 'direct item link was not provided' in hint

@pytest.mark.parametrize('link',['https://evil.example/product','https://swiggy.com.evil.example/item','http://swiggy.com/item','https://user:password@swiggy.com/item'])
def test_untrusted_links_rejected(link):
    assert public_product_link(link) is None

def test_account_data_stripped_from_share():
    p=replace(P,link=P.link+'?addressId=private&token=secret&spinId=123#private')
    markup,_=deal_actions(p)
    params=parse_qs(urlparse(markup.inline_keyboard[0][1].url).query)
    assert params['url']==[P.link+'?spinId=123']
    assert 'private' not in str(markup) and 'secret' not in str(markup)

async def test_image_fallback_keeps_both_buttons():
    d=Database(':memory:')
    b=Bot(Settings('fake',frozenset({1})),d)
    bot=SimpleNamespace(send_photo=AsyncMock(side_effect=BadRequest('bad image')),send_message=AsyncMock())
    await b.notify(bot,1,'area','Private home label',replace(P,image='https://example.com/image.png'))
    markup=bot.send_message.call_args.kwargs['reply_markup']
    assert [x.text for x in markup.inline_keyboard[0]]==['Buy now','Share']
    assert 'Private home label' not in markup.inline_keyboard[0][1].url
    d.conn.close()

def test_verified_share_route_uses_product_id_not_variant_id():
    from instadeals.products import parse_products
    data={'products':[{'displayName':'Bingo Mad Angles Achaari Masti Masala Crisps','productId':'5DCZOB0FZI','inStock':True,'isAvail':True,'variations':[{'skuId':'I9GPYOKFYF','spinId':'CGV5YYUXYA','quantityDescription':'100 g','price':{'mrp':100,'offerPrice':40},'isInStockAndAvailable':True}]}]}
    p=parse_products(data)[0]
    assert p.link=='https://instamart.in/item/5DCZOB0FZI?share=true'
    assert p.check_variant
    markup,hint=deal_actions(p)
    assert markup.inline_keyboard[0][0].url==p.link
    assert 'pack size' in hint
    share=parse_qs(urlparse(markup.inline_keyboard[0][1].url).query)
    assert share['url']==[p.link]
    assert '100 g' in share['text'][0]


def test_invalid_product_id_not_used():
    from instadeals.deal_actions import catalog_link
    assert catalog_link('../../private?token=bad') is None
    assert catalog_link(None) is None
