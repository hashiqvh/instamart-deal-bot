import asyncio
import json
import time
from contextlib import asynccontextmanager
import httpx
from jsonschema import validate
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from .products import parse_products
from .accounts import AccountAuth

_request_lock = asyncio.Lock()
_last_request = 0.0

ENDPOINT = "https://mcp.swiggy.com/im"

class SourceError(RuntimeError):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind=kind


def payload_of(result):
    payload=result.structuredContent
    texts=[c.text for c in getattr(result,'content',[]) if c.type=='text']
    if payload is None and len(texts)==1:
        try:
            payload=json.loads(texts[0])
        except json.JSONDecodeError:
            pass
    if result.isError or (isinstance(payload,dict) and payload.get('success') is False):
        diagnostic=(json.dumps(payload)+ ' '.join(texts)).lower()
        if 'addressid is required' in diagnostic:
            raise SourceError('address_required')
        raise SourceError('search_failed')
    if not isinstance(payload,dict):
        raise SourceError('response_format')
    if payload.get('success') is True:
        payload=payload.get('data')
    if not isinstance(payload,dict):
        raise SourceError('response_format')
    return payload


def failure_status(error):
    children=getattr(error,'exceptions',())
    if children:
        messages=[failure_status(e) for e in children]
        return next((m for m in messages if m[0]!='temporary'),messages[0])
    if isinstance(error,SourceError):
        if error.kind=='address_required':
            return 'address', 'Swiggy requires a saved delivery address for this location. Use /addresses, then /link LOCATION_ID ADDRESS_NUMBER. Your login is still connected.'
        if error.kind=='response_format':
            return 'format', 'Swiggy returned an unsupported response format. Your saved login has not been removed.'
    response=getattr(error,'response',None)
    if response is not None:
        if response.status_code in (401,403,419):
            return 'auth', 'Swiggy rejected authentication or account access. Use /connect to sign in again.'
        if response.status_code==429:
            return 'rate', 'Swiggy rate-limited requests. Waiting before retrying; no new login is needed.'
    return 'temporary', 'Product search is temporarily unavailable. Your saved login has not been removed.'

class Source:
    def __init__(self, session, settings):
        self.session, self.settings = session, settings
        self.schema = None
        self.last_call = 0

    async def discover(self):
        cursor = None
        tools = []
        seen = set()
        while True:
            page = await self.session.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.nextCursor
            if not cursor:
                break
            if cursor in seen:
                raise ValueError("Repeated tools/list cursor")
            seen.add(cursor)
        found = next((t for t in tools if t.name == "search_products"), None)
        if found is None:
            raise ValueError("search_products unavailable for this account")
        self.schema = found.inputSchema
        for key in ("query", "latitude", "longitude", "offset"):
            if key not in self.schema.get("properties", {}):
                raise ValueError("Live search schema does not support coordinate polling")
        return [t.model_dump(mode="json", exclude_none=True) for t in tools]

    async def addresses(self):
        rows=[]
        for page in range(1,101):
            result=await self.session.call_tool('get_addresses',{'page':page,'pageSize':10})
            data=payload_of(result)
            if not isinstance(data.get('addresses'),list):
                raise SourceError('response_format')
            rows.extend(data['addresses'])
            pagination=data.get('pagination',{})
            if pagination.get('hasMore') is not True:
                return rows
            await asyncio.sleep(self.settings.gap)
        raise SourceError('response_format')

    async def search(self, lat, lon, address_id=None):
        products, conflicts = {}, set()
        for query in self.settings.queries:
            offset, seen = 0, set()
            for _ in range(self.settings.pages):
                args = dict(query=query, latitude=lat, longitude=lon, offset=offset)
                if address_id:
                    args = dict(query=query,addressId=address_id,offset=offset)
                validate(args, self.schema)
                global _last_request
                async with _request_lock:
                    await asyncio.sleep(max(0, self.settings.gap-(time.monotonic()-_last_request)))
                    _last_request = time.monotonic()
                    result = await self.session.call_tool("search_products", args)
                data = payload_of(result)
                for p in parse_products(data):
                    if p.sku in products and products[p.sku] != p:
                        conflicts.add(p.sku)
                    products[p.sku] = p
                nxt = data.get("nextOffset")
                if nxt in (None, "", "-1", -1):
                    break
                try:
                    nxt = int(nxt)
                except (ValueError, TypeError):
                    raise ValueError("Unsupported pagination cursor") from None
                if nxt <= offset or nxt in seen:
                    break
                seen.add(nxt)
                offset = nxt
        return [p for sku,p in products.items() if sku not in conflicts]

@asynccontextmanager
async def connect(settings, accounts, uid, generation):
    # SDK handles initialization, SSE, protocol headers and persistent sessions.
    async with httpx.AsyncClient(auth=AccountAuth(accounts, uid, generation), timeout=60) as http:
        async with streamable_http_client(ENDPOINT, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                source = Source(session, settings)
                await source.discover()
                yield source
