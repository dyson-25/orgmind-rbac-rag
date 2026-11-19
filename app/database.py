"""Anonymous workspace storage with backed-up migration and non-destructive expiry."""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


# Exact legacy identities are used only for migration, never for provisioning.
LEGACY_SEEDS = {
    'DOC_01': 'DOC_01_Employee_Handbook.pdf',
    'DOC_02': 'DOC_02_HR_Leave_And_Comp_Policy.pdf',
    'DOC_03': 'DOC_03_Finance_Q4_Budget_Report.pdf',
    'DOC_04': 'DOC_04_Executive_Bonus_And_MA.pdf',
    'DOC_05': 'DOC_05_Engineering_System_Architecture.pdf',
    'DOC_06': 'DOC_06_Tech_Infra_Runbook.pdf',
    'DOC_07': 'DOC_07_Procurement_Vendor_Policy.pdf',
    'DOC_08': 'DOC_08_General_IT_Acceptable_Use.pdf',
    'DOC_09': 'DOC_09_Poisoned_Vendor_Brochure.pdf',
}
SESSION_SCHEMA = (
    'CREATE TABLE sessions(owner_id TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL)',
    'CREATE INDEX sessions_expiry ON sessions(expires_at)',
    'CREATE INDEX documents_owner ON documents(owner_id)',
)
MAX_SESSIONS = 10000
SCHEMA = (
    'CREATE TABLE documents(id TEXT PRIMARY KEY, filename TEXT NOT NULL, pages INTEGER NOT NULL, '
    "status TEXT NOT NULL DEFAULT 'indexed', uploaded_at TEXT NOT NULL, storage_path TEXT, digest TEXT NOT NULL, owner_id TEXT)",
    'CREATE TABLE chunks(id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, '
    'page INTEGER NOT NULL, text TEXT NOT NULL)',
    'CREATE INDEX chunks_document ON chunks(document_id)',
    'CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED, text)',
    'CREATE TABLE meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL)',
    "INSERT INTO meta VALUES('index_version',0)",
    'CREATE TRIGGER chunks_insert AFTER INSERT ON chunks BEGIN '
    'INSERT INTO chunks_fts(chunk_id,text) VALUES(new.id,new.text); '
    "UPDATE meta SET value=value+1 WHERE key='index_version'; END",
    'CREATE TRIGGER chunks_delete AFTER DELETE ON chunks BEGIN '
    'DELETE FROM chunks_fts WHERE chunk_id=old.id; '
    "UPDATE meta SET value=value+1 WHERE key='index_version'; END",
    'CREATE TRIGGER chunks_update AFTER UPDATE ON chunks BEGIN '
    'DELETE FROM chunks_fts WHERE chunk_id=old.id; '
    'INSERT INTO chunks_fts(chunk_id,text) VALUES(new.id,new.text); '
    "UPDATE meta SET value=value+1 WHERE key='index_version'; END",
    'CREATE TRIGGER documents_update AFTER UPDATE ON documents BEGIN '
    "UPDATE meta SET value=value+1 WHERE key='index_version'; END",
    *SESSION_SCHEMA,
    'PRAGMA user_version=4',
)


class Database:
    def __init__(self, settings):
        self.settings = settings
        self.path = Path(settings.db_path)
        self.lock = threading.RLock()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, self.connect() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'documents' in tables:
                columns = {r[1] for r in conn.execute('PRAGMA table_info(documents)')}
                legacy = 'metadata' in columns or bool(tables & {'users', 'document_scopes', 'audit_logs'})
                if legacy:
                    self._backup(conn)
                    self._migrate(conn, tables)
                elif conn.execute('PRAGMA user_version').fetchone()[0] == 3:
                    expected = {'id', 'filename', 'pages', 'status', 'uploaded_at', 'storage_path', 'digest'}
                    if columns != expected or not {'chunks', 'chunks_fts', 'meta'} <= tables:
                        raise ValueError('Unrecognized document database; no data was changed')
                    self._backup(conn)
                    self._migrate_shared(conn)
                elif conn.execute('PRAGMA user_version').fetchone()[0] != 4 or 'owner_id' not in columns or 'sessions' not in tables:
                    raise ValueError('Unrecognized document database; no data was changed')
            elif tables:
                raise ValueError('Unrecognized database; no data was changed')
            else:
                conn.execute('BEGIN IMMEDIATE')
                self._create_schema(conn)
        self.path.chmod(0o600)

    @staticmethod
    def _create_schema(conn):
        # executescript commits implicitly, so use individual transactional statements.
        for statement in SCHEMA:
            conn.execute(statement)

    @staticmethod
    def _migrate_shared(conn):
        conn.execute('BEGIN IMMEDIATE')
        # NULL ownership quarantines every old shared document; visitors never inherit it.
        conn.execute('ALTER TABLE documents ADD COLUMN owner_id TEXT')
        # A pre-workspace pending delete must not remove a quarantined original on recovery.
        conn.execute("UPDATE documents SET status='failed' WHERE status='deleting'")
        for statement in SESSION_SCHEMA:
            conn.execute(statement)
        conn.execute('PRAGMA user_version=4')
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Document migration failed integrity validation')

    def _backup(self, conn):
        destination = self.path.with_name(self.path.name + '.legacy-' + uuid.uuid4().hex + '.bak')
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        try:
            with sqlite3.connect(destination) as backup:
                conn.backup(backup)
            with destination.open('rb') as stream:
                os.fsync(stream.fileno())
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    def _is_seed(self, row):
        filename = LEGACY_SEEDS.get(row['id'])
        if filename is None or row['filename'] != filename:
            return False
        try:
            if json.loads(row.get('metadata') or '{}').get('source') != 'synthetic-acme':
                return False
            path = self._safe_path(self.settings.data_dir / 'corpus' / filename)
            return path is not None and hashlib.sha256(path.read_bytes()).hexdigest() == row['digest']
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def _migrate(self, conn, tables):
        # No original files are changed by migration; uncertain identities are retained.
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('BEGIN IMMEDIATE')
        documents = [dict(row) for row in conn.execute('SELECT * FROM documents')]
        documents = [row for row in documents if not self._is_seed(row)]
        keep = {row['id'] for row in documents}
        chunks = [tuple(row) for row in conn.execute('SELECT id,document_id,page,text FROM chunks')
                  if row['document_id'] in keep] if 'chunks' in tables else []
        for table in ('document_scopes', 'audit_logs', 'analytics', 'chunks_fts', 'chunks', 'documents',
                      'users', 'departments', 'meta'):
            conn.execute(f'DROP TABLE IF EXISTS {table}')
        self._create_schema(conn)
        timestamp = datetime.now(timezone.utc).isoformat()
        for row in documents:
            stored = row.get('storage_path')
            if not stored:
                for root in (self.settings.data_dir / 'uploads', self.settings.data_dir / 'runtime' / 'uploads',
                             self.settings.data_dir / 'corpus'):
                    for name in (row['id'] + '.pdf', row['filename']):
                        candidate = self._safe_path(root / name)
                        if candidate is not None:
                            stored = str(candidate)
                            break
                    if stored:
                        break
            status = row.get('status') or 'indexed'
            if status == 'ready':
                status = 'indexed'
            elif status == 'deleting':
                status = 'failed'
            conn.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)', (
                row['id'], row['filename'], row['pages'], status, row.get('uploaded_at') or timestamp,
                stored, row['digest'], None,
            ))
        conn.executemany('INSERT INTO chunks VALUES(?,?,?,?)', chunks)
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Legacy document migration failed integrity validation')

    def _safe_path(self, stored, *, must_exist=True):
        if not stored:
            return None
        candidate = Path(stored)
        root = Path(self.settings.data_dir).resolve()
        allowed = (root / 'uploads', root / 'runtime' / 'uploads', root / 'corpus')
        # Do not follow symlinked originals or storage directories, even within DATA_DIR.
        if any(parent.is_symlink() for parent in (candidate, *candidate.parents)):
            return None
        candidate = candidate.absolute()
        if candidate.parent not in allowed or candidate.resolve().parent not in allowed:
            return None
        if must_exist and not candidate.is_file():
            return None
        return candidate

    @staticmethod
    def _session_ttl(ttl_seconds):
        if not isinstance(ttl_seconds, (int, float)) or not 0 < ttl_seconds <= 366 * 86400:
            raise ValueError('Invalid session retention')
        return ttl_seconds

    def create_session(self, ttl_seconds):
        ttl = self._session_ttl(ttl_seconds)
        token = secrets.token_urlsafe(32)
        owner_id = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self.lock, self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM sessions WHERE expires_at<=? AND NOT EXISTS '
                         '(SELECT 1 FROM documents WHERE documents.owner_id=sessions.owner_id)', (now,))
            if conn.execute('SELECT count(*) FROM sessions').fetchone()[0] >= MAX_SESSIONS:
                raise ValueError('Workspace limit reached')
            conn.execute('INSERT INTO sessions VALUES(?,?,?)', (owner_id, now, now + ttl))
        return token

    def resolve_session(self, token, ttl_seconds):
        ttl = self._session_ttl(ttl_seconds)
        if not isinstance(token, str) or re.fullmatch(r'[A-Za-z0-9_-]{43}', token) is None:
            return None
        owner_id = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self.lock, self.connect() as conn:
            renewed = conn.execute('UPDATE sessions SET expires_at=MAX(expires_at,?) '
                                   'WHERE owner_id=? AND expires_at>?', (now + ttl, owner_id, now))
            return owner_id if renewed.rowcount else None

    def cleanup_sessions(self):
        """Discard only expired empty workspaces; retained originals never expire on disk."""
        with self.lock, self.connect() as conn:
            return conn.execute('DELETE FROM sessions WHERE expires_at<=? AND NOT EXISTS '
                                '(SELECT 1 FROM documents WHERE documents.owner_id=sessions.owner_id)',
                                (time.time(),)).rowcount

    @staticmethod
    def owner_filter(owner_id, alias='documents'):
        # None is reserved for trusted maintenance, not anonymous/public access.
        if owner_id is None:
            return '1=1', ()
        return (f'{alias}.owner_id=? AND EXISTS '
                '(SELECT 1 FROM sessions WHERE owner_id=? AND expires_at>?)',
                (owner_id, owner_id, time.time()))

    def list_documents(self, *, owner_id=None):
        where, params = self.owner_filter(owner_id)
        with self.lock, self.connect() as conn:
            return [dict(row) for row in conn.execute(
                f'SELECT * FROM documents WHERE {where} ORDER BY uploaded_at DESC,id', params)]

    def get_document(self, doc_id, *, owner_id=None):
        where, params = self.owner_filter(owner_id)
        with self.lock, self.connect() as conn:
            row = conn.execute(f'SELECT * FROM documents WHERE id=? AND {where}', (doc_id, *params)).fetchone()
            if row is None:
                raise KeyError(doc_id)
            return dict(row)

    def get_chunk(self, doc_id, chunk_id, *, owner_id=None):
        where, params = self.owner_filter(owner_id, 'd')
        with self.lock, self.connect() as conn:
            row = conn.execute('SELECT c.*,d.filename FROM chunks c JOIN documents d ON d.id=c.document_id '
                               f"WHERE c.document_id=? AND c.id=? AND d.status='indexed' AND {where}",
                               (doc_id, chunk_id, *params)).fetchone()
            if row is None:
                raise KeyError(chunk_id)
            return dict(row)

    def original_path(self, doc_id, *, owner_id=None):
        row = self.get_document(doc_id, owner_id=owner_id)
        path = self._safe_path(row['storage_path'])
        if path is None:
            raise KeyError(doc_id)
        return path

    def epoch(self, conn=None):
        if conn is not None:
            return conn.execute("SELECT value FROM meta WHERE key='index_version'").fetchone()[0]
        with self.connect() as connection:
            return self.epoch(connection)
