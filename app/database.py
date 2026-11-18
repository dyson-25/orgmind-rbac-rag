import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from pwdlib import PasswordHash

from app.rbac_guard import PERSONAS, ROLE_SCOPES

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT OR IGNORE INTO meta(key,value) VALUES ('acl_epoch', 0);
CREATE TABLE IF NOT EXISTS departments(id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE, description TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, role TEXT NOT NULL, password_hash TEXT NOT NULL, auth_version INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL DEFAULT '', email TEXT, department_id TEXT REFERENCES departments(id) ON DELETE SET NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)));
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL, pages INTEGER NOT NULL, metadata TEXT NOT NULL, digest TEXT NOT NULL, owner_id TEXT REFERENCES users(id) ON DELETE SET NULL, department_id TEXT REFERENCES departments(id) ON DELETE SET NULL, visibility TEXT NOT NULL DEFAULT 'scoped', status TEXT NOT NULL DEFAULT 'ready', uploaded_at TEXT, storage_path TEXT);
CREATE TABLE IF NOT EXISTS document_scopes(document_id TEXT REFERENCES documents(id) ON DELETE CASCADE, scope TEXT NOT NULL, PRIMARY KEY(document_id,scope));
CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY, document_id TEXT REFERENCES documents(id) ON DELETE CASCADE, page INTEGER NOT NULL, text TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, text);
CREATE TRIGGER IF NOT EXISTS chunks_delete AFTER DELETE ON chunks BEGIN DELETE FROM chunks_fts WHERE chunk_id=old.id; END;
CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, user_id TEXT, role TEXT, event_id TEXT NOT NULL, action TEXT NOT NULL, decision TEXT NOT NULL, policy_intercept INTEGER NOT NULL, blocked_document_ids TEXT NOT NULL, policy_rule TEXT NOT NULL, acl_epoch INTEGER NOT NULL, previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS analytics(id INTEGER PRIMARY KEY, metric_id TEXT NOT NULL, scope TEXT NOT NULL, label TEXT NOT NULL, period TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL);
'''


class AccountConflict(ValueError):
    pass


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
        with self.lock, self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute('BEGIN IMMEDIATE')
            self._migrate_accounts(conn)
            for uid, role in PERSONAS.items():
                if not conn.execute('SELECT 1 FROM users WHERE id=?', (uid,)).fetchone():
                    conn.execute('INSERT INTO users(id,role,password_hash,name) VALUES(?,?,?,?)',
                                 (uid, role, PasswordHash.recommended().hash(secrets.token_urlsafe(32)),
                                  uid.replace('_', ' ').title()))
            self._provision_demo_accounts(conn)
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

    def _migrate_accounts(self, conn):
        additions = {
            'users': {
                'name': "TEXT NOT NULL DEFAULT ''", 'email': 'TEXT',
                'department_id': 'TEXT REFERENCES departments(id) ON DELETE SET NULL',
                'active': 'INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))',
            },
            'documents': {
                'owner_id': 'TEXT REFERENCES users(id) ON DELETE SET NULL',
                'department_id': 'TEXT REFERENCES departments(id) ON DELETE SET NULL',
                'visibility': "TEXT NOT NULL DEFAULT 'scoped'",
                'status': "TEXT NOT NULL DEFAULT 'ready'", 'uploaded_at': 'TEXT', 'storage_path': 'TEXT',
            },
        }
        for table, columns in additions.items():
            existing = {row['name'] for row in conn.execute(f'PRAGMA table_info({table})')}
            for name, definition in columns.items():
                if name not in existing:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
                    if table == 'users' and name == 'name':
                        conn.execute("UPDATE users SET name=replace(id,'_',' ')")
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_login_id ON users(lower(trim(id)))')
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_login_email ON users(lower(trim(email))) WHERE email IS NOT NULL')
        conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',1) ON CONFLICT(key) DO UPDATE SET value=max(value,1)")

    @staticmethod
    def normalize_username(username):
        username = username.strip().casefold()
        if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,99}', username):
            raise ValueError('Username must use 1 to 100 letters, digits, dots, underscores or hyphens')
        return username

    @staticmethod
    def _account_values(values):
        allowed = {'name', 'email', 'role', 'department_id', 'active', 'password'}
        if not values or not set(values) <= allowed:
            raise ValueError('Invalid account fields')
        values = dict(values)
        if 'name' in values:
            if not isinstance(values['name'], str) or not 1 <= len(values['name'].strip()) <= 200:
                raise ValueError('Name must contain 1 to 200 characters')
            values['name'] = values['name'].strip()
        if 'email' in values and values['email'] is not None:
            email = values['email']
            if not isinstance(email, str):
                raise ValueError('Invalid email address')
            email = email.strip().casefold()
            if len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
                raise ValueError('Invalid email address')
            values['email'] = email
        if 'role' in values and values['role'] not in ROLE_SCOPES:
            raise ValueError('Invalid role')
        if 'active' in values and not isinstance(values['active'], bool):
            raise ValueError('Active must be a boolean')
        if 'password' in values:
            password = values.pop('password')
            if not isinstance(password, str) or not 12 <= len(password) <= 256:
                raise ValueError('Password must contain 12 to 256 characters')
            values['password_hash'] = PasswordHash.recommended().hash(password)
        return values

    @staticmethod
    def _check_department(conn, values):
        if values.get('department_id') is not None and not conn.execute(
            'SELECT 1 FROM departments WHERE id=?', (values['department_id'],)
        ).fetchone():
            raise ValueError('Unknown department')

    def _provision_demo_accounts(self, conn):
        for role in ('admin', 'employee'):
            username = getattr(self.settings, f'demo_{role}_username', '')
            if not username:
                continue
            uid = self.normalize_username(username)
            key = f'demo_account_provisioned:{role}:{uid}'
            if conn.execute('SELECT 1 FROM meta WHERE key=?', (key,)).fetchone():
                continue
            existing = conn.execute('SELECT role FROM users WHERE lower(trim(id))=?', (uid,)).fetchone()
            if existing and existing['role'] != role:
                raise ValueError('Configured demo username belongs to a different role')
            password_hash = PasswordHash.recommended().hash(getattr(self.settings, f'demo_{role}_password'))
            if existing:
                conn.execute('UPDATE users SET password_hash=?,auth_version=auth_version+1 WHERE lower(trim(id))=?',
                             (password_hash, uid))
            else:
                conn.execute('INSERT INTO users(id,role,password_hash,name) VALUES(?,?,?,?)',
                             (uid, role, password_hash, f'Demo {role.title()}'))
            conn.execute('INSERT INTO meta(key,value) VALUES(?,1)', (key,))
            self.bump_epoch(conn)

    def create_account(self, username, *, name, password, role='employee', email=None, department_id=None, active=True):
        uid = self.normalize_username(username)
        values = self._account_values(dict(name=name, password=password, role=role, email=email,
                                           department_id=department_id, active=active))
        try:
            with self.lock, self.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                self._check_department(conn, values)
                conn.execute(f'INSERT INTO users(id,{",".join(values)}) VALUES({",".join("?" for _ in range(len(values)+1))})',
                             (uid, *values.values()))
                self.bump_epoch(conn)
        except sqlite3.IntegrityError:
            raise AccountConflict('Username or email already exists') from None
        return self.user(uid)

    def update_account(self, uid, changes):
        values = self._account_values(changes)
        try:
            with self.lock, self.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
                if not current:
                    raise KeyError('User not found')
                self._check_department(conn, values)
                if current['role'] == 'admin' and current['active'] and (
                    values.get('role', current['role']) != 'admin' or not values.get('active', current['active'])
                ) and conn.execute("SELECT count(*) FROM users WHERE role='admin' AND active=1").fetchone()[0] <= 1:
                    raise AccountConflict('Cannot deactivate or demote the last active administrator')
                conn.execute(f'UPDATE users SET {",".join(f"{key}=?" for key in values)},auth_version=auth_version+1 WHERE id=?',
                             (*values.values(), uid))
                self.bump_epoch(conn)
        except sqlite3.IntegrityError:
            raise AccountConflict('Email already exists') from None
        return self.user(uid)

    def delete_department(self, department_id):
        with self.lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM departments WHERE id=?', (department_id,)).fetchone():
                raise KeyError('Department not found')
            if conn.execute('SELECT 1 FROM documents WHERE department_id=?', (department_id,)).fetchone():
                raise AccountConflict('Department is assigned to documents; reassign documents before deleting it')
            conn.execute('UPDATE users SET department_id=NULL,auth_version=auth_version+1 WHERE department_id=?',
                         (department_id,))
            conn.execute('DELETE FROM departments WHERE id=?', (department_id,))
            self.bump_epoch(conn)

    def seed_documents(self, specs):
        with self.lock, self.connect() as conn:
            if conn.execute("SELECT 1 FROM meta WHERE key='corpus_seeded'").fetchone():
                return
            for spec in specs:
                from pathlib import Path
                raw = Path(spec['path']).read_bytes()
                conn.execute('INSERT OR IGNORE INTO documents(id,title,filename,pages,metadata,digest) VALUES(?,?,?,?,?,?)',
                             (spec['id'], spec['title'], spec['filename'], len(spec['pages']),
                              json.dumps({'source': 'synthetic-acme'}), hashlib.sha256(raw).hexdigest()))
                conn.executemany('INSERT OR IGNORE INTO document_scopes(document_id,scope) VALUES(?,?)',
                                 [(spec['id'], scope) for scope in spec['allowed_scopes']])
            conn.execute("INSERT INTO meta(key,value) VALUES('corpus_seeded',1)")
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

    def user_by_login(self, login):
        normalized = login.strip().casefold()
        with self.connect() as conn:
            rows = conn.execute('SELECT * FROM users WHERE lower(trim(id))=? OR lower(trim(email))=?',
                                (normalized, normalized)).fetchall()
            return dict(rows[0]) if len(rows) == 1 else None

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
