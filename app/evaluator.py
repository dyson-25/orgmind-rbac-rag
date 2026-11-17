"""Fixed, corpus-dependent retrieval benchmark; never tune truth to measured scores.

Citation accuracy checks actual local-answer citations against curated evidence pages.
Candidate evidence precision is reported separately; neither measures full answer faithfulness.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from app.llm_service import LLMService
from app.rbac_guard import DENIAL, authorized_chunk_ids


@dataclass(frozen=True)
class BenchmarkCase:
    user_id: str
    query: str
    relevant_pages: tuple[tuple[str, int], ...]
    denied: bool = False

    @property
    def expected_document_ids(self):
        return frozenset(document_id for document_id, _ in self.relevant_pages)


BENCHMARK = (
    BenchmarkCase('general_user', 'How much can I claim per kilometre for driving my car on a business trip?', (('DOC_01', 1),)),
    BenchmarkCase('general_user', 'What are the normal office hours and lunch break?', (('DOC_01', 2),)),
    BenchmarkCase('hr_user', 'How many paid vacation days do staff receive annually?', (('DOC_02', 1),)),
    BenchmarkCase('hr_user', 'What are the salary ranges for job levels L3 and L4?', (('DOC_02', 2),)),
    BenchmarkCase('finance_user', 'How much did the company spend on operations in the fourth quarter?', (('DOC_03', 1),)),
    BenchmarkCase('finance_user', 'What cash reserves cover delayed customer collections?', (('DOC_03', 2),)),
    BenchmarkCase('finance_user', 'How large is the executive incentive pool for Q4?', (('DOC_04', 1),)),
    BenchmarkCase('executive_user', 'What valuation is assumed for the Project Titan acquisition?', (('DOC_04', 2),)),
    BenchmarkCase('eng_user', 'How do internal services authenticate each other across trust boundaries?', (('DOC_05', 1),)),
    BenchmarkCase('eng_user', 'What cloud availability percentage does the infrastructure SLA promise?', (('DOC_05', 2),)),
    BenchmarkCase('eng_user', 'How quickly must critical operations be restored after an outage?', (('DOC_06', 1),)),
    BenchmarkCase('hr_user', 'What checks are required before onboarding a new supplier?', (('DOC_07', 1),)),
    BenchmarkCase('finance_user', 'When does a purchase need approval from the chief financial officer?', (('DOC_07', 2),)),
    BenchmarkCase('general_user', 'How should I connect remotely to internal resources on an untrusted network?', (('DOC_08', 1),)),
    BenchmarkCase('general_user', 'Where should I report a suspicious email and what should I preserve?', (('DOC_08', 2),)),
    BenchmarkCase('admin_user', 'What are our infrastructure availability commitment and disaster recovery time objective?', (('DOC_05', 2), ('DOC_06', 1))),
    BenchmarkCase('general_user', 'What are the salary bands for L3 and L4 employees?', (('DOC_02', 2),), denied=True),
    BenchmarkCase('finance_user', 'How many days of annual paid leave are staff entitled to?', (('DOC_02', 1),), denied=True),
    BenchmarkCase('hr_user', 'What was the Q4 operating expenditure and reserve liquidity?', (('DOC_03', 1), ('DOC_03', 2)), denied=True),
    BenchmarkCase('eng_user', 'What is the executive bonus pool?', (('DOC_04', 1),), denied=True),
    BenchmarkCase('executive_user', 'What is the database failover recovery time objective?', (('DOC_06', 1),), denied=True),
)

METRIC_DEFINITIONS = {
    'recall_at_5': 'Macro mean over positive cases of distinct authorized relevant document IDs in the first five chunks divided by fixed expected document IDs. IDs must resolve to the claimed document in the database; duplicate chunks do not add recall.',
    'mrr': 'Macro mean over positive cases of reciprocal first authorized relevant document rank among the first five chunks; zero for a miss. Chunk ranks are not deduplicated.',
    'citation_accuracy': 'Fraction of positive answered queries with at least one actual local-answer citation to an authorized returned chunk with exact database provenance and a ground-truth document/page labeled as containing the expected evidence. Each answered query counts once, including answers without citations as failures; denials and empty answers are excluded and reported separately. Zero if no positive queries are answered. Page-level citation support, not full answer faithfulness or sentence-level entailment.',
    'evidence_precision': 'Micro fraction of all positive-case returned chunks whose id, document_id, title, page and text match database provenance, are authorized, and have a curated relevant document/page. Zero when no chunks are returned. Preserves the former citation_accuracy candidate-precision metric.',
    'unauthorized_retrieval_rate': 'Micro fraction of all returned chunks across positive and denied cases whose IDs are absent from authorized_chunk_ids for that user at retrieval time; zero if no chunks are returned. Unknown IDs count as unauthorized.',
    'denied_cases': 'Restricted-topic probes are excluded from recall, MRR and citation accuracy, but included in unauthorized retrieval rate. Returning unrelated authorized evidence is not an authorization violation.',
    'rss_mb': 'Process-wide resident memory sampled after evaluation in MiB (bytes / 1024**2); Linux current RSS from /proc/self/statm, otherwise resource peak RSS explicitly labeled.',
}


def _memory_usage():
    try:
        resident_pages = int(Path('/proc/self/statm').read_text().split()[1])
        return resident_pages * os.sysconf('SC_PAGE_SIZE') / 1024**2, 'current_rss:/proc/self/statm'
    except (OSError, ValueError, IndexError):
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        divisor = 1024**2 if sys.platform == 'darwin' else 1024
        return peak / divisor, 'peak_rss:resource.ru_maxrss'


class _LocalBenchmarkAdapter:
    """Force the normal extractive path without consulting external providers."""

    def generate(self, messages):
        return None


def evaluate(rag, db):
    """Evaluate an already seeded/indexed corpus without changing it or its truth.

    Requires the original synthetic ACME documents and persona permissions.
    Missing documents, unindexed pages, or changed benchmark permissions fail
    explicitly rather than silently changing the set of expected answers.
    """
    methods = {}
    generator = LLMService(adapter=_LocalBenchmarkAdapter())
    with db.lock:
        with db.connect() as conn:
            sources = {row['id']: dict(row) for row in conn.execute(
                'SELECT c.*, d.title FROM chunks c JOIN documents d ON d.id=c.document_id'
            )}
            available_pages = {(row['document_id'], row['page']) for row in sources.values()}
            prepared = []
            for case in BENCHMARK:
                user = db.user(case.user_id)
                if user is None or not set(case.relevant_pages) <= available_pages:
                    raise ValueError('Benchmark requires seeded personas and indexed ACME source pages')
                allowed = authorized_chunk_ids(conn, user)
                allowed_documents = {sources[cid]['document_id'] for cid in allowed}
                expected = case.expected_document_ids
                if (case.denied and expected & allowed_documents) or (
                    not case.denied and not expected <= allowed_documents
                ):
                    raise ValueError('Benchmark persona permissions differ from the fixed corpus truth')
                prepared.append((case, user))

        for mode in ('bm25', 'lsa', 'hybrid'):
            recall_sum = reciprocal_sum = 0.0
            positive_count = positive_hits = valid_citations = 0
            answered_count = correct_answered_count = 0
            returned_count = unauthorized_count = 0
            details = []
            for case, user in prepared:
                hits = rag.retrieve(case.query, user, mode=mode, limit=5)[:5]
                with db.connect() as conn:
                    allowed = authorized_chunk_ids(conn, user)
                judged = []
                valid_hits = {}
                relevant_documents = set()
                first_relevant_rank = 0
                for rank, hit in enumerate(hits, 1):
                    source = sources.get(hit.get('id'))
                    authorized = hit.get('id') in allowed
                    provenance = source is not None and all(
                        hit.get(field) == source[field]
                        for field in ('id', 'document_id', 'title', 'page', 'text')
                    )
                    relevant = bool(
                        not case.denied and authorized and source is not None
                        and hit.get('document_id') == source['document_id']
                        and source['document_id'] in case.expected_document_ids
                    )
                    citation_correct = bool(
                        relevant and provenance
                        and (source['document_id'], source['page']) in case.relevant_pages
                    )
                    if authorized and provenance:
                        valid_hits[hit['id']] = hit
                    if relevant:
                        relevant_documents.add(source['document_id'])
                        if not first_relevant_rank:
                            first_relevant_rank = rank
                    returned_count += 1
                    unauthorized_count += not authorized
                    if not case.denied:
                        positive_hits += 1
                        valid_citations += citation_correct
                    judged.append({
                        **{field: hit.get(field) for field in ('id', 'document_id', 'title', 'page')},
                        'authorized': authorized, 'valid_provenance': provenance,
                        'relevant_document': relevant, 'citation_correct': citation_correct,
                    })
                answered = answer_citation_correct = False
                answer, citations = '', []
                if not case.denied:
                    positive_count += 1
                    recall_sum += len(relevant_documents) / len(case.expected_document_ids)
                    reciprocal_sum += 1 / first_relevant_rank if first_relevant_rank else 0
                    generated = generator.generate(case.query, list(valid_hits.values()))
                    answer, citations = generated['answer'], generated['citations']
                    answered = bool(answer.strip()) and answer.strip() != DENIAL
                    for citation in citations:
                        source = valid_hits.get(citation.get('chunk_id'))
                        if source is not None and all(
                            citation.get(field) == source[field]
                            for field in ('document_id', 'title', 'page')
                        ) and (source['document_id'], source['page']) in case.relevant_pages:
                            answer_citation_correct = answered
                            break
                    answered_count += answered
                    correct_answered_count += answer_citation_correct
                details.append({
                    'user_id': case.user_id, 'query': case.query, 'denied': case.denied,
                    'expected_document_ids': sorted(case.expected_document_ids),
                    'relevant_pages': [list(pair) for pair in case.relevant_pages], 'hits': judged,
                    'answer': answer, 'citations': citations, 'answered': answered,
                    'answer_citation_correct': answer_citation_correct,
                })
            methods[mode] = {
                'recall_at_5': recall_sum / positive_count,
                'mrr': reciprocal_sum / positive_count,
                'citation_accuracy': correct_answered_count / answered_count if answered_count else 0.0,
                'positive_answered_count': answered_count,
                'positive_unanswered_count': positive_count - answered_count,
                'correct_answered_count': correct_answered_count,
                'evidence_precision': valid_citations / positive_hits if positive_hits else 0.0,
                'unauthorized_retrieval_rate': unauthorized_count / returned_count if returned_count else 0.0,
                'returned_count': returned_count, 'unauthorized_count': unauthorized_count,
                'positive_returned_count': positive_hits, 'correct_citation_count': valid_citations,
                'cases': details,
            }
    rss_mb, rss_source = _memory_usage()
    return {
        'methods': methods, 'rss_mb': rss_mb, 'rss_source': rss_source,
        'generation_mode': 'extractive_fallback',
        'case_count': len(BENCHMARK),
        'positive_case_count': sum(not case.denied for case in BENCHMARK),
        'denied_case_count': sum(case.denied for case in BENCHMARK),
        'metric_definitions': dict(METRIC_DEFINITIONS),
        'corpus_dependence': 'Fixed synthetic ACME DOC_01–DOC_08 document/page truth, with DOC_09 as a distractor. Results depend on corpus contents, chunking, index, retrieval settings and library versions; not a general-purpose or held-out quality guarantee. Truth is never adjusted to scores.',
        'recall_target': 0.85,
    }
