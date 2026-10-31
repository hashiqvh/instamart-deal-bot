import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from cryptography.fernet import Fernet
from instadeals.app import Bot
from instadeals.config import Settings
from instadeals.db import Database

@pytest.fixture
def service():
    db=Database(':memory:')
    bot=Bot(Settings('fake',frozenset({1,2}),encryption_key=Fernet.generate_key().decode()),db)
    db.user(1)
    generation=bot.accounts.save(1,{'access_token':'test'})
    bot.address_choices[1]=(generation,time.time()+600,[{'id':'saved-id','addressTag':'Home'}])
    bot.address_menus[1]='nonce'
    yield bot
    db.conn.close()


def update(uid=1):
    q=SimpleNamespace(data='address:nonce:0',answer=AsyncMock(),message=SimpleNamespace(reply_text=AsyncMock()))
    return SimpleNamespace(callback_query=q,effective_user=SimpleNamespace(id=uid),effective_chat=SimpleNamespace(type='private'))

async def test_select_stores_real_address_id_without_invented_coordinates(service):
    u=update()
    await service.select_address(u,None)
    row=service.db.locations(1)[0]
    assert row['swiggy_address_id']=='saved-id' and row['lat'] is None and row['lon'] is None
    await service.select_address(u,None)
    assert len(service.db.locations(1))==1

async def test_other_user_cannot_select_address(service):
    await service.select_address(update(2),None)
    assert not service.db.locations(2)

async def test_expired_generation_cannot_select(service):
    service.accounts.delete(1)
    await service.select_address(update(),None)
    assert not service.db.locations(1)

def test_old_location_schema_migrates_without_losing_data(tmp_path):
    import sqlite3
    path=str(tmp_path/'legacy.db')
    c=sqlite3.connect(path)
    c.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY,threshold INTEGER DEFAULT 50,enabled INTEGER DEFAULT 1); INSERT INTO users VALUES(1,50,1); CREATE TABLE locations(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL REFERENCES users(id),name TEXT NOT NULL,lat REAL NOT NULL,lon REAL NOT NULL,UNIQUE(user_id,name)); INSERT INTO locations VALUES(1,1,'Home',10,76);")
    c.close()
    db=Database(path)
    assert db.locations(1)[0]['lat']==10
    db.run("INSERT INTO locations(user_id,name,lat,lon) VALUES(1,'Saved',NULL,NULL)")
    assert len(db.locations(1))==2
    db.conn.close()

async def test_selection_immediately_replaces_stale_status(service):
    service.user_health[1]='Old scan: no locations checked'
    await service.select_address(update(),None)
    assert 'fresh scan is queued' in service.user_health[1]
