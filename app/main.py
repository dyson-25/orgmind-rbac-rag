import json
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.auth import Auth
from app.config import Settings
from app.database import Database
from app.evaluator import evaluate
from app.http_limits import UploadBodyLimit
from app.llm_service import LLMService
from app.pdf_corpus import generate_corpus
from app.rag_engine import RagEngine
from app.rbac_guard import DENIAL, PERSONAS, ROLE_SCOPES, SCOPES, authorized_chunk_ids, authorized_scopes, validate_citations
from app.sql_analytics import SQLAnalytics

ROOT = Path(__file__).resolve().parent.parent


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Login(StrictModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class Persona(StrictModel):
    persona_id: str = Field(min_length=1, max_length=100)


class Chat(StrictModel):
    query: str = Field(min_length=2, max_length=2000)


class Metric(StrictModel):
    metric_id: str = Field(min_length=1, max_length=80)
    period: str | None = Field(default=None, max_length=40)


class Permissions(StrictModel):
    allowed_scopes: list[str] = Field(min_length=1, max_length=5)


class Role(StrictModel):
    role: str = Field(min_length=1, max_length=30)


def create_app(settings=None, llm=None):
    settings = settings or Settings()
    db = Database(settings)
    rag = RagEngine(db, settings)
    generator = llm or LLMService()

    @asynccontextmanager
    async def lifespan(application):
        db.initialize()
        specs = generate_corpus(settings.data_dir / 'corpus')
        db.seed_documents(specs)
        rag.index_seeded(specs)
        application.state.auth = Auth(db, settings)
        application.state.ready = True
        yield
        application.state.ready = False

    app = FastAPI(title='OrgMind · Synthetic Enterprise Studio', lifespan=lifespan)
    app.add_middleware(UploadBodyLimit, max_bytes=settings.max_upload_bytes)
    app.state.db, app.state.rag, app.state.llm = db, rag, generator
    app.state.settings, app.state.ready = settings, False

    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    def bearer(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith('Bearer '):
            raise HTTPException(401, 'Authentication required')
        return authorization[7:]

    @contextmanager
    def identity(token, admin=False):
        # Serialize authorization, context assembly and lifecycle writes in the single worker.
        with db.lock:
            user = app.state.auth.verify(token)
            if admin and user['role'] != 'admin':
                db.audit(user, 'admin-access', 'deny', rule='admin-only')
                raise HTTPException(403, 'Administrator access required')
            yield user

    def answer(query, user):
        chunks = rag.retrieve(query, user)
        result = app.state.llm.generate(query, chunks)
        with db.connect() as conn:
            allowed = authorized_chunk_ids(conn, user)
            validate_citations(result['citations'], allowed)
            # Also validate cited document/page provenance, not merely the chunk ID.
            lookup = {r['id']: r for r in chunks}
            for citation in result['citations']:
                source = lookup.get(citation['chunk_id'])
                if not source or any(citation[k] != source[k] for k in ('document_id', 'page', 'title')):
                    raise HTTPException(500, 'Evidence validation failed')
            denied = result['answer'] == DENIAL
            blocked = []
            if denied:
                scopes = authorized_scopes(user)
                blocked = [r[0] for r in conn.execute('SELECT id FROM documents d WHERE NOT EXISTS '
                    '(SELECT 1 FROM document_scopes s WHERE s.document_id=d.id '
                    f'AND s.scope IN ({",".join("?" for _ in scopes)}))', scopes)]
        db.audit(user, 'chat', 'deny' if denied else 'allow',
                 intercept=result.get('injection_warning', False) or denied, blocked=blocked,
                 rule='candidate-scope-exclusion' if denied else 'authorized-context')
        if result.pop('provider_failed', False):
            db.audit(user, 'provider', 'fallback', rule='provider-unavailable')
        return {**result, 'acl_epoch': db.epoch()}

    @app.get('/api/health')
    def health():
        return {'status': 'ok' if app.state.ready else 'starting', 'synthetic_data': True}

    @app.get('/api/config')
    def config():
        return {'demo_enabled': settings.allow_demo, 'personas': PERSONAS if settings.allow_demo else {},
                'scopes': SCOPES, 'synthetic_data': True}

    @app.post('/api/auth/login')
    def login(body: Login):
        with db.lock:
            return app.state.auth.login(body.username, body.password)

    @app.post('/api/auth/demo-persona')
    def demo(body: Persona):
        with db.lock:
            return app.state.auth.demo(body.persona_id)

    @app.get('/api/auth/me')
    def me(token=Depends(bearer)):
        with identity(token) as user:
            return app.state.auth.public(user)

    @app.post('/api/chat')
    def chat(body: Chat, token=Depends(bearer)):
        with identity(token) as user:
            return answer(body.query, user)

    @app.post('/api/chat/compare-roles')
    def compare(body: Chat, token=Depends(bearer)):
        with identity(token, admin=True) as user:
            results = []
            for uid in ('finance_user', 'hr_user', 'eng_user', 'general_user', 'executive_user'):
                persona = db.user(uid)
                results.append({'persona_id': uid, 'role': persona['role'], **answer(body.query, persona)})
            db.audit(user, 'compare-roles', 'allow', rule='admin-demo-comparison')
            return {'results': results}

    @app.post('/api/sql/query')
    def sql_query(body: Metric, token=Depends(bearer)):
        with identity(token) as user:
            try:
                result = SQLAnalytics(db).query(body.metric_id, user, body.period)
            except PermissionError:
                db.audit(user, 'analytics', 'deny', intercept=True, rule='metric-scope')
                raise HTTPException(403, DENIAL) from None
            except ValueError:
                raise HTTPException(400, 'Unknown analytical metric or invalid parameters') from None
            db.audit(user, 'analytics', 'allow', rule='approved-template')
            return result

    @app.get('/api/documents')
    def documents(token=Depends(bearer)):
        with identity(token) as user, db.connect() as conn:
            scopes = authorized_scopes(user)
            rows = conn.execute('SELECT d.*, (SELECT count(*) FROM chunks c WHERE c.document_id=d.id) AS chunks '
                'FROM documents d WHERE EXISTS (SELECT 1 FROM document_scopes s WHERE s.document_id=d.id '
                f'AND s.scope IN ({",".join("?" for _ in scopes)})) ORDER BY d.id', scopes).fetchall()
            result = []
            for row in rows:
                result.append({k: row[k] for k in ('id', 'title', 'pages', 'chunks')})
                result[-1]['allowed_scopes'] = [r[0] for r in conn.execute(
                    'SELECT scope FROM document_scopes WHERE document_id=? ORDER BY scope', (row['id'],))]
            return {'documents': result}

    @app.get('/api/documents/{doc_id}/chunks/{chunk_id}')
    def citation(doc_id: str, chunk_id: str, token=Depends(bearer)):
        with identity(token) as user, db.connect() as conn:
            if chunk_id not in authorized_chunk_ids(conn, user):
                raise HTTPException(404, 'Evidence unavailable')
            row = conn.execute('SELECT c.*,d.title FROM chunks c JOIN documents d ON d.id=c.document_id '
                               'WHERE c.id=? AND c.document_id=?', (chunk_id, doc_id)).fetchone()
            if not row:
                raise HTTPException(404, 'Evidence unavailable')
            return dict(row)

    @app.post('/api/documents/upload', status_code=201)
    async def upload(file: UploadFile = File(...), allowed_scopes: str = Form(...), token=Depends(bearer)):
        with identity(token, admin=True):
            pass
        if file.content_type != 'application/pdf':
            await file.close()
            raise HTTPException(400, 'Only application/pdf is accepted')
        raw = await file.read(settings.max_upload_bytes + 1)
        filename = file.filename or 'upload.pdf'
        await file.close()
        try:
            scopes = json.loads(allowed_scopes)
            if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
                raise ValueError('Invalid scopes')
            with identity(token, admin=True) as user:
                doc_id = rag.ingest(raw, Path(filename).stem, filename, scopes)
                db.audit(user, 'document-upload', 'allow', rule='admin-synthetic-lifecycle')
                return {'document_id': doc_id, 'acl_epoch': db.epoch()}
        except (ValueError, TypeError):
            raise HTTPException(400, 'Invalid PDF or scopes; check file and size limits') from None

    @app.patch('/api/documents/{doc_id}/permissions')
    def permissions(doc_id: str, body: Permissions, token=Depends(bearer)):
        with identity(token, admin=True) as user:
            try:
                rag.permissions(doc_id, body.allowed_scopes)
            except ValueError:
                raise HTTPException(400, 'Invalid scopes') from None
            except KeyError:
                raise HTTPException(404, 'Document not found') from None
            db.audit(user, 'document-permissions', 'allow', rule='admin-synthetic-lifecycle')
            return {'acl_epoch': db.epoch()}

    @app.delete('/api/documents/{doc_id}', status_code=204)
    def delete(doc_id: str, token=Depends(bearer)):
        with identity(token, admin=True) as user:
            try:
                rag.delete(doc_id)
            except KeyError:
                raise HTTPException(404, 'Document not found') from None
            db.audit(user, 'document-delete', 'allow', rule='admin-synthetic-lifecycle')
            return Response(status_code=204)

    @app.patch('/api/admin/users/{uid}/role')
    def role(uid: str, body: Role, token=Depends(bearer)):
        with identity(token, admin=True) as user:
            if uid not in PERSONAS or body.role not in ROLE_SCOPES:
                raise HTTPException(400, 'Invalid demo identity or role')
            db.change_role(uid, body.role)
            db.audit(user, 'user-role', 'allow', rule='admin-synthetic-identity')
            return {'acl_epoch': db.epoch()}

    @app.get('/api/admin/audit-logs')
    def audit_logs(limit: int = Query(default=50, ge=1, le=100), token=Depends(bearer)):
        with identity(token, admin=True), db.connect() as conn:
            return {'logs': [dict(r) for r in conn.execute('SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?', (limit,))],
                    'integrity_valid': db.verify_audit()}

    @app.get('/api/eval/benchmark')
    def benchmark(token=Depends(bearer)):
        with identity(token, admin=True) as user:
            result = evaluate(rag, db)
            db.audit(user, 'benchmark', 'allow', rule='admin-only')
            return result

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'static' / 'index.html')

    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    return app


app = create_app()
