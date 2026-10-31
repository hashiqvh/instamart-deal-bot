from types import SimpleNamespace
import pytest
from instadeals.source import payload_of, failure_status, SourceError, Source
from instadeals.config import Settings


def test_raw_and_wrapped_payloads():
    data={'products':[]}
    assert payload_of(SimpleNamespace(isError=False,structuredContent=data))==data
    assert payload_of(SimpleNamespace(isError=False,structuredContent={'success':True,'data':data}))==data


def test_required_address_not_authentication():
    result=SimpleNamespace(isError=True,structuredContent={'success':False,'error':'addressId is required'})
    with pytest.raises(SourceError) as caught:
        payload_of(result)
    kind,message=failure_status(ExceptionGroup('sdk',[caught.value]))
    assert kind=='address' and '/addresses' in message and 'login is still connected' in message


async def test_linked_address_used_in_search():
    calls=[]
    async def call(name,args):
        calls.append(args)
        return SimpleNamespace(isError=False,structuredContent={'products':[]})
    source=Source(SimpleNamespace(call_tool=call),Settings('fake',frozenset({1}),queries=('coffee',),gap=0))
    source.schema={'type':'object'}
    await source.search(10,76,address_id='returned-address')
    assert calls==[{'query':'coffee','addressId':'returned-address','offset':0}]
