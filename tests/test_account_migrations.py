import sqlite3

import pytest
from pwdlib import PasswordHash

from app.auth import Auth
from app.config import Settings
from app.database import Database
from app.rbac_guard import PERSONAS


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key in ('DEMO_ADMIN_USERNAME', 'DEMO_ADMIN_PASSWORD', 'DEMO_EMPLOYEE_USERNAME', 'DEMO_EMPLOYEE_PASSWORD'):
        monkeypatch.delenv(key, raising=False)
    return Settings(data_dir=tmp_path, jwt_secret='migration-tests-only-' * 3, allow_demo=True)


def schema_snapshot(db):
    with db.connect() as conn:
        return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY 1')]
                for table in ('meta', 'users', 'documents', 'document_scopes', 'chunks', 'analytics', 'departments', 'audit_logs')}


def test_fresh_schema_is_idempotent_and_preserves_seed_contract(settings):
    db = Database(settings)
    db.initialize()
    before = schema_snapshot(db)
    db.initialize()
    assert schema_snapshot(db) == before
    assert len(before['users']) == 6 and len(before['analytics']) == 14
    assert {uid: db.user(uid)['role'] for uid in PERSONAS} == PERSONAS
    assert before['departments'] == []
    assert db.epoch() == 0
    with db.connect() as conn:
        assert {'name', 'email', 'department_id', 'active'} <= {row['name'] for row in conn.execute('PRAGMA table_info(users)')}
        assert {'owner_id', 'department_id', 'visibility', 'status', 'uploaded_at', 'storage_path'} <= {
            row['name'] for row in conn.execute('PRAGMA table_info(documents)')}
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_legacy_migration_preserves_accounts_corpus_acl_and_versions(settings):
    settings.db_path.parent.mkdir(parents=True)
    password_hash = PasswordHash.recommended().hash('legacy-test-password')
    with sqlite3.connect(settings.db_path) as conn:
        conn.executescript('''
            CREATE TABLE meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
            INSERT INTO meta(key,value) VALUES('acl_epoch',7),('corpus_seeded',1);
            CREATE TABLE users(id TEXT PRIMARY KEY,role TEXT NOT NULL,password_hash TEXT NOT NULL,auth_version INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE documents(id TEXT PRIMARY KEY,title TEXT NOT NULL,filename TEXT NOT NULL,pages INTEGER NOT NULL,metadata TEXT NOT NULL,digest TEXT NOT NULL);
            CREATE TABLE document_scopes(document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,scope TEXT NOT NULL,PRIMARY KEY(document_id,scope));
            CREATE TABLE chunks(id TEXT PRIMARY KEY,document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,page INTEGER NOT NULL,text TEXT NOT NULL);
            CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED,text);
            INSERT INTO chunks_fts(chunk_id,text) VALUES('chunk-1','Original text');
            CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,timestamp TEXT NOT NULL,user_id TEXT,role TEXT,event_id TEXT NOT NULL,action TEXT NOT NULL,decision TEXT NOT NULL,policy_intercept INTEGER NOT NULL,blocked_document_ids TEXT NOT NULL,policy_rule TEXT NOT NULL,acl_epoch INTEGER NOT NULL,previous_hash TEXT NOT NULL,entry_hash TEXT NOT NULL);
        ''')
        conn.executemany('INSERT INTO users(id,role,password_hash,auth_version) VALUES(?,?,?,?)',
                         [(uid, role, password_hash, 3) for uid, role in PERSONAS.items()])
        conn.execute('INSERT INTO documents(id,title,filename,pages,metadata,digest) VALUES(?,?,?,?,?,?)',
                     ('DOC_01', 'Original title', 'original.pdf', 2, '{"source":"synthetic-acme"}', 'unchanged-digest'))
        conn.execute('INSERT INTO document_scopes(document_id,scope) VALUES(?,?)', ('DOC_01', 'general'))
        conn.execute('INSERT INTO chunks(id,document_id,page,text) VALUES(?,?,?,?)', ('chunk-1', 'DOC_01', 1, 'Original text'))
    db = Database(settings)
    db.audit_key = b'migration-audit-test-only-key-1234'
    (settings.db_path.parent / 'audit.key').write_bytes(db.audit_key)
    db.audit(db.user('eng_user'), 'legacy-audit-event', 'allow')
    with db.connect() as conn:
        legacy_audit = tuple(conn.execute('SELECT * FROM audit_logs').fetchone())
    db.initialize()
    with db.connect() as conn:
        assert tuple(conn.execute('SELECT * FROM audit_logs').fetchone()) == legacy_audit
    assert db.verify_audit()
    auth = Auth(db, settings)
    assert auth.login('ENG_USER', 'legacy-test-password')['user']['id'] == 'eng_user'
    for uid, role in PERSONAS.items():
        user = db.user(uid)
        assert user['role'] == role and user['password_hash'] == password_hash
        assert user['auth_version'] == 3 and user['active'] == 1
        assert user['email'] is None and user['department_id'] is None
    assert db.epoch() == 7
    with db.connect() as conn:
        doc = dict(conn.execute('SELECT * FROM documents').fetchone())
        assert doc['title'] == 'Original title' and doc['digest'] == 'unchanged-digest'
        assert doc['visibility'] == 'scoped' and doc['status'] == 'ready'
        assert doc['owner_id'] is None and doc['uploaded_at'] is None and doc['storage_path'] is None
        assert conn.execute('SELECT scope FROM document_scopes').fetchone()[0] == 'general'
        assert conn.execute('SELECT text FROM chunks').fetchone()[0] == 'Original text'
        assert conn.execute("SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH 'Original'").fetchone()[0] == 'chunk-1'
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    before = schema_snapshot(db)
    db.initialize()
    db.seed_documents([])
    assert schema_snapshot(db) == before
    assert db.verify_audit()


def test_private_document_foreign_keys_and_named_seed_inserts(settings):
    db = Database(settings)
    db.initialize()
    source = settings.data_dir / 'source.pdf'
    source.write_bytes(b'synthetic-pdf-placeholder')
    db.seed_documents([{'id': 'seed', 'title': 'Seed', 'filename': source.name, 'path': str(source),
                        'pages': ['one'], 'allowed_scopes': ['general']}])
    with db.connect() as conn:
        conn.execute('INSERT INTO departments(id,name) VALUES(?,?)', ('eng', 'Engineering'))
        conn.execute('INSERT INTO documents(id,title,filename,pages,metadata,digest,owner_id,department_id,visibility,status,uploaded_at,storage_path) '
                     'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     ('private', 'Private', 'private.pdf', 1, '{}', 'digest', 'eng_user', 'eng', 'private', 'ready', '2026-11-17T00:00:00+00:00', 'private/doc.pdf'))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE documents SET owner_id='missing' WHERE id='private'")
        conn.execute("DELETE FROM users WHERE id='eng_user'")
        assert conn.execute("SELECT owner_id FROM documents WHERE id='private'").fetchone()[0] is None
        assert conn.execute("SELECT visibility FROM documents WHERE id='seed'").fetchone()[0] == 'scoped'
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_explicit_demo_credentials_are_one_time_and_argon2(settings, monkeypatch):
    monkeypatch.setenv('DEMO_ADMIN_USERNAME', ' Config_Admin ')
    monkeypatch.setenv('DEMO_ADMIN_PASSWORD', 'explicit-admin-test-password')
    monkeypatch.setenv('DEMO_EMPLOYEE_USERNAME', 'config_employee')
    monkeypatch.setenv('DEMO_EMPLOYEE_PASSWORD', 'explicit-employee-test-password')
    configured = Settings(data_dir=settings.data_dir, jwt_secret=settings.jwt_secret, allow_demo=False)
    assert 'explicit-admin-test-password' not in repr(configured)
    assert 'explicit-employee-test-password' not in repr(configured)
    db = Database(configured)
    db.initialize()
    auth = Auth(db, configured)
    assert auth.login('CONFIG_ADMIN', 'explicit-admin-test-password')['user']['role'] == 'admin'
    assert auth.login('config_employee', 'explicit-employee-test-password')['user']['scopes'] == ('general',)
    assert db.user('config_admin')['password_hash'].startswith('$argon2id$')
    before = schema_snapshot(db)
    db.initialize()
    assert schema_snapshot(db) == before
    assert len(before['users']) == 8
    db.update_account('config_employee', {'active': False, 'password': 'locally-rotated-test-password'})
    before = schema_snapshot(db)
    db.initialize()
    assert schema_snapshot(db) == before


def test_demo_provision_existing_seed_and_no_unconfigured_extras(settings):
    settings.demo_admin_username = 'admin_user'
    settings.demo_admin_password = 'explicit-seed-test-password'
    db = Database(settings)
    db.initialize()
    assert Auth(db, settings).login('admin_user', 'explicit-seed-test-password')['user']['role'] == 'admin'
    assert len(schema_snapshot(db)['users']) == 6


@pytest.mark.parametrize('changes', [
    {'demo_admin_username': 'admin'}, {'demo_employee_password': 'explicit-test-password'},
    {'demo_admin_username': 'admin', 'demo_admin_password': 'short'},
    {'demo_admin_username': 'same', 'demo_admin_password': 'explicit-test-password',
     'demo_employee_username': 'SAME', 'demo_employee_password': 'explicit-other-password'},
])
def test_demo_configuration_requires_explicit_valid_pairs(settings, changes):
    with pytest.raises(ValueError):
        Settings(data_dir=settings.data_dir, jwt_secret=settings.jwt_secret, **changes)


def test_demo_configuration_cannot_elevate_existing_seed(settings):
    settings.demo_admin_username = 'eng_user'
    settings.demo_admin_password = 'explicit-seed-test-password'
    with pytest.raises(ValueError, match='different role'):
        Database(settings).initialize()
