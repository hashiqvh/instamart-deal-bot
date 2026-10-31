import sqlite3
import time

class Database:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, threshold INTEGER NOT NULL DEFAULT 50, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS locations(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL, lat REAL, lon REAL, UNIQUE(user_id,name));
        CREATE TABLE IF NOT EXISTS prices(area TEXT, sku TEXT, time REAL, mrp INTEGER, price INTEGER, available INTEGER);
        CREATE INDEX IF NOT EXISTS price_lookup ON prices(area,sku,time);
        CREATE TABLE IF NOT EXISTS observations(area TEXT, sku TEXT, mrp INTEGER, price INTEGER, streak INTEGER, time REAL, PRIMARY KEY(area,sku));
        CREATE TABLE IF NOT EXISTS notifications(user_id INTEGER, area TEXT, sku TEXT, price INTEGER, time REAL, status TEXT, PRIMARY KEY(user_id,area,sku));
        ''')

        columns = {r[1] for r in self.conn.execute('PRAGMA table_info(locations)')}
        for column in ('swiggy_address_id','swiggy_generation'):
            if column not in columns:
                self.run(f'ALTER TABLE locations ADD COLUMN {column} TEXT')

        if any(r[1]=='lat' and r[3] for r in self.conn.execute('PRAGMA table_info(locations)')):
            with self.conn:
                self.conn.execute('CREATE TABLE locations_new(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL, lat REAL, lon REAL, swiggy_address_id TEXT, swiggy_generation TEXT, UNIQUE(user_id,name))')
                self.conn.execute('INSERT INTO locations_new SELECT id,user_id,name,lat,lon,swiggy_address_id,swiggy_generation FROM locations')
                self.conn.execute('DROP TABLE locations')
                self.conn.execute('ALTER TABLE locations_new RENAME TO locations')

    def run(self, sql, args=()):
        with self.conn:
            return self.conn.execute(sql, args)

    def user(self, uid):
        self.run("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,))
        return self.run("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    def locations(self, uid):
        return self.run("SELECT * FROM locations WHERE user_id=? ORDER BY id", (uid,)).fetchall()

    def targets(self):
        return self.run("SELECT locations.*, users.threshold FROM locations JOIN users ON users.id=user_id WHERE enabled=1").fetchall()

    @staticmethod
    def area(lat, lon):
        return f"{lat:.6f},{lon:.6f}" if lat is not None and lon is not None else "saved-address"

    def observe(self, area, p, now, max_age, confirmations, min_savings):
        old = self.run("SELECT * FROM observations WHERE area=? AND sku=?", (area,p.sku)).fetchone()
        history = self.run("SELECT MIN(mrp), MIN(price) FROM prices WHERE area=? AND sku=? AND available=1", (area,p.sku)).fetchone()
        streak = 1
        if old and p.available and old['mrp'] == p.mrp and old['price'] == p.price and now-old['time'] <= max_age:
            streak = old['streak'] + 1
        if not p.available:
            streak = 0
        self.run("INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?)", (area,p.sku,p.mrp,p.price,streak,now))
        self.run("INSERT INTO prices VALUES(?,?,?,?,?,?)", (area,p.sku,now,p.mrp,p.price,int(p.available)))
        # Never trust a newly inflated MRP; suppress prices above a recent low.
        genuine = (history[0] is None or p.mrp <= history[0]) and (history[1] is None or p.price <= history[1])
        return p.available and streak >= confirmations and genuine and p.mrp-p.price >= min_savings

    def reserve(self, uid, area, p):
        old = self.run("SELECT * FROM notifications WHERE user_id=? AND area=? AND sku=?", (uid,area,p.sku)).fetchone()
        # A further >=5% price improvement is a new deal; MRP changes aren't.
        if old and p.price > old['price'] * .95:
            return False
        self.run("INSERT OR REPLACE INTO notifications VALUES(?,?,?,?,?,?)", (uid,area,p.sku,p.price,time.time(),'pending'))
        return True

    def cleanup(self, days):
        cutoff = time.time()-days*86400
        self.run("DELETE FROM prices WHERE time<?", (cutoff,))
        self.run("DELETE FROM observations WHERE time<?", (cutoff,))
