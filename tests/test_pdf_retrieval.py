from pathlib import Path

import pytest

from app.rag_engine import RagEngine, query_terms
from tests.test_pdf_storage import pdf_bytes, settings_for, start


@pytest.fixture
def corpus(tmp_path):
    settings = settings_for(tmp_path)
    db, rag = start(settings)
    manual = rag.ingest(pdf_bytes(
        'The turbine inspection interval is forty days. Lubrication avoids bearing friction and damage.',
        'Emergency shutdown requires closing the intake valve. The shutdown checklist is stored at the control desk.',
        'Replacement filters use part number ZX450. Filter replacement takes twenty minutes.'), 'turbine.pdf')
    garden = rag.ingest(pdf_bytes(
        'Orchid irrigation uses filtered water every six days. Orchids prefer diffuse sunlight.',
        'Compost temperature should stay warm. Mulch preserves moisture in garden soil.'), 'garden.pdf')
    return settings, db, rag, manual, garden


def test_hybrid_ranking_preserves_pages_and_filenames(corpus):
    _, _, rag, manual, _ = corpus
    hits = rag.retrieve('What is the emergency shutdown checklist?')
    assert hits[0]['document_id'] == manual['id']
    assert hits[0]['filename'] == 'turbine.pdf' and hits[0]['page'] == 2
    assert hits[0]['score'] > 0
    assert {'id', 'document_id', 'filename', 'page', 'text', 'score'} <= hits[0].keys()
    assert rag.svd is not None
    assert rag.retrieve('ZX450')[0]['page'] == 3


def test_selection_empty_selection_and_unrelated_questions(corpus):
    _, _, rag, manual, garden = corpus
    assert rag.retrieve('Orchid irrigation', [garden['id']])[0]['document_id'] == garden['id']
    assert rag.retrieve('Orchid irrigation', [manual['id']]) == []
    assert rag.retrieve('Emergency shutdown', []) == []
    assert rag.retrieve('Emergency shutdown', ['unknown']) == []
    assert rag.retrieve('Quantum entanglement superconductivity') == []
    assert rag.retrieve('What does the PDF document tell me please?') == []
    assert rag.retrieve('Emergency shutdown', limit=0) == []


def test_query_terms_are_safe_deduplicated_and_not_enterprise_filtered():
    assert query_terms('Please tell me about Q4 2026 company finances FINANCES') == ['q4', '2026', 'company', 'finances']
    assert query_terms('" OR * NEAR() --') == ['near']
    assert query_terms('révision année 2026') == ['révision', 'année', '2026']


def test_chunk_overlap_and_blank_page_numbers(tmp_path):
    settings = settings_for(tmp_path, chunk_size=120, chunk_overlap=30)
    db, rag = start(settings)
    text = ' '.join(f'token{index:03d}' for index in range(70))
    chunks = list(rag._chunks('sample', [text, '', 'last page contains zirconium specifications']))
    first_page = [chunk for chunk in chunks if chunk[2] == 1]
    assert len(first_page) > 2
    assert all(len(chunk[3]) <= 120 for chunk in first_page)
    for left, right in zip(first_page, first_page[1:]):
        assert any(left[3].endswith(right[3][:overlap]) for overlap in range(20, 31))
    assert chunks[-1][2] == 3
    row = rag.ingest(pdf_bytes('first page', '', 'zirconium specifications'), 'blank-middle.pdf')
    assert row['pages'] == 3
    assert rag.retrieve('zirconium')[0]['page'] == 3
    assert db.get_document(row['id'])['pages'] == 3


def test_top_five_deduplicates_identical_text(tmp_path):
    settings = settings_for(tmp_path)
    _, rag = start(settings)
    texts = [f'Calibration procedure {number} requires checking pressure sensor {number}.' for number in range(8)]
    rag.ingest(pdf_bytes(*texts, texts[0]), 'calibration.pdf')
    hits = rag.retrieve('Calibration pressure sensor', limit=100)
    assert len(hits) == 5
    assert len({hit['id'] for hit in hits}) == len({hit['text'] for hit in hits}) == 5
    assert len(rag.retrieve('Calibration pressure sensor', limit=2)) == 2


def test_late_page_terms_not_lost_by_query_token_cap(tmp_path):
    settings = settings_for(tmp_path)
    _, rag = start(settings)
    text = ' '.join(f'word{index}' for index in range(55)) + ' platinum recalibration'
    rag.ingest(pdf_bytes(text), 'late.pdf')
    assert rag.retrieve('platinum recalibration')


def test_restart_and_another_engine_refresh_index(corpus):
    settings, db, rag, manual, _ = corpus
    _, other = start(settings)
    row = rag.ingest(pdf_bytes('Saffron harvesting occurs during autumn flowering.'), 'saffron.pdf')
    assert other.retrieve('Saffron harvesting')[0]['document_id'] == row['id']
    rag.delete(row['id'])
    assert other.retrieve('Saffron harvesting') == []
    chunk = rag.retrieve('Emergency shutdown', [manual['id']])[0]
    with db.connect() as conn:
        conn.execute('UPDATE chunks SET text=? WHERE id=?', ('New platinum metallurgy reference', chunk['id']))
    assert other.retrieve('platinum metallurgy')[0]['id'] == chunk['id']


def test_empty_vocabulary_and_empty_corpus(tmp_path):
    settings = settings_for(tmp_path)
    _, rag = start(settings)
    assert rag.retrieve('anything') == []
    rag.ingest(pdf_bytes('the and or is'), 'stopwords.pdf')
    assert rag.vectorizer is None
    assert rag.retrieve('zirconium') == []


def test_invalid_overlap_rejected(tmp_path):
    settings = settings_for(tmp_path, chunk_size=100, chunk_overlap=100)
    with pytest.raises(ValueError, match='overlap'):
        RagEngine(None, settings)


@pytest.fixture
def finance(tmp_path):
    settings = settings_for(tmp_path)
    db, rag = start(settings)
    token = db.create_session(86400)
    owner = db.resolve_session(token, 86400)
    raw = (Path(__file__).parents[1] / 'samples' / 'personal-finance-guide.pdf').read_bytes()
    doc = rag.ingest(raw, 'personal-finance-guide.pdf', owner_id=owner)
    return db, rag, owner, doc


def test_actual_sample_numeric_budget_and_page_extraction(finance):
    db, rag, owner, doc = finance
    assert doc['pages'] == 4
    hits = rag.retrieve('How is the $3,000 monthly income divided using the 50/30/20 budget?',
                        [doc['id']], owner_id=owner)
    assert hits[0]['page'] == 1
    assert all(amount in hits[0]['text'] for amount in ('$3,000', '50/30/20', '$1,500', '$900', '$600'))
    assert all(hit['document_id'] == doc['id'] and hit['filename'] == doc['filename'] for hit in hits)
    assert all(db.get_chunk(doc['id'], hit['id'], owner_id=owner)['text'] == hit['text'] for hit in hits)
    assert rag.svd is not None and rag.sparse_matrix is not None
    assert len(rag.rows) == 8
    assert query_terms('$3,000 monthly 50/30/20 budget 8 months') == ['3000', 'monthly', '50', '30', '20', 'budget', '8', 'months']


@pytest.mark.parametrize('question,expected', [
    ('What emergency fund targets does the guide recommend?', ('$4,500', '$9,000')),
    ('How long does it take to build the smaller emergency fund by saving $600 per month?', ('month 8', '$4,800')),
    ('What if the monthly saving increases to $800?', ('$4,500', '$600')),
    ('What if the monthly saving increases to $800? Previous question: How long does it take to build the smaller emergency fund by saving $600 per month?', ('$4,500', '$600')),
])
def test_actual_sample_emergency_and_followup_evidence(finance, question, expected):
    _, rag, owner, doc = finance
    hits = rag.retrieve(question, [doc['id']], owner_id=owner)
    evidence = ' '.join(hit['text'] for hit in hits if hit['page'] == 2)
    assert all(value in evidence for value in expected)


def test_actual_sample_budgeting_and_unknown_subsidy(finance):
    _, rag, owner, doc = finance
    hits = rag.retrieve('Which document pages contain the information about budgeting?', [doc['id']], owner_id=owner)
    assert any(hit['page'] == 1 for hit in hits)
    assert rag.retrieve('What government subsidy is available for solar installations?', [doc['id']], owner_id=owner) == []


def test_summary_spans_sample_pages_without_overlapping_chunks(finance):
    _, rag, owner, doc = finance
    for query in ('Give me a summary of this document.', 'Summarize the selected PDFs', 'A brief overview please'):
        hits = rag.retrieve(query, [doc['id']], owner_id=owner)
        assert {hit['page'] for hit in hits} == {1, 2, 3, 4}
        assert len(hits) == 4
    assert rag.retrieve('Summary', [], owner_id=owner) == []


def test_summary_round_robin_and_spread_across_selected_documents(tmp_path):
    _, rag = start(settings_for(tmp_path))
    first = rag.ingest(pdf_bytes(*[f'Chapter {i} examines process technology {i}.' for i in range(12)]), 'first.pdf')
    second = rag.ingest(pdf_bytes(*[f'Section {i} details gardening practice {i}.' for i in range(8)]), 'second.pdf')
    hits = rag.retrieve('Summary', limit=100)
    assert len(hits) == 5
    assert {hit['document_id'] for hit in hits} == {first['id'], second['id']}
    assert len({(hit['document_id'], hit['page']) for hit in hits}) == 5
    selected = rag.retrieve('Summary', [first['id']])
    assert {hit['page'] for hit in selected} == {1, 3, 6, 9, 12}
    assert all(hit['document_id'] == first['id'] for hit in selected)


def test_owner_and_selection_corpora_have_no_ranking_or_vocabulary_contamination(tmp_path):
    db, rag = start(settings_for(tmp_path))
    alice = db.resolve_session(db.create_session(86400), 86400)
    bob = db.resolve_session(db.create_session(86400), 86400)
    first = rag.ingest(pdf_bytes('Cobalt reactor inspection is performed monthly.',
                                 'Inspect the cobalt reactor pressure gauge weekly.',
                                 'Cobalt replacement requires a calibrated spanner.'), 'alice.pdf', owner_id=alice)
    baseline = rag.retrieve('Cobalt reactor inspection', owner_id=alice)
    vocabulary = dict(rag.vectorizer.vocabulary_)
    other = rag.ingest(pdf_bytes(*['Classifiedquasar cobalt reactor inspection secret calibration.'] * 8),
                       'bob.pdf', owner_id=bob)
    assert rag.retrieve('Cobalt reactor inspection', owner_id=alice) == baseline
    assert rag.vectorizer.vocabulary_ == vocabulary
    assert 'classifiedquasar' not in rag.vectorizer.vocabulary_
    assert all(row['document_id'] == first['id'] for row in rag.rows)
    assert rag.retrieve('Classifiedquasar', [first['id']], owner_id=bob) == []
    assert rag.retrieve('Classifiedquasar', owner_id=bob)[0]['document_id'] == other['id']
    extra = rag.ingest(pdf_bytes('Unrelatednebula cobalt reactor inspection.'), 'extra.pdf', owner_id=alice)
    assert rag.retrieve('Cobalt reactor inspection', [first['id']], owner_id=alice) == baseline
    assert 'unrelatednebula' not in rag.vectorizer.vocabulary_
    assert rag.retrieve('Unrelatednebula', [extra['id']], owner_id=alice)


def test_rrf_combines_local_bm25_and_vector_ranks(tmp_path):
    _, rag = start(settings_for(tmp_path))
    rag.ingest(pdf_bytes('Zirconium calibration specifications.'), 'one.pdf')
    hit = rag.retrieve('Zirconium calibration')[0]
    assert hit['score'] == pytest.approx(2 / 61)
    assert rag._bm25(rag.rows, ['zirconium']) == [hit['id']]
