# OrgMind · PDF Knowledge Assistant

Upload a PDF, ask a question, and inspect the original page behind each cited answer. OrgMind is a passwordless, public PDF assistant with isolated anonymous browser workspaces: FastAPI, SQLite, local hybrid retrieval, OpenRouter generation, and an original HTML/CSS/JavaScript knowledge studio.

The library starts empty. **Try sample PDF** optionally loads one original, four-page *Personal Finance and Budgeting Guide* through the same ingestion path as an upload. Nothing is seeded automatically.

## Run locally

Python 3.12 is the tested runtime. From this project directory:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Do not overwrite an existing private `.env`. Set these values in that file:

```dotenv
APP_ENV=development
DATA_DIR=data
GENERATION_PROVIDER=openai
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_MODEL=baidu/ernie-5.5-flash-preview:free
OPENAI_API_KEY=your-private-openrouter-key
```

Get a key from your own OpenRouter account. Never place it in browser code, screenshots, commits, or public configuration. No paid provider is needed or configured by this repository. Free-model availability and quotas are controlled by OpenRouter and may change.

Start the application:

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Open <http://127.0.0.1:8000>. The existing **OrgMind: serve** VS Code task runs the same application. No account or password is required in any mode. Keep development mode bound to loopback; use production mode and HTTPS for public access.

Without a key, PDF upload, extraction, retrieval, and previews still work, but answer generation reports that the provider is unavailable. `GENERATION_PROVIDER=local` explicitly disables remote generation; it does **not** substitute copied passages for an AI answer. Insufficient evidence is a separate, explicit response.

## Use the assistant

1. Upload one or more text-based PDFs, or click **Try sample PDF**.
2. Wait until the document is indexed. Files are uploaded sequentially; identical bytes reuse an existing indexed document only within your workspace.
3. Select the PDFs that may supply evidence, then ask a natural-language question.
4. Open an answer's citation to read the exact retrieved passage and view its original PDF page.
5. Ask a follow-up or clear the conversation to begin again.
6. Remove a document to delete its stored original, chunks, and search-index entries.

The default maximum is **5 MiB per PDF**, **50 documents per workspace** (**500 service-wide**), **100 pages per PDF**, **1,000,000 extracted characters per PDF**, and **2,000 characters per question**. Image-only scans, encrypted files, invalid PDFs, and files without extractable text are rejected; OCR is not included. Limits with environment controls are listed in [.env.example](.env.example).

Sample questions:

- How is the $3,000 monthly income divided?
- What are the three- and six-month emergency reserve targets?
- How long does it take to build the smaller reserve at $600 per month?
- What changes if $50 per month goes toward the annual insurance bill?

The sample cannot answer tax-bracket or stock-selection questions. Its editable source is [samples/personal-finance-guide.md](samples/personal-finance-guide.md). Regenerate the single PDF after editing it:

```bash
.venv/bin/python samples/build_sample.py
```

## How RAG works here

1. **Extract:** PyMuPDF reads text separately from each PDF page. Original bytes remain on local storage for previews.
2. **Chunk:** text is divided into overlapping, page-preserving passages (900 characters with 150-character overlap by default). Every chunk retains its document ID and 1-based page number.
3. **Retrieve:** SQLite FTS5/BM25 and local TF-IDF/LSA produce candidates. Reciprocal rank fusion combines rankings; lexical/relevance guards discard weak matches. The corpus, vocabulary, statistics, and rankings contain only the current workspace's selected documents. At most five unique chunks are returned; summaries choose representative passages across eligible pages/documents. No embedding API or external vector database is used.
4. **Generate:** recent user questions help resolve follow-ups. Current retrieved passages—not previous assistant answers—are the evidence. The server calls OpenRouter's `/chat/completions` endpoint with strict `response_format.type=json_schema`: an object with exactly `answer` and `citation_ids`, both required, `additionalProperties=false`. Routing sets `provider.require_parameters=true`; `max_tokens` bounds output. Refusals, truncation, empty content, invalid JSON/schema, and unknown citations have distinct errors. There are no tool calls, automatic retries, or alternative-provider fallbacks.
5. **Validate citations:** returned source IDs must belong to the retrieved context. The server supplies document, filename, page, and excerpt from stored evidence; model-generated citation metadata is not trusted. Unknown IDs or malformed provider output cause a clear error.

Citation validation establishes **source provenance**, not mathematical proof that every generated statement follows from the passage. Inspect sources for important decisions. Suspicious instructions in retrieved content are quarantined heuristically; this is not a guarantee against every prompt-injection technique. If evidence is insufficient, the response says so instead of guessing.

The question, bounded earlier user questions, filenames, and selected retrieved text are sent to OpenRouter. Original PDF files and unrelated library documents are not uploaded to the provider. Only use documents you have permission to process and send to that service.

## Storage and existing installations

`DATA_DIR` contains `runtime/orgmind.sqlite3` and the `uploads/` directory. Preserve **both** to retain documents and previews. Normal local restarts rebuild the in-memory retrieval index from SQLite. In-progress uploads/deletions are recovered on startup. Anonymous session ownership is durable in SQLite. The browser holds a cryptographically random HttpOnly cookie; SQLite stores only its hash and a sliding expiry (30 days by default). Reloads and ordinary restarts retain the same workspace while that cookie and storage remain valid. Losing/clearing the cookie or reaching expiry loses access; there is no account recovery. Treat the cookie like an access credential and do not share it. Browser conversation history is bounded server memory (256 active workspaces, eight conversations per workspace, 12 messages per conversation), not durable history. A restart or cache eviction expires conversations without deleting documents. Deleting a document clears only that workspace's conversation context.

Schema 4 migration preserves existing shared/unowned documents in quarantine: they are never assigned to the first visitor and never appear in public workspace searches. Pending legacy deletions are cancelled. Expired workspaces' documents, chunks, and originals are retained, not silently deleted; only expired empty session records are cleaned. Retained documents count toward the global storage cap. There is no automatic recovery or retention-purge interface: operators must plan backups and deliberate retention management. Session records are capped at 10,000.

An existing recognized database is migrated transactionally. Before migration, the application creates a restricted-permission `orgmind.sqlite3.legacy-<id>.bak` beside the database. Nonseed documents and chunks are retained, and original files are not deleted by migration. Only exact recognized synthetic seed identities with matching original-file digests are excluded from the new library. Ambiguous records are preserved; unrecognized schemas are rejected without rewriting them. Old records whose originals were never stored can retain searchable text but cannot provide an original-page preview.

Before upgrading an installation with important data, stop the old process and back up the entire `DATA_DIR`. Protect migration backups because they contain the previous database. Do not run the old and new applications concurrently against the same database.

## Hosted access and deployment

Hosted mode is **public and passwordless**. Every visitor gets an isolated anonymous workspace. Ownership is checked for listing, upload/deduplication, retrieval/chat selections, source excerpts, page previews, and deletion. Knowing another document or conversation ID does not grant access. Anonymous access is not verified identity or a substitute for sensitive-data governance.

Set `APP_ENV=production` and serve over HTTPS: cookies are Secure, HttpOnly, and SameSite=Strict. The browser sends a same-origin mutation header; the server rejects foreign origins. Basic rolling-minute limits cap new workspace issuance (10/IP), uploads (5/workspace and 10/IP), and generation (30/workspace, 30/IP, and 30 service-wide by default). Active counters are never evicted to admit new clients. Only the socket client address is read by the application; configure the ASGI server to trust forwarded addresses solely from your known reverse proxy, never arbitrary clients. IP limits may be shared by visitors behind NAT. These are modest, restart-reset single-process safeguards, not distributed abuse prevention. Run **one Uvicorn worker**.

[Dockerfile](Dockerfile) packages the application and optional sample, runs as a nonroot user, and checks `/api/health`. It does not include your `.env`, database, or uploads. To build:

```bash
docker build -t orgmind-pdf .
```

For a loopback-only local container using the private `.env` and persistent named volume:

```bash
docker run --rm -p 127.0.0.1:8000:8000 --env-file .env \
  -e APP_ENV=development -e DATA_DIR=/app/data \
  -v orgmind-data:/app/data orgmind-pdf
```

For hosting, use production mode and HTTPS instead of the development override. The existing [render.yaml](render.yaml) service name is preserved. Configure only `OPENAI_API_KEY` as the provider secret in the existing Render service; remove the obsolete `APP_PASSWORD` setting and keep the specified OpenRouter URL/model and one worker. The application reads Render's `PORT`. Updating these files does not deploy anything.

**Render Free storage is ephemeral.** SQLite and PDFs can disappear on redeploy, restart, or service replacement; they are not durable backups. The blueprint sets `EPHEMERAL_STORAGE=true` so the interface warns users. Keep original PDFs elsewhere and re-upload after storage loss. Local storage survives ordinary restarts only while the same data directory or volume is retained.

## Environment inventory

Only `OPENAI_API_KEY` is required **for generated answers**; it is not required to start or inspect PDFs. Other settings have defaults. [.env.example](.env.example) contains the complete application configuration without credentials.

| Variable | Default | Purpose / valid range |
|---|---|---|
| `OPENAI_API_KEY` | empty | Private OpenRouter key; never exposed to the browser |
| `OPENAI_BASE_URL` | `https://openrouter.ai/api/v1` | Pinned endpoint, not a generic provider switch |
| `OPENAI_MODEL` | `baidu/ernie-5.5-flash-preview:free` | Free model; must support strict structured output |
| `GENERATION_PROVIDER` | `openai` | `local` explicitly disables generation |
| `GENERATION_TIMEOUT_SECONDS` | `30` | 1–120 seconds |
| `GENERATION_MAX_TOKENS` | `3072` | 128–8192 output tokens |
| `APP_ENV` | `development` | `development`, `production`, or `test`; production enables Secure cookies |
| `DATA_DIR` | `data` | SQLite, original files, and migration backups |
| `EPHEMERAL_STORAGE` | `false` | `true` displays storage-loss warning |
| `SESSION_TTL_DAYS` | `30` | Sliding anonymous access lifetime, 1–90 days |
| `MAX_UPLOAD_BYTES` | `5242880` | 1 byte–20 MiB per PDF |
| `MAX_DOCUMENTS` | `50` | 1–100 per workspace |
| `MAX_TOTAL_DOCUMENTS` | `500` | 1–5000 service-wide, including retained documents |
| `MAX_QUERY_CHARS` | `2000` | 1–4000 characters |
| `REQUESTS_PER_MINUTE` | `30` | Generation per IP and per workspace, 1–120 |
| `GLOBAL_REQUESTS_PER_MINUTE` | `30` | Service-wide generation, 1–120 |
| `UPLOAD_REQUESTS_PER_MINUTE` | `5` | Upload/sample attempts per workspace, 1–120 |
| `IP_UPLOAD_REQUESTS_PER_MINUTE` | `10` | Upload/sample attempts per IP, 1–120 |
| `CHUNK_SIZE` | `900` | 100–4000 characters |
| `CHUNK_OVERLAP` | `150` | Nonnegative and smaller than chunk size |
| `PORT` | `8000` in Docker | Container launch port; local Uvicorn uses `--port` |

`APP_PASSWORD` is obsolete and ignored; remove it from private/hosted settings. `DEMO_ADMIN_USERNAME`, `DEMO_ADMIN_PASSWORD`, `DEMO_EMPLOYEE_USERNAME`, `DEMO_EMPLOYEE_PASSWORD`, `ALLOW_DEMO_PERSONA`, `JWT_SECRET`, and `EMBEDDING_PROVIDER` are obsolete: the remaining application does not read them. Other enterprise database, SQL-agent, and benchmark configuration is likewise no longer part of this product. Do not add old variables to the new environment. Runtime/container-only `TMPDIR`, `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, `PIP_NO_CACHE_DIR`, `OMP_NUM_THREADS`, and `OPENBLAS_NUM_THREADS` are packaging controls, not application secrets or user-facing requirements.

## Tests

```bash
.venv/bin/python -m pytest -v -W error
node --check static/app.js
git diff --check
```

The focused suite covers startup, optional sample loading, PDF validation/extraction/chunking, hybrid retrieval, selected-source filtering, OpenRouter requests/responses/errors, source provenance, follow-ups, deletion, restart persistence, legacy migration/backups, passwordless production access, persistent session isolation, request/storage limits, representative summaries, numerical evidence, and frontend accessibility contracts. Obsolete tests for the previous product scope have been replaced intentionally. Automated provider tests use mocked HTTP, an isolated temporary data directory, and explicit test configuration; they do not consume real credentials or provider quotas.

Provider authentication, quota, model, timeout, malformed-response, and service errors are surfaced honestly. A healthy application or a mocked test does not establish that a real OpenRouter request succeeds.

## Code map

- [app/main.py](app/main.py): public API, persistent anonymous access, resource limits, page previews, and static files.
- [app/database.py](app/database.py): document/chunk schema and conservative migration.
- [app/rag_engine.py](app/rag_engine.py): durable ingestion/deletion and local hybrid retrieval.
- [app/llm_service.py](app/llm_service.py): grounded OpenRouter generation and source validation.
- [app/config.py](app/config.py), [app/http_limits.py](app/http_limits.py): settings and bounded request bodies.
- [static/](static/): dependency-free document/chat interface.
- [samples/](samples/): exactly one optional PDF, editable source, and generator.
- [tests/](tests/): offline focused regression suite.
