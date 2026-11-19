import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from app.database import Database, LEGACY_SEEDS
from app.rag_engine import RagEngine, extract_pdf


def pdf_bytes(*pages, encrypted=False):
    with pymupdf.open() as pdf:
        for text in pages:
            pdf.new_page().insert_textbox(pymupdf.Rect(40, 40, 570, 800), text, fontsize=10)
        kwargs = {'encryption': pymupdf.PDF_ENCRYPT_AES_256, 'owner_pw': 'owner', 'user_pw': 'reader'} if encrypted else {}
        return pdf.tobytes(**kwargs)


def settings_for(root, **overrides):
    return SimpleNamespace(**({'data_dir': root, 'db_path': root / 'runtime' / 'orgmind.sqlite3',
                              'max_upload_bytes': 5 * 1024 * 1024, 'chunk_size': 900,
                              'chunk_overlap': 150, 'max_documents': 50} | overrides))


def start(settings):
    db = Database(settings)
    db.initialize()
    rag = RagEngine(db, settings)
    rag.recover()
    rag.rebuild()
    return db, rag


@pytest.fixture
def store(tmp_path):
    settings = settings_for(tmp_path)
    db, rag = start(settings)
    return settings, db, rag


def test_roundtrip_restart_delete_and_original(store):
    settings, db, rag = store
    raw = pdf_bytes('The turbine inspection interval is forty days.', 'Oil pressure should remain above seventy units.')
    row = rag.ingest(raw, '../manual.PDF')
    assert row['filename'] == 'manual.PDF'
    assert row['pages'] == 2 and row['status'] == 'indexed'
    assert db.original_path(row['id']).parent == settings.data_dir / 'uploads'
    assert db.original_path(row['id']).read_bytes() == raw
    assert db.original_path(row['id']).stat().st_mode & 0o777 == 0o600
    assert db.path.stat().st_mode & 0o777 == 0o600
    db2, rag2 = start(settings)
    hits = rag2.retrieve('Oil pressure')
    assert hits[0]['page'] == 2
    assert db2.get_chunk(row['id'], hits[0]['id'])['text'] == hits[0]['text']
    with pytest.raises(KeyError):
        db2.get_chunk('another-document', hits[0]['id'])
    original = db2.original_path(row['id'])
    rag2.delete(row['id'])
    assert not original.exists()
    assert not db2.list_documents() and not rag2.retrieve('Oil pressure')
    with db2.connect() as conn:
        assert conn.execute('SELECT count(*) FROM chunks_fts').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM chunks').fetchone()[0] == 0
    with pytest.raises(KeyError):
        db2.get_document(row['id'])
    with pytest.raises(KeyError):
        db2.original_path(row['id'])
    with pytest.raises(KeyError):
        rag2.delete(row['id'])


@pytest.mark.parametrize('raw,filename,message', [
    (b'plain text', 'notes.txt', 'Only PDF'),
    (b'plain text', 'notes.pdf', 'signature'),
    (b'%PDF-broken', 'notes.pdf', 'parse PDF'),
    (pdf_bytes(''), 'blank.pdf', 'no extractable text'),
    (pdf_bytes('Private manual', encrypted=True), 'locked.pdf', 'Encrypted'),
    (pdf_bytes('hello'), 'bad\nname.pdf', 'filename'),
])
def test_invalid_pdfs_never_registered(store, raw, filename, message):
    _, db, rag = store
    with pytest.raises(ValueError, match=message):
        rag.ingest(raw, filename)
    assert db.list_documents() == []


def test_size_and_page_limits(store):
    settings, db, rag = store
    with pytest.raises(ValueError, match='upload limit'):
        rag.ingest(b'%PDF-' + b'x' * settings.max_upload_bytes, 'large.pdf')
    with pytest.raises(ValueError, match='1–100 pages'):
        extract_pdf(pdf_bytes(*['hello'] * 101), settings)
    assert not db.list_documents()


def test_duplicate_content_and_document_limit(store):
    settings, db, rag = store
    settings.max_documents = 1
    raw = pdf_bytes('Exact original document content')
    row = rag.ingest(raw, 'first.pdf')
    assert rag.ingest(raw, 'renamed.pdf')['id'] == row['id']
    with pytest.raises(ValueError, match='Document limit'):
        rag.ingest(pdf_bytes('Different content'), 'second.pdf')
    assert len(db.list_documents()) == 1


def test_chunk_transaction_failure_recovers(store, monkeypatch):
    settings, db, rag = store
    original_insert = rag._insert_chunks

    def fail(conn, doc_id, pages):
        original_insert(conn, doc_id, pages)
        raise sqlite3.OperationalError('simulated indexing failure')

    monkeypatch.setattr(rag, '_insert_chunks', fail)
    with pytest.raises(sqlite3.OperationalError):
        rag.ingest(pdf_bytes('Resilient persistence retains cobalt laboratory protocols.'), 'protocol.pdf')
    assert db.list_documents()[0]['status'] == 'uploading'
    with db.connect() as conn:
        assert conn.execute('SELECT count(*) FROM chunks').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM chunks_fts').fetchone()[0] == 0
    db2, rag2 = start(settings)
    assert db2.list_documents()[0]['status'] == 'indexed'
    assert rag2.retrieve('cobalt laboratory')


def test_staged_original_recovers_after_atomic_rename_failure(store, monkeypatch):
    settings, db, rag = store
    raw = pdf_bytes('Atomic persistence survives sudden restarts.')
    with monkeypatch.context() as patch:
        patch.setattr(os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('interrupted rename')))
        with pytest.raises(OSError):
            rag.ingest(raw, 'atomic.pdf')
    row = db.list_documents()[0]
    assert Path(row['storage_path']).with_suffix('.pdf.part').exists()
    db2, rag2 = start(settings)
    assert db2.original_path(row['id']).read_bytes() == raw
    assert rag2.retrieve('Atomic persistence')
    assert not Path(row['storage_path']).with_suffix('.pdf.part').exists()


def test_incomplete_write_marked_failed_without_removing_unknown_files(store, monkeypatch):
    settings, db, rag = store

    def fail(path, raw):
        path.with_suffix('.pdf.part').write_bytes(raw[:30])
        raise OSError('disk full')

    monkeypatch.setattr(rag, '_write_original', fail)
    with pytest.raises(OSError):
        rag.ingest(pdf_bytes('Unfinished durable upload'), 'incomplete.pdf')
    unknown = settings.data_dir / 'uploads' / 'unrecognized.pdf'
    unknown.write_bytes(b'unknown original')
    db2, rag2 = start(settings)
    assert db2.list_documents()[0]['status'] == 'failed'
    assert not rag2.retrieve('Unfinished')
    assert unknown.read_bytes() == b'unknown original'
    assert list(unknown.parent.glob('*.part'))
    rag2.delete(db2.list_documents()[0]['id'])
    assert not db2.list_documents() and not list(unknown.parent.glob('*.part'))
    assert unknown.read_bytes() == b'unknown original'


def test_delete_recovers_after_unlink_failure(store, monkeypatch):
    settings, db, rag = store
    row = rag.ingest(pdf_bytes('Documents can be durably removed.'), 'delete.pdf')
    original = db.original_path(row['id'])
    unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path == original:
            raise OSError('interrupted unlink')
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail)
        with pytest.raises(OSError):
            rag.delete(row['id'])
    assert db.get_document(row['id'])['status'] == 'deleting'
    assert not rag.retrieve('durably removed')
    db2, rag2 = start(settings)
    assert not db2.list_documents() and not original.exists()
    assert not rag2.retrieve('durably removed')


def test_delete_recovers_after_file_removed_before_database_commit(store):
    settings, db, rag = store
    row = rag.ingest(pdf_bytes('A pending deletion survives restart.'), 'pending.pdf')
    with db.connect() as conn:
        conn.execute("UPDATE documents SET status='deleting' WHERE id=?", (row['id'],))
    db.original_path(row['id']).unlink()
    db2, _ = start(settings)
    assert not db2.list_documents()


def test_safe_paths_and_changed_original_preserved(store):
    settings, db, rag = store
    row = rag.ingest(pdf_bytes('An original worth preserving.'), 'safe.pdf')
    original = db.original_path(row['id'])
    original.write_bytes(b'unknown replacement')
    with pytest.raises(ValueError, match='unrecognized'):
        rag.delete(row['id'])
    assert original.read_bytes() == b'unknown replacement'
    start(settings)
    assert original.exists()
    outside = settings.data_dir / 'private.txt'
    outside.write_text('unrelated bytes')
    with db.connect() as conn:
        conn.execute('UPDATE documents SET storage_path=? WHERE id=?', (str(outside), row['id']))
    with pytest.raises(KeyError):
        db.original_path(row['id'])
    original.unlink()
    original.symlink_to(outside)
    with db.connect() as conn:
        conn.execute('UPDATE documents SET storage_path=? WHERE id=?', (str(original), row['id']))
    with pytest.raises(KeyError):
        db.original_path(row['id'])
    rag.delete(row['id'])
    assert outside.read_text() == 'unrelated bytes'


def legacy_database(settings, product):
    settings.db_path.parent.mkdir(parents=True)
    upload_root = settings.data_dir / 'runtime' / 'uploads'
    upload_root.mkdir()
    corpus = settings.data_dir / 'corpus'
    corpus.mkdir()
    originals = {}
    with sqlite3.connect(settings.db_path) as conn:
        conn.executescript('''
            CREATE TABLE users(id TEXT PRIMARY KEY, role TEXT);
            CREATE TABLE documents(id TEXT PRIMARY KEY,title TEXT,filename TEXT,pages INTEGER,metadata TEXT,digest TEXT);
            CREATE TABLE document_scopes(document_id TEXT REFERENCES documents(id),scope TEXT);
            CREATE TABLE chunks(id TEXT PRIMARY KEY,document_id TEXT REFERENCES documents(id),page INTEGER,text TEXT);
            CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED,text);
            CREATE TRIGGER chunks_delete AFTER DELETE ON chunks BEGIN DELETE FROM chunks_fts WHERE chunk_id=old.id; END;
            CREATE TABLE meta(key TEXT PRIMARY KEY,value INTEGER);
            CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,action TEXT);
            CREATE TABLE analytics(id INTEGER PRIMARY KEY,value REAL);
        ''')
        if product:
            conn.executescript('''
                CREATE TABLE departments(id TEXT PRIMARY KEY);
                ALTER TABLE documents ADD COLUMN owner_id TEXT;
                ALTER TABLE documents ADD COLUMN department_id TEXT;
                ALTER TABLE documents ADD COLUMN visibility TEXT DEFAULT 'private';
                ALTER TABLE documents ADD COLUMN status TEXT DEFAULT 'indexed';
                ALTER TABLE documents ADD COLUMN uploaded_at TEXT;
                ALTER TABLE documents ADD COLUMN storage_path TEXT;
            ''')
        for doc_id, filename, seed in [('WS_upload', 'retained.pdf', False),
                                       ('DOC_01', LEGACY_SEEDS['DOC_01'], False),
                                       ('DOC_09', LEGACY_SEEDS['DOC_09'], True),
                                       ('DOC_09_custom', 'custom.pdf', False)]:
            raw = pdf_bytes(f'{doc_id} original contains durable orbital calibration instructions.')
            path = corpus / filename if seed else upload_root / (doc_id + '.pdf')
            path.write_bytes(raw)
            originals[doc_id] = path
            digest = hashlib.sha256(raw).hexdigest()
            conn.execute('INSERT INTO documents(id,title,filename,pages,metadata,digest) VALUES(?,?,?,?,?,?)',
                         (doc_id, 'Retained title', filename, 1, json.dumps({'source': 'synthetic-acme'} if seed else {}), digest))
            conn.execute('INSERT INTO document_scopes VALUES(?,?)', (doc_id, 'engineering'))
            conn.execute('INSERT INTO chunks VALUES(?,?,?,?)', (doc_id + '_chunk', doc_id, 1, 'durable orbital calibration'))
            conn.execute('INSERT INTO chunks_fts VALUES(?,?)', (doc_id + '_chunk', 'stale discarded index text'))
            if product and not seed:
                conn.execute('UPDATE documents SET storage_path=?,uploaded_at=? WHERE id=?',
                             (str(path), '2025-01-01T00:00:00+00:00', doc_id))
    return originals


@pytest.mark.parametrize('product', [False, True])
def test_legacy_migration_preserves_nonseed_uploads_backup_and_idempotence(tmp_path, product):
    settings = settings_for(tmp_path)
    originals = legacy_database(settings, product)
    db, rag = start(settings)
    assert {r['id'] for r in db.list_documents()} == {'WS_upload', 'DOC_01', 'DOC_09_custom'}
    assert all(path.exists() for path in originals.values())
    assert db.original_path('WS_upload') == originals['WS_upload']
    assert rag.retrieve('orbital calibration')
    backups = list(settings.db_path.parent.glob('*.legacy-*.bak'))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 4
        assert conn.execute('SELECT count(*) FROM chunks').fetchone()[0] == 4
    with db.connect() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not tables & {'users', 'departments', 'document_scopes', 'audit_logs', 'analytics'}
        assert conn.execute('SELECT count(*) FROM chunks').fetchone()[0] == 3
        assert conn.execute('SELECT count(*) FROM chunks_fts').fetchone()[0] == 3
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    start(settings)
    assert len(list(settings.db_path.parent.glob('*.legacy-*.bak'))) == 1
    rag.delete('WS_upload')
    assert not originals['WS_upload'].exists()
    assert originals['DOC_01'].exists() and originals['DOC_09'].exists()


def test_seed_digest_mismatch_is_preserved(tmp_path):
    settings = settings_for(tmp_path)
    originals = legacy_database(settings, False)
    originals['DOC_09'].write_bytes(pdf_bytes('Different original, not the known seed digest'))
    db, _ = start(settings)
    assert db.get_document('DOC_09')
    assert originals['DOC_09'].exists()


def test_migration_failure_rolls_back_and_keeps_backup(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    originals = legacy_database(settings, True)
    db = Database(settings)
    create = db._create_schema

    def fail(conn):
        create(conn)
        raise sqlite3.OperationalError('simulated migration failure')

    monkeypatch.setattr(db, '_create_schema', fail)
    with pytest.raises(sqlite3.OperationalError):
        db.initialize()
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute('SELECT count(*) FROM documents').fetchone()[0] == 4
        assert 'metadata' in {r[1] for r in conn.execute('PRAGMA table_info(documents)')}
    assert all(path.exists() for path in originals.values())
    assert len(list(settings.db_path.parent.glob('*.legacy-*.bak'))) == 1
    assert len(start(settings)[0].list_documents()) == 3


def test_unknown_schema_is_not_destroyed(tmp_path):
    settings = settings_for(tmp_path)
    settings.db_path.parent.mkdir(parents=True)
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute('CREATE TABLE unrelated(value TEXT)')
        conn.execute("INSERT INTO unrelated VALUES('preserve')")
    with pytest.raises(ValueError, match='Unrecognized database'):
        Database(settings).initialize()
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute('SELECT value FROM unrelated').fetchone()[0] == 'preserve'
