import fitz
import pytest

from app.config import Settings
from app.database import Database
from app.pdf_corpus import generate_corpus
from app.rag_engine import RagEngine, extract_pdf
from app.rbac_guard import authorized_chunk_ids, validate_citations


@pytest.fixture(scope='module')
def indexed(tmp_path_factory):
    settings = Settings(data_dir=tmp_path_factory.mktemp('retrieval'), jwt_secret='test-only-' * 5)
    db = Database(settings)
    db.initialize()
    specs = generate_corpus(settings.data_dir / 'corpus')
    db.seed_documents(specs)
    rag = RagEngine(db, settings)
    rag.index_seeded(specs)
    return db, rag, specs


@pytest.mark.parametrize('mode', ['bm25', 'lsa', 'hybrid'])
@pytest.mark.parametrize('uid', ['admin_user', 'eng_user', 'finance_user', 'hr_user', 'general_user', 'executive_user'])
def test_all_rankings_authorized(indexed, uid, mode):
    db, rag, _ = indexed
    user = db.user(uid)
    with db.connect() as conn: allowed = authorized_chunk_ids(conn, user)
    for query in ['executive bonus pool', 'operating expenditure reserve liquidity', 'infrastructure uptime cloud availability', 'remuneration salary compensation', 'office catering vendor']:
        hits = rag.retrieve(query, user, mode)
        assert all(h['id'] in allowed for h in hits)
        validate_citations([{'chunk_id': h['id']} for h in hits], allowed)


@pytest.mark.parametrize('uid,query,expected', [
    ('general_user', 'car travel reimbursement', 'DOC_01'),
    ('hr_user', 'staff vacation entitlement', 'DOC_02'),
    ('finance_user', 'quarterly company spending', 'DOC_03'),
    ('finance_user', 'executive bonus pool', 'DOC_04'),
    ('eng_user', 'cloud server availability', 'DOC_05'),
    ('eng_user', 'disaster recovery time objective', 'DOC_06'),
    ('hr_user', 'vendor procurement CFO sign-off', 'DOC_07'),
    ('general_user', 'VPN password hygiene', 'DOC_08'),
])
def test_curated_retrieval(indexed, uid, query, expected):
    db, rag, _ = indexed
    assert expected in {h['document_id'] for h in rag.retrieve(query, db.user(uid))}


def test_repeat_index_stable(indexed):
    db, rag, specs = indexed
    with db.connect() as conn: before = [tuple(r) for r in conn.execute('SELECT id,text FROM chunks ORDER BY id')]
    rag.index_seeded(specs)
    with db.connect() as conn:
        assert before == [tuple(r) for r in conn.execute('SELECT id,text FROM chunks ORDER BY id')]
        assert conn.execute('SELECT count(*) FROM chunks_fts').fetchone()[0] == len(before)


def test_tiny_corpus_fallback(tmp_path):
    settings = Settings(data_dir=tmp_path, jwt_secret='test-only-' * 5)
    db = Database(settings); db.initialize()
    rag = RagEngine(db, settings)
    with fitz.open() as pdf:
        pdf.new_page().insert_text((72,72), 'Travel mileage reimbursement policy.')
        rag.ingest(pdf.tobytes(), 'Tiny', 'tiny.pdf', ['general'])
    assert rag.svd is None
    assert rag.retrieve('travel reimbursement', db.user('general_user'), 'lsa')


def test_malformed_and_oversized(indexed):
    _, rag, _ = indexed
    with pytest.raises(ValueError): extract_pdf(b'not a pdf', rag.settings)
    with pytest.raises(ValueError): extract_pdf(b'%PDF-' + b'x' * rag.settings.max_upload_bytes, rag.settings)


def test_invalid_citation_rejected():
    with pytest.raises(ValueError): validate_citations([{'chunk_id': 'restricted'}], {'allowed'})
