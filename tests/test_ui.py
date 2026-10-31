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
    b=Bot(Settings('fake',frozenset({1,2}),encryption_key=Fernet.generate_key().decode()),db)
    yield b
    db.conn.close()


def event(uid,action):
    q=SimpleNamespace(data='ui:'+action,answer=AsyncMock(),message=SimpleNamespace(reply_text=AsyncMock()))
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid),effective_chat=SimpleNamespace(type='private'),callback_query=q)

async def test_threshold_pause_and_resume_buttons(service):
    await service.ui(event(1,'discount:60'),None)
    assert service.db.user(1)['threshold']==60
    await service.ui(event(1,'pause'),None)
    assert not service.db.user(1)['enabled']
    await service.ui(event(1,'resume'),None)
    assert service.db.user(1)['enabled']

async def test_remove_button_cannot_remove_another_users_location(service):
    service.db.user(1)
    service.db.run("INSERT INTO locations(user_id,name) VALUES(1,'Home')")
    await service.ui(event(2,'remove:1'),None)
    assert len(service.db.locations(1))==1
    await service.ui(event(1,'remove:1'),None)
    assert not service.db.locations(1)

async def test_disconnect_requires_second_button(service):
    service.db.user(1)
    service.accounts.save(1,{'access_token':'test'})
    u=event(1,'disconnect')
    await service.ui(u,None)
    assert service.accounts.generation(1)
    buttons=u.callback_query.message.reply_text.call_args.kwargs['reply_markup'].inline_keyboard
    assert any(b.callback_data=='ui:disconnect_yes' for row in buttons for b in row)

async def test_private_menu_rejects_unlisted_user(service):
    u=event(3,'discount:80')
    await service.ui(u,None)
    assert service.db.run('SELECT * FROM users WHERE id=3').fetchone() is None
    u.callback_query.answer.assert_awaited_once()

async def test_custom_threshold_prompt_and_input(service):
    await service.ui(event(1,'custom'),None)
    message=SimpleNamespace(text='55',reply_text=AsyncMock())
    u=SimpleNamespace(effective_user=SimpleNamespace(id=1),effective_chat=SimpleNamespace(type='private'),callback_query=None,message=message)
    await service.text(u,None)
    assert service.db.user(1)['threshold']==55
    assert 1 not in service.ui_inputs
