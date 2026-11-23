"""Durable PDF ingestion and local BM25/TF-IDF/LSA reciprocal-rank retrieval."""

import hashlib
import math
import os
from difflib import SequenceMatcher
from collections import Counter
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pymupdf
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.preprocessing import normalize
from sklearn.utils.sparsefuncs import mean_variance_axis


QUERY_STOP = set(ENGLISH_STOP_WORDS) | {
    'tell', 'does', 'please', 'document', 'pdf', 'documents', 'pdfs', 'page', 'pages',
    'contain', 'contains', 'information', 'give', 'provide', 'guide',
    'use', 'inside', 'things', 'main',
}
_DATE_VALUE = re.compile(
    r'\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|'
    r'apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|'
    r'nov(?:ember)?|dec(?:ember)?)(?:\s+\d{2,4})?|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|'
    r'apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|'
    r'nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:,?\s+\d{2,4})?|20\d{2})\b', re.I,
)
_NUMBER_VALUE = re.compile(
    r'(?<![\w])(?:₹|rs\.?\s*|inr\s*|\$\s*)?(?:\d[\d,]*(?:\.\d+)?|'
    r'(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|'
    r'fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|'
    r'eighty|ninety|hundred|thousand)(?:[ -]+(?:one|two|three|four|five|six|seven|eight|nine|'
    r'ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|'
    r'twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand))*)\s*'
    r'(?:%|marks?|kw|w|kwh|days?|months?|years?)?(?!\w)', re.I,
)


def _tokens(text):
    text = re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', text.casefold())
    return [t for t in re.findall(r'[^\W_]+', text)
            if t not in QUERY_STOP and (len(t) > 1 or t.isdigit())]


def query_terms(query):
    return list(dict.fromkeys(_tokens(query)))[:40]


_QUERY_CORRECTIONS = {
    'dis': 'this', 'whats': 'what is', 'submision': 'submission',
    'submissions': 'submission', 'inveter': 'inverter', 'electrisity': 'electricity',
    'genrate': 'generate', 'genrates': 'generates', 'doc': 'document',
    'docs': 'documents', 'pdfs': 'pdf', 'simple': 'beginner',
}


def normalize_query(query, vocabulary=()):
    """Return an additional, conservative retrieval representation; never replace the user's text."""
    query = re.sub(r"\bwhat['’]s\b", 'what is', query, flags=re.I)
    words = re.findall(r'\d[\d,]*(?:\.\d+)?|[^\W_]+|[^\w\s]', query)
    known = set(vocabulary)
    normalized = []
    for original in words:
        word = original.casefold()
        if (not word.isalnum() or any(char.isdigit() for char in word)
                or word in known):
            normalized.append(original)
            continue
        correction = _QUERY_CORRECTIONS.get(word)
        if correction:
            normalized.extend(correction.split())
            continue
        if original[:1].isupper():
            normalized.append(original)
            continue
        if len(word) < 4:
            normalized.append(original)
            continue
        if known:
            matches = [term for term in known if len(term) >= 4 and abs(len(term) - len(word)) <= 1
                       and SequenceMatcher(None, word, term, autojunk=False).ratio() >= .86]
            if len(matches) == 1:
                normalized.append(matches[0])
                continue
        normalized.append(original)
    return ' '.join(normalized)


def _index_terms(text):
    # Apply the same light inflection normalization to documents and questions.
    terms = []
    for token in _tokens(text):
        if len(token) > 4 and token.endswith('s') and not token.endswith(('ss', 'us', 'is')):
            token = token[:-1]
        if len(token) > 5 and token.endswith('ing'):
            token = token[:-3]
        terms.append(token)
    return terms


def classify_query_intent(query, document_count=1):
    """Small, deterministic routing rules; this never supplies factual content."""
    text = re.sub(r'[^\w\s]', ' ', normalize_query(query).casefold())
    text = re.sub(r'\s+', ' ', text).strip()
    if re.search(r'\b(compare|comparison|contrast|differences?|differentiate)\b', text):
        return 'comparison'
    wants_dates = bool(re.search(r'\b(date|dates|deadline|deadlines|due date|submission|announc(?:e|ement|ements)|upcoming)\b', text))
    wants_numbers = bool(re.search(r'\b(number|numbers|numeric|numerical|figure|figures|value|values|mark|marks|score|scores|amount|amounts|price|cost|budget|how many|how much|financial|electricity|generate)\b', text))
    if wants_dates and wants_numbers:
        return 'date_number_extraction'
    if wants_dates:
        return 'date_extraction'
    if wants_numbers:
        return 'numeric_extraction'
    if re.search(r'\b(list|extract|enumerate|table|names|topics|assignments)\b', text):
        return 'list_extraction'
    if re.search(r'\b(five|\d+)\s+(?:most\s+)?(?:important|key|main)\s+points\b|\b(key|main|important) points\b', text):
        return 'key_points'
    overview_patterns = (
        r'\bwhat\s+(?:does\s+)?(?:this|the|my)\s+(?:pdf|document|file)\s+(?:has|have|contain|contains)\b',
        r'\bwhat\s+(?:is\s+)?(?:inside|in)\s+(?:this|the|my)\s+(?:pdf|document|file)\b',
        r'\b(?:tell|explain)\s+me\s+about\s+(?:this|the|my)\s+(?:pdf|document|file)\b',
        r'\b(?:main|important)\s+(?:things|points)\s+in\s+(?:this|the|my)\s+(?:pdf|document|file)\b',
        r'\bmain\s+things\s+in\s+(?:this|the|my)\s+(?:pdf|document|file)\b',
        r'\bwhat\s+(?:can\s+i\s+)?learn\s+from\s+(?:this|the|my)\s+(?:pdf|document|file)\b',
        r'\bwhat\s+is\s+(this|the|my)\s+(pdf|document|file)\s+(about|for)\b',
        r'\bwhat\s+does\s+(this|the|my)\s+(pdf|document|file)\s+(contain|tell)\b',
        r'\bwhat\s+is\s+(this|the|my)\s+(pdf|document|file)\s+telling\s+me\b',
        r'\bwhat\s+can\s+i\s+learn\s+from\s+(this|the|my)\s+(pdf|document|file)\b',
        r'\bwhat\s+is\s+in\s+(this|the|my)\s+(pdf|document|file)\b',
        r'\bwhat\s+(?:important\s+)?information\s+(?:is\s+mentioned|can\s+i\s+find)\b',
        r'\b(explain|describe)\s+(?:the\s+)?(both|each|these|selected)\s+(?:pdfs?|documents?|files?)\b',
        r'\b(explain|describe)\s+(this|the|my)\s+(pdf|document|file)(?:\s+to\s+(?:a\s+)?beginner)?\b',
        r'\bwhat\s+does\s+each\s+(?:pdf|document|file)\s+contain\b',
        r'\b(which|what)\s+(document\s+)?pages?\s+contain\s+(the\s+)?(information|details)\s+about\b',
    )
    summary = bool(re.search(r'\b(summary|summari[sz]e|overview)\b', text))
    overview = summary or any(re.search(pattern, text) for pattern in overview_patterns)
    if re.search(r'\b(important|key)\s+(topics|themes|ideas)\b', text):
        return 'key_points'
    if overview and document_count > 1:
        return 'multi_document_overview'
    if overview:
        return 'summary' if summary else 'overview'
    if re.search(r'\b(?:it|they|them|those|these|both|above|previous)\b|^\s*(?:and|also|what about|how about|why|tell me more)\b', text):
        return 'follow_up'
    return 'factual'


def _summary_request(query, document_count=1):
    return classify_query_intent(query, document_count) in {
        'overview', 'summary', 'key_points', 'multi_document_overview', 'comparison',
    }


def extract_pdf(raw, settings):
    if len(raw) > settings.max_upload_bytes:
        raise ValueError(f'PDF exceeds the upload limit ({settings.max_upload_bytes} bytes)')
    if not raw.startswith(b'%PDF-'):
        raise ValueError('Invalid PDF signature')
    try:
        with pymupdf.open(stream=raw, filetype='pdf') as pdf:
            if pdf.needs_pass:
                raise ValueError('Encrypted PDFs are not supported')
            if not 1 <= len(pdf) <= 100:
                raise ValueError('PDF must contain 1–100 pages')
            pages, total = [], 0
            for page in pdf:
                text = page.get_text(sort=True).replace('\r\n', '\n').replace('\r', '\n')
                text = re.sub(r'[\t\f\v ]+', ' ', text)
                text = re.sub(r' *\n *', '\n', text)
                text = re.sub(r'\n{3,}', '\n\n', text).strip()
                text = re.sub(r'(?<!\n)\n(?!\n)', ' ', text)
                total += len(text)
                if total > 1_000_000:
                    raise ValueError('PDF extracted text exceeds the limit')
                pages.append(text)
            if not any(pages):
                raise ValueError('PDF has no extractable text; OCR is not supported')
            return pages, dict(pdf.metadata or {})
    except (pymupdf.FileDataError, RuntimeError) as exc:
        raise ValueError('Unable to parse PDF') from exc


class RagEngine:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        if not 0 <= settings.chunk_overlap < settings.chunk_size:
            raise ValueError('Chunk overlap must be smaller than chunk size')
        self.rows = []
        self.vectorizer = self.svd = self.matrix = self.sparse_matrix = None
        self.index_epoch = -1
        self.index_scope = None

    def _chunks(self, doc_id, pages):
        for number, text in enumerate(pages, 1):
            start = 0
            while start < len(text):
                hard_end = min(start + self.settings.chunk_size, len(text))
                end = hard_end
                if hard_end < len(text):
                    floor = start + max(40, self.settings.chunk_size // 2)
                    for pattern in (r'\n\n', r'[.!?](?:[”’\"\']*)\s+', r'\s+'):
                        matches = list(re.finditer(pattern, text[floor:hard_end]))
                        if matches:
                            match = matches[-1]
                            end = floor + match.end()
                            break
                    # A single long token is the only case where a hard split is needed.
                chunk = text[start:end].strip()
                if end < len(text) and end > start and not text[end - 1].isspace():
                    next_space = text.find(' ', end)
                    next_para = text.find('\n', end)
                    choices = [i for i in (next_space, next_para) if i >= 0 and i - end < 80]
                    if choices:
                        end = min(choices) + 1
                        chunk = text[start:end].strip()
                cid = hashlib.sha256(f'{doc_id}:{number}:{start}:{chunk}'.encode()).hexdigest()[:32]
                if chunk:
                    yield (cid, doc_id, number, chunk)
                if end == len(text):
                    break
                target = max(start + 1, end - self.settings.chunk_overlap)
                if target > start:
                    next_boundary = re.search(r'\s', text[target:end])
                    if next_boundary:
                        target += next_boundary.end()
                start = target

    def _insert_chunks(self, conn, doc_id, pages):
        conn.executemany('INSERT INTO chunks VALUES(?,?,?,?)', self._chunks(doc_id, pages))

    @staticmethod
    def _sync_directory(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write_original(self, path, raw):
        part = path.with_suffix('.pdf.part')
        with part.open('xb') as stream:
            part.chmod(0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(part, path)
        self._sync_directory(path.parent)

    def _index_document(self, doc_id, pages):
        with self.db.connect() as conn:
            conn.execute('DELETE FROM chunks WHERE document_id=?', (doc_id,))
            self._insert_chunks(conn, doc_id, pages)
            conn.execute("UPDATE documents SET pages=?,status='indexed' WHERE id=?", (len(pages), doc_id))
        self.index_epoch = -1

    def ingest(self, raw, filename, *, owner_id=None):
        if not isinstance(filename, str) or not filename.lower().endswith('.pdf'):
            raise ValueError('Only PDF files are supported')
        filename = Path(filename.replace('\\', '/')).name
        if not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename):
            raise ValueError('Invalid PDF filename')
        pages, _ = extract_pdf(raw, self.settings)
        digest = hashlib.sha256(raw).hexdigest()
        with self.db.lock:
            with self.db.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                if owner_id is not None:
                    _, params = self.db.owner_filter(owner_id)
                    # Validate even an empty workspace before writing a persistent row.
                    if not conn.execute('SELECT 1 FROM sessions WHERE owner_id=? AND expires_at>?',
                                        (owner_id, params[-1])).fetchone():
                        raise ValueError('Workspace expired or unavailable')
                existing = conn.execute("SELECT * FROM documents WHERE digest=? AND status='indexed' "
                                        'AND owner_id IS ?', (digest, owner_id)).fetchone()
                if existing is not None:
                    return dict(existing)
                if conn.execute('SELECT count(*) FROM documents WHERE owner_id IS ?', (owner_id,)).fetchone()[0] >= self.settings.max_documents:
                    raise ValueError('Document limit reached')
                if conn.execute('SELECT count(*) FROM documents').fetchone()[0] >= getattr(self.settings, 'max_total_documents', 500):
                    raise ValueError('Global document limit reached')
                root = Path(self.settings.data_dir).resolve() / 'uploads'
                if root.is_symlink():
                    raise ValueError('Invalid upload storage directory')
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                doc_id = 'PDF_' + uuid.uuid4().hex
                path = root / (doc_id + '.pdf')
                conn.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)', (
                    doc_id, filename, len(pages), 'uploading', datetime.now(timezone.utc).isoformat(), str(path), digest, owner_id,
                ))
            # The durable pending row owns the staged file before any bytes are written.
            self._write_original(path, raw)
            self._index_document(doc_id, pages)
            self.rebuild()
            return self.db.get_document(doc_id, owner_id=owner_id)

    def recover(self):
        """Repair only recorded operations; never scan/delete unrecognized originals."""
        with self.db.lock:
            for row in self.db.list_documents():
                if row['status'] == 'deleting':
                    try:
                        self._finish_delete(row)
                    except (OSError, ValueError):
                        pass
                    continue
                if row['status'] not in {'uploading', 'uploaded', 'processing', 'failed'}:
                    continue
                try:
                    path = self.db._safe_path(row['storage_path'], must_exist=False)
                    if path is None:
                        raise ValueError('Original unavailable')
                    if not path.is_file():
                        part = self.db._safe_path(path.with_suffix('.pdf.part'))
                        if part is None or hashlib.sha256(part.read_bytes()).hexdigest() != row['digest']:
                            raise ValueError('Original unavailable or incomplete')
                        os.replace(part, path)
                        self._sync_directory(path.parent)
                    raw = path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != row['digest']:
                        raise ValueError('Original does not match the recorded PDF')
                    pages, _ = extract_pdf(raw, self.settings)
                    self._index_document(row['id'], pages)
                except (OSError, ValueError):
                    with self.db.connect() as conn:
                        conn.execute("UPDATE documents SET status='failed' WHERE id=?", (row['id'],))
            self.index_epoch = -1

    def _finish_delete(self, row):
        path = self.db._safe_path(row['storage_path'], must_exist=False)
        if path is not None:
            for candidate in (path, path.with_suffix('.pdf.part')):
                safe = self.db._safe_path(candidate)
                if safe is not None:
                    owned_stage = (safe.name == row['id'] + '.pdf.part'
                                   and re.fullmatch(r'PDF_[0-9a-f]{32}', row['id']) is not None
                                   and safe.parent == Path(self.settings.data_dir).resolve() / 'uploads')
                    if not owned_stage and hashlib.sha256(safe.read_bytes()).hexdigest() != row['digest']:
                        raise ValueError('Original changed; refusing to remove unrecognized bytes')
                    safe.unlink()
                    self._sync_directory(safe.parent)
        with self.db.connect() as conn:
            conn.execute('DELETE FROM documents WHERE id=?', (row['id'],))
        self.index_epoch = -1

    def delete(self, doc_id, *, owner_id=None):
        with self.db.lock:
            row = self.db.get_document(doc_id, owner_id=owner_id)
            with self.db.connect() as conn:
                conn.execute("UPDATE documents SET status='deleting' WHERE id=?", (doc_id,))
            self._finish_delete(row)
            self.rebuild()

    def rebuild(self, *, owner_id=None, document_ids=None):
        with self.db.lock, self.db.connect() as conn:
            where, params = self.db.owner_filter(owner_id, 'd')
            selected = None if document_ids is None else tuple(sorted(set(document_ids)))
            rows = [dict(r) for r in conn.execute(
                'SELECT c.*,d.filename FROM chunks c JOIN documents d ON d.id=c.document_id '
                f"WHERE d.status='indexed' AND {where} ORDER BY c.document_id,c.page,c.id", params)
                if selected is None or r['document_id'] in selected]
            vectorizer = svd = matrix = sparse = None
            if rows:
                candidate = TfidfVectorizer(ngram_range=(1, 2), max_features=12000,
                                            tokenizer=_index_terms, token_pattern=None, lowercase=False,
                                            dtype=np.float32)
                try:
                    sparse = candidate.fit_transform(r['text'] for r in rows)
                except ValueError:
                    pass
                if sparse is not None:
                    vectorizer = candidate
                    dimensions = min(64, sparse.shape[0] - 1, sparse.shape[1] - 1)
                    _, variance = mean_variance_axis(sparse, axis=0)
                    if dimensions >= 2 and float(variance.sum()) > 1e-9:
                        svd = TruncatedSVD(n_components=dimensions, random_state=42)
                        matrix = normalize(svd.fit_transform(sparse))
                    else:
                        matrix = normalize(sparse)
            self.rows, self.vectorizer, self.svd = rows, vectorizer, svd
            self.matrix, self.sparse_matrix = matrix, sparse
            self.index_epoch = self.db.epoch(conn)
            self.index_scope = (owner_id, selected)

    @staticmethod
    def _bm25(rows, terms):
        counts = [Counter(_index_terms(row['text'])) for row in rows]
        average = sum(sum(count.values()) for count in counts) / max(len(counts), 1)
        if not average:
            return []
        frequencies = {term: sum(term in count for count in counts) for term in terms}
        scores = []
        for row, count in zip(rows, counts):
            length = sum(count.values())
            score = 0.0
            for term in terms:
                tf = count[term]
                if tf:
                    df = frequencies[term]
                    idf = math.log(1 + (len(rows) - df + 0.5) / (df + 0.5))
                    score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / average))
            if score:
                scores.append((row['id'], score))
        return [cid for cid, _ in sorted(scores, key=lambda item: (-item[1], item[0]))][:30]

    def _representative(self, limit):
        documents = {}
        for row in self.rows:
            pages = documents.setdefault(row['document_id'], {})
            current = pages.get(row['page'])
            if current is None or len(row['text']) > len(current['text']):
                pages[row['page']] = row
        queues = []
        for pages in documents.values():
            page_numbers = sorted(pages)
            if len(page_numbers) == 1:
                page_chunks = [row for row in self.rows
                               if row['document_id'] == pages[page_numbers[0]]['document_id']
                               and row['page'] == page_numbers[0]]
                positions = np.linspace(0, len(page_chunks) - 1,
                                        min(limit, len(page_chunks)), dtype=int)
                ordered = [page_chunks[index] for index in positions]
            else:
                positions = np.linspace(0, len(page_numbers) - 1,
                                        min(limit, len(page_numbers)), dtype=int)
                ordered = [pages[page_numbers[index]] for index in positions]
            queues.append(ordered)
        candidates = []
        for depth in range(limit):
            for queue in queues:
                if depth < len(queue):
                    candidates.append({**queue[depth], 'score': 1 / (61 + depth)})
        return self._deduplicate(candidates, limit)

    def _structured_retrieve(self, intent, terms, limit):
        candidates = []
        for row in self.rows:
            text = row['text']
            dates = len(_DATE_VALUE.findall(text))
            numbers = len(_NUMBER_VALUE.findall(text))
            lower = text.casefold()
            lexical = len(set(terms) & set(_index_terms(text)))
            labels = sum(label in lower for label in (
                'assignment', 'announcement', 'submission', 'deadline', 'due date',
                'marks', 'course code', 'total budget', 'payback', 'inverter',
            ))
            if intent == 'date_extraction' and not dates and not any(
                label in lower for label in ('announcement', 'submission', 'deadline', 'due date')
            ):
                continue
            if intent == 'date_number_extraction' and not dates and not numbers:
                continue
            if intent == 'numeric_extraction' and not numbers and not labels:
                continue
            score = lexical * 2 + min(dates, 8) * 2 + min(numbers, 12) + labels * 2
            if score:
                candidates.append(({**row, 'score': float(score)}, score))
        if not candidates and intent == 'list_extraction':
            return self._representative(limit)
        candidates.sort(key=lambda item: (-item[1], item[0]['document_id'], item[0]['page'], item[0]['id']))
        # First take the strongest passage from each document/page, then fill from
        # remaining evidence. Rows are already scoped to the selected workspace docs.
        selected, seen_pages = [], set()
        for row, _ in candidates:
            key = (row['document_id'], row['page'])
            if key not in seen_pages:
                selected.append(row)
                seen_pages.add(key)
                if len(selected) == limit:
                    break
        if len(selected) < limit:
            selected_ids = {row['id'] for row in selected}
            remaining = [row for row, _ in candidates if row['id'] not in selected_ids]
            selected.extend(remaining[:limit - len(selected)])
        return self._deduplicate(selected, limit)

    def suggest_questions(self, document_ids, *, owner_id=None):
        """Build locally grounded question prompts from only indexed, selected rows."""
        selected = tuple(sorted(set(document_ids or ())))
        if not selected:
            return []
        with self.db.lock, self.db.connect() as conn:
            if owner_id is not None:
                _, params = self.db.owner_filter(owner_id)
                if not conn.execute('SELECT 1 FROM sessions WHERE owner_id=? AND expires_at>?',
                                    (owner_id, params[-1])).fetchone():
                    return []
            placeholders = ','.join('?' for _ in selected)
            owner_where, owner_params = self.db.owner_filter(owner_id, 'd')
            rows = [dict(row) for row in conn.execute(
                'SELECT c.document_id,c.page,c.text,d.filename FROM chunks c JOIN documents d ON d.id=c.document_id '
                f"WHERE d.status='indexed' AND d.id IN ({placeholders}) AND {owner_where} "
                'ORDER BY d.filename,c.page,c.id', (*selected, *owner_params))]
        by_doc = {}
        for row in rows:
            by_doc.setdefault(row['document_id'], {'filename': row['filename'], 'texts': []})['texts'].append(row['text'])
        suggestions = []
        for doc_id, document in by_doc.items():
            text = '\n'.join(document['texts'])
            if not text.strip():
                continue
            filename = document['filename']
            scope = [doc_id]
            if len(by_doc) > 1:
                suggestions.append({'question': f"What is {filename} about?", 'document_ids': scope})
            else:
                suggestions.append({'question': 'What is this document about?', 'document_ids': scope})
            headings = []
            for line in text.splitlines():
                line = line.strip(' \t#.-')
                words = _tokens(line)
                if 2 <= len(words) <= 9 and (len(line) < 90 and (line.istitle() or re.match(r'^(?:\d+(?:\.\d+)*[.)]?\s+|[A-Z][A-Z\s&-]{4,})', line))):
                    if line.casefold() not in {item.casefold() for item in headings}:
                        headings.append(line)
            if headings and len(text) > 500:
                suggestions.append({'question': f"What are the main sections, including {headings[0]}?", 'document_ids': scope})
            term_counts = Counter(term for term in _index_terms(text) if len(term) >= 4 and not term.isdigit())
            minimum_topic_frequency = 1 if sum(term_counts.values()) < 120 else 2
            topics = [term for term, count in term_counts.most_common(12)
                      if count >= minimum_topic_frequency and term not in {'document', 'information', 'important', 'system', 'using'}]
            if topics:
                suggestions.append({'question': f'What does the document say about {topics[0]}?', 'document_ids': scope})
            if _DATE_VALUE.search(text):
                suggestions.append({'question': 'What important dates or deadlines are mentioned?', 'document_ids': scope})
            if _NUMBER_VALUE.search(text):
                suggestions.append({'question': 'What important numbers and measurements should I know?', 'document_ids': scope})
            if re.search(r'\b(?:₹|\$|\b(?:INR|Rs\.?|budget|cost|price|savings|payback)\b)', text, re.I):
                suggestions.append({'question': 'What financial amounts or estimates does the document give?', 'document_ids': scope})
            if re.search(r'\S.*\|.*\S|\S\s{2,}\S.*\s{2,}\S', text):
                suggestions.append({'question': 'What information is organized in tables or lists?', 'document_ids': scope})
            if headings and len(suggestions) < 6:
                suggestions.append({'question': f'What does the document say about {headings[-1]}?', 'document_ids': scope})
        if len(by_doc) > 1:
            overview_items = [item for item in suggestions if item['question'].startswith('What is ')
                              and item['question'].endswith(' about?')]
            other_items = [item for item in suggestions if item not in overview_items]
            suggestions = ([{'question': 'Explain the selected documents separately.', 'document_ids': list(selected)}]
                           + overview_items + other_items)
        unique, seen = [], set()
        for item in suggestions:
            key = (item['question'].casefold(), tuple(item['document_ids']))
            if key not in seen:
                seen.add(key)
                unique.append(item)
            if len(unique) == 6:
                break
        return unique

    @staticmethod
    def _deduplicate(candidates, limit):
        results, seen = [], set()
        for row in candidates:
            fingerprint = re.sub(r'\s+', ' ', row['text']).strip().casefold()
            if fingerprint in seen:
                continue
            if any(min(len(fingerprint), len(previous)) > 120
                   and min(len(fingerprint), len(previous)) / max(len(fingerprint), len(previous), 1) > 0.78
                   and ((fingerprint in previous or previous in fingerprint)
                        or SequenceMatcher(None, fingerprint, previous, autojunk=False).ratio() >= 0.9)
                   for previous in seen):
                continue
            seen.add(fingerprint)
            results.append(row)
            if len(results) == limit:
                break
        return results

    def retrieve(self, query, document_ids=None, limit=5, *, owner_id=None):
        terms = list(dict.fromkeys(_index_terms(' '.join(query_terms(query)))))
        if limit <= 0:
            return []
        limit = min(limit, 5)
        selected = None if document_ids is None else tuple(sorted(set(document_ids)))
        with self.db.lock, self.db.connect() as conn:
            if owner_id is not None:
                _, params = self.db.owner_filter(owner_id)
                if not conn.execute('SELECT 1 FROM sessions WHERE owner_id=? AND expires_at>?',
                                    (owner_id, params[-1])).fetchone():
                    return []
            if self.index_epoch != self.db.epoch(conn) or self.index_scope != (owner_id, selected):
                self.rebuild(owner_id=owner_id, document_ids=selected)
            if not self.rows:
                return []
            document_count = len({row['document_id'] for row in self.rows})
            vocabulary = set().union(*(set(_index_terms(row['text'])) for row in self.rows))
            normalized = normalize_query(query, vocabulary)
            terms = list(dict.fromkeys(_index_terms(' '.join(query_terms(normalized)))))
            intent = classify_query_intent(normalized, document_count)
            if intent in {'overview', 'summary', 'key_points', 'multi_document_overview', 'comparison'}:
                return self._representative(max(limit, document_count) if intent == 'multi_document_overview' else limit)
            if intent in {'date_extraction', 'date_number_extraction', 'numeric_extraction', 'list_extraction'}:
                return self._structured_retrieve(intent, terms, limit)
            if not terms:
                return []
            lookup = {row['id']: row for row in self.rows}
            corpus_terms = set().union(*(set(_index_terms(row['text'])) for row in self.rows))
            # A single generic match must not admit a question whose subject is absent.
            if len(set(terms) & corpus_terms) / len(terms) < 0.3:
                return []
            evidence = {cid for cid, row in lookup.items() if set(terms) & set(_index_terms(row['text']))}
            if not evidence:
                return []
            # BM25 statistics, vocabulary and LSA are all fitted to this workspace/selection.
            sparse_ids = self._bm25(self.rows, terms)
            dense_ids, similarities = [], {}
            if self.vectorizer is not None:
                query_vector = self.vectorizer.transform([' '.join(query_terms(normalized))])
                lexical = cosine_similarity(query_vector, self.sparse_matrix).ravel()
                if self.svd is not None:
                    semantic = cosine_similarity(self.svd.transform(query_vector), self.matrix).ravel()
                    dense = 0.75 * lexical + 0.25 * semantic
                else:
                    dense = lexical
                similarities = {row['id']: float(score) for row, score in zip(self.rows, lexical)}
                ranked = sorted(zip(self.rows, dense), key=lambda item: (-item[1], item[0]['id']))
                dense_ids = [row['id'] for row, score in ranked if score >= 0.12 and row['id'] in evidence][:30]
            scores = {}
            for ranking in (sparse_ids, dense_ids):
                for rank, cid in enumerate(ranking, 1):
                    if similarities and similarities.get(cid, 0) < 0.06:
                        continue
                    scores[cid] = scores.get(cid, 0) + 1 / (60 + rank)
            candidates = [{**lookup[cid], 'score': scores[cid]}
                          for cid in sorted(scores, key=lambda key: (-scores[key], key))]
            return self._deduplicate(candidates, limit)
