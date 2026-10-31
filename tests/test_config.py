import pytest
from cryptography.fernet import Fernet
from instadeals.config import Settings

@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','test')
    monkeypatch.setenv('TOKEN_ENCRYPTION_KEY',Fernet.generate_key().decode())
    monkeypatch.setenv('PUBLIC_ACCESS','true')
    monkeypatch.setenv('OAUTH_PORT','8765')
    monkeypatch.setenv('OAUTH_BIND_HOST','127.0.0.1')
    return monkeypatch


def test_local_callback_allowed_only_on_loopback(env):
    env.setenv('OAUTH_CALLBACK_URL','http://localhost:8765/callback')
    assert Settings.env().callback_url=='http://localhost:8765/callback'
    env.setenv('OAUTH_BIND_HOST','0.0.0.0')
    with pytest.raises(ValueError):
        Settings.env()

@pytest.mark.parametrize('url',['http://example.com/callback','http://localhost.evil.com:8765/callback','http://localhost:8765/callback?redirect=x'])
def test_insecure_callback_rejected(env,url):
    env.setenv('OAUTH_CALLBACK_URL',url)
    with pytest.raises(ValueError):
        Settings.env()
