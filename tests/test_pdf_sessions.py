import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import database
from app.database import Database, SCHEMA, SESSION_SCHEMA
from tests.test_pdf_storage import legacy_database, pdf_bytes, settings_for, start


def session(db, ttl=30 * 86400):
    token = db.create_session(ttl)
    return token, db.resolve_session(token, ttl)


def test_session_is_random_hashed_persistent_and_sliding(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    db, _ = start(settings)
    monkeypatch.setattr(database.time, 'time', lambda: 1000)
    token, owner = session(db, 100)
    assert len(token) == 43 and owner == hashlib.sha256(token.encode()).hexdigest()
    assert token != session(db, 100)[0]
    with db.connect() as conn:
        row = dict(conn.execute('SELECT * FROM sessions WHERE owner_id=?', (owner,)).fetchone())
    assert token not in str(row) and row['expires_at'] == 1100
    db2, _ = start(settings)
    monkeypatch.setattr(database.time, 'time', lambda: 1090)
    assert db2.resolve_session(token, 100) == owner
    monkeypatch.setattr(database.time, 'time', lambda: 1101)
    assert db.resolve_session(token, 100) == owner
    for invalid in (None, '', 'guess', 'x' * 43, token + 'x', '!' * 43):
        assert db.resolve_session(invalid, 100) is None
    for invalid in (0, -1, float('nan'), float('inf'), '30'):
        with pytest.raises(ValueError):
            db.create_session(invalid)


def test_owner_isolation_dedup_limits_and_originals(tmp_path):
    db, rag = start(settings_for(tmp_path, max_documents=1))
    _, alice = session(db)
    _, bob = session(db)
    raw = pdf_bytes('Zirconium emergency valve instructions.')
    a = rag.ingest(raw, 'alice.pdf', owner_id=alice)
    b = rag.ingest(raw, 'bob.pdf', owner_id=bob)
    assert a['id'] != b['id']
    assert rag.ingest(raw, 'renamed.pdf', owner_id=alice)['id'] == a['id']
    assert [d['id'] for d in db.list_documents(owner_id=alice)] == [a['id']]
    hit = rag.retrieve('Zirconium', owner_id=alice)[0]
    assert db.get_chunk(a['id'], hit['id'], owner_id=alice)['text'] == hit['text']
    assert db.original_path(a['id'], owner_id=alice).read_bytes() == raw
    for action in (lambda: db.get_document(a['id'], owner_id=bob),
                   lambda: db.original_path(a['id'], owner_id=bob),
                   lambda: db.get_chunk(a['id'], hit['id'], owner_id=bob),
                   lambda: rag.delete(a['id'], owner_id=bob)):
        with pytest.raises(KeyError):
            action()
    assert rag.retrieve('Zirconium', [a['id']], owner_id=bob) == []
    with pytest.raises(ValueError, match='Document limit'):
        rag.ingest(pdf_bytes('New document'), 'new.pdf', owner_id=alice)
    rag.delete(a['id'], owner_id=alice)
    assert db.original_path(b['id'], owner_id=bob).read_bytes() == raw
    assert len(db.list_documents()) == 1


def test_expiry_preserves_all_owned_rows_and_bytes(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    db, rag = start(settings)
    monkeypatch.setattr(database.time, 'time', lambda: 1000)
    token, owner = session(db, 100)
    _, empty = session(db, 100)
    raw = pdf_bytes('Preserved cobalt reference.')
    doc = rag.ingest(raw, 'retained.pdf', owner_id=owner)
    hit = rag.retrieve('cobalt', owner_id=owner)[0]
    original = db.original_path(doc['id'], owner_id=owner)
    monkeypatch.setattr(database.time, 'time', lambda: 1100)
    assert db.resolve_session(token, 100) is None
    assert db.list_documents(owner_id=owner) == []
    assert rag.retrieve('cobalt', owner_id=owner) == []
    assert rag.retrieve('Give me a summary of this document', owner_id=owner) == []
    with pytest.raises(KeyError):
        rag.delete(doc['id'], owner_id=owner)
    with pytest.raises(KeyError):
        db.original_path(doc['id'], owner_id=owner)
    with pytest.raises(KeyError):
        db.get_chunk(doc['id'], hit['id'], owner_id=owner)
    with pytest.raises(ValueError, match='expired'):
        rag.ingest(raw, 'retained.pdf', owner_id=owner)
    assert db.cleanup_sessions() == 1
    with db.connect() as conn:
        assert {r[0] for r in conn.execute('SELECT owner_id FROM sessions')} == {owner}
        assert conn.execute('SELECT count(*) FROM chunks').fetchone()[0] > 0
    assert original.read_bytes() == raw
    db2, rag2 = start(settings)
    assert db2.get_document(doc['id'])['owner_id'] == owner
    assert rag2.retrieve('cobalt')
    _, visitor = session(db2)
    assert visitor not in {owner, empty} and db2.list_documents(owner_id=visitor) == []


def test_session_bound_and_concurrent_creation(tmp_path, monkeypatch):
    db, _ = start(settings_for(tmp_path))
    monkeypatch.setattr(database, 'MAX_SESSIONS', 4)
    with ThreadPoolExecutor(max_workers=4) as pool:
        tokens = list(pool.map(lambda _: db.create_session(100), range(4)))
    assert len(set(tokens)) == 4
    with pytest.raises(ValueError, match='Workspace limit'):
        db.create_session(100)
    with db.connect() as conn:
        conn.execute('UPDATE sessions SET expires_at=0')
    assert db.create_session(100) not in tokens
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0] == 1


def test_global_document_limit_counts_quarantined_and_other_owners(tmp_path):
    db, rag = start(settings_for(tmp_path, max_total_documents=2))
    _, alice = session(db)
    _, bob = session(db)
    raw = pdf_bytes('One shared legacy reference.')
    rag.ingest(raw, 'quarantined.pdf')
    a = rag.ingest(raw, 'a.pdf', owner_id=alice)
    assert rag.ingest(raw, 'same.pdf', owner_id=alice)['id'] == a['id']
    with pytest.raises(ValueError, match='Global document limit'):
        rag.ingest(raw, 'b.pdf', owner_id=bob)
    assert len(db.list_documents()) == 2


def schema3_database(settings):
    settings.db_path.parent.mkdir(parents=True)
    root = settings.data_dir / 'uploads'
    root.mkdir()
    raw = pdf_bytes('Preserve old shared platinum specifications.')
    original = root / 'previous.pdf'
    original.write_bytes(raw)
    with sqlite3.connect(settings.db_path) as conn:
        for statement in SCHEMA:
            if statement not in SESSION_SCHEMA:
                conn.execute(statement.replace(', owner_id TEXT)', ')').replace('user_version=4', 'user_version=3'))
        conn.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)',
                     ('old', 'previous.pdf', 1, 'indexed', '2025-01-01', str(original), hashlib.sha256(raw).hexdigest()))
        conn.execute('INSERT INTO chunks VALUES(?,?,?,?)', ('old-chunk', 'old', 1, 'shared platinum specifications'))
    return original, raw


def test_schema3_backed_up_transactional_quarantine_and_idempotence(tmp_path):
    settings = settings_for(tmp_path)
    original, raw = schema3_database(settings)
    db, rag = start(settings)
    assert db.get_document('old')['owner_id'] is None
    assert original.read_bytes() == raw
    token, owner = session(db)
    assert db.list_documents(owner_id=owner) == []
    assert rag.retrieve('platinum', owner_id=owner) == []
    with pytest.raises(KeyError):
        db.original_path('old', owner_id=owner)
    backups = list(settings.db_path.parent.glob('*.legacy-*.bak'))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 3
        assert conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM chunks').fetchone()[0] == 1
    db2, _ = start(settings)
    assert db2.resolve_session(token, 30 * 86400) == owner
    assert len(list(settings.db_path.parent.glob('*.legacy-*.bak'))) == 1


@pytest.mark.parametrize('legacy', [False, True])
def test_migration_cancels_unowned_pending_deletes_without_removing_originals(tmp_path, legacy):
    settings = settings_for(tmp_path)
    if legacy:
        originals = legacy_database(settings, True)
        doc_id, original = 'WS_upload', originals['WS_upload']
        raw = original.read_bytes()
    else:
        original, raw = schema3_database(settings)
        doc_id = 'old'
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("UPDATE documents SET status='deleting' WHERE id=?", (doc_id,))
    db, _ = start(settings)
    assert db.get_document(doc_id)['owner_id'] is None
    assert original.read_bytes() == raw
    start(settings)
    assert original.read_bytes() == raw


def test_schema3_failed_migration_rolls_back(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    original, raw = schema3_database(settings)
    db = Database(settings)
    migrate = db._migrate_shared

    def fail(conn):
        migrate(conn)
        raise sqlite3.OperationalError('interrupted migration')

    monkeypatch.setattr(db, '_migrate_shared', fail)
    with pytest.raises(sqlite3.OperationalError):
        db.initialize()
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 3
        assert 'owner_id' not in {r[1] for r in conn.execute('PRAGMA table_info(documents)')}
        assert conn.execute('SELECT count(*) FROM chunks').fetchone()[0] == 1
    assert original.read_bytes() == raw
    assert list(settings.db_path.parent.glob('*.legacy-*.bak'))


@pytest.mark.parametrize('product', [False, True])
def test_legacy_retained_documents_never_assigned_to_visitors(tmp_path, product):
    settings = settings_for(tmp_path)
    originals = legacy_database(settings, product)
    db, rag = start(settings)
    _, owner = session(db)
    assert len(db.list_documents()) == 3
    assert all(row['owner_id'] is None for row in db.list_documents())
    assert db.list_documents(owner_id=owner) == []
    assert rag.retrieve('orbital calibration', owner_id=owner) == []
    assert all(path.exists() for path in originals.values())
