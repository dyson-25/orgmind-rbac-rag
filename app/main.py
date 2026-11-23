import hashlib
import secrets
import threading
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import pymupdf
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.database import Database
from app.http_limits import RequestBodyLimit
from app.llm_service import GenerationError, GenerationService, compose_retrieval_query
from app.rag_engine import RagEngine

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / 'samples' / 'personal-finance-guide.pdf'
COOKIE = 'orgmind_session'


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Chat(StrictModel):
    query: str = Field(min_length=1, max_length=4000)
    document_ids: list[str] | None = Field(default=None, max_length=100)
    conversation_id: str | None = Field(default=None, max_length=80)


class Clear(StrictModel):
    conversation_id: str | None = Field(default=None, max_length=80)


class Sessions:
    def __init__(self):
        self.lock = threading.RLock()
        self.attempts = OrderedDict()
        self.chats = OrderedDict()

    def limit(self, key, count):
        with self.lock:
            now = time.monotonic()
            while self.attempts and next(iter(self.attempts.values()))[-1] <= now - 60:
                self.attempts.popitem(last=False)
            queue = self.attempts.get(key)
            if queue is None:
                if len(self.attempts) >= 4096:
                    raise HTTPException(429, 'The service is busy. Please try again in a minute.', headers={'Retry-After': '60'})
                queue = deque()
            while queue and queue[0] <= now - 60:
                queue.popleft()
            if len(queue) >= count:
                raise HTTPException(429, 'Too many requests. Please wait a minute and try again.', headers={'Retry-After': '60'})
            queue.append(now)
            self.attempts[key] = queue
            self.attempts.move_to_end(key)


def create_app(settings=None, generator=None):
    settings = settings or Settings()
    db = Database(settings)
    rag = RagEngine(db, settings)
    generation = generator or GenerationService(settings)
    sessions = Sessions()

    @asynccontextmanager
    async def lifespan(application):
        db.initialize()
        rag.recover()
        rag.rebuild()
        application.state.ready = True
        yield
        application.state.ready = False
        with sessions.lock:
            sessions.chats.clear()
            sessions.attempts.clear()

    app = FastAPI(title='OrgMind · PDF Knowledge Assistant', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestBodyLimit, max_bytes=settings.max_upload_bytes)
    app.state.db, app.state.rag, app.state.generator = db, rag, generation
    app.state.settings, app.state.ready, app.state.sessions = settings, False, sessions

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({'detail': 'Invalid request. Check the required fields and input limits.'}, status_code=422)

    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        if request.url.path == '/' or request.url.path.startswith(('/api/', '/static/')):
            response.headers['Cache-Control'] = 'no-store'
        if getattr(request.state, 'session_token', None):
            response.set_cookie(COOKIE, request.state.session_token,
                                max_age=settings.session_ttl_days * 86400, httponly=True,
                                secure=settings.environment == 'production', samesite='strict')
        return response

    def mutation(request):
        if request.method not in {'GET', 'HEAD'}:
            if request.headers.get('X-OrgMind-Request') != '1':
                raise HTTPException(403, 'Use the application to submit this request.')
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                raise HTTPException(403, 'Cross-origin requests are not allowed.')

    def access(request: Request):
        mutation(request)
        token = request.cookies.get(COOKIE)
        ttl = settings.session_ttl_days * 86400
        owner_id = db.resolve_session(token, ttl)
        if owner_id is None:
            peer = request.client.host if request.client else 'unknown'
            sessions.limit(('session-issue', peer), 10)
            try:
                token = db.create_session(ttl)
            except ValueError:
                raise HTTPException(503, 'Workspace capacity reached. Please try later.') from None
            owner_id = db.resolve_session(token, ttl)
            if owner_id is None:
                raise HTTPException(503, 'Unable to open a workspace. Please try again.')
        request.state.session_token = token
        return owner_id

    def limited(request, kind, owner_id):
        peer = request.client.host if request.client else 'unknown'
        if kind == 'upload':
            sessions.limit(('upload-ip', peer), settings.ip_upload_requests_per_minute)
            sessions.limit(('upload-session', owner_id), settings.upload_requests_per_minute)
        else:
            sessions.limit(('generation-global',), settings.global_requests_per_minute)
            sessions.limit(('generation-ip', peer), settings.requests_per_minute)
            sessions.limit(('generation-session', owner_id), settings.requests_per_minute)

    def document(doc):
        return {key: doc[key] for key in ('id', 'filename', 'pages', 'status', 'uploaded_at')} | {
            'is_sample': doc.get('digest') == hashlib.sha256(SAMPLE.read_bytes()).hexdigest()
        }

    @app.get('/api/health')
    def health():
        if not app.state.ready:
            raise HTTPException(503, 'Starting up')
        return {'status': 'ok'}

    @app.get('/api/config')
    def config(token=Depends(access)):
        return {'name': 'OrgMind', 'description': 'PDF Knowledge Assistant',
                'session_ttl_days': settings.session_ttl_days,
                'ephemeral_storage': settings.ephemeral_storage, 'max_upload_bytes': settings.max_upload_bytes,
                'max_query_chars': settings.max_query_chars,
                'generation_available': settings.generation_provider == 'openai' and bool(settings.openai_api_key)}

    @app.get('/api/documents')
    def documents(token=Depends(access)):
        with db.lock:
            return {'documents': [document(doc) for doc in db.list_documents(owner_id=token)]}

    @app.get('/api/suggestions')
    def suggestions(document_ids: list[str] = Query(default=[]), token=Depends(access)):
        selected = list(dict.fromkeys(document_ids))
        if not selected or len(selected) > 50:
            raise HTTPException(400, 'Select between 1 and 50 indexed documents.')
        with db.lock:
            try:
                for doc_id in selected:
                    if db.get_document(doc_id, owner_id=token)['status'] != 'indexed':
                        raise HTTPException(400, 'Selected document is not indexed.')
            except KeyError:
                raise HTTPException(404, 'Selected document no longer exists.') from None
            return {'suggestions': rag.suggest_questions(selected, owner_id=token)}

    def ingest(raw, filename, owner_id):
        try:
            with db.lock:
                return document(rag.ingest(raw, filename, owner_id=owner_id))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except OSError:
            raise HTTPException(503, 'Unable to save the PDF. Check available storage.') from None

    @app.post('/api/documents/upload')
    async def upload(request: Request, file: UploadFile = File(...), token=Depends(access)):
        limited(request, 'upload', token)
        try:
            raw = await file.read(settings.max_upload_bytes + 1)
            if len(raw) > settings.max_upload_bytes:
                raise HTTPException(413, 'PDF exceeds the upload limit.')
            return await run_in_threadpool(ingest, raw, file.filename or '', token)
        finally:
            await file.close()

    @app.post('/api/documents/sample')
    def sample(request: Request, token=Depends(access)):
        limited(request, 'upload', token)
        return ingest(SAMPLE.read_bytes(), SAMPLE.name, token)

    @app.delete('/api/documents/{doc_id}')
    def delete(doc_id: str, token=Depends(access)):
        with db.lock:
            try:
                rag.delete(doc_id, owner_id=token)
            except KeyError:
                raise HTTPException(404, 'Document not found.') from None
            with sessions.lock:
                sessions.chats.pop(token, None)
        return {'deleted': True}

    @app.get('/api/documents/{doc_id}/sources/{chunk_id}')
    def source(doc_id: str, chunk_id: str, token=Depends(access)):
        with db.lock:
            try:
                row = db.get_chunk(doc_id, chunk_id, owner_id=token)
                doc = db.get_document(doc_id, owner_id=token)
            except KeyError:
                raise HTTPException(404, 'Source not found.') from None
            return {'document_id': doc_id, 'chunk_id': row['id'], 'filename': doc['filename'], 'page': row['page'], 'text': row['text']}

    @app.get('/api/documents/{doc_id}/preview')
    def preview(doc_id: str, page: int = Query(default=1, ge=1, le=100), token=Depends(access)):
        with db.lock:
            try:
                path = db.original_path(doc_id, owner_id=token)
                with pymupdf.open(path) as pdf:
                    if page > len(pdf):
                        raise HTTPException(404, 'Page not found.')
                    source_page = pdf[page - 1]
                    scale = min(1.5, 1200 / max(source_page.rect.width, source_page.rect.height, 1))
                    image = source_page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes('png')
            except (KeyError, FileNotFoundError):
                raise HTTPException(404, 'Original PDF is unavailable.') from None
            except (RuntimeError, ValueError):
                raise HTTPException(400, 'Unable to preview this PDF.') from None
        return Response(image, media_type='image/png')

    @app.post('/api/chat')
    def chat(payload: Chat, request: Request, token=Depends(access)):
        query = payload.query.strip()
        if not query or len(query) > settings.max_query_chars:
            raise HTTPException(422, 'Question is empty or exceeds the query limit.')
        limited(request, 'generation', token)
        with db.lock:
            selected = list(dict.fromkeys(payload.document_ids)) if payload.document_ids is not None else None
            if selected == []:
                raise HTTPException(400, 'Select at least one document.')
            if selected:
                try:
                    for did in selected:
                        if db.get_document(did, owner_id=token)['status'] != 'indexed':
                            raise HTTPException(400, 'Selected document is not indexed.')
                except KeyError:
                    raise HTTPException(404, 'Selected document no longer exists.') from None
            with sessions.lock:
                conversations = sessions.chats.setdefault(token, OrderedDict())
                sessions.chats.move_to_end(token)
                while len(sessions.chats) > 256:
                    sessions.chats.popitem(last=False)
                cid = payload.conversation_id or secrets.token_urlsafe(18)
                existing = conversations.get(cid)
                if payload.conversation_id and existing is None:
                    raise HTTPException(404, 'Conversation expired. Clear the conversation and try again.')
                history = existing['messages'] if existing and existing['documents'] == selected else []
            chunks = rag.retrieve(compose_retrieval_query(query, history, max_query_chars=settings.max_query_chars), document_ids=selected, limit=5, owner_id=token)
            try:
                result = app.state.generator.answer(query, chunks, history=history)
            except GenerationError as exc:
                headers = {'Retry-After': str(exc.retry_after)} if exc.retry_after else None
                raise HTTPException(exc.status_code, str(exc), headers=headers) from None
            with sessions.lock:
                messages = history + [{'role': 'user', 'content': query}, {'role': 'assistant', 'content': result['answer'][:8000]}]
                conversations[cid] = {'documents': selected, 'messages': messages[-12:]}
                while len(conversations) > 8:
                    conversations.popitem(last=False)
            return {**result, 'conversation_id': cid}

    @app.post('/api/conversations/clear')
    def clear(payload: Clear, token=Depends(access)):
        with sessions.lock:
            if payload.conversation_id:
                sessions.chats.get(token, {}).pop(payload.conversation_id, None)
            else:
                sessions.chats.pop(token, None)
        return {'cleared': True}

    @app.get('/')
    def index(token=Depends(access)):
        return FileResponse(ROOT / 'static' / 'index.html')

    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    return app


app = create_app()
