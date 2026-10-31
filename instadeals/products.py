from dataclasses import dataclass
from .offers import multibuy_badges
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from .deal_actions import public_product_link, catalog_link

@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    mrp: int
    price: int
    available: bool
    image: str | None = None
    link: str | None = None
    check_variant: bool = False
    offers: tuple[str, ...] = ()

    @property
    def discount(self):
        return 100 * (self.mrp-self.price) / self.mrp


def url(value):
    if isinstance(value, str) and len(value) <= 2000:
        p = urlparse(value)
        if p.scheme == "https" and p.hostname and not p.username and not p.password:
            return value
    return None


def money(value):
    if isinstance(value, bool):
        raise ValueError("Boolean price")
    n = Decimal(str(value))
    if not n.is_finite() or n <= 0 or n > 10000000:
        raise ValueError("Invalid price")
    return int((n * 100).quantize(Decimal("1")))


def parse_products(data):
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        raise ValueError("Unrecognized search_products payload")
    result = {}
    conflicts = set()
    for row in data["products"]:
        if not isinstance(row, dict):
            continue
        for v in row.get("variations", []) or []:
            try:
                sku = v["skuId"]
                name = v.get("displayName") or row["displayName"]
                if not isinstance(sku, str) or not sku or not isinstance(name, str) or not name:
                    continue
                mrp, price = money(v["price"]["mrp"]), money(v["price"]["offerPrice"])
                if price > mrp:
                    continue
                direct=public_product_link(v.get("productUrl")) or public_product_link(row.get("productUrl"))
                generated=catalog_link(row.get("productId"))
                p = Product(sku, (name + " " + str(v.get("quantityDescription", "")))[:250], mrp, price,
                            row.get("inStock") is True and row.get("isAvail") is True and v.get("isInStockAndAvailable") is True,
                            url(v.get("imageUrl")), direct or generated, bool(generated and not direct), multibuy_badges(row))
                if sku in result and result[sku] != p:
                    conflicts.add(sku)
                result[sku] = p
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
    return [p for k, p in result.items() if k not in conflicts]
