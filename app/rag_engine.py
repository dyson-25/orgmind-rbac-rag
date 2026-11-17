import hashlib
import json
import re
from pathlib import Path

import fitz
import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.preprocessing import normalize

from app.rbac_guard import authorized_chunk_ids, validate_scopes

QUERY_STOP = set(ENGLISH_STOP_WORDS) | {'q4', 'acme', 'technologies', 'company', 'tell', 'does', 'please', '2026'}


def query_terms(query):
    return list(dict.fromkeys(t for t in re.findall(r'[a-zA-Z][a-zA-Z0-9]+', query.lower()) if t not in QUERY_STOP))[:40]


def extract_pdf(raw, settings):
    if len(raw) > settings.max_upload_bytes:
        raise ValueError('PDF exceeds the upload limit')
    if not raw.startswith(b'%PDF-'):
        raise ValueError('Invalid PDF content')
    try:
        with fitz.open(stream=raw, filetype='pdf') as pdf:
            if pdf.needs_pass or not 1 <= len(pdf) <= 100:
                raise ValueError('PDF must be unencrypted and contain 1–100 pages')
            pages = []
            total = 0
            for page in pdf:
                text = re.sub(r'\s+', ' ', page.get_text()).strip()
                total += len(text)
                if total > 1_000_000:
                    raise ValueError('PDF extracted text exceeds the limit')
                pages.append(text)
            if not any(pages):
                raise ValueError('PDF has no extractable text; OCR is not supported')
            return pages, dict(pdf.metadata or {})
    except (fitz.FileDataError, RuntimeError) as exc:
        raise ValueError('Unable to parse PDF') from exc


class RagEngine:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.rows = []
        self.vectorizer = None
        self.svd = None
        self.matrix = None
        self.index_epoch = -1

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
        chunks = list(self._chunks(doc_id, pages))
        conn.executemany('INSERT INTO chunks VALUES(?,?,?,?)', chunks)
        conn.executemany('INSERT INTO chunks_fts(chunk_id,text) VALUES(?,?)', [(c[0], c[3]) for c in chunks])

    def index_seeded(self, specs):
        with self.db.lock, self.db.connect() as conn:
            for spec in specs:
                exists = conn.execute('SELECT 1 FROM documents WHERE id=?', (spec['id'],)).fetchone()
                indexed = conn.execute('SELECT 1 FROM chunks WHERE document_id=?', (spec['id'],)).fetchone()
                if exists and not indexed:
                    pages, _ = extract_pdf(Path(spec['path']).read_bytes(), self.settings)
                    self._insert_chunks(conn, spec['id'], pages)
        self.rebuild()

    def rebuild(self):
        with self.db.lock, self.db.connect() as conn:
            self.rows = [dict(r) for r in conn.execute(
                'SELECT c.*,d.title FROM chunks c JOIN documents d ON d.id=c.document_id ORDER BY c.id')]
            self.vectorizer, self.svd, self.matrix = None, None, None
            if self.rows:
                vectorizer = TfidfVectorizer(ngram_range=(1, 2), max_features=6000, stop_words='english', dtype=np.float32)
                try:
                    sparse = vectorizer.fit_transform(r['text'] for r in self.rows)
                except ValueError:
                    sparse = None
                if sparse is not None:
                    self.vectorizer = vectorizer
                    dimensions = min(64, sparse.shape[0] - 1, sparse.shape[1] - 1)
                    if dimensions >= 2:
                        self.svd = TruncatedSVD(n_components=dimensions, random_state=42)
                        self.matrix = normalize(self.svd.fit_transform(sparse))
                    else:
                        self.matrix = normalize(sparse)
            self.index_epoch = self.db.epoch(conn)

    def ingest(self, raw, title, filename, scopes, doc_id=None):
        scopes = validate_scopes(scopes)
        pages, metadata = extract_pdf(raw, self.settings)
        digest = hashlib.sha256(raw).hexdigest()
        doc_id = doc_id or 'UP_' + digest[:24]
        with self.db.lock:
            with self.db.connect() as conn:
                conn.execute('DELETE FROM chunks WHERE document_id=?', (doc_id,))
                conn.execute('INSERT INTO documents VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                             'title=excluded.title,filename=excluded.filename,pages=excluded.pages,metadata=excluded.metadata,digest=excluded.digest',
                             (doc_id, title[:200], Path(filename).name[:200], len(pages), json.dumps(metadata), digest))
                conn.execute('DELETE FROM document_scopes WHERE document_id=?', (doc_id,))
                conn.executemany('INSERT INTO document_scopes VALUES(?,?)', [(doc_id, s) for s in scopes])
                self._insert_chunks(conn, doc_id, pages)
                self.db.bump_epoch(conn)
            self.rebuild()
        return doc_id

    def permissions(self, doc_id, scopes):
        scopes = validate_scopes(scopes)
        with self.db.lock:
            with self.db.connect() as conn:
                if not conn.execute('SELECT 1 FROM documents WHERE id=?', (doc_id,)).fetchone():
                    raise KeyError(doc_id)
                conn.execute('DELETE FROM document_scopes WHERE document_id=?', (doc_id,))
                conn.executemany('INSERT INTO document_scopes VALUES(?,?)', [(doc_id, s) for s in scopes])
                self.db.bump_epoch(conn)
            self.rebuild()

    def delete(self, doc_id):
        with self.db.lock:
            with self.db.connect() as conn:
                if not conn.execute('DELETE FROM documents WHERE id=?', (doc_id,)).rowcount:
                    raise KeyError(doc_id)
                self.db.bump_epoch(conn)
            self.rebuild()

    def retrieve(self, query, user, mode='hybrid', limit=5):
        if mode not in {'bm25', 'lsa', 'hybrid'}:
            raise ValueError('Invalid retrieval mode')
        terms = query_terms(query)
        if not terms:
            return []
        with self.db.lock, self.db.connect() as conn:
            if self.index_epoch != self.db.epoch(conn):
                self.rebuild()
            allowed = authorized_chunk_ids(conn, user)
            if not allowed:
                return []
            sparse_ids, dense_ids = [], []
            if mode in {'bm25', 'hybrid'}:
                placeholders = ','.join('?' for _ in allowed)
                # MATCH and the ACL predicate are applied before ORDER BY / LIMIT.
                sparse_ids = [r[0] for r in conn.execute(
                    f'SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? AND chunk_id IN ({placeholders}) '
                    'ORDER BY bm25(chunks_fts),chunk_id LIMIT 30', (' OR '.join(f'"{t}"' for t in terms), *sorted(allowed)))]
            if mode in {'lsa', 'hybrid'} and self.vectorizer is not None:
                indices = [i for i, row in enumerate(self.rows) if row['id'] in allowed]
                query_vector = self.vectorizer.transform([' '.join(terms)])
                if self.svd is not None:
                    query_vector = self.svd.transform(query_vector)
                if indices:
                    # Only authorized vectors participate in similarity computation.
                    scores = cosine_similarity(query_vector, self.matrix[indices]).ravel()
                    ranked = sorted(zip(indices, scores), key=lambda x: (-x[1], self.rows[x[0]]['id']))
                    dense_ids = [self.rows[i]['id'] for i, score in ranked if score > 0.12][:30]
            scores = {}
            rankings = [sparse_ids] if mode == 'bm25' else [dense_ids] if mode == 'lsa' else [sparse_ids, dense_ids]
            for ranking in rankings:
                for rank, cid in enumerate(ranking, 1):
                    if cid in allowed:
                        scores[cid] = scores.get(cid, 0) + 1 / (60 + rank)
            lookup = {r['id']: r for r in self.rows if r['id'] in allowed}
            ranked_ids = sorted(scores, key=lambda cid: (-scores[cid], cid))[:min(limit, 50)]
            return [{**lookup[cid], 'score': scores[cid]} for cid in ranked_ids]
