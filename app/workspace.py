"""Authenticated document lifecycle and session-scoped conversational workspace."""

import hashlib
import json
import secrets
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import fitz
from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.datastructures import UploadFile

from app.pdf_corpus import CORPUS
from app.product_generation import ProductGeneration, is_greeting, safe_evidence, wants_overview
from app.rag_engine import extract_pdf
from app.rbac_guard import SCOPES, authorized_chunk_ids, authorized_document_ids, validate_citations

MAX_DOCUMENTS = 1000
MAX_SESSION_CONVERSATIONS = 10
MAX_SESSIONS = 64
MAX_MESSAGES = 20


class WorkspaceChat(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=2000)
    document_ids: list[str] | None = Field(default=None, max_length=20)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)


class WorkspacePermissions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    visibility: Literal['private', 'scoped']
    allowed_scopes: list[str] = Field(max_length=5)


def now():
    return datetime.now(timezone.utc).isoformat()


def original_path(settings, document):
    """Only server-generated paths may resolve to an original, never client filenames."""
    stored = document.get('storage_path')
    upload_dir = (settings.data_dir / 'runtime' / 'uploads').resolve()
    if stored:
        expected = upload_dir / (document['id'] + '.pdf')
        candidate = Path(stored)
        if candidate == expected and candidate.resolve().parent == upload_dir and not candidate.is_symlink():
            return candidate if candidate.is_file() else None
        return None
    spec = next((spec for spec in CORPUS if spec['id'] == document['id']), None)
    if spec:
        root = (settings.data_dir / 'corpus').resolve()
        candidate = root / spec['filename']
        if candidate.resolve().parent == root and not candidate.is_symlink() and candidate.is_file():
            return candidate
    return None


def index_original(db, rag, doc_id):
    """Persist each transition; roll back partial chunks and retain the failed original."""
    with db.lock:
        with db.connect() as conn:
            row = conn.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone()
            if row is None:
                raise KeyError(doc_id)
            document = dict(row)
            conn.execute("UPDATE documents SET status='processing' WHERE id=?", (doc_id,))
            conn.execute('DELETE FROM chunks WHERE document_id=?', (doc_id,))
            db.bump_epoch(conn)
        try:
            path = original_path(rag.settings, document)
            if path is None:
                raise ValueError('Original unavailable')
            pages, metadata = extract_pdf(path.read_bytes(), rag.settings)
            with db.connect() as conn:
                rag._insert_chunks(conn, doc_id, pages)
                conn.execute("UPDATE documents SET pages=?,metadata=?,status='indexed' WHERE id=?",
                             (len(pages), json.dumps(metadata), doc_id))
                db.bump_epoch(conn)
            rag.rebuild()
        except Exception:
            with db.connect() as conn:
                conn.execute('DELETE FROM chunks WHERE document_id=?', (doc_id,))
                conn.execute("UPDATE documents SET status='failed' WHERE id=?", (doc_id,))
                db.bump_epoch(conn)
            # A failed rebuild must not leave stale chunks visible to the next retrieval.
            rag.index_epoch = -1
            return False
        return True


def recover_workspace(db, rag):
    """Invoke after seed indexing at startup to recover interrupted workspace uploads."""
    with db.lock, db.connect() as conn:
        pending = [row[0] for row in conn.execute(
            "SELECT id FROM documents WHERE status IN ('uploaded','processing')")]
    for doc_id in pending:
        index_original(db, rag, doc_id)


def register_workspace(app, db, rag, identity, bearer):
    settings = rag.settings
    sessions = OrderedDict()
    memory_epoch = [-1]

    def session(token, user):
        epoch = db.epoch()
        if memory_epoch[0] != epoch:
            sessions.clear()
            memory_epoch[0] = epoch
        key = hashlib.sha256((user['id'] + '\0' + token).encode()).hexdigest()
        if key not in sessions:
            sessions[key] = OrderedDict()
        sessions.move_to_end(key)
        while len(sessions) > MAX_SESSIONS:
            sessions.popitem(last=False)
        return sessions[key]

    def document(conn, user, doc_id, *, manage=False):
        allowed = authorized_document_ids(conn, user)
        if doc_id == 'DOC_09' or doc_id not in allowed:
            raise HTTPException(404, 'Document not found')
        row = dict(conn.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone())
        if manage and not (user['role'] == 'admin' or row['owner_id'] == user['id']):
            raise HTTPException(404, 'Document not found')
        return row

    def product_status(value):
        # Seeded documents retain their legacy storage status outside this API.
        return 'indexed' if value == 'ready' else value

    def serialize(conn, user, row):
        manageable = user['role'] == 'admin' or row['owner_id'] == user['id']
        has_original = original_path(settings, row) is not None
        owner = conn.execute('SELECT name FROM users WHERE id=?', (row['owner_id'],)).fetchone()
        department = conn.execute('SELECT name FROM departments WHERE id=?', (row['department_id'],)).fetchone()
        return {key: row[key] for key in ('id', 'title', 'filename', 'pages', 'owner_id', 'department_id',
                                        'visibility', 'uploaded_at')} | {
            'status': product_status(row['status']),
            'chunks': conn.execute('SELECT count(*) FROM chunks WHERE document_id=?', (row['id'],)).fetchone()[0],
            'owner': owner[0] if owner else None, 'department': department[0] if department else None,
            'allowed_scopes': [r[0] for r in conn.execute('SELECT scope FROM document_scopes WHERE document_id=? ORDER BY scope', (row['id'],))],
            'can_delete': manageable, 'can_reindex': manageable and has_original, 'has_original': has_original,
        }

    @app.get('/api/workspace/documents')
    def documents(q: str = Query(default='', max_length=200), status: str = Query(default='', max_length=20), token=Depends(bearer)):
        with identity(token) as user, db.connect() as conn:
            if status not in {'', 'uploaded', 'processing', 'indexed', 'ready', 'failed'}:
                raise HTTPException(422, 'Invalid document status')
            status = product_status(status)
            allowed = authorized_document_ids(conn, user) - {'DOC_09'}
            rows = [dict(row) for row in conn.execute('SELECT * FROM documents ORDER BY uploaded_at DESC,id')
                    if row['id'] in allowed and (not status or product_status(row['status']) == status)
                    and (not q or q.casefold() in (row['title'] + ' ' + row['filename']).casefold())]
            return {'documents': [serialize(conn, user, row) for row in rows[:MAX_DOCUMENTS]]}

    @app.post('/api/workspace/documents/upload', status_code=201)
    async def upload(request: Request, token=Depends(bearer)):
        with identity(token):
            pass
        if not request.headers.get('content-type', '').lower().startswith('multipart/form-data;'):
            raise HTTPException(415, 'Expected multipart PDF upload')

        class MemoryParser(MultiPartParser):
            spool_max_size = settings.max_upload_bytes + 65537
            max_file_size = settings.max_upload_bytes + 65537

        try:
            form = await MemoryParser(request.headers, request.stream(), max_files=1, max_fields=0).parse()
        except (MultiPartException, ValueError):
            raise HTTPException(413, 'Invalid or oversized upload') from None
        try:
            items = form.multi_items()
            if len(items) != 1 or items[0][0] != 'file' or not isinstance(items[0][1], UploadFile):
                raise HTTPException(422, 'Supply only one file field')
            file = items[0][1]
            if file.content_type != 'application/pdf':
                raise HTTPException(415, 'Only application/pdf is supported')
            raw = await file.read(settings.max_upload_bytes + 1)
            if len(raw) > settings.max_upload_bytes:
                raise HTTPException(413, 'PDF exceeds the upload limit')
            filename = Path((file.filename or 'document.pdf').replace('\\', '/')).name[:200]
            if not filename.lower().endswith('.pdf') or not raw.startswith(b'%PDF-'):
                raise HTTPException(415, 'Invalid PDF file')
        finally:
            await form.close()
        with identity(token) as user:
            with db.connect() as conn:
                if conn.execute('SELECT count(*) FROM documents WHERE owner_id=?', (user['id'],)).fetchone()[0] >= 100:
                    raise HTTPException(409, 'Document limit reached')
                if conn.execute('SELECT count(*) FROM documents').fetchone()[0] >= MAX_DOCUMENTS:
                    raise HTTPException(409, 'Workspace document limit reached')
            doc_id = 'WS_' + secrets.token_hex(16)
            root = settings.data_dir / 'runtime' / 'uploads'
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = root.resolve() / (doc_id + '.pdf')
            try:
                with path.open('xb') as stream:
                    path.chmod(0o600)
                    stream.write(raw)
                with db.connect() as conn:
                    conn.execute('INSERT INTO documents(id,title,filename,pages,metadata,digest,owner_id,department_id,visibility,status,uploaded_at,storage_path) '
                                 "VALUES(?,?,?,0,'{}',?,?,?,'private','uploaded',?,?)",
                                 (doc_id, filename[:-4], filename, hashlib.sha256(raw).hexdigest(), user['id'],
                                  user.get('department_id'), now(), str(path)))
                    db.bump_epoch(conn)
            except Exception:
                path.unlink(missing_ok=True)
                raise HTTPException(500, 'Unable to store document') from None
            succeeded = index_original(db, rag, doc_id)
            db.audit(user, 'workspace-upload', 'allow' if succeeded else 'failed', rule='private-owner-upload')
            with db.connect() as conn:
                return serialize(conn, user, document(conn, user, doc_id))

    @app.get('/api/workspace/documents/{doc_id}/preview')
    def preview(doc_id: str, page: int = Query(default=1, ge=1, le=100), token=Depends(bearer)):
        with identity(token) as user, db.connect() as conn:
            row = document(conn, user, doc_id)
            path = original_path(settings, row)
            if path is None:
                raise HTTPException(404, 'Original not available')
            try:
                with fitz.open(path) as pdf:
                    if pdf.needs_pass or page > len(pdf):
                        raise HTTPException(404, 'Page not available')
                    source = pdf[page - 1]
                    largest = max(source.rect.width, source.rect.height)
                    if largest <= 0:
                        raise ValueError('Invalid page')
                    pix = source.get_pixmap(matrix=fitz.Matrix(1200 / largest, 1200 / largest), alpha=False)
                    return Response(pix.tobytes('png'), media_type='image/png')
            except (RuntimeError, ValueError):
                raise HTTPException(422, 'Preview not available') from None

    @app.get('/api/workspace/documents/{doc_id}/download')
    def download(doc_id: str, token=Depends(bearer)):
        with identity(token) as user, db.connect() as conn:
            row = document(conn, user, doc_id)
            path = original_path(settings, row)
            if path is None:
                raise HTTPException(404, 'Original not available')
            # Materialize under the authorization lock, avoiding a streamed delete/ACL race.
            return Response(path.read_bytes(), media_type='application/pdf',
                            headers={'Content-Disposition': 'attachment; filename="' + doc_id + '.pdf"'})

    @app.delete('/api/workspace/documents/{doc_id}', status_code=204)
    def delete(doc_id: str, token=Depends(bearer)):
        with identity(token) as user:
            with db.connect() as conn:
                row = document(conn, user, doc_id, manage=True)
                path = original_path(settings, row)
                if path:
                    path.unlink(missing_ok=True)
                conn.execute('DELETE FROM documents WHERE id=?', (doc_id,))
                db.bump_epoch(conn)
            rag.index_epoch = -1
            db.audit(user, 'workspace-delete', 'allow', rule='owner-or-admin')
            return Response(status_code=204)

    @app.post('/api/workspace/documents/{doc_id}/reindex')
    def reindex(doc_id: str, token=Depends(bearer)):
        with identity(token) as user:
            with db.connect() as conn:
                row = document(conn, user, doc_id, manage=True)
                if original_path(settings, row) is None:
                    raise HTTPException(404, 'Original not available')
            succeeded = index_original(db, rag, doc_id)
            db.audit(user, 'workspace-reindex', 'allow' if succeeded else 'failed', rule='owner-or-admin')
            with db.connect() as conn:
                return serialize(conn, user, document(conn, user, doc_id))

    @app.patch('/api/workspace/documents/{doc_id}/permissions')
    def permissions(doc_id: str, body: WorkspacePermissions, token=Depends(bearer)):
        with identity(token, admin=True) as user:
            if (not set(body.allowed_scopes) <= set(SCOPES)
                    or (body.visibility == 'scoped' and not body.allowed_scopes)):
                raise HTTPException(422, 'Invalid document scopes')
            with db.connect() as conn:
                document(conn, user, doc_id)
                conn.execute('UPDATE documents SET visibility=? WHERE id=?', (body.visibility, doc_id))
                conn.execute('DELETE FROM document_scopes WHERE document_id=?', (doc_id,))
                conn.executemany('INSERT INTO document_scopes VALUES(?,?)',
                                 [(doc_id, scope) for scope in sorted(set(body.allowed_scopes))])
                db.bump_epoch(conn)
            db.audit(user, 'workspace-permissions', 'allow', rule='admin-explicit-scopes')
            with db.connect() as conn:
                return serialize(conn, user, document(conn, user, doc_id))

    @app.post('/api/workspace/chat')
    def chat(body: WorkspaceChat, token=Depends(bearer)):
        query = body.query.strip()
        if not query or (body.document_ids is not None and any(not 1 <= len(s) <= 100 for s in body.document_ids)):
            raise HTTPException(422, 'Invalid question or document selection')
        with identity(token) as user:
            conversations = session(token, user)
            previous = conversations.get(body.conversation_id) if body.conversation_id else None
            if body.conversation_id and previous is None:
                raise HTTPException(404, 'Conversation not found')
            selected = body.document_ids
            if previous and 'document_ids' not in body.model_fields_set:
                selected = previous['document_ids']
            with db.connect() as conn:
                allowed_docs = authorized_document_ids(conn, user) - {'DOC_09'}
                if selected is not None and not set(selected) <= allowed_docs:
                    db.audit(user, 'workspace-chat', 'deny', intercept=True, rule='document-selection')
                    raise HTTPException(404, 'Document not found')
            history = [message['content'] for message in (previous['messages'] if previous else []) if message['role'] == 'user'][-3:]
            retrieval_query = ' '.join(history + [query])[-6000:]
            chunks = [] if is_greeting(query) else rag.retrieve(retrieval_query, user, limit=12,
                                document_ids=selected, exclude_fixtures=True)
            if not is_greeting(query) and wants_overview(query):
                # Overview questions need representative evidence even without lexical overlap.
                with db.connect() as conn:
                    allowed_chunks = authorized_chunk_ids(conn, user)
                    overview_docs = sorted(set(selected) if selected is not None else {c['document_id'] for c in chunks})
                    representatives = []
                    for doc_id in overview_docs[:20]:
                        representatives.extend(dict(row) | {'score': 0.0} for row in conn.execute(
                            'SELECT c.*,d.title FROM chunks c JOIN documents d ON d.id=c.document_id '
                            'WHERE c.document_id=? ORDER BY c.page,c.id LIMIT 2', (doc_id,)) if row['id'] in allowed_chunks)
                    chunks = list({c['id']: c for c in representatives + chunks}.values())[:40]
            result = ProductGeneration(app.state.llm).generate(query, chunks)
            with db.connect() as conn:
                allowed = authorized_chunk_ids(conn, user)
                validate_citations(result['citations'], allowed)
                lookup = {chunk['id']: chunk for chunk in safe_evidence(chunks)}
                for cited in result['citations']:
                    source = lookup.get(cited['chunk_id'])
                    actual = conn.execute('SELECT c.document_id,c.page,d.title FROM chunks c JOIN documents d ON d.id=c.document_id WHERE c.id=?',
                                          (cited['chunk_id'],)).fetchone()
                    if source is None or actual is None or any(cited[k] != source[k] or cited[k] != actual[k] for k in ('document_id', 'page', 'title')):
                        raise HTTPException(500, 'Evidence validation failed')
            diagnostic = result['provider_status']
            if diagnostic:
                db.audit(user, 'provider', 'success' if diagnostic['succeeded'] else 'fallback', rule='workspace-authorized-context')
            db.audit(user, 'workspace-chat', 'allow' if result['citations'] or is_greeting(query) else 'deny',
                     intercept=result['injection_warning'], rule='authorized-context')
            conversation_id = previous['id'] if previous else secrets.token_urlsafe(24)
            conversation = previous or {'id': conversation_id, 'title': query[:100], 'messages': [], 'acl_epoch': db.epoch()}
            conversation.update(document_ids=selected, updated_at=now())
            conversation['messages'].extend([
                {'role': 'user', 'content': query},
                {'role': 'assistant', 'content': result['answer'], **{k: result[k] for k in ('citations', 'generation_mode', 'provider_status')}},
            ])
            conversation['messages'] = conversation['messages'][-MAX_MESSAGES:]
            conversations[conversation_id] = conversation
            conversations.move_to_end(conversation_id)
            while len(conversations) > MAX_SESSION_CONVERSATIONS:
                conversations.popitem(last=False)
            return result | {'acl_epoch': db.epoch(), 'conversation_id': conversation_id}

    @app.get('/api/workspace/conversations')
    def conversations(token=Depends(bearer)):
        with identity(token) as user:
            return {'conversations': [{key: value[key] for key in ('id', 'title', 'updated_at')}
                                      for value in reversed(session(token, user).values())]}

    @app.get('/api/workspace/conversations/{conversation_id}')
    def get_conversation(conversation_id: str, token=Depends(bearer)):
        with identity(token) as user:
            value = session(token, user).get(conversation_id)
            if value is None:
                raise HTTPException(404, 'Conversation not found')
            return value

    @app.delete('/api/workspace/conversations/{conversation_id}', status_code=204)
    def delete_conversation(conversation_id: str, token=Depends(bearer)):
        with identity(token) as user:
            if session(token, user).pop(conversation_id, None) is None:
                raise HTTPException(404, 'Conversation not found')
            return Response(status_code=204)
