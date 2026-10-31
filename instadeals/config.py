import os
from dataclasses import dataclass
from dotenv import load_dotenv
from urllib.parse import urlparse
from cryptography.fernet import Fernet

# Broad discovery terms, not an exhaustive catalog/category API.
DEFAULT_QUERIES = (
    'fruits', 'vegetables', 'milk', 'curd', 'paneer',
    'cheese', 'butter', 'eggs', 'bread', 'bakery',
    'meat', 'fish', 'frozen food', 'ice cream', 'rice',
    'atta', 'dal', 'cooking oil', 'spices', 'dry fruits',
    'breakfast cereal', 'noodles', 'pasta', 'sauces', 'snacks',
    'biscuits', 'chocolate', 'sweets', 'coffee', 'tea',
    'juice', 'soft drinks', 'health drinks', 'baby food', 'diapers',
    'shampoo', 'skin care', 'cosmetics', 'soap', 'oral care',
    'deodorant', 'sanitary pads', 'wellness', 'detergent', 'cleaning supplies',
    'kitchen', 'home essentials', 'pet food', 'stationery', 'toys',
    'sports', 'electronics', 'mobile accessories', 'charger', 'earphones',
    'Digitek', 'microphone', 'tripod', 'batteries', 'appliances',
)

@dataclass(frozen=True)
class Settings:
    telegram_token: str
    users: frozenset[int]
    database: str = "instadeals.db"
    poll: int = 300
    gap: float = 3
    queries: tuple[str, ...] = DEFAULT_QUERIES
    pages: int = 3
    savings: int = 10000
    confirmations: int = 2
    history_days: int = 30

    encryption_key: str = ""
    callback_url: str = ""
    oauth_host: str = "127.0.0.1"
    oauth_port: int = 8765
    public_access: bool = False

    @classmethod
    def env(cls):
        load_dotenv()
        s = cls(os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                frozenset(int(x.strip()) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()),
                os.getenv("DATABASE_PATH", "instadeals.db"),
                int(os.getenv("POLL_SECONDS", "300")), float(os.getenv("REQUEST_GAP_SECONDS", "3")),
                tuple(dict.fromkeys(x.strip() for x in os.getenv("SEARCH_QUERIES", ",".join(DEFAULT_QUERIES)).split(",") if x.strip())),
                int(os.getenv("MAX_PAGES", "3")), int(os.getenv("MIN_SAVINGS_RUPEES", "100"))*100,
                int(os.getenv("CONFIRMATIONS", "2")), int(os.getenv("HISTORY_DAYS", "30")),
                os.getenv("TOKEN_ENCRYPTION_KEY", ""), os.getenv("OAUTH_CALLBACK_URL", ""),
                os.getenv("OAUTH_BIND_HOST", "127.0.0.1"), int(os.getenv("OAUTH_PORT", "8765")),
                os.getenv("PUBLIC_ACCESS", "false").lower()=="true")
        if not s.telegram_token or not s.encryption_key:
            raise ValueError("Set TELEGRAM_BOT_TOKEN and TOKEN_ENCRYPTION_KEY in .env")
        Fernet(s.encryption_key)
        if not s.public_access and not s.users:
            raise ValueError("Set ALLOWED_USER_IDS or explicitly enable PUBLIC_ACCESS")
        if s.callback_url:
            u = urlparse(s.callback_url)
            local = s.callback_url == f"http://localhost:{s.oauth_port}/callback" and s.oauth_host == "127.0.0.1"
            if not local and (u.scheme != "https" or not u.hostname or u.username or u.password or u.path != "/callback" or u.query or u.fragment):
                raise ValueError("Use an HTTPS callback, or http://localhost:OAUTH_PORT/callback with loopback binding for local testing")
        if s.poll < 60 or s.gap < 1 or not s.queries or not 1 <= s.pages <= 20 or s.confirmations < 2 or s.history_days < 1 or s.savings < 0:
            raise ValueError("Invalid polling, query, confirmation or retention settings")
        return s
