"""Durable PDF ingestion and local BM25/TF-IDF/LSA reciprocal-rank retrieval."""

import hashlib
import math
import os
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
}


def _tokens(text):
    text = re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', text.casefold())
    return [t for t in re.findall(r'[^\W_]+', text)
            if t not in QUERY_STOP and (len(t) > 1 or t.isdigit())]


def query_terms(query):
    return list(dict.fromkeys(_tokens(query)))[:40]


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


def _summary_request(query):
    terms = set(query_terms(query))
    return bool(terms & {'summary', 'summarize', 'summarise', 'overview'}) and terms <= {
        'summary', 'summarize', 'summarise', 'overview', 'brief', 'concise', 'detailed',
        'comprehensive', 'overall', 'give', 'provide', 'guide', 'documents', 'pdfs',
        'selected', 'uploaded', 'entire', 'main', 'points', 'key', 'content',
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
                text = re.sub(r'\s+', ' ', page.get_text()).strip()
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
                end = min(start + self.settings.chunk_size, len(text))
                if end < len(text):
                    boundary = text.rfind(' ', start + self.settings.chunk_size // 2, end)
                    if boundary > start:
                        end = boundary
                chunk = text[start:end].strip()
                cid = hashlib.sha256(f'{doc_id}:{number}:{start}:{chunk}'.encode()).hexdigest()[:32]
                if chunk:
                    yield (cid, doc_id, number, chunk)
                if end == len(text):
                    break
                start = max(start + 1, end - self.settings.chunk_overlap)

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
            ordered = [pages[page] for page in sorted(pages)]
            positions = np.linspace(0, len(ordered) - 1, min(limit, len(ordered)), dtype=int)
            queues.append([ordered[i] for i in positions])
        candidates = []
        for depth in range(limit):
            for queue in queues:
                if depth < len(queue):
                    candidates.append({**queue[depth], 'score': 1 / (61 + depth)})
        return self._deduplicate(candidates, limit)

    @staticmethod
    def _deduplicate(candidates, limit):
        results, seen = [], set()
        for row in candidates:
            fingerprint = re.sub(r'\s+', ' ', row['text']).strip().casefold()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            results.append(row)
            if len(results) == limit:
                break
        return results

    def retrieve(self, query, document_ids=None, limit=5, *, owner_id=None):
        terms = list(dict.fromkeys(_index_terms(' '.join(query_terms(query)))))
        if not terms or limit <= 0:
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
            if _summary_request(query):
                return self._representative(limit)
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
                query_vector = self.vectorizer.transform([' '.join(query_terms(query))])
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
