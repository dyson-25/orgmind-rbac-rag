'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const state = { token: null, user: null, config: null, generation: 0, tasks: new Map(), tab: 'assistant', selected: new Set(), conversation: null, documents: [], urls: new Set(), preview: null, roles: [], departments: [] };
  const tabNames = { overview: 'Overview', assistant: 'Knowledge Workspace', documents: 'Documents', users: 'Users & roles', departments: 'Departments', sql: 'Organization analytics', provider: 'AI status', audit: 'Audit trail', compare: 'Diagnostics · Role comparator', benchmark: 'Diagnostics · Benchmark', profile: 'My account' };
  let elementId = 0;
  const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
  const percent = new Intl.NumberFormat(undefined, { style: 'percent', maximumFractionDigits: 2 });
  const node = (tag, text, className) => {
    const result = document.createElement(tag);
    if (text !== undefined && text !== null) result.textContent = String(text);
    if (className) result.className = className;
    return result;
  };
  const empty = (id, text) => $(id).replaceChildren(node('p', text, 'empty-state'));
  function status(message, error = false) {
    $('global-status').textContent = message;
    $('global-status').classList.toggle('error', error);
    if ($('login-dialog').open) $('login-status').textContent = message;
  }
  function abortTask(key) {
    const task = state.tasks.get(key);
    if (!task) return;
    task.controller.abort();
    if (task.button) task.button.disabled = !state.user && task.button.hasAttribute('data-auth');
    if (task.target) task.target.removeAttribute('aria-busy');
    state.tasks.delete(key);
  }
  async function work(key, button, target, pending, execute, onError) {
    if (key === 'mutation' && state.tasks.has(key)) { status('A change is in progress. Please wait.'); return; }
    abortTask(key);
    const generation = state.generation;
    const token = state.token;
    const task = { controller: new AbortController(), button, target };
    const current = () => (key === 'config' || generation === state.generation) && state.tasks.get(key) === task && !task.controller.signal.aborted;
    state.tasks.set(key, task);
    if (button) button.disabled = true;
    if (target) target.setAttribute('aria-busy', 'true');
    status(pending);
    const request = async (path, { method = 'GET', body, publicRequest = false, blob = false } = {}) => {
      const headers = { Accept: blob ? 'application/pdf, image/png' : 'application/json' };
      if (!publicRequest) {
        if (!token) throw new Error('Sign in to continue.');
        headers.Authorization = `Bearer ${token}`;
      }
      const isForm = body instanceof FormData;
      if (body !== undefined && !isForm) headers['Content-Type'] = 'application/json';
      const response = await fetch(path, { method, headers, body: body === undefined ? undefined : (isForm ? body : JSON.stringify(body)), signal: task.controller.signal, cache: 'no-store', credentials: 'omit', redirect: 'error' });
      if (!current()) throw new DOMException('Superseded request', 'AbortError');
      if (response.status === 401 && !publicRequest) {
        resetSession('Session expired or identity changed. Sign in again.');
        throw new DOMException('Session ended', 'AbortError');
      }
      let data = null;
      if (response.status !== 204) {
        if (blob && response.ok) {
          const type = response.headers.get('Content-Type')?.split(';')[0];
          if (!['image/png', 'application/pdf'].includes(type)) throw new Error('The server returned an unexpected file type.');
          data = await response.blob();
        } else {
          try { data = await response.json(); } catch { /* Never display raw server HTML. */ }
        }
      }
      if (!current()) throw new DOMException('Superseded request', 'AbortError');
      if (!response.ok) {
        const detail = typeof data?.detail === 'string' && response.status < 500 ? data.detail : null;
        throw new Error(detail || (response.status >= 500 ? 'The server could not complete this operation. Please retry.' : `Request failed (${response.status}). Check your inputs and access permissions.`));
      }
      if (response.status !== 204 && data === null) throw new Error('The server returned an invalid response.');
      return data;
    };
    try { await execute(request); }
    catch (error) {
      if (current() && error.name !== 'AbortError') {
        const message = error instanceof TypeError ? 'Unable to reach the API. Check the connection and retry.' : error.message;
        status(message, true);
        if (onError) onError(message);
      }
    } finally {
      if (current()) {
        state.tasks.delete(key);
        if (button) button.disabled = !state.user && button.hasAttribute('data-auth');
        if (target) target.removeAttribute('aria-busy');
      }
    }
  }
  function authenticated(admin = false) {
    if (!state.user || (admin && state.user.role !== 'admin')) { status(admin ? 'Administrator access is required.' : 'Sign in to continue.', true); return false; }
    return true;
  }
  function clearChat() {
    abortTask('chat'); abortTask('conversation');
    state.conversation = null;
    $('chat-messages').replaceChildren();
    $('chat-empty').hidden = false;
    $('chat-form').reset();
    closeEvidence();
  }
  function resetSession(message) {
    state.generation += 1;
    for (const key of [...state.tasks.keys()]) if (key !== 'config') abortTask(key);
    state.token = null; state.user = null; state.documents = []; state.selected.clear(); state.roles = []; state.departments = [];
    clearChat();
    for (const id of ['document-list','conversation-list','overview-results','users-results','roles-results','departments-results','provider-results','audit-results','profile-results','compare-results','sql-results','benchmark-results','user-editor','department-editor']) $(id).replaceChildren();
    for (const form of document.querySelectorAll('form')) form.reset();
    $('login-dialog').close();
    updateSession(); updateSelection();
    status(message);
  }
  function updateSession() {
    const user = state.user;
    document.body.classList.toggle('signed-in', Boolean(user));
    $('app-shell').hidden = !user; $('landing').hidden = Boolean(user);
    document.querySelectorAll('[data-admin]').forEach((item) => { item.hidden = user?.role !== 'admin'; });
    document.querySelectorAll('[data-auth]').forEach((item) => { item.disabled = !user; });
    $('session-label').textContent = user?.name || user?.id || 'Signed out';
    $('session-scopes').textContent = user ? `${user.role} · ${user.department?.name || 'No department'}` : '';
    $('nav-heading').textContent = user?.role === 'admin' ? 'ADMINISTRATION' : 'KNOWLEDGE WORKSPACE';
    $('workspace-kind').textContent = user?.role === 'admin' ? 'Admin dashboard' : 'Employee workspace';
    $('context-identity').textContent = user?.name || user?.id || 'Not signed in';
    $('context-role').textContent = user?.role || '—';
    $('context-epoch').textContent = user?.acl_epoch ?? '—';
    $('context-scopes').replaceChildren(...(user?.scopes || []).map((scope) => node('span', scope, 'badge quiet')));
  }
  function selectTab(name, focus = false) {
    const tab = $(`tab-${name}`);
    if (!state.user || !tab || tab.hidden) return;
    state.tab = name;
    document.querySelectorAll('[role="tab"]').forEach((item) => {
      const active = item === tab;
      item.setAttribute('aria-selected', String(active)); item.tabIndex = active ? 0 : -1;
      $(item.getAttribute('aria-controls')).hidden = !active;
    });
    $('breadcrumb').textContent = tabNames[name];
    if (focus) tab.focus();
    const loaders = { overview: loadOverview, documents: loadDocuments, users: loadUsers, departments: loadDepartments, provider: loadProvider, audit: loadAudit, assistant: loadConversations, profile: renderProfile };
    if (loaders[name]) loaders[name]();
  }
  document.querySelectorAll('[role="tab"]').forEach((tab) => {
    tab.addEventListener('click', () => selectTab(tab.dataset.tab));
    tab.addEventListener('keydown', (event) => {
      const visible = [...document.querySelectorAll('[role="tab"]')].filter((item) => !item.hidden);
      let index = visible.indexOf(tab);
      if (['ArrowRight','ArrowDown'].includes(event.key)) index = (index + 1) % visible.length;
      else if (['ArrowLeft','ArrowUp'].includes(event.key)) index = (index - 1 + visible.length) % visible.length;
      else if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = visible.length - 1;
      else return;
      event.preventDefault(); selectTab(visible[index].dataset.tab, true);
    });
  });
  function openLogin(demo) {
    $('demo-explanation').hidden = !demo;
    $('login-status').textContent = '';
    $('login-dialog').showModal(); $('username').focus();
  }
  $('landing-signin').addEventListener('click', () => openLogin(false));
  $('hero-signin').addEventListener('click', () => openLogin(false));
  $('explore-demo').addEventListener('click', () => openLogin(true));
  function closeLogin() { abortTask('auth'); $('password').value = ''; $('login-dialog').close(); }
  $('close-login').addEventListener('click', closeLogin);
  $('login-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeLogin(); });
  $('logout').addEventListener('click', () => { resetSession('Signed out. Your session and workspace content have been cleared.'); $('landing-signin').focus(); });
  $('login-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const credentials = { username: $('username').value.trim(), password: $('password').value };
    work('auth', $('login-submit'), $('login-form'), 'Signing in…', async (request) => {
      try {
        const data = await request('/api/auth/login', { method: 'POST', body: credentials, publicRequest: true });
        if (!data.access_token || !data.user?.id || !Array.isArray(data.user.scopes)) throw new Error('Invalid authentication response.');
        state.token = data.access_token; state.user = data.user;
        $('password').value = ''; $('login-dialog').close(); updateSession();
        selectTab(data.user.role === 'admin' ? 'overview' : 'assistant');
        $('main').focus(); status(`Welcome, ${data.user.name || data.user.id}.`);
      } finally { credentials.password = ''; $('password').value = ''; }
    });
  });
  $('theme-toggle').addEventListener('click', () => {
    const dark = document.body.classList.toggle('dark');
    $('theme-toggle').setAttribute('aria-pressed', String(dark));
    $('theme-toggle').setAttribute('aria-label', dark ? 'Use light theme' : 'Use dark theme');
  });
  function safeMarkdown(text) {
    const fragment = document.createDocumentFragment();
    let list = null;
    for (const line of String(text || '').split('\n')) {
      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      const item = line.match(/^\s*(?:[-*]|\d+\.)\s+(.+)$/);
      if (item) {
        if (!list) { list = node('ul'); fragment.append(list); }
        list.append(node('li', item[1]));
      } else {
        list = null;
        if (heading) fragment.append(node(`h${Math.min(heading[1].length + 2, 6)}`, heading[2]));
        else if (line.trim()) fragment.append(node('p', line));
      }
    }
    return fragment;
  }
  function providerLabel(diagnostics) {
    if (diagnostics?.succeeded === true) return `Model response · ${diagnostics.provider || 'provider'} / ${diagnostics.model || 'model'}`;
    if (diagnostics?.fallback_used === true) return 'Local fallback · model did not succeed';
    return 'Local / unverified generation · no confirmed model success';
  }
  function answerContent(result) {
    const fragment = document.createDocumentFragment();
    const badges = node('div', null, 'badge-row');
    badges.append(node('span', providerLabel(result.provider_status), 'badge violet'), node('span', `Mode: ${result.generation_mode || 'not recorded'}`, 'badge quiet'));
    if (result.acl_epoch !== undefined) badges.append(node('span', `ACL epoch: ${result.acl_epoch}`, 'badge quiet'));
    fragment.append(badges);
    if (result.injection_warning) fragment.append(node('p', 'Injection warning: suspicious source instructions were detected. Review evidence carefully.', 'notice'));
    const answer = node('div', null, 'answer-text'); answer.append(safeMarkdown(result.answer)); fragment.append(answer);
    if (!result.citations?.length) fragment.append(node('p', 'No cited evidence was returned. The answer may be access-limited or lack sufficient evidence.', 'small'));
    else {
      const citations = node('div', null, 'citations');
      for (const citation of result.citations) {
        const button = action(`${citation.title} · p. ${citation.page} ↗`, () => inspectCitation(citation), 'citation');
        button.setAttribute('aria-label', `Inspect ${citation.title}, page ${citation.page}, chunk ${citation.chunk_id}`); citations.append(button);
      }
      fragment.append(citations);
    }
    const generation = state.generation;
    fragment.append(action('Copy answer', async () => {
      try { await navigator.clipboard.writeText(String(result.answer || '')); if (generation === state.generation) status('Answer copied.'); }
      catch { if (generation === state.generation) status('Clipboard unavailable. Select the answer text to copy it.', true); }
    }));
    return fragment;
  }
  function message(role, content, metadata = {}) {
    const article = node('article', null, role === 'user' ? 'message question' : 'message');
    article.append(node('h3', role === 'user' ? 'YOU' : 'ORGMIND'));
    article.append(role === 'user' ? node('p', content, 'question-text') : answerContent({ ...metadata, answer: content }));
    return article;
  }
  document.querySelectorAll('[data-query]').forEach((button) => button.addEventListener('click', () => { $('chat-query').value = button.dataset.query; $('chat-query').focus(); }));
  $('new-chat').addEventListener('click', () => { clearChat(); loadConversations(); status('New conversation. Document selection is preserved.'); $('chat-query').focus(); });
  $('chat-form').addEventListener('submit', (event) => {
    event.preventDefault();
    if (!authenticated() || state.tasks.has('chat') || state.tasks.has('conversation')) return;
    const query = $('chat-query').value.trim();
    if (query.length < 2) { status('Enter a question with at least two characters.', true); return; }
    const body = { query, document_ids: state.selected.size ? [...state.selected] : null, conversation_id: state.conversation };
    $('chat-empty').hidden = true;
    const answer = node('article', null, 'message'); answer.append(node('p', 'Retrieving authorized evidence…', 'small'));
    $('chat-messages').append(message('user', query), answer); $('chat-query').value = '';
    work('chat', event.submitter, answer, 'Retrieving authorized evidence…', async (request) => {
      const result = await request('/api/workspace/chat', { method: 'POST', body });
      state.conversation = result.conversation_id;
      answer.replaceChildren(node('h3', 'ORGMIND'), answerContent(result));
      $('context-epoch').textContent = result.acl_epoch;
      $('chat-messages').scrollTop = $('chat-messages').scrollHeight;
      loadConversations(); status('Answer received. Inspect citations to review the evidence.');
    }, (text) => { answer.replaceChildren(node('h3', 'REQUEST NOT COMPLETED'), node('p', text, 'notice')); $('chat-query').value = query; });
  });
  function loadConversations() {
    if (!authenticated()) return;
    empty('conversation-list', 'Loading conversations…');
    work('conversations', $('refresh-conversations'), $('conversation-list'), 'Loading conversation history…', async (request) => {
      const data = await request('/api/workspace/conversations');
      $('conversation-list').replaceChildren();
      if (!data.conversations.length) { empty('conversation-list', 'No conversations yet. Ask your first question.'); return; }
      for (const chat of data.conversations) {
        const row = node('div', null, 'history-row');
        const open = action(chat.title || 'Untitled conversation', () => openConversation(chat.id));
        if (state.conversation === chat.id) open.setAttribute('aria-current', 'true');
        const remove = action('Delete', () => {
          if (!window.confirm(`Delete conversation “${chat.title}”?`)) return;
          if (state.conversation === chat.id || state.tasks.has('conversation')) clearChat();
          work('mutation', remove, row, 'Deleting conversation…', async (request) => {
            await request(`/api/workspace/conversations/${encodeURIComponent(chat.id)}`, { method: 'DELETE' });
            if (state.conversation === chat.id) clearChat();
            loadConversations();
          });
        }, 'button danger');
        remove.setAttribute('aria-label', `Delete conversation ${chat.title}`);
        row.append(open, node('small', formatDate(chat.updated_at)), remove); $('conversation-list').append(row);
      }
    }, (text) => empty('conversation-list', text));
  }
  function openConversation(id) {
    clearChat();
    work('conversation', null, $('chat-messages'), 'Loading conversation…', async (request) => {
      const chat = await request(`/api/workspace/conversations/${encodeURIComponent(id)}`);
      state.conversation = chat.id; state.selected = new Set(chat.document_ids || []); updateSelection();
      $('chat-empty').hidden = chat.messages.length > 0;
      $('chat-messages').replaceChildren(...chat.messages.map((item) => message(item.role, item.content, item)));
      $('context-epoch').textContent = chat.acl_epoch; status('Conversation loaded. Access was checked by the server.');
    }, (text) => empty('chat-messages', text));
  }
  $('refresh-conversations').addEventListener('click', loadConversations);
  function revokeUrls() { for (const url of state.urls) URL.revokeObjectURL(url); state.urls.clear(); }
  function closeEvidence() {
    abortTask('citation'); abortTask('preview'); abortTask('download');
    $('evidence-dialog').close(); revokeUrls(); state.preview = null;
    $('evidence-title').textContent = 'Source evidence'; $('evidence-meta').textContent = ''; $('evidence-text').textContent = '';
    $('page-preview').replaceChildren(); $('download-result').replaceChildren(); $('page-details').open = false;
  }
  $('close-evidence').addEventListener('click', closeEvidence);
  $('evidence-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeEvidence(); });
  function showSource(doc, page = 1) {
    closeEvidence(); state.preview = doc;
    $('evidence-title').textContent = doc.title; $('preview-page').value = page;
    $('preview-page').max = doc.pages || '';
    $('evidence-dialog').showModal();
  }
  function inspectCitation(citation) {
    if (!authenticated()) return;
    showSource({ id: citation.document_id, title: citation.title }, citation.page);
    $('evidence-text').textContent = 'Loading authorized passage…';
    work('citation', null, $('evidence-text'), 'Inspecting cited passage…', async (request) => {
      const source = await request(`/api/documents/${encodeURIComponent(citation.document_id)}/chunks/${encodeURIComponent(citation.chunk_id)}`);
      $('evidence-title').textContent = source.title;
      $('evidence-meta').textContent = `${source.document_id} · Page ${source.page} · Chunk ${source.id}`;
      $('evidence-text').textContent = source.text;
    }, (text) => { $('evidence-text').textContent = text; });
  }
  function previewPage() {
    if (!state.preview || !authenticated()) return;
    const page = Number($('preview-page').value);
    if (!Number.isInteger(page) || page < 1) { status('Choose a valid page number.', true); return; }
    const doc = state.preview;
    empty('page-preview', 'Loading original page…');
    work('preview', null, $('page-preview'), 'Loading authorized PDF page…', async (request) => {
      const blob = await request(`/api/workspace/documents/${encodeURIComponent(doc.id)}/preview?page=${page}`, { blob: true });
      if (blob.type !== 'image/png') throw new Error('Expected a PNG page preview.');
      const url = URL.createObjectURL(blob); state.urls.add(url);
      const image = node('img'); image.src = url; image.alt = `${doc.title}, page ${page}`;
      $('page-preview').replaceChildren(image);
    }, (text) => empty('page-preview', text));
  }
  $('page-details').addEventListener('toggle', () => { if ($('page-details').open) previewPage(); else abortTask('preview'); });
  $('page-form').addEventListener('submit', (event) => { event.preventDefault(); previewPage(); });
  function downloadDocument(doc) {
    showSource(doc); $('evidence-text').textContent = 'Preparing authorized PDF download…';
    work('download', null, $('download-result'), 'Downloading authorized original…', async (request) => {
      const blob = await request(`/api/workspace/documents/${encodeURIComponent(doc.id)}/download`, { blob: true });
      if (blob.type !== 'application/pdf') throw new Error('Expected a PDF original.');
      const url = URL.createObjectURL(blob); state.urls.add(url);
      const link = node('a', 'Save PDF', 'button primary'); link.href = url; link.download = doc.filename || `${doc.id}.pdf`;
      $('download-result').replaceChildren(link); $('evidence-text').textContent = 'Your PDF is ready. Save it before closing this window.';
    }, (text) => empty('download-result', text));
  }
  $('compare-form').addEventListener('submit', (event) => {
    event.preventDefault(); if (!authenticated(true)) return;
    const query = $('compare-query').value.trim();
    if (query.length < 2) { status('Enter a question with at least two characters.', true); return; }
    empty('compare-results', 'Generating live, permission-aware responses…');
    work('compare', event.submitter, $('compare-results'), 'Comparing authorized roles…', async (request) => {
      const data = await request('/api/chat/compare-roles', { method: 'POST', body: { query } });
      $('compare-results').replaceChildren(...data.results.map((result) => {
        const card = node('article', null, 'card result-card'); card.append(node('h2', result.role), node('p', result.persona_id, 'small'), answerContent(result)); return card;
      }));
    }, (text) => empty('compare-results', text));
  });
  function table(columns, rows, title) {
    const wrapper = node('div', null, 'table-wrap');
    wrapper.tabIndex = 0;
    wrapper.setAttribute('role', 'region');
    wrapper.setAttribute('aria-label', `${title}; scroll to see all columns`);
    const grid = node('table');
    const head = node('thead');
    const header = node('tr');
    for (const column of columns) {
      const th = node('th', column);
      th.scope = 'col';
      header.append(th);
    }
    head.append(header);
    const body = node('tbody');
    for (const row of rows) {
      const tr = node('tr');
      for (const column of columns) {
        const value = row[column];
        tr.append(node('td', value !== null && typeof value === 'object' ? JSON.stringify(value) : (value ?? '—')));
      }
      body.append(tr);
    }
    grid.append(node('caption', title), head, body);
    wrapper.append(grid);
    return wrapper;
  }

  function bar(label, value, min, max, formatted) {
    const row = node('div', null, 'bar-row');
    const meter = node('meter');
    meter.id = `metric-bar-${++elementId}`;
    meter.min = min;
    meter.max = max;
    meter.value = value;
    meter.textContent = formatted;
    const name = node('label', label);
    name.htmlFor = meter.id;
    row.append(name, meter, node('output', formatted));
    return row;
  }

  $('sql-form').addEventListener('submit', (event) => {
    event.preventDefault();
    if (!authenticated(true)) return;
    const body = { metric_id: $('metric').value };
    if ($('period').value.trim()) body.period = $('period').value.trim();
    empty('sql-results', 'Running the approved metric…');
    work('sql', event.submitter || $('sql-form').querySelector('button'), $('sql-results'), 'Querying authorized analytics rows…', async (request) => {
      const data = await request('/api/sql/query', { method: 'POST', body });
      const target = $('sql-results');
      target.replaceChildren(node('p', `${data.metric_id} · ${data.rows.length} rows · server cap ${data.max_rows}`, 'small'));
      if (!data.rows.length) { target.append(node('p', 'No authorized rows match this metric and period.', 'empty-state')); }
      else {
        target.append(table(data.columns, data.rows, 'Authorized metric rows'));
        const groups = new Map();
        for (const row of data.rows) {
          if (typeof row.value !== 'number' || !Number.isFinite(row.value)) continue;
          if (!groups.has(row.unit)) groups.set(row.unit, []);
          groups.get(row.unit).push(row);
        }
        for (const [unit, rows] of groups) {
          const values = rows.map((row) => row.value);
          const min = Math.min(0, ...values);
          const max = Math.max(0, ...values) || (min === 0 ? 1 : 0);
          const chart = node('section', null, 'card chart-card');
          chart.append(node('h3', `Metric comparison · ${unit}`), node('p', `Scale: ${number.format(min)} to ${number.format(max)} ${unit}. Bars are grouped by unit.`, 'small'));
          rows.forEach((row) => chart.append(bar(`${row.label} · ${row.period}`, row.value, min, max, `${number.format(row.value)} ${unit}`)));
          target.append(chart);
        }
      }
      status('Metric query complete. The table and chart use the returned rows.');
    }, (message) => empty('sql-results', message));
  });

  function rawDetails(label, data) {
    const details = node('details', null, 'raw-details');
    details.append(node('summary', label), node('pre', JSON.stringify(data, null, 2)));
    return details;
  }
  function loadAudit() {
    if (!authenticated(true)) return;
    empty('audit-results', 'Loading server audit events…');
    work('audit', $('refresh-audit'), $('audit-results'), 'Checking the audit trail…', async (request) => {
      const data = await request('/api/admin/audit-logs?limit=50');
      const target = $('audit-results');
      target.replaceChildren(node('p', data.integrity_valid ? 'Audit integrity: valid (server verification).' : 'Warning: audit integrity verification failed.', data.integrity_valid ? 'badge outlined' : 'notice'));
      if (!data.logs.length) target.append(node('p', 'No audit events returned.', 'empty-state'));
      else target.append(table(['timestamp', 'user_id', 'role', 'action', 'decision', 'policy_intercept', 'policy_rule', 'acl_epoch'], data.logs, 'Latest server audit events'));
      target.append(rawDetails('Inspect complete audit records, event IDs, blocked documents, and hashes', data.logs));
      status('Audit trail loaded. Integrity status reflects backend verification.');
    }, (message) => empty('audit-results', message));
  }
  $('refresh-audit').addEventListener('click', loadAudit);

  $('run-benchmark').addEventListener('click', () => {
    if (!authenticated(true)) return;
    empty('benchmark-results', 'Evaluating live BM25, LSA, and hybrid retrieval. This may take a moment…');
    work('benchmark', $('run-benchmark'), $('benchmark-results'), 'Running the fixed-corpus retrieval benchmark…', async (request) => {
      const data = await request('/api/eval/benchmark');
      const target = $('benchmark-results');
      target.replaceChildren();
      const stats = node('div', null, 'stat-grid');
      for (const [label, value, detail] of [
        ['Benchmark cases', data.case_count, `${data.positive_case_count} positive · ${data.denied_case_count} denied probes`],
        ['Process-wide RSS', `${number.format(data.rss_mb)} MiB`, data.rss_source],
        ['Recall@5 target', percent.format(data.recall_target), 'Configured benchmark target, not a measured score']
      ]) {
        const card = node('div', null, 'card stat-card');
        card.append(node('p', label, 'small'), node('strong', value), node('small', detail));
        stats.append(card);
      }
      target.append(stats);
      const metrics = [
        ['recall_at_5', 'Recall@5'], ['mrr', 'MRR'],
        ['citation_accuracy', 'Citation accuracy (answered queries)'],
        ['unauthorized_retrieval_rate', 'Unauthorized retrieval rate (lower is better)']
      ];
      const rows = Object.entries(data.methods).map(([method, result]) => ({
        Method: method.toUpperCase(), 'Recall@5': percent.format(result.recall_at_5), MRR: number.format(result.mrr),
        'Answer citation accuracy': percent.format(result.citation_accuracy),
        'Unauthorized rate': percent.format(result.unauthorized_retrieval_rate),
        'Returned chunks': result.returned_count, 'Unauthorized chunks': result.unauthorized_count,
        'Positive-case chunks': result.positive_returned_count, 'Correct evidence citations': result.correct_citation_count
      }));
      target.append(table(['Method', 'Recall@5', 'MRR', 'Answer citation accuracy', 'Unauthorized rate', 'Returned chunks', 'Unauthorized chunks', 'Positive-case chunks', 'Correct evidence citations'], rows, 'Live retrieval evaluation results'));
      const charts = node('div', null, 'benchmark-charts');
      for (const [key, title] of metrics) {
        const card = node('section', null, 'card chart-card');
        card.append(node('h3', title));
        for (const [method, result] of Object.entries(data.methods)) {
          card.append(bar(method.toUpperCase(), result[key], 0, 1, key === 'mrr' ? number.format(result[key]) : percent.format(result[key])));
        }
        charts.append(card);
      }
      target.append(charts, node('p', data.corpus_dependence, 'notice'));
      const definitions = node('details', null, 'raw-details');
      definitions.append(node('summary', 'Metric definitions and interpretation'));
      for (const [key, definition] of Object.entries(data.metric_definitions)) {
        const paragraph = node('p', null, 'notice');
        paragraph.append(node('strong', `${key}: `), node('span', definition));
        definitions.append(paragraph);
      }
      target.append(definitions, rawDetails('Inspect per-case retrieval evidence and judgments', data.methods));
      status('Live benchmark complete. Citation accuracy checks answer citations against ground-truth evidence pages, not full answer factuality.');
    }, (message) => empty('benchmark-results', message));
  });

  function action(label, handler, className = 'button secondary') {
    const button = node('button', label, className); button.type = 'button'; button.addEventListener('click', handler); return button;
  }
  function formatDate(value) {
    if (!value) return 'Not recorded';
    const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }
  function field(form, label, value = '', type = 'text', required = false) {
    const input = node('input'); input.id = `field-${++elementId}`; input.type = type; input.value = value ?? ''; input.required = required;
    const caption = node('label', label); caption.htmlFor = input.id; form.append(caption, input); return input;
  }
  function selectField(form, label, options, value) {
    const select = node('select'); select.id = `field-${++elementId}`;
    for (const [id, text] of options) { const option = node('option', text); option.value = id; select.append(option); }
    select.value = value ?? ''; const caption = node('label', label); caption.htmlFor = select.id; form.append(caption, select); return select;
  }
  function submitButton(form, label) { const button = node('button', label, 'button primary'); button.type = 'submit'; form.append(button); return button; }
  function updateSelection() {
    const count = state.selected.size;
    $('document-selection').textContent = count ? `${count} document${count === 1 ? '' : 's'} selected` : 'No documents selected';
    $('chat-selection').textContent = count ? `Chat context: ${count} selected document${count === 1 ? '' : 's'}` : 'Chat context: all authorized documents';
    $('chat-selected').disabled = !count;
    document.querySelectorAll('[data-document-select]').forEach((input) => { input.checked = state.selected.has(input.dataset.documentSelect); });
  }
  function changeSelection(id, checked) {
    clearChat();
    if (checked) state.selected.add(id); else state.selected.delete(id);
    updateSelection();
  }
  $('clear-selection').addEventListener('click', () => { clearChat(); state.selected.clear(); updateSelection(); });
  $('chat-selected').addEventListener('click', () => { if (!state.selected.size) return; clearChat(); selectTab('assistant'); $('chat-query').focus(); });
  function documentChanged(text) {
    clearChat(); abortTask('conversations'); abortTask('compare'); abortTask('benchmark');
    $('compare-results').replaceChildren(); $('benchmark-results').replaceChildren(); $('conversation-list').replaceChildren();
    state.selected.clear(); updateSelection(); loadDocuments(); status(`${text} Previous evidence cleared.`);
  }
  function scopeFields(fieldset, selected = []) {
    fieldset.replaceChildren(node('legend', 'Allowed scopes'));
    for (const scope of state.config?.scopes || []) {
      const label = node('label', null, 'scope-option'); const input = node('input');
      input.type = 'checkbox'; input.name = 'allowed_scopes'; input.value = scope; input.checked = selected.includes(scope);
      label.append(input, node('span', scope)); fieldset.append(label);
    }
  }
  function renderDocuments(documents) {
    state.documents = documents;
    $('document-list').replaceChildren();
    if (!documents.length) { empty('document-list', 'No documents match. Try another filter or upload a private PDF.'); return; }
    for (const doc of documents) {
      const card = node('article', null, 'card document-card');
      const check = node('input'); check.type = 'checkbox'; check.dataset.documentSelect = doc.id; check.checked = state.selected.has(doc.id);
      check.disabled = doc.status !== 'indexed';
      const label = node('label', null, 'scope-option'); label.append(check, node('span', `Select ${doc.title}`));
      check.addEventListener('change', () => changeSelection(doc.id, check.checked));
      card.append(node('h3', doc.title), label, node('p', doc.filename || doc.id, 'small'));
      const badges = node('div', null, 'badge-row');
      badges.append(node('span', doc.status, `badge ${doc.status === 'failed' ? 'failed' : 'quiet'}`), node('span', doc.visibility || 'private', 'badge quiet'));
      card.append(badges, node('p', `${doc.pages ?? 0} pages · ${doc.chunks ?? 0} chunks`, 'small'), node('p', `Owner: ${doc.owner || doc.owner_id || 'Not recorded'} · Department: ${doc.department || 'None'}`, 'small'), node('p', `Uploaded: ${formatDate(doc.uploaded_at)}`, 'small'));
      if (doc.status === 'processing' || doc.status === 'uploaded') card.append(node('p', 'Not ready for chat yet. Refresh the library to check indexing progress.', 'notice'));
      if (doc.status === 'failed') card.append(node('p', 'Indexing failed. Reindex if available, or contact an administrator.', 'notice'));
      const actions = node('div', null, 'document-actions');
      if (doc.has_original) {
        actions.append(action('Preview PDF', () => { showSource(doc); $('evidence-text').textContent = 'Original PDF page preview.'; $('page-details').open = true; }), action('Download PDF', () => downloadDocument(doc)));
      } else actions.append(node('p', 'Original PDF unavailable. Indexed citations remain inspectable.', 'small'));
      if (doc.can_reindex) {
        const reindex = action('Reindex', () => work('mutation', reindex, card, 'Reindexing PDF…', async (request) => {
          const result = await request(`/api/workspace/documents/${encodeURIComponent(doc.id)}/reindex`, { method: 'POST' }); documentChanged(`Document status: ${result.status}.`);
        })); actions.append(reindex);
      }
      if (doc.can_delete) {
        const remove = action('Delete document', () => {
          if (!window.confirm(`Delete “${doc.title}” and its indexed content? This cannot be undone.`)) return;
          work('mutation', remove, card, 'Deleting document…', async (request) => {
            await request(`/api/workspace/documents/${encodeURIComponent(doc.id)}`, { method: 'DELETE' }); documentChanged('Document deleted.');
          });
        }, 'button danger'); actions.append(remove);
      }
      card.append(actions);
      if (state.user.role === 'admin') {
        const details = node('details', null, 'raw-details'); details.append(node('summary', 'Manage document access'));
        const form = node('form');
        const visibility = selectField(form, 'Visibility', [['private','Private — owner only'],['scoped','Shared with allowed scopes']], doc.visibility);
        const scopes = node('fieldset'); scopeFields(scopes, doc.allowed_scopes || []); form.append(scopes);
        const syncScopes = () => { scopes.disabled = visibility.value === 'private'; };
        visibility.addEventListener('change', syncScopes); syncScopes();
        const save = submitButton(form, 'Save permissions');
        form.addEventListener('submit', (event) => {
          event.preventDefault(); if (!authenticated(true)) return;
          const allowed_scopes = visibility.value === 'private' ? [] : [...scopes.querySelectorAll('input:checked')].map((input) => input.value);
          if (visibility.value === 'scoped' && !allowed_scopes.length) { status('Select at least one allowed scope.', true); return; }
          work('mutation', save, card, 'Saving document permissions…', async (request) => {
            await request(`/api/workspace/documents/${encodeURIComponent(doc.id)}/permissions`, { method: 'PATCH', body: { visibility: visibility.value, allowed_scopes } }); documentChanged('Permissions saved.');
          });
        });
        details.append(form); card.append(details);
      }
      $('document-list').append(card);
    }
    updateSelection();
  }
  function loadDocuments() {
    if (!authenticated()) return;
    empty('document-list', 'Loading authorized documents…');
    const params = new URLSearchParams({ q: $('document-search').value.trim(), status: $('document-status').value });
    work('documents', $('refresh-documents'), $('document-list'), 'Loading document library…', async (request) => {
      const data = await request(`/api/workspace/documents?${params}`); renderDocuments(data.documents); status(`${data.documents.length} documents found.`);
    }, (text) => empty('document-list', text));
  }
  $('document-filter').addEventListener('submit', (event) => { event.preventDefault(); loadDocuments(); });
  $('document-status').addEventListener('change', loadDocuments);
  $('upload-form').addEventListener('submit', (event) => {
    event.preventDefault(); if (!authenticated()) return;
    const file = $('pdf-file').files[0];
    if (!file || (file.type && file.type !== 'application/pdf')) { status('Choose a PDF document.', true); return; }
    const form = new FormData(); form.append('file', file);
    work('mutation', event.submitter, $('upload-form'), 'Uploading private PDF…', async (request) => {
      const doc = await request('/api/workspace/documents/upload', { method: 'POST', body: form });
      $('upload-form').reset(); $('document-filter').reset(); documentChanged(`Uploaded ${doc.title}. Status: ${doc.status}.`);
    });
  });
  function loadOverview() {
    if (!authenticated(true)) return;
    empty('overview-results', 'Loading organization overview…');
    work('overview', $('refresh-overview'), $('overview-results'), 'Loading overview…', async (request) => {
      const data = await request('/api/admin/overview'); const stats = node('div', null, 'stat-grid');
      for (const [key, label] of [['users','Total users'],['active_users','Active users'],['inactive_users','Inactive users'],['active_admins','Active administrators'],['departments','Departments'],['documents','Documents'],['chunks','Indexed chunks'],['audit_logs','Audit events'],['acl_epoch','Access policy epoch']]) {
        const card = node('article', null, 'card stat-card'); card.append(node('p', label, 'small'), node('strong', data[key] ?? 'Not available')); stats.append(card);
      }
      $('overview-results').replaceChildren(stats); status('Organization overview updated.');
    }, (text) => empty('overview-results', text));
  }
  $('refresh-overview').addEventListener('click', loadOverview);
  function renderProfile() {
    if (!state.user) return;
    const user = state.user; const list = node('dl', null, 'context-list');
    for (const [key, value] of [['Name',user.name],['Account ID',user.id],['Email',user.email],['Role',user.role],['Department',user.department?.name],['Status',user.active ? 'Active' : 'Inactive'],['Authorized scopes',user.scopes.join(', ')]]) {
      const row = node('div'); row.append(node('dt',key),node('dd',value || 'Not set')); list.append(row);
    }
    $('profile-results').replaceChildren(list, node('p','Contact your administrator to update your account.', 'small'));
  }
  function loadUsers() {
    if (!authenticated(true)) return;
    empty('users-results', 'Loading users…'); empty('roles-results','Loading roles…');
    work('users', $('refresh-users'), $('users-results'), 'Loading accounts and roles…', async (request) => {
      const [users, roles, departments] = await Promise.all([request('/api/admin/users'),request('/api/admin/roles'),request('/api/admin/departments')]);
      state.roles = roles.roles; state.departments = departments.departments;
      $('users-results').replaceChildren();
      if (!users.users.length) empty('users-results','No accounts found. Add a user to begin.');
      for (const user of users.users) {
        const card = node('article',null,'card account-row');
        const info = node('div'); info.append(node('h3',user.name || user.id),node('p',`${user.email || user.id} · ${user.role} · ${user.department?.name || 'No department'}`, 'small'),node('span',user.active ? 'Active' : 'Inactive','badge quiet'));
        const edit = action('Edit user', () => editUser(user)); edit.setAttribute('aria-label',`Edit user ${user.name || user.id}`);
        card.append(info,edit); $('users-results').append(card);
      }
      $('roles-results').replaceChildren(...roles.roles.map((role) => {
        const card = node('article',null,'card document-card'); card.append(node('h3',role.name),node('p',`${role.user_count} accounts`, 'small'),node('p',`Scopes: ${role.scopes.join(', ')}`,'small')); return card;
      }));
      status('Users and roles updated.');
    }, (text) => { empty('users-results',text); empty('roles-results','Roles unavailable. Refresh to retry.'); });
  }
  function editUser(user = null) {
    if (!authenticated(true)) return;
    if (!state.roles.length) { status('Load users & roles before editing accounts.', true); return; }
    const form = node('form',null,'card query-card account-form'); form.append(node('h2',user ? 'Edit user' : 'Add user'));
    const name = field(form,'Full name',user?.name,'text',true); name.maxLength = 200;
    const username = user ? null : field(form,'Username','','text',true); if (username) { username.maxLength = 100; username.autocomplete = 'off'; }
    const email = field(form,'Email',user?.email,'email'); email.maxLength = 254;
    const password = field(form,user ? 'New password (leave blank to keep unchanged)' : 'Initial password','','password',!user); password.minLength = 12; password.maxLength = 256; password.autocomplete = 'new-password';
    const role = selectField(form,'Role',state.roles.map((item) => [item.id,item.name]),user?.role || 'employee');
    const department = selectField(form,'Department',[['','No department'],...state.departments.map((item) => [item.id,item.name])],user?.department_id || '');
    const active = field(form,'Account active','','checkbox'); active.checked = user ? user.active : true;
    const save = submitButton(form,'Save user'); form.append(action('Cancel', () => $('user-editor').replaceChildren()));
    form.addEventListener('submit',(event) => {
      event.preventDefault();
      const body = { name: name.value.trim(), email: email.value.trim() || null, department_id: department.value || null, role: role.value, active: active.checked };
      if (!body.name || (username && !username.value.trim())) { status('Name and username cannot be blank.',true); return; }
      if (username) body.username = username.value.trim();
      if (password.value) body.password = password.value;
      work('mutation',save,form,'Saving user…',async (request) => {
        try {
          await request(user ? `/api/admin/users/${encodeURIComponent(user.id)}` : '/api/admin/users',{ method: user ? 'PATCH' : 'POST',body });
          $('user-editor').replaceChildren();
          if (user?.id === state.user.id) { resetSession('Your account changed. Sign in again to refresh your identity.'); return; }
          loadUsers(); status('User saved.');
        } finally { delete body.password; password.value = ''; }
      });
    });
    $('user-editor').replaceChildren(form); name.focus();
  }
  $('new-user').addEventListener('click',() => editUser()); $('refresh-users').addEventListener('click',loadUsers);
  function loadDepartments() {
    if (!authenticated(true)) return;
    empty('departments-results','Loading departments…');
    work('departments',$('refresh-departments'),$('departments-results'),'Loading departments…',async (request) => {
      const data = await request('/api/admin/departments'); state.departments = data.departments;
      $('departments-results').replaceChildren();
      if (!data.departments.length) { empty('departments-results','No departments yet. Add a department to organize your team.'); return; }
      for (const department of data.departments) {
        const card = node('article',null,'card account-row'); const info = node('div');
        info.append(node('h3',department.name),node('p',department.description || 'No description','small'),node('p',`${department.user_count ?? 0} accounts · ${department.id}`,'small'));
        const actions = node('div',null,'document-actions'); const remove = action('Delete department',() => {
          if (!window.confirm(`Delete department “${department.name}”? Assigned accounts may prevent deletion.`)) return;
          work('mutation',remove,card,'Deleting department…',async (request) => {
            await request(`/api/admin/departments/${encodeURIComponent(department.id)}`,{method:'DELETE'}); loadDepartments(); status('Department deleted.');
          });
        },'button danger');
        actions.append(action('Edit department',() => editDepartment(department)),remove); card.append(info,actions); $('departments-results').append(card);
      }
      status('Departments updated.');
    },(text) => empty('departments-results',text));
  }
  function editDepartment(department = null) {
    if (!authenticated(true)) return;
    const form = node('form',null,'card query-card account-form'); form.append(node('h2',department ? 'Edit department' : 'Add department'));
    const name = field(form,'Department name',department?.name,'text',true); name.maxLength = 200;
    const description = field(form,'Description',department?.description); description.maxLength = 2000;
    const save = submitButton(form,'Save department'); form.append(action('Cancel',() => $('department-editor').replaceChildren()));
    form.addEventListener('submit',(event) => {
      event.preventDefault();
      const body = {name:name.value.trim(),description:description.value.trim()};
      if (!body.name) { status('Department name cannot be blank.',true); return; }
      work('mutation',save,form,'Saving department…',async (request) => {
        await request(department ? `/api/admin/departments/${encodeURIComponent(department.id)}` : '/api/admin/departments',{method:department ? 'PATCH' : 'POST',body});
        $('department-editor').replaceChildren();
        if (department?.id === state.user.department_id) { resetSession('Your department changed. Sign in again to refresh your identity.'); return; }
        loadDepartments(); status('Department saved.');
      });
    });
    $('department-editor').replaceChildren(form); name.focus();
  }
  $('new-department').addEventListener('click',() => editDepartment()); $('refresh-departments').addEventListener('click',loadDepartments);
  function loadProvider() {
    if (!authenticated(true)) return;
    empty('provider-results','Loading recorded AI status…');
    work('provider',$('refresh-provider'),$('provider-results'),'Loading recorded provider outcomes…',async (request) => {
      const data = await request('/api/admin/provider-status'); const target = $('provider-results');
      const card = node('article',null,'card document-card');
      card.append(node('h2',providerLabel(data.last_generation)),node('p',`${data.successful_generations} successful generations · ${data.fallback_generations} fallback generations`),node('p',data.note,'notice'));
      if (data.last_generation) {
        const info = data.last_generation;
        card.append(table(['provider','model','http_status','error_category','succeeded','fallback_used'],[info],'Last recorded generation'));
      } else card.append(node('p','No generation has been recorded in this process.','empty-state'));
      target.replaceChildren(card); status('AI status reflects recorded requests, not a live provider probe.');
    },(text) => empty('provider-results',text));
  }
  $('refresh-provider').addEventListener('click',loadProvider);
  function loadConfig() {
    work('config',null,null,'Loading workspace configuration…',async (request) => {
      state.config = await request('/api/config',{publicRequest:true});
      if (!state.user && !$('login-dialog').open) status('Sign in to access your workspace.');
    },() => status('Scope configuration unavailable. Sign-in remains available; reload before sharing document permissions.',true));
  }
  // Never restore authentication from browser storage or the back-forward cache.
  window.addEventListener('pagehide',() => resetSession('Signed out. Sign in to start a new session.'));
  window.addEventListener('pageshow',(event) => { if (event.persisted) { resetSession('Restored page is signed out.'); if (!state.config) loadConfig(); } });
  updateSession(); updateSelection(); loadConfig();
})();
