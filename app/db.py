import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from cryptography.fernet import Fernet


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Busy(Exception):
    pass


class Store:
    def __init__(self, directory, key):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.path = directory / 'briefing.sqlite3'
        self.cipher = Fernet(key.encode())
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL, oauth_state TEXT);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, created TEXT NOT NULL, status TEXT NOT NULL,
                config TEXT NOT NULL, markdown TEXT NOT NULL DEFAULT '',
                confirmed TEXT, archive_status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '', read_completed TEXT);
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
                source_item TEXT NOT NULL, video_id TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
                target TEXT, target_item TEXT,
                move_status TEXT NOT NULL DEFAULT 'pending',
                UNIQUE(run_id, source_item));
            CREATE TABLE IF NOT EXISTS sections (
                id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
                position INTEGER NOT NULL, title TEXT NOT NULL, markdown TEXT NOT NULL, read_at TEXT,
                UNIQUE(run_id,position));
            CREATE TABLE IF NOT EXISTS work_lock (id INTEGER PRIMARY KEY CHECK(id=1), kind TEXT NOT NULL, run_id INTEGER);
            CREATE TABLE IF NOT EXISTS probes (id INTEGER PRIMARY KEY, created TEXT NOT NULL, config TEXT NOT NULL, results TEXT NOT NULL, success INTEGER NOT NULL);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def query(self, sql, params=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    def execute(self, sql, params=()):
        with self.connect() as db:
            return db.execute(sql, params).lastrowid

    def settings(self):
        return {r['key']: self.cipher.decrypt(r['value'].encode()).decode() for r in self.query('SELECT * FROM settings')}

    def save_settings(self, values):
        with self.connect() as db:
            for k, v in values.items():
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (k, self.cipher.encrypt(str(v).encode()).decode()))

    def claim(self, kind, run_id=None):
        try:
            self.execute('INSERT INTO work_lock VALUES (1,?,?)', (kind, run_id))
        except sqlite3.IntegrityError:
            raise Busy('Ein Durchlauf oder eine Archivierung läuft bereits.')

    def release(self):
        self.execute('DELETE FROM work_lock')

    def recover(self):
        # Only called while holding the process-level exclusive lock on startup.
        self.execute("UPDATE runs SET status='interrupted', error='Durch Serverneustart unterbrochen. Erneuter manueller Start möglich.' WHERE status='running'")
        self.execute("UPDATE runs SET archive_status='partial' WHERE archive_status='running'")
        self.release()

    def run(self, run_id):
        rows = self.query('SELECT * FROM runs WHERE id=?', (run_id,))
        if not rows:
            raise KeyError(run_id)
        r = rows[0]
        r['sections'] = self.query('SELECT * FROM sections WHERE run_id=? ORDER BY position', (run_id,))
        r['unread'] = sum(not section['read_at'] for section in r['sections'])
        r['items'] = self.query('SELECT * FROM items WHERE run_id=? ORDER BY id', (run_id,))
        r['counts'] = {s: sum(i['status'] == s for i in r['items']) for s in ['success', 'unavailable', 'temporary', 'pending']}
        return r

    def probe_ready(self, config):
        rows = self.query('SELECT * FROM probes ORDER BY id DESC LIMIT 1')
        return bool(rows and rows[0]['success'] and json.loads(rows[0]['config']) == config)
