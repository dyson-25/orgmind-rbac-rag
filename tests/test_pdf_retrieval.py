from pathlib import Path
import re

import pytest

from app.rag_engine import RagEngine, classify_query_intent, normalize_query, query_terms
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


@pytest.mark.parametrize('question,intent', [
    ('what this pdf has', 'overview'), ('whats inside this pdf', 'overview'),
    ('tell me about this document', 'overview'), ('explain this pdf simple', 'overview'),
    ('main things in this doc', 'overview'), ('what can i learn from this file', 'overview'),
    ('dates and numbers in pdf', 'date_number_extraction'),
    ('list all submision dates', 'date_extraction'),
    ('what are the main differences between these two documents', 'comparison'),
    ('Explain the selected documents separately.', 'overview'),
])
def test_imperfect_english_routes_deterministically(question, intent):
    assert classify_query_intent(question) == intent


def test_fuzzy_query_normalization_recovers_document_vocabulary(corpus):
    _, _, rag, manual, _ = corpus
    assert rag.retrieve('what is the use of turbin', [manual['id']])[0]['page'] == 1
    assert classify_query_intent("what's inside this pdf") == 'overview'
    assert classify_query_intent('Explain the selected documents separately.', 2) == 'multi_document_overview'
    normalized = normalize_query('CCW332 Assignment 2026 ₹2,500 Inveter', {'assignment', 'inverter'})
    assert 'CCW332' in normalized and '2026' in normalized and '₹' in normalized and '2,500' in normalized
    assert normalized.endswith('inverter')


def test_suggestions_are_local_document_scoped_and_content_conditional(corpus):
    _, _, rag, manual, garden = corpus
    manual_questions = [item['question'] for item in rag.suggest_questions([manual['id']])]
    garden_questions = [item['question'] for item in rag.suggest_questions([garden['id']])]
    assert manual_questions and manual_questions[0] == 'What is this document about?'
    assert any('numbers' in question for question in manual_questions)
    assert not any('dates' in question for question in manual_questions)
    assert any('orchid' in question.casefold() or 'garden' in question.casefold() for question in garden_questions)
    both = rag.suggest_questions([manual['id'], garden['id']])
    assert both[0]['question'] == 'Explain the selected documents separately.'
    assert {doc_id for item in both for doc_id in item['document_ids']} == {manual['id'], garden['id']}


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


def test_boundary_aware_chunks_keep_paragraphs_sentences_unicode_and_numbers(tmp_path):
    settings = settings_for(tmp_path, chunk_size=150, chunk_overlap=24)
    _, rag = start(settings)
    paragraphs = [
        'Solar panels produce electricity from sunlight. An inverter converts the direct current into usable alternating current.',
        'The household pays $1,250.50 per month for electricity. A long paragraph continues with useful technical detail about the system and its measured seasonal output. ' * 2,
        'Résumé notes: café equipment uses 230 V.编号 ４ includes Unicode and full-width numerals.',
    ]
    text = '\n\n'.join(paragraphs)
    chunks = list(rag._chunks('solar', [text]))
    assert chunks and all(row[1] == 'solar' and row[2] == 1 and row[3].strip() for row in chunks)
    assert all(len(row[3]) <= settings.chunk_size + 40 for row in chunks)
    assert all(not re.search(r'\b[a-zA-Z]{1,2}$', row[3]) for row in chunks[:-1])
    assert any('Solar panels produce electricity from sunlight.' in row[3] for row in chunks)
    assert any('$1,250.50' in row[3] for row in chunks)
    assert any('café' in row[3] for row in chunks)
    for left, right in zip(chunks, chunks[1:]):
        overlap = max((size for size in range(1, min(len(left[3]), len(right[3])) + 1)
                       if left[3][-size:] == right[3][:size]), default=0)
        assert overlap >= 12


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


def test_natural_document_discovery_queries_use_representative_evidence(finance):
    db, rag, owner, doc = finance
    queries = (
        'What is this PDF about?', 'What does this document contain?', 'Explain this file.',
        'What are the important topics?', 'What can I learn from this PDF?',
    )
    for query in queries:
        hits = rag.retrieve(query, [doc['id']], owner_id=owner)
        assert hits, query
        assert all(hit['document_id'] == doc['id'] and hit['page'] > 0 for hit in hits)
    assert rag.retrieve('What government subsidy is available for solar installations?',
                        [doc['id']], owner_id=owner) == []


def test_one_page_overview_can_use_multiple_distinct_passages(tmp_path):
    settings = settings_for(tmp_path, chunk_size=280, chunk_overlap=45)
    db, rag = start(settings)
    owner = db.resolve_session(db.create_session(86400), 86400)
    sections = (
        'Photovoltaic cells convert incoming sunlight into direct-current electrical energy for homes.',
        'An inverter changes direct current into alternating current used by household appliances.',
        'Battery storage keeps extra daytime electricity available after sunset or during outages.',
        'Roof orientation and nearby shade affect the amount of electricity a solar array can produce.',
        'A monitoring display tracks daily generation and helps identify unexpected system changes.',
        'Utility bill credits depend on the local agreement for sending unused electricity to the grid.',
        'Installers inspect roof structure, wiring, grounding, and disconnect switches for safe operation.',
        'Periodic visual checks and prompt repair help maintain reliable equipment over the years.',
    )
    text = '\n\n'.join(sections)
    doc = rag.ingest(pdf_bytes(text), 'overview.pdf', owner_id=owner)
    hits = rag.retrieve('What is this PDF about?', [doc['id']], owner_id=owner)
    assert len(hits) >= 2
    assert len({hit['text'] for hit in hits}) == len(hits)
    assert {hit['page'] for hit in hits} == {1}


def test_deterministic_query_intent_variants():
    cases = {
        'What is this PDF about?': 'overview',
        'Explain this document to me.': 'overview',
        'What can I learn from this file?': 'overview',
        'What are the main points?': 'key_points',
        'What are the five most important points?': 'key_points',
        'Find the important dates.': 'date_extraction',
        'Extract all values and deadlines.': 'date_number_extraction',
        'What important information is mentioned?': 'overview',
        'List all marks and amounts.': 'numeric_extraction',
        'List names, dates and numbers.': 'date_number_extraction',
        'Compare the two documents.': 'comparison',
        'What does each document contain?': 'multi_document_overview',
        'Explain this document to a beginner.': 'multi_document_overview',
        'What does it say about those?': 'follow_up',
        'What does the inverter do?': 'factual',
    }
    for question, expected in cases.items():
        count = 2 if question in {'What does each document contain?',
                                 'Explain this document to a beginner.'} else 1
        assert classify_query_intent(question, count) == expected, question


def test_assignment_style_date_number_and_topic_extraction_stays_labeled(tmp_path):
    settings = settings_for(tmp_path)
    db, rag = start(settings)
    owner = db.resolve_session(db.create_session(86400), 86400)
    text = '''Course code: CCW332
Assignment 1 — Distributed systems
Announcement date: 01/02/2025 | Last submission date: 15/02/2025 | Marks: 10
Assignment 2 — Database design
Announcement date: 03/02/2025 | Last submission date: 17/02/2025 | Marks: 15
Assignment 3 — Cloud security
Announcement date: 05/02/2025 | Last submission date: 19/02/2025 | Marks: 20
Assignment 4 — Data analytics
Announcement date: 07/02/2025 | Last submission date: 21/02/2025 | Marks: 25
Assignment 5 — Mobile computing
Announcement date: 09/02/2025 | Last submission date: 23/02/2025 | Marks: 30'''
    doc = rag.ingest(pdf_bytes(text), 'Assignment DM.pdf', owner_id=owner)
    queries = (
        'What are the five assignment topics?',
        'What important dates and numbers are mentioned?',
        'List all announcement dates.', 'List all submission deadlines.',
        'How many marks does each assignment carry?',
        'What are the dates associated with Assignment 3?',
        'What information can I find in this PDF?',
    )
    for question in queries:
        hits = rag.retrieve(question, [doc['id']], owner_id=owner)
        assert hits, question
        assert all(hit['document_id'] == doc['id'] and hit['page'] == 1 for hit in hits)
        evidence = '\n'.join(hit['text'] for hit in hits)
        for required in ('CCW332', 'Assignment 1', 'Assignment 3', '01/02/2025',
                         '15/02/2025', 'Marks: 20'):
            assert required in evidence, (question, required)
    normalized = re.sub(r'\s+', ' ', evidence)
    expected_rows = (
        ('Assignment 1', '01/02/2025', '15/02/2025', '10'),
        ('Assignment 2', '03/02/2025', '17/02/2025', '15'),
        ('Assignment 3', '05/02/2025', '19/02/2025', '20'),
        ('Assignment 4', '07/02/2025', '21/02/2025', '25'),
        ('Assignment 5', '09/02/2025', '23/02/2025', '30'),
    )
    for name, announced, deadline, marks in expected_rows:
        row_pattern = rf'{name}.*?Announcement date: {announced}.*?Last submission date: {deadline}.*?Marks: {marks}'
        assert re.search(row_pattern, normalized), name
    assert rag.retrieve('What government grant is available for this course?',
                        [doc['id']], owner_id=owner) == []


def test_five_page_key_points_cover_opening_and_conclusion(tmp_path):
    _, rag = start(settings_for(tmp_path))
    pages = (
        'Introduction: A household solar photovoltaic system converts sunlight into electricity.',
        'Panels produce direct current, and an inverter changes it into alternating current.',
        'A 3 kW design uses six 500 W panels connected according to the system plan.',
        'At 5 peak-sun-hours, daily generation is about 15 kWh, or 450 kWh in 30 days.',
        'Conclusion: final yield and payback depend on shade, weather, tariff, and maintenance.',
    )
    doc = rag.ingest(pdf_bytes(*pages), 'Home Solar PV Guide.pdf')
    hits = rag.retrieve('What are the five most important points?', [doc['id']])
    assert {hit['page'] for hit in hits} == {1, 2, 3, 4, 5}
    assert any('Introduction:' in hit['text'] for hit in hits)
    assert any('Conclusion:' in hit['text'] for hit in hits)


def test_generated_solar_guide_questions_retrieve_labeled_values(tmp_path):
    _, rag = start(settings_for(tmp_path))
    pages = (
        'Home Solar PV Guide. A photovoltaic array supplies a home with electricity from sunlight.',
        'The inverter converts the panels direct current into alternating current used by appliances.',
        'Six 500 W panels make a 3 kW system. At 5 peak sun hours it generates about 15 kWh daily and 450 kWh in 30 days.',
        'The total initial budget is Rs 60,000. Monthly bill savings are Rs 2,000, so the simple payback period is 30 months.',
        'Panel output varies with shade, weather, orientation, inverter efficiency, and maintenance.',
    )
    doc = rag.ingest(pdf_bytes(*pages), 'Home Solar PV Guide.pdf')
    questions = {
        'What is this PDF about?': 'photovoltaic',
        'Explain this document to a beginner.': 'photovoltaic',
        'What does the inverter do?': 'alternating current',
        'How many 500 W panels are required for a 3 kW system?': 'six 500 W panels',
        'How much electricity does it generate in 30 days?': '450 kWh in 30 days',
        'What is the total initial budget?': 'Rs 60,000',
        'What is the simple payback period?': '30 months',
        'What if monthly savings increase to ₹2,500?': 'Rs 2,000',
        'What are the five most important points?': 'maintenance',
    }
    for question, evidence in questions.items():
        hits = rag.retrieve(question, [doc['id']])
        assert hits, question
        assert evidence.casefold() in '\n'.join(hit['text'] for hit in hits).casefold(), question
    assert rag.retrieve('What government subsidy is available?', [doc['id']]) == []


def test_selected_multi_document_overview_is_scoped_and_distributed(tmp_path):
    settings = settings_for(tmp_path)
    db, rag = start(settings)
    owner = db.resolve_session(db.create_session(86400), 86400)
    first = rag.ingest(pdf_bytes('Assignment DM covers five course tasks and their submission dates.'),
                       'Assignment DM.pdf', owner_id=owner)
    second = rag.ingest(pdf_bytes('Home Solar PV explains panels, inverters, and household energy yield.'),
                        'Home Solar PV Guide.pdf', owner_id=owner)
    both = rag.retrieve('Explain each document to a beginner.', [first['id'], second['id']], owner_id=owner)
    assert {hit['document_id'] for hit in both} == {first['id'], second['id']}
    only_first = rag.retrieve('Explain this document to a beginner.', [first['id']], owner_id=owner)
    assert only_first and {hit['document_id'] for hit in only_first} == {first['id']}


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
