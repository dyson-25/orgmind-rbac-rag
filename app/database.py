import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from pwdlib import PasswordHash

from app.rbac_guard import PERSONAS, ROLE_SCOPES

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT OR IGNORE INTO meta VALUES ('acl_epoch', 0);
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, role TEXT NOT NULL, password_hash TEXT NOT NULL, auth_version INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL, pages INTEGER NOT NULL, metadata TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS document_scopes(document_id TEXT REFERENCES documents(id) ON DELETE CASCADE, scope TEXT NOT NULL, PRIMARY KEY(document_id,scope));
CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY, document_id TEXT REFERENCES documents(id) ON DELETE CASCADE, page INTEGER NOT NULL, text TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, text);
CREATE TRIGGER IF NOT EXISTS chunks_delete AFTER DELETE ON chunks BEGIN DELETE FROM chunks_fts WHERE chunk_id=old.id; END;
CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, user_id TEXT, role TEXT, event_id TEXT NOT NULL, action TEXT NOT NULL, decision TEXT NOT NULL, policy_intercept INTEGER NOT NULL, blocked_document_ids TEXT NOT NULL, policy_rule TEXT NOT NULL, acl_epoch INTEGER NOT NULL, previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS analytics(id INTEGER PRIMARY KEY, metric_id TEXT NOT NULL, scope TEXT NOT NULL, label TEXT NOT NULL, period TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL);
'''


class Database:
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.db_path
        self.lock = threading.RLock()
        self.audit_key = b''

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A separate persistent key keeps the audit chain verifiable across dev JWT rotations.
        key_path = self.path.parent / 'audit.key'
        try:
            with key_path.open('xb') as stream:
                key_path.chmod(0o600)
                stream.write(secrets.token_bytes(32))
        except FileExistsError:
            pass
        self.audit_key = key_path.read_bytes()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            for uid, role in PERSONAS.items():
                if not conn.execute('SELECT 1 FROM users WHERE id=?', (uid,)).fetchone():
                    conn.execute('INSERT INTO users(id,role,password_hash) VALUES(?,?,?)',
                                 (uid, role, PasswordHash.recommended().hash(secrets.token_urlsafe(32))))
            if not conn.execute('SELECT 1 FROM analytics').fetchone():
                rows = []
                for scope, count in [('hr', 18), ('finance', 24), ('engineering', 96), ('general', 12), ('executive', 6)]:
                    rows.append(('department_headcount', scope, scope.title(), '2026-Q4', count, 'people'))
                rows += [
                    ('quarterly_budget_summary','finance','Operating expenditure','2026-Q4',4200000,'USD'),
                    ('quarterly_budget_summary','finance','Reserve liquidity','2026-Q4',11800000,'USD'),
                    ('sla_uptime_metrics','engineering','Service availability','2026-Q4',99.982,'percent'),
                    ('compensation_band_summary','hr','L3 minimum','2026',1400000,'INR'),
                    ('compensation_band_summary','hr','L3 maximum','2026',1800000,'INR'),
                    ('compensation_band_summary','hr','L4 minimum','2026',2200000,'INR'),
                    ('compensation_band_summary','hr','L4 maximum','2026',3000000,'INR'),
                    ('vendor_spend_rollup','finance','Cloud infrastructure','2026-Q4',120000,'USD'),
                    ('vendor_spend_rollup','hr','Recruitment services','2026-Q4',18000,'USD'),
                ]
                conn.executemany('INSERT INTO analytics(metric_id,scope,label,period,value,unit) VALUES(?,?,?,?,?,?)', rows)

    def seed_documents(self, specs):
        with self.lock, self.connect() as conn:
            if conn.execute("SELECT 1 FROM meta WHERE key='corpus_seeded'").fetchone():
                return
            for spec in specs:
                from pathlib import Path
                raw = Path(spec['path']).read_bytes()
                conn.execute('INSERT OR IGNORE INTO documents VALUES(?,?,?,?,?,?)',
                             (spec['id'], spec['title'], spec['filename'], len(spec['pages']),
                              json.dumps({'source': 'synthetic-acme'}), hashlib.sha256(raw).hexdigest()))
                conn.executemany('INSERT OR IGNORE INTO document_scopes VALUES(?,?)',
                                 [(spec['id'], scope) for scope in spec['allowed_scopes']])
            conn.execute("INSERT INTO meta VALUES('corpus_seeded',1)")
            self.bump_epoch(conn)

    def epoch(self, conn=None):
        if conn is not None:
            return int(conn.execute("SELECT value FROM meta WHERE key='acl_epoch'").fetchone()[0])
        with self.connect() as connection:
            return self.epoch(connection)

    def bump_epoch(self, conn):
        conn.execute("UPDATE meta SET value=value+1 WHERE key='acl_epoch'")

    def user(self, uid):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
            return dict(row) if row else None

    def change_role(self, uid, role):
        if role not in ROLE_SCOPES:
            raise ValueError('Invalid role')
        with self.lock, self.connect() as conn:
            conn.execute('UPDATE users SET role=?, auth_version=auth_version+1 WHERE id=?', (role, uid))
            self.bump_epoch(conn)

    def audit(self, user, action, decision, intercept=False, blocked=(), rule='scope-intersection', event_id=None):
        with self.lock, self.connect() as conn:
            previous = conn.execute('SELECT entry_hash FROM audit_logs ORDER BY id DESC LIMIT 1').fetchone()
            values = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'user_id': user['id'] if user else None, 'role': user['role'] if user else None,
                'event_id': event_id or secrets.token_hex(12), 'action': action, 'decision': decision,
                'policy_intercept': int(intercept), 'blocked_document_ids': json.dumps(list(blocked)),
                'policy_rule': rule, 'acl_epoch': self.epoch(conn),
                'previous_hash': previous[0] if previous else '0' * 64,
            }
            digest = hmac.new(self.audit_key, json.dumps(values, sort_keys=True).encode(), hashlib.sha256).hexdigest()
            conn.execute(f'INSERT INTO audit_logs({",".join(values)},entry_hash) VALUES({",".join("?" for _ in range(len(values)+1))})',
                         (*values.values(), digest))

    def verify_audit(self):
        with self.connect() as conn:
            rows = conn.execute('SELECT * FROM audit_logs ORDER BY id').fetchall()
        previous = '0' * 64
        for row in rows:
            values = dict(row)
            values.pop('id')
            digest = values.pop('entry_hash')
            expected = hmac.new(self.audit_key, json.dumps(values, sort_keys=True).encode(), hashlib.sha256).hexdigest()
            if values['previous_hash'] != previous or not hmac.compare_digest(expected, digest):
                return False
            previous = digest
        return True
