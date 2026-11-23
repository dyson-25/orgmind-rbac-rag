"""Browser-independent contracts for the focused PDF frontend, with no app imports."""

from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if "id" in attributes:
            self.ids.append(attributes["id"])


def test_editorial_pdf_markup_and_accessibility():
    html = (STATIC / "index.html").read_text()
    parser = Markup()
    parser.feed(html)
    assert len(parser.ids) == len(set(parser.ids))
    assert "From pages." in html
    assert "possibilities." in html
    assert "Cookie access does not prove identity" in html
    assert "no accounts or account recovery" in html
    assert "sent to OpenRouter" in html
    for identifier in (
        "app-shell", "chat-panel", "pdf-files", "document-list", "chat-form",
        "query", "source-dialog", "source-passages", "source-image", "storage-notice",
        "privacy-note", "session-notice",
    ):
        assert identifier in parser.ids
    fields = [attrs for tag, attrs in parser.tags if tag == "input"]
    assert {attrs.get("type") for attrs in fields} == {"file"}
    assert all(not key.startswith("on") and key != "style" for _, attrs in parser.tags for key in attrs)
    assert all(attrs.get("src", "").startswith("/static/") for tag, attrs in parser.tags if tag == "script")
    assert all(attrs.get("href", "").startswith("/static/") for tag, attrs in parser.tags if tag == "link")
    assert any(tag == "svg" and attrs.get("aria-hidden") == "true" for tag, attrs in parser.tags)
    assert not re.search(r"Acme|SQL|benchmark|dashboard|diagnostics|roles|username", html, re.I)
    assert all("aria-label" in attrs or "aria-labelledby" in attrs for tag, attrs in parser.tags if tag == "dialog")
    js = (STATIC / "app.js").read_text()
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", js)
    assert "credentials: 'same-origin'" in js
    assert "headers['X-OrgMind-Request'] = '1'" in js
    css = (STATIC / "styles.css").read_text()
    assert "@media (max-width: 720px)" in css
    assert "prefers-reduced-motion" in css
    theme = (STATIC / "theme.js").read_text()
    assert "orgmind-theme" in theme and "prefers-color-scheme: dark" in theme
    assert "localStorage.setItem" in js
    assert "discovery-suggestions" in parser.ids and "suggestion-list" in parser.ids
    assert "START EXPLORING YOUR DOCUMENT" in html
    assert "data-theme" in css and "dot-field" in js
    assert "forced-colors" in css
    assert "@import" not in css and "url(" not in css
    assert not re.search(r"password|authenticated|/api/unlock|/api/lock|login|registration", html + js + css, re.I)
    assert set(re.findall(r"\$\('([^']+)'\)", js)).issubset(parser.ids)


HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.listeners = {}; this.attributes = {};
    this.dataset = {}; this.className = ''; this.value = ''; this.hidden = false;
    this.disabled = false; this.open = false; this._text = '';
    this.classList = {
      contains: name => this.className.split(' ').includes(name),
      toggle: (name, force) => {
        const set = new Set(this.className.split(' ').filter(Boolean));
        if (force === undefined ? !set.has(name) : force) set.add(name); else set.delete(name);
        this.className = [...set].join(' ');
      },
      add: name => this.classList.toggle(name, true),
      remove: name => this.classList.toggle(name, false),
    };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) { throw new Error('Unsafe HTML assignment: ' + value); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this._text = ''; this.children = nodes; }
  insertBefore(node, other) { this.children.splice(this.children.indexOf(other), 0, node); }
  get lastChild() { return this.children.at(-1); }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; if (name === 'src') delete this.src; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  async emit(name, extra = {}) {
    for (const fn of this.listeners[name] || []) fn({ target: this, preventDefault() {}, ...extra });
    await tick(60);
  }
  focus() { this.focused = true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}
const html = fs.readFileSync('static/index.html', 'utf8');
const elements = new Map();
for (const match of html.matchAll(/<([a-z][a-z0-9]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
  const el = new Element(match[1]);
  el.hidden = /\bhidden\b/.test(match[2]);
  el.disabled = /\bdisabled\b/.test(match[2]);
  el.className = /\bclass="([^"]*)"/.exec(match[2])?.[1] || '';
  elements.set(match[3], el);
}
const suggestions = [];
const $ = id => elements.get(id);
const descendants = el => [el, ...el.children.flatMap(descendants)];
const all = () => [...elements.values()].flatMap(descendants).concat(suggestions);
const document = {
  getElementById: $, createElement: tag => new Element(tag),
  querySelectorAll: selectors => all().filter(el => selectors.split(',').some(s => el.classList.contains(s.trim().slice(1)))),
};
const handlers = new Map();
const calls = [];
const revoked = [];
let blobNumber = 0;
const response = (data, status = 200, type = 'application/json') => ({
  ok: status >= 200 && status < 300, status,
  headers: { get: () => type }, json: async () => data, blob: async () => data,
});
const fetch = async (path, options) => {
  calls.push({ path, ...options });
  const handler = handlers.get(path) || (path.startsWith('/api/suggestions?') ? handlers.get('/api/suggestions') : undefined);
  assert.ok(handler, 'Unexpected endpoint ' + path);
  return typeof handler === 'function' ? handler(options) : response(handler);
};
const tick = async (rounds = 12) => { for (let i = 0; i < rounds; i++) await new Promise(resolve => setImmediate(resolve)); };
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const config = { name: 'OrgMind', session_ttl_days: 30, ephemeral_storage: true,
  generation_available: true, max_upload_bytes: 10485760, max_query_chars: 2000 };
handlers.set('/api/config', config);
handlers.set('/api/suggestions', { suggestions: [] });
handlers.set('/api/documents', { documents: [] });
const bad = '<img src=x onerror="globalThis.executed=true">';
const doc = { id: 'doc1', filename: bad + '.pdf', pages: 2, status: 'ready', is_sample: false };
const citation = { document_id: 'doc1', chunk_id: 'chunk1', filename: doc.filename, page: 1, excerpt: bad };
class FormData { constructor() { this.values = []; } append(...args) { this.values.push(args); } }
function boot() {
  vm.runInNewContext(fs.readFileSync('static/app.js', 'utf8'), {
    document, window: { addEventListener() {} }, fetch, FormData, DOMException, AbortController, URLSearchParams,
    setTimeout, clearTimeout, URL: { createObjectURL: () => 'blob:test-' + ++blobNumber, revokeObjectURL: value => revoked.push(value) },
  });
}
const chatSubmit = async query => { $('query').value = query; await $('query').emit('input'); await $('chat-form').emit('submit'); };
const findClass = name => document.querySelectorAll('.' + name);
"""


SCENARIOS = {
    "citation_page_grouping": r"""
const secondDoc = { ...doc, id: 'doc2', filename: doc.filename };
handlers.set('/api/documents', { documents: [doc, secondDoc] });
const citations = [citation, { ...citation, chunk_id: 'chunk2' },
  { ...citation, chunk_id: 'chunk3', page: 2 }, { ...citation, document_id: 'doc2', chunk_id: 'chunk4' }];
handlers.set('/api/chat', { answer: 'Grounded answer', citations, status: 'generated', conversation_id: 'c1' });
for (const [id, text] of [['chunk1', 'First exact passage.'], ['chunk2', 'Second exact passage.'],
  ['chunk3', 'A different page.'], ['chunk4', 'A different document.']]) {
  handlers.set(`/api/documents/${id === 'chunk4' ? 'doc2' : 'doc1'}/sources/${id}`,
    { text, filename: doc.filename, page: id === 'chunk3' ? 2 : 1 });
}
handlers.set('/api/documents/doc1/preview?page=1', () => response('one page image', 200, 'image/png'));
boot(); await tick();
await chatSubmit('What supports the answer?');
assert.equal(findClass('citation').length, 3);
assert.ok(findClass('citation').some(button => button.textContent.includes('2 supporting passages')));
await findClass('citation')[0].emit('click');
assert.ok($('source-passages').textContent.includes('First exact passage.'));
assert.ok($('source-passages').textContent.includes('Second exact passage.'));
assert.equal(calls.filter(call => call.path === '/api/documents/doc1/preview?page=1').length, 1);
assert.deepEqual(calls.filter(call => call.path.includes('/sources/')).map(call => call.path),
  ['/api/documents/doc1/sources/chunk1', '/api/documents/doc1/sources/chunk2']);
""",
    "safe_sources_and_removal_races": r"""
handlers.set('/api/documents', { documents: [doc] });
handlers.set('/api/chat', { answer: bad, citations: [citation], status: 'generated', conversation_id: 'c1' });
handlers.set('/api/documents/doc1/sources/chunk1', { text: bad, filename: doc.filename, page: 1 });
handlers.set('/api/documents/doc1/preview?page=1', () => response('png', 200, 'image/png'));
handlers.set('/api/documents/doc1', { deleted: true });
boot(); await tick();
assert.equal($('app-shell').hidden, false);
assert.equal($('app-shell').classList.contains('is-empty'), false);
assert.equal($('chat-panel').hidden, false);
assert.equal($('storage-notice').hidden, false);
assert.ok($('session-notice').textContent.includes('30 days'));
assert.equal($('discovery-suggestions').hidden, true);
assert.ok($('document-list').textContent.includes(bad));
await chatSubmit('What does this PDF say?');
assert.ok($('chat-messages').textContent.includes(bad));
assert.equal(findClass('citation').length, 1);
await findClass('citation')[0].emit('click');
assert.equal($('source-passages').textContent, 'Supporting passage 1' + bad);
assert.equal($('source-image').src, 'blob:test-1');
await $('close-source').emit('click');
assert.deepEqual(revoked, ['blob:test-1']);
assert.equal($('source-dialog').open, false);
const pendingPreview = deferred();
handlers.set('/api/documents/doc1/preview?page=1', () => pendingPreview.promise);
await findClass('citation')[0].emit('click');
const pendingChat = deferred();
handlers.set('/api/chat', () => pendingChat.promise);
await chatSubmit('Second question');
assert.equal($('chat-progress').hidden, false);
await findClass('remove-document')[0].emit('click');
pendingChat.resolve(response({ answer: 'STALE ANSWER', citations: [citation], status: 'generated', conversation_id: 'c2' }));
pendingPreview.resolve(response('png', 200, 'image/png')); await tick();
assert.equal($('chat-messages').children.length, 0);
assert.equal($('source-image').src, undefined);
assert.equal(blobNumber, 1);
assert.equal($('document-count').textContent, '0');
assert.equal($('app-shell').classList.contains('is-empty'), true);
assert.equal($('chat-panel').hidden, true);
assert.equal($('source-dialog').open, false);
assert.ok(calls.every(call => call.credentials === 'same-origin'));
assert.ok(calls.filter(call => call.method !== 'GET').every(call => call.headers['X-OrgMind-Request'] === '1'));
""",
    "sample_gating_and_provider_failure": r"""
handlers.set('/api/documents/sample', { ...doc, is_sample: true });
handlers.set('/api/suggestions', { suggestions: [{ question: 'What does this sample guide explain?', document_ids: ['doc1'] }] });
handlers.set('/api/chat', () => response({ detail: 'Provider unavailable: ' + bad }, 503));
handlers.set('/api/conversations/clear', { cleared: true });
boot(); await tick();
assert.equal($('discovery-suggestions').hidden, true);
assert.equal($('query').disabled, true);
await $('load-sample').emit('click');
assert.equal($('discovery-suggestions').hidden, false);
assert.equal($('load-sample').hidden, true);
assert.equal($('query').disabled, false);
await findClass('suggestion')[0].emit('click');
assert.ok($('chat-messages').textContent.includes('No answer was generated.'));
assert.ok($('chat-messages').textContent.includes(bad));
assert.ok($('query').value.includes('What does this sample guide explain?'));
assert.equal(findClass('citation').length, 0);
assert.equal(findClass('assistant').length, 0);
await $('clear-conversation').emit('click');
assert.equal($('chat-messages').children.length, 0);
const check = findClass('document-check')[0]; check.checked = false; await check.emit('change');
assert.equal($('discovery-suggestions').hidden, true);
assert.equal($('query').disabled, true);
assert.equal(findClass('document-check')[0].focused, true);
""",
    "public_workspace_and_expired_conversation": r"""
handlers.set('/api/documents', { documents: [doc] });
handlers.set('/api/chat', { answer: 'First answer', citations: [], status: 'generated', conversation_id: 'c1' });
boot(); await tick();
assert.equal($('app-shell').hidden, false);
assert.equal(calls.some(call => call.path === '/api/documents'), true);
assert.equal($('query').disabled, false);
await chatSubmit('First question');
handlers.set('/api/chat', () => response({ detail: 'Conversation expired' }, 404));
await chatSubmit('Follow-up question');
assert.ok($('global-status').textContent.includes('expired'));
assert.equal($('query').value, 'Follow-up question');
assert.equal(findClass('assistant').length, 1);
assert.equal(calls.filter(call => call.path === '/api/chat').length, 2);
assert.equal(calls.filter(call => call.path === '/api/documents').length, 2);
handlers.set('/api/chat', { answer: 'Fresh answer', citations: [], status: 'generated', conversation_id: 'c2' });
await chatSubmit($('query').value);
const requests = calls.filter(call => call.path === '/api/chat').map(call => JSON.parse(call.body));
assert.equal(requests[1].conversation_id, 'c1');
assert.equal(requests[2].conversation_id, null);
assert.equal(findClass('assistant').length, 2);
""",
    "sequential_uploads_and_clear_request": r"""
let active = 0; let maximum = 0; let count = 0;
handlers.set('/api/documents/upload', async () => {
  maximum = Math.max(maximum, ++active); await tick(); active--; count++;
  return response({ ...doc, id: 'upload' + count, filename: 'file' + count + '.pdf' });
});
handlers.set('/api/chat', { answer: 'Answer', citations: [], status: 'generated', conversation_id: 'conversation1' });
handlers.set('/api/conversations/clear', { cleared: true });
boot(); await tick();
$('pdf-files').files = [{ name: 'one.pdf', size: 20 }, { name: 'two.pdf', size: 20 }, { name: 'not-pdf.txt', size: 20 }];
await $('pdf-files').emit('change');
assert.equal(maximum, 1);
assert.equal(count, 2);
assert.equal($('document-count').textContent, '2');
assert.ok($('upload-status').textContent.startsWith('2 PDFs in your library.'));
assert.ok($('upload-status').textContent.includes('not-pdf.txt: choose a PDF file.'));
await chatSubmit('Question');
assert.deepEqual(JSON.parse(calls.find(call => call.path === '/api/chat').body).document_ids, ['upload1', 'upload2']);
await $('clear-conversation').emit('click');
assert.deepEqual(JSON.parse(calls.find(call => call.path === '/api/conversations/clear').body), { conversation_id: 'conversation1' });
assert.equal($('chat-messages').children.length, 0);
""",
    "initial_upload_state_and_generation_unavailable": r"""
config.generation_available = false;
boot(); await tick();
assert.equal($('app-shell').hidden, false);
assert.equal($('app-shell').classList.contains('is-empty'), true);
assert.equal($('chat-panel').hidden, true);
assert.equal($('pdf-files').disabled, false);
assert.equal($('generation-notice').hidden, false);
assert.ok($('session-notice').textContent.includes('does not prove identity'));
handlers.set('/api/documents/sample', { ...doc, is_sample: true });
handlers.set('/api/suggestions', { suggestions: [{ question: 'What does this sample guide explain?', document_ids: ['doc1'] }] });
await $('load-sample').emit('click');
assert.equal($('app-shell').classList.contains('is-empty'), false);
assert.equal($('chat-panel').hidden, false);
assert.equal($('query').disabled, true);
assert.ok(suggestions.every(button => button.disabled));
await chatSubmit('Do not send');
assert.equal(calls.some(call => call.path === '/api/chat'), false);
""",
    "startup_retry_and_failed_library": r"""
handlers.set('/api/config', () => { throw new TypeError('Network unavailable'); });
boot(); await tick();
assert.equal($('app-shell').hidden, true);
assert.equal($('retry-config').hidden, false);
assert.equal($('pdf-files').disabled, true);
handlers.set('/api/config', config);
handlers.set('/api/documents', () => response({ detail: 'Workspace unavailable' }, 503));
await $('retry-config').emit('click');
assert.equal($('app-shell').hidden, true);
assert.equal($('pdf-files').disabled, true);
assert.equal($('retry-config').hidden, false);
handlers.set('/api/documents', { documents: [] });
await $('retry-config').emit('click');
assert.equal($('app-shell').hidden, false);
assert.equal($('pdf-files').disabled, false);
assert.equal($('retry-config').hidden, true);
""",
    "keyboard_followups_and_insufficient_evidence": r"""
handlers.set('/api/documents', { documents: [doc] });
handlers.set('/api/chat', { answer: 'Not found in these documents.', citations: [], status: 'insufficient_evidence', conversation_id: 'c1' });
boot(); await tick();
$('query').value = 'Question';
await $('query').emit('keydown', { key: 'Enter', shiftKey: true });
await $('query').emit('keydown', { key: 'Enter', isComposing: true });
assert.equal(calls.some(call => call.path === '/api/chat'), false);
await $('query').emit('keydown', { key: 'Enter' });
assert.ok($('chat-messages').textContent.includes('Not enough evidence'));
assert.equal(findClass('citation').length, 0);
$('query').value = 'Follow-up';
await $('query').emit('keydown', { key: 'Enter' });
assert.equal(JSON.parse(calls.filter(call => call.path === '/api/chat').at(-1).body).conversation_id, 'c1');
assert.equal($('query').focused, true);
config.max_query_chars = 5;
await chatSubmit('Too long');
assert.equal(calls.filter(call => call.path === '/api/chat').length, 2);
assert.ok($('global-status').textContent.includes('under 5'));
""",
    "expired_workspace_and_already_removed_document": r"""
handlers.set('/api/documents', { documents: [doc] });
handlers.set('/api/chat', () => response({ detail: 'Document unavailable' }, 404));
boot(); await tick();
handlers.set('/api/documents', { documents: [] });
await chatSubmit('Question');
assert.equal($('document-list').children.length, 0);
assert.equal($('chat-panel').hidden, true);
assert.equal($('query').disabled, true);
assert.ok($('global-status').textContent.includes('expired'));
handlers.set('/api/documents/sample', { ...doc, is_sample: true });
handlers.set('/api/suggestions', { suggestions: [{ question: 'What does this sample guide explain?', document_ids: ['doc1'] }] });
await $('load-sample').emit('click');
handlers.set('/api/documents/doc1', () => response({ detail: 'Document unavailable' }, 404));
await findClass('remove-document')[0].emit('click');
assert.equal($('document-list').children.length, 0);
assert.ok($('global-status').textContent.includes('refreshed'));
""",
    "drop_validation_and_expired_clear": r"""
handlers.set('/api/documents/upload', { ...doc, filename: 'valid.pdf' });
handlers.set('/api/chat', { answer: 'Answer', citations: [], status: 'generated', conversation_id: 'c1' });
handlers.set('/api/conversations/clear', () => response({ detail: 'Already expired' }, 404));
boot(); await tick();
await $('drop-zone').emit('dragover');
assert.equal($('drop-zone').classList.contains('dragging'), true);
await $('drop-zone').emit('drop', { dataTransfer: { files: [
  { name: 'huge.pdf', size: config.max_upload_bytes + 1 }, { name: 'valid.pdf', size: 20 },
] } });
assert.equal($('drop-zone').classList.contains('dragging'), false);
assert.equal(calls.filter(call => call.path === '/api/documents/upload').length, 1);
assert.ok($('upload-status').textContent.includes('huge.pdf: exceeds'));
assert.equal($('query').focused, true);
await chatSubmit('Question');
await $('clear-conversation').emit('click');
assert.equal($('chat-messages').children.length, 0);
assert.ok($('global-status').textContent.includes('already unavailable'));
assert.equal($('query').disabled, false);
""",
    "provider_errors_never_create_answers": r"""
handlers.set('/api/documents', { documents: [doc] });
boot(); await tick();
for (const code of [400, 401, 403, 429, 500, 502, 503]) {
  handlers.set('/api/chat', () => response({ detail: 'Provider error ' + code }, code));
  await chatSubmit('Retryable question');
  assert.equal(findClass('assistant').length, 0);
  assert.equal(findClass('citation').length, 0);
  assert.equal($('query').value, 'Retryable question');
  assert.ok($('global-status').textContent.includes('Provider error ' + code));
  assert.equal($('query').disabled, false);
  assert.equal($('chat-progress').hidden, true);
}
""",
    "preview_failure_and_modal_escape": r"""
handlers.set('/api/documents', { documents: [doc] });
handlers.set('/api/chat', { answer: 'Answer', citations: [citation], status: 'generated', conversation_id: 'c1' });
handlers.set('/api/documents/doc1/sources/chunk1', { text: 'Evidence remains readable', filename: doc.filename, page: 1 });
handlers.set('/api/documents/doc1/preview?page=1', () => response('html', 200, 'text/html'));
boot(); await tick(); await chatSubmit('Question');
await findClass('citation')[0].emit('click');
assert.equal($('source-passages').textContent, 'Supporting passage 1Evidence remains readable');
assert.equal($('source-image').hidden, true);
assert.ok($('preview-status').textContent.includes('Preview unavailable'));
assert.equal(blobNumber, 0);
await $('source-dialog').emit('cancel');
assert.equal($('source-dialog').open, false);
assert.equal($('source-passages').textContent, '');
handlers.set('/api/documents/doc1/sources/chunk1', () => response({ detail: 'This source has expired.' }, 404));
await findClass('citation')[0].emit('click');
assert.ok($('source-status').textContent.includes('expired'));
assert.equal($('source-image').hidden, true);
""",
    "source_selection_race": r"""
handlers.set('/api/documents', { documents: [doc] });
handlers.set('/api/chat', { answer: 'Answer', citations: [citation], status: 'generated', conversation_id: 'c1' });
const pending = deferred();
handlers.set('/api/documents/doc1/sources/chunk1', () => pending.promise);
boot(); await tick(); await chatSubmit('Question');
await findClass('citation')[0].emit('click');
await $('close-source').emit('click');
pending.resolve(response({ text: 'STALE SOURCE', filename: doc.filename, page: 1 })); await tick();
assert.equal($('source-passages').textContent, '');
assert.equal(calls.some(call => call.path.includes('/preview')), false);
""",
}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_frontend_runtime_contracts(scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the browser-independent DOM harness")
    program = HARNESS + "\n(async () => {\n" + SCENARIOS[scenario] + "\n})().catch(error => { console.error(error); process.exitCode = 1; });"
    result = subprocess.run(
        [node, "-e", program], cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
