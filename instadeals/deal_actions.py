"""Public deal links: never include the user's delivery address or account data."""
import re
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

INSTAMART_HOME='https://www.swiggy.com/instamart'


def public_product_link(value):
    if not isinstance(value,str) or len(value)>2000:
        return None
    try:
        p=urlparse(value)
        port=p.port
    except ValueError:
        return None
    if p.scheme!='https' or p.hostname not in {'swiggy.com','www.swiggy.com','instamart.in','www.instamart.in'} or p.username or p.password or port not in (None,443):
        return None
    # Preserve only product identifiers, excluding delivery/account/tracking parameters.
    query=urlencode([(k,v) for k,v in parse_qsl(p.query) if k in {'productId','skuId','spinId'} or (k=='share' and v=='true')])
    return urlunparse((p.scheme,p.netloc,p.path,'',query,''))


def catalog_link(product_id):
    # Verified against a user-shared Instamart link and the matching live MCP productId.
    # Do not use skuId or spinId: those identify variations, not this share route.
    if isinstance(product_id,str) and re.fullmatch(r'[A-Za-z0-9_-]{6,64}',product_id):
        return f'https://instamart.in/item/{product_id}?share=true'
    return None


def deal_actions(product):
    link=public_product_link(product.link)
    destination=link or INSTAMART_HOME
    share_text=(f"{product.name}\n₹{product.price/100:.2f} (MRP ₹{product.mrp/100:.2f}) · {product.discount:.1f}% off\n"
                "Price and availability depend on your delivery address and may change.")
    if product.check_variant and link:
        share_text+='\nSelect the pack size shown above on the product page.'
    if not link:
        share_text+='\nOpen Instamart and search for the product name above.'
    share_url='https://t.me/share/url?'+urlencode({'url':destination,'text':share_text})
    markup=InlineKeyboardMarkup([[InlineKeyboardButton('Buy now',url=destination),InlineKeyboardButton('Share',url=share_url)]])
    hint='' if link else '\nBuy now opens Instamart; search for this product. A direct item link was not provided.'
    if product.check_variant and link:
        hint='\nBuy now opens the product page. Check the pack size and delivery address in Instamart.'
    return markup,hint
