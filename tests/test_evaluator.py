"""Fixed-truth evaluation against the real seeded retrieval pipeline."""

import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from app.config import Settings
from app.database import Database
from app.evaluator import BENCHMARK, _memory_usage, evaluate
from app.pdf_corpus import generate_corpus
from app.rag_engine import RagEngine


@pytest.fixture(scope='module')
def indexed_corpus():
    root = Path(__file__).parent / f'.evaluator-test-{uuid4().hex}'
    try:
        settings = Settings(data_dir=root, jwt_secret='evaluation-only-' * 4)
        db = Database(settings)
        db.initialize()
        specs = generate_corpus(settings.data_dir / 'corpus')
        db.seed_documents(specs)
        rag = RagEngine(db, settings)
        rag.index_seeded(specs)
        yield rag, db
    finally:
        if root.exists():
            shutil.rmtree(root)


class FixedRetriever:
    def __init__(self, case, hits):
        self.case, self.hits = case, hits

    def retrieve(self, query, user, mode='hybrid', limit=5):
        assert mode in ('bm25', 'lsa', 'hybrid')
        assert limit == 5
        if query == self.case.query and user['id'] == self.case.user_id:
            return [dict(hit) for hit in self.hits]
        return []


def source_hit(db, document_id, page):
    with db.connect() as conn:
        row = conn.execute(
            'SELECT c.*, d.title FROM chunks c JOIN documents d ON d.id=c.document_id '
            'WHERE c.document_id=? AND c.page=? ORDER BY length(c.text) DESC LIMIT 1',
            (document_id, page),
        ).fetchone()
    return {**dict(row), 'score': 0.5}


def test_real_benchmark_metrics_and_provenance(indexed_corpus):
    rag, db = indexed_corpus
    report = evaluate(rag, db)
    assert report['case_count'] == len(BENCHMARK) >= 12
    assert report['positive_case_count'] >= 12
    assert report['denied_case_count'] >= 5
    assert len({case.user_id for case in BENCHMARK}) == 6
    assert report['rss_mb'] > 0
    assert report['rss_source'].startswith(('current_rss:', 'peak_rss:'))
    assert report['metric_definitions'] and report['corpus_dependence']
    assert set(report['methods']) == {'bm25', 'lsa', 'hybrid'}
    for metrics in report['methods'].values():
        for name in ('recall_at_5', 'mrr', 'citation_accuracy', 'unauthorized_retrieval_rate'):
            assert 0 <= metrics[name] <= 1
        assert metrics['unauthorized_retrieval_rate'] == metrics['unauthorized_count'] == 0
        assert metrics['returned_count'] > 0
        assert metrics['correct_citation_count'] > 0
        hits = [hit for case in metrics['cases'] for hit in case['hits']]
        assert len(hits) == metrics['returned_count']
        assert all(hit['authorized'] and hit['valid_provenance'] for hit in hits)
        with db.connect() as conn:
            for hit in hits:
                row = conn.execute(
                    'SELECT c.document_id,c.page,d.title FROM chunks c '
                    'JOIN documents d ON d.id=c.document_id WHERE c.id=?', (hit['id'],),
                ).fetchone()
                assert dict(row) == {key: hit[key] for key in ('document_id', 'page', 'title')}
        assert metrics['evidence_precision'] == pytest.approx(
            metrics['correct_citation_count'] / metrics['positive_returned_count']
        )
        answered = [case for case in metrics['cases'] if not case['denied'] and case['answered']]
        assert metrics['positive_answered_count'] == len(answered)
        assert metrics['correct_answered_count'] == sum(case['answer_citation_correct'] for case in answered)
        assert metrics['citation_accuracy'] == pytest.approx(
            metrics['correct_answered_count'] / len(answered)
        )
    print(json.dumps({
        **{key: report[key] for key in ('case_count', 'positive_case_count', 'denied_case_count', 'rss_mb', 'rss_source')},
        'methods': {mode: {key: value for key, value in metrics.items() if key != 'cases'}
                    for mode, metrics in report['methods'].items()},
    }, indent=2))


@pytest.mark.parametrize('field,value', [
    ('title', 'Invented title'), ('page', 999), ('document_id', 'DOC_08'),
    ('id', 'nonexistent-chunk'), ('text', 'Invented evidence'),
])
def test_forged_provenance_is_not_an_accurate_citation(indexed_corpus, field, value):
    _, db = indexed_corpus
    case = BENCHMARK[0]
    hit = source_hit(db, 'DOC_01', 1)
    hit[field] = value
    report = evaluate(FixedRetriever(case, [hit]), db)
    for metrics in report['methods'].values():
        assert metrics['citation_accuracy'] == metrics['evidence_precision'] == 0
        assert metrics['cases'][0]['hits'][0]['valid_provenance'] is False
        assert metrics['unauthorized_retrieval_rate'] == (1.0 if field == 'id' else 0.0)


def test_valid_provenance_alone_does_not_establish_relevance(indexed_corpus):
    _, db = indexed_corpus
    case = BENCHMARK[0]
    wrong_page = source_hit(db, 'DOC_01', 2)
    report = evaluate(FixedRetriever(case, [wrong_page]), db)
    for metrics in report['methods'].values():
        assert metrics['cases'][0]['hits'][0]['valid_provenance'] is True
        assert metrics['citation_accuracy'] == metrics['evidence_precision'] == 0
        assert metrics['recall_at_5'] == pytest.approx(1 / report['positive_case_count'])
    irrelevant = source_hit(db, 'DOC_08', 1)
    report = evaluate(FixedRetriever(case, [irrelevant]), db)
    for metrics in report['methods'].values():
        assert metrics['citation_accuracy'] == metrics['recall_at_5'] == metrics['mrr'] == 0


def test_denied_case_contributes_actual_unauthorized_hits(indexed_corpus):
    _, db = indexed_corpus
    case = next(case for case in BENCHMARK if case.denied)
    hit = source_hit(db, *case.relevant_pages[0])
    report = evaluate(FixedRetriever(case, [hit]), db)
    for metrics in report['methods'].values():
        assert metrics['returned_count'] == metrics['unauthorized_count'] == 1
        assert metrics['unauthorized_retrieval_rate'] == 1
        assert metrics['positive_returned_count'] == 0
        assert metrics['citation_accuracy'] == metrics['recall_at_5'] == metrics['mrr'] == 0


def test_recall_deduplicates_documents_and_mrr_uses_chunk_rank(indexed_corpus):
    _, db = indexed_corpus
    case = next(case for case in BENCHMARK if len(case.expected_document_ids) == 2)
    irrelevant = source_hit(db, 'DOC_08', 1)
    relevant = source_hit(db, 'DOC_05', 2)
    report = evaluate(FixedRetriever(case, [irrelevant, relevant, relevant]), db)
    for metrics in report['methods'].values():
        assert metrics['recall_at_5'] == pytest.approx(0.5 / report['positive_case_count'])
        assert metrics['mrr'] == pytest.approx(0.5 / report['positive_case_count'])
        assert metrics['evidence_precision'] == pytest.approx(2 / 3)
        assert metrics['citation_accuracy'] == 1
        assert metrics['positive_answered_count'] == metrics['correct_answered_count'] == 1


def test_empty_results_have_zero_metrics(indexed_corpus):
    _, db = indexed_corpus
    report = evaluate(FixedRetriever(BENCHMARK[0], []), db)
    for metrics in report['methods'].values():
        for key in ('recall_at_5', 'mrr', 'citation_accuracy', 'unauthorized_retrieval_rate'):
            assert metrics[key] == 0
        assert metrics['returned_count'] == 0


@pytest.mark.parametrize('citation_kind', ['correct', 'wrong-page', 'wrong-document', 'unknown', 'forged-title', 'not-returned', 'none'])
def test_answer_citations_not_retrieved_candidates_determine_accuracy(indexed_corpus, monkeypatch, citation_kind):
    from app.llm_service import LLMService
    from app.rbac_guard import DENIAL

    _, db = indexed_corpus
    case = BENCHMARK[0]
    correct = source_hit(db, 'DOC_01', 1)
    wrong_page = source_hit(db, 'DOC_01', 2)
    wrong_document = source_hit(db, 'DOC_08', 1)
    cited = {'wrong-page': wrong_page, 'wrong-document': wrong_document}.get(citation_kind, correct)
    citation = {key: cited[key] for key in ('document_id', 'page', 'title')}
    citation['chunk_id'] = cited['id']
    if citation_kind == 'unknown':
        citation['chunk_id'] = 'unknown'
    if citation_kind == 'forged-title':
        citation['title'] = 'Invented title'

    def generate(self, query, chunks):
        if query != case.query:
            return {'answer': DENIAL, 'citations': []}
        return {'answer': 'An answered positive query',
                'citations': [] if citation_kind == 'none' else [citation, citation]}

    monkeypatch.setattr(LLMService, 'generate', generate)
    hits = [wrong_page, wrong_document] if citation_kind == 'not-returned' else [correct, wrong_page, wrong_document]
    report = evaluate(FixedRetriever(case, hits), db)
    for metrics in report['methods'].values():
        assert metrics['positive_answered_count'] == 1
        assert metrics['correct_answered_count'] == (1 if citation_kind == 'correct' else 0)
        assert metrics['citation_accuracy'] == (1.0 if citation_kind == 'correct' else 0.0)
        assert metrics['evidence_precision'] == (0 if citation_kind == 'not-returned' else pytest.approx(1 / 3))


def test_citation_accuracy_averages_answered_queries_not_citations(indexed_corpus, monkeypatch):
    from app.llm_service import LLMService
    from app.rbac_guard import DENIAL

    _, db = indexed_corpus
    hit = source_hit(db, 'DOC_01', 1)
    citation = {key: hit[key] for key in ('document_id', 'page', 'title')}
    citation['chunk_id'] = hit['id']

    def generate(self, query, chunks):
        if query == BENCHMARK[0].query:
            return {'answer': 'Supported answer', 'citations': [citation] * 5}
        if query == BENCHMARK[1].query:
            return {'answer': 'Uncited answer', 'citations': []}
        return {'answer': DENIAL, 'citations': [citation]}

    monkeypatch.setattr(LLMService, 'generate', generate)
    report = evaluate(FixedRetriever(BENCHMARK[0], [hit]), db)
    for metrics in report['methods'].values():
        assert metrics['positive_answered_count'] == 2
        assert metrics['positive_unanswered_count'] == report['positive_case_count'] - 2
        assert metrics['correct_answered_count'] == 1
        assert metrics['citation_accuracy'] == 0.5


def test_benchmark_generation_never_calls_external_provider(indexed_corpus, monkeypatch):
    from app.llm_service import HTTPGenerationAdapter

    rag, db = indexed_corpus
    monkeypatch.setenv('GENERATION_PROVIDER', 'openai')

    def forbidden(*args, **kwargs):
        pytest.fail('Benchmark must use deterministic local generation')

    monkeypatch.setattr(HTTPGenerationAdapter, 'generate', forbidden)
    report = evaluate(rag, db)
    assert report['generation_mode'] == 'extractive_fallback'
    assert all(metrics['positive_answered_count'] > 0 for metrics in report['methods'].values())


def test_memory_fallback_is_labeled_peak(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError('procfs unavailable')

    monkeypatch.setattr(Path, 'read_text', unavailable)
    value, source = _memory_usage()
    assert value > 0
    assert source == 'peak_rss:resource.ru_maxrss'
