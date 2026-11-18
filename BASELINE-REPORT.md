# OrgMind productization baseline

## Verified starting point

- Existing commit: `8781513`; clean working tree before productization. Existing origin and Render service remain unchanged.
- Exact command `.venv/bin/python -m pytest -v`: **285 passed in 45.36s, no warnings**.
- Local and public Render health endpoints returned `status: ok`. This verifies readiness, not a successful Gemini answer.
- Existing on-disk SQLite schema inspected read-only: six users, nine documents; users contain identity/role/password hash/token version, documents contain id/title/filename/pages/metadata/digest. No owner, department, active flag, processing state or conversation model exists.
- Docker daemon access fails with permission denied at `/var/run/docker.sock`; image build/container execution are not verified.

## Working, partial and missing capabilities

| Area | Observed baseline |
|---|---|
| Authentication | Real Argon2 credential verification and signed, expiring JWTs; roles/token versions reloaded on each request. Public demo-persona endpoint is explicitly configurable, but the normal UI foregrounds passwordless role selection. |
| Accounts | Six synthetic identities only. Local password provisioning works. No administrator account creation, names, email, departments or activation lifecycle. |
| Authorization | Central scope filtering before sparse/dense ranking, citation provenance checks, role revocation and scoped SQL. No private ownership model for employee uploads. |
| PDF ingestion | Administrator upload validates/extracts/chunks/indexes PDFs synchronously. Duplicate content replaces its digest-based document and ACL. Employees cannot upload; original uploaded bytes are not preserved; no lifecycle states. |
| Retrieval | BM25, TF-IDF/LSA and hybrid RRF work. Broad lexical matching and dense candidates can include unrelated sources. DOC_09 is a deliberate injection fixture but appears in normal listings/retrieval. |
| Generation | Deterministic top-three sentence extraction works and quarantines suspicious context. It can concatenate unrelated sentences. HTTP adapters exist, but failures collapse into a Boolean and hide actionable status/category. No conversation history. |
| Citations | Backend checks actual chunk/document/page/title. Existing UI inspects indexed text only, not original PDFs. External plain-text responses attach supplied context citations rather than model-selected source references. |
| Interface | Five functional tools, cancellation/stale-response protection, SQL tables, audit, live comparator and benchmark. No public product landing, separate account experiences, user/department management or employee document workflow. |
| Operations | HMAC-chained audit, request-size guard, CSP, nonroot Docker configuration and existing Render Blueprint. Render free storage is ephemeral; no durable-data guarantee. |

## Gemini findings and limits

The supplied OpenAI-compatible base URL and endpoint construction are correct: trailing slashes are normalized and `chat/completions` is appended. Bearer authentication is already implemented. Official Google documentation confirms the stable model identifier `gemini-3.8-flash`; it must not be replaced on the unsupported assumption that it is nonexistent.

The adapter always sends `temperature: 0`, although current Gemini guidance says newer models ignore custom sampling parameters and may reject them in future. Its default 15-second timeout may also be inadequate for some requests. These are repairable compatibility/diagnostic concerns, **not evidence of the actual remote failure**.

The decisive observed application defect is missing safe observability: HTTP status, sanitized category, model and successful/fallback outcome are not exposed. All provider exceptions are swallowed into extractive fallback. The Render key and current remote environment have not been inspected; the precise Google-side rejection remains **unknown**. A real API response using an authorized key is required to distinguish authentication, permission, quota, unavailable model, timeout and service errors. No real Gemini success is claimed.

Official references consulted:
- https://aistudio.google.com/docs/openai
- https://aistudio.google.com/docs/models/gemini-3.8-flash
- https://aistudio.google.com/docs/troubleshooting

## Compatibility gates

Retain all 285 tests unchanged and add coverage. Preserve fixed benchmark cases/pages, legacy diagnostic APIs and deterministic benchmark generation. Extend the existing SQLite schema with idempotent migrations and named-column inserts. Preserve legacy administrator upload semantics while providing a separately authorized ownership workflow. Keep private uploads isolated from scope-shared documents. Normal knowledge views must exclude DOC_09 without deleting its security fixture or changing benchmark truth.

Baseline fixed-corpus results from the preceding verified polish:

| Method | Recall@5 | MRR | Citation accuracy | Evidence precision | Unauthorized retrieval |
|---|---:|---:|---:|---:|---:|
| BM25 | 1.0 | 1.0 | 0.9375 | 0.3333333333 | 0.0 |
| LSA | 1.0 | 0.96875 | 0.9375 | 0.475 | 0.0 |
| Hybrid RRF | 1.0 | 0.96875 | 0.9375 | 0.3333333333 | 0.0 |

Citation accuracy is 15/16 answered positives, not full answer faithfulness. The suspicious-email case remains an honest miss. Recompute after implementation; do not tune labels or scores.
