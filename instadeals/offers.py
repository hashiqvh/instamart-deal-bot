"""Conservative promotion labels; never infer checkout savings from a badge."""
import re

MULTIBUY = re.compile(r'\b(?:bogo|b[12]g1|buy\s*(?:one|two|[12])\s*get\s*(?:one|1)(?:\s*free)?)\b', re.I)


def multibuy_badges(row):
    badges = row.get('badges')
    if not isinstance(badges, list):
        return ()
    return tuple(sorted({b['text'].strip()[:180] for b in badges
                         if isinstance(b, dict) and isinstance(b.get('text'), str)
                         and MULTIBUY.search(b['text'])}))[:3]


def rank_products(products, order):
    if order == 'price_low':
        return sorted(products, key=lambda p: (p.price, -p.discount, p.sku))
    if order == 'price_high':
        return sorted(products, key=lambda p: (-p.price, -p.discount, p.sku))
    return sorted(products, key=lambda p: (-p.discount, -(p.mrp-p.price), p.sku))
