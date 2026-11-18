"""Conversational generation; the fixed evaluation generator remains unchanged."""

import json
import re
from collections import OrderedDict

from app.llm_service import build_model_messages, injection_indicators
from app.rag_engine import query_terms
from app.rbac_guard import DENIAL


PRODUCT_INSTRUCTIONS = (
    '\nWrite a natural, coherent answer with paragraphs, or a concise summary or comparison '
    'when requested. Explain only facts supported by the supplied context; distinguish documents '
    'and mention uncertainty. Do not invent facts, links, sources or page numbers. '
    'Return ONLY JSON with keys answer (string) and citation_ids (a list of the exact supplied '
    'chunk id strings supporting the answer). If unsupported, answer with the prescribed denial '
    'and an empty citation_ids list. Never include citation metadata yourself.'
)


def is_greeting(query):
    return bool(re.fullmatch(r'\s*(?:hi|hello|hey|good morning|good evening|thanks|thank you)[!.?\s]*', query, re.I))


def wants_overview(query):
    return bool(re.search(r'\b(?:summari[sz]e|summary|overview|compare|comparison|explain|elaborate|more|details)\b', query, re.I))


def citation(chunk):
    return {'document_id': chunk['document_id'], 'chunk_id': chunk['id'],
            'title': chunk['title'], 'page': chunk['page']}


def safe_evidence(chunks):
    return [chunk for chunk in chunks if not any(
        injection_indicators(str(chunk.get(field, '')))
        for field in ('text', 'title', 'id', 'document_id', 'page'))]


def fallback(query, chunks):
    grouped = OrderedDict()
    terms = set(query_terms(query))
    for chunk in chunks:
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+|[\r\n]+', chunk['text']) if s.strip()]
        matching = [s for s in sentences if terms.intersection(query_terms(s))]
        selected = (sentences if wants_overview(query) else matching)[:3]
        if selected:
            group = grouped.setdefault(chunk['document_id'], {'title': chunk['title'], 'parts': [], 'sources': []})
            for sentence in selected:
                if sentence not in group['parts'] and len(group['parts']) < 5:
                    group['parts'].append(sentence)
                    if chunk not in group['sources']:
                        group['sources'].append(chunk)
    if not grouped:
        return DENIAL, []
    paragraphs, citations = [], []
    for group in grouped.values():
        paragraph = group['title'] + ':\n' + ' '.join(group['parts'])
        if sum(len(part) + 2 for part in paragraphs) + len(paragraph) > 12000:
            break
        paragraphs.append(paragraph)
        citations.extend(citation(chunk) for chunk in group['sources'])
    return '\n\n'.join(paragraphs), citations


class ProductGeneration:
    def __init__(self, service):
        self.service = service

    def generate(self, query, chunks):
        safe = safe_evidence(chunks)
        warning = len(safe) != len(chunks) or injection_indicators(query)
        answer, citations = fallback(query, safe)
        if is_greeting(query):
            answer, citations = 'Hello! Ask a question about your permitted documents, or select PDFs to summarize or compare.', []
        result = {'answer': answer, 'citations': citations, 'generation_mode': 'extractive_fallback',
                  'provider_status': None, 'injection_warning': warning}
        adapter = getattr(self.service, 'adapter', None)
        if adapter is None or not safe or is_greeting(query):
            return result
        provider = getattr(adapter, 'provider', 'custom')
        diagnostic = {'provider': provider if isinstance(provider, str) and re.fullmatch(r'[\w-]{1,40}', provider) else 'custom',
                      'model': None, 'http_status': None, 'error_category': None,
                      'succeeded': False, 'fallback_used': True}
        messages = build_model_messages(query, safe)
        messages[0]['content'] += PRODUCT_INSTRUCTIONS
        try:
            raw = adapter.generate(messages)
            if not isinstance(raw, str) or len(raw) > 16000:
                raise ValueError('Invalid response')
            raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
            output = json.loads(raw)
            text, ids = output['answer'], output['citation_ids']
            lookup = {chunk['id']: chunk for chunk in safe}
            if (not isinstance(text, str) or not text.strip() or len(text) > 12000
                    or not isinstance(ids, list) or len(ids) > len(safe)
                    or any(not isinstance(cid, str) or cid not in lookup for cid in ids)
                    or (text.strip() != DENIAL and not ids)
                    or (text.strip() == DENIAL and ids)):
                raise ValueError('Invalid evidence')
            result.update(answer=text.strip(), citations=[citation(lookup[cid]) for cid in dict.fromkeys(ids)],
                          generation_mode=diagnostic['provider'])
            diagnostic.update(succeeded=True, fallback_used=False)
        except Exception as error:
            diagnostic['error_category'] = 'malformed' if isinstance(error, (ValueError, KeyError, TypeError)) else 'service'
        adapter_diagnostic = getattr(adapter, 'last_diagnostics', None)
        if isinstance(adapter_diagnostic, dict):
            model = adapter_diagnostic.get('model')
            if isinstance(model, str) and re.fullmatch(r'[\w./-]{1,128}', model):
                diagnostic['model'] = model
            status = adapter_diagnostic.get('http_status')
            if isinstance(status, int) and 100 <= status <= 599:
                diagnostic['http_status'] = status
            category = adapter_diagnostic.get('error_category')
            if category in {'configuration', 'authentication', 'authorization', 'rate_limit', 'timeout', 'transport', 'service', 'malformed', 'not_found', 'quota'}:
                diagnostic['error_category'] = category
        self.service.last_diagnostics = dict(diagnostic)
        result['provider_status'] = diagnostic
        return result
