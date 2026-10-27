"""store: products, prices, sizes and stock.

Reads the Shopify Admin GraphQL API when SHOPIFY_STORE and SHOPIFY_TOKEN are set,
else demo/catalogue.json. Answers only with fields present in the data: a field the
data does not have is left out, never filled in.
"""
import html
import json
import re
import time
import urllib.request
from pathlib import Path

from kit import env

ROOT = Path(__file__).resolve().parents[2]
DEMO_CATALOGUE = ROOT / "demo" / "catalogue.json"
DEMO_DELIVERY = ROOT / "demo" / "delivery.json"
# Not checked against live docs today: set SHOPIFY_API_VERSION to the current one from shopify.dev.
SHOPIFY_DEFAULT_VERSION = "2026-10"
CACHE_SECONDS = 300

NAME = "store"
DEFINITION = {"type": "function", "function": {
    "name": NAME,
    "description": (
        "Look up the store's products: names, prices, sizes and stock. Search in English words "
        "like 'black socks' or 'shin pad'. Leave query empty to get every product."),
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Short search words in English, or empty."}},
        "additionalProperties": False,
    },
}}

_cache = {"at": 0.0, "items": None}


def check(args, ctx):
    extra = set(args) - {"query"}
    if extra:
        return False, f"Unknown arguments: {', '.join(sorted(extra))}. Only query is allowed."
    q = args.get("query", "")
    if not isinstance(q, str) or len(q) > 100:
        return False, "query must be a short string, 100 characters at most."
    return True, ""


def run(args, ctx):
    items = catalogue()
    q = (args.get("query") or "").lower()
    words = [w for w in re.findall(r"\w+", q) if len(w) > 1]
    if not words:
        return {"ok": True, "items": items}
    hits = [i for i in items if all(w.rstrip("s") in _haystack(i) for w in words)]
    hits = hits or [i for i in items if any(w.rstrip("s") in _haystack(i) for w in words)]
    if not hits:
        return {"ok": True, "items": items, "note": "Nothing matched the search, so this is every product."}
    return {"ok": True, "items": hits}


def _haystack(item):
    return json.dumps(item, ensure_ascii=False).lower()


# data

def catalogue(fresh=False):
    """Every product as a list of plain dicts, cached for a few minutes."""
    if not fresh and _cache["items"] is not None and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["items"]
    if env.get("SHOPIFY_STORE") and env.get("SHOPIFY_TOKEN"):
        items = _from_shopify()
    else:
        items = _from_demo(DEMO_CATALOGUE)
    _cache.update(at=time.time(), items=items)
    return items


def _put(d, key, value):
    if value is not None and value != "" and value != []:
        d[key] = value


def _clean(text):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())


def _from_demo(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    products = data.get("variables", data)
    items = []
    for key in sorted(products, key=lambda k: int(re.sub(r"\D", "", k) or 0)):
        p = products[key]
        item = {}
        _put(item, "title", p.get("title"))
        _put(item, "type", p.get("productType"))
        _put(item, "description", _clean(p.get("descriptionHtml")) or None)
        variants = []
        for v in p.get("variants", []):
            var = {}
            _put(var, "size", " / ".join(o["name"] for o in v.get("optionValues", []) if o.get("name")))
            _put(var, "price", v.get("price"))
            _put(var, "sku", v.get("sku"))
            qty = [q["quantity"] for q in v.get("inventoryQuantities", []) if q.get("name") == "available"]
            if qty:
                var["available"] = sum(qty)
                var["in_stock"] = sum(qty) > 0
            variants.append(var)
        _put(item, "variants", variants)
        items.append(item)
    return items


SHOPIFY_QUERY = """
query Products($cursor: String) {
  shop { currencyCode }
  products(first: 50, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes {
      title productType description
      variants(first: 50) { nodes { title price sku inventoryQuantity } }
    }
  }
}"""


def _from_shopify():
    shop = env.get("SHOPIFY_STORE").removeprefix("https://").split("/")[0]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", shop):
        raise ValueError("SHOPIFY_STORE must look like name.myshopify.com")
    version = env.get("SHOPIFY_API_VERSION", SHOPIFY_DEFAULT_VERSION)
    url = f"https://{shop}/admin/api/{version}/graphql.json"
    items, cursor = [], None
    for _ in range(20):
        body = json.dumps({"query": SHOPIFY_QUERY, "variables": {"cursor": cursor}}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "X-Shopify-Access-Token": env.get("SHOPIFY_TOKEN"), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.loads(r.read().decode("utf-8"))
        if out.get("errors"):
            raise RuntimeError("Shopify answered with errors")
        data = out["data"]
        currency = (data.get("shop") or {}).get("currencyCode")
        for p in data["products"]["nodes"]:
            item = {}
            _put(item, "title", p.get("title"))
            _put(item, "type", p.get("productType"))
            _put(item, "description", p.get("description"))
            variants = []
            for v in p["variants"]["nodes"]:
                var = {}
                _put(var, "size", v.get("title"))
                _put(var, "price", v.get("price"))
                _put(var, "currency", currency)
                _put(var, "sku", v.get("sku"))
                if v.get("inventoryQuantity") is not None:
                    var["available"] = v["inventoryQuantity"]
                    var["in_stock"] = v["inventoryQuantity"] > 0
                variants.append(var)
            _put(item, "variants", variants)
            items.append(item)
        page = data["products"]["pageInfo"]
        if not page["hasNextPage"]:
            break
        cursor = page["endCursor"]
    return items


def delivery():
    """Delivery fees and days. Demo data from the 27 Oct language test until the delivery tool lands."""
    return json.loads(DEMO_DELIVERY.read_text(encoding="utf-8"))


def price_points():
    """Every price and fee in the store and delivery data, for the send check."""
    out = set()
    for item in catalogue():
        for v in item.get("variants", []):
            if "price" in v:
                out.add(int(float(v["price"])))
    for zone in delivery().get("zones", []):
        if "fee" in zone:
            out.add(int(zone["fee"]))
    return out


def vocabulary():
    """Lowercase words in product names and sizes, allowed in Latin letters in an Arabic reply."""
    words = set()
    for item in catalogue():
        words |= set(re.findall(r"[a-z]+", item.get("title", "").lower()))
        for v in item.get("variants", []):
            words |= set(re.findall(r"[a-z]+", v.get("size", "").lower()))
    return frozenset(words)
