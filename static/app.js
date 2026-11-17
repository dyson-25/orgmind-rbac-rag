'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const state = { token: null, user: null, config: null, generation: 0, tasks: new Map(), tab: 'assistant' };
  const tabNames = { assistant: 'Knowledge Assistant', compare: 'Live Role Comparator', sql: 'SQL Studio', documents: 'Document ACL & Audit', benchmark: 'Live Benchmark' };
  let elementId = 0;
  const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
  const percent = new Intl.NumberFormat(undefined, { style: 'percent', maximumFractionDigits: 2 });

  function node(tag, text, className) {
    const result = document.createElement(tag);
    if (text !== undefined && text !== null) result.textContent = String(text);
    if (className) result.className = className;
    return result;
  }

  function status(message, error = false) {
    $('global-status').textContent = message;
    $('global-status').classList.toggle('error', error);
  }

  function empty(target, message) {
    $(target).replaceChildren(node('p', message, 'empty-state'));
  }

  function abortTask(key) {
    const task = state.tasks.get(key);
    if (task) {
      task.controller.abort();
      if (task.button) task.button.disabled = !state.user && task.button.hasAttribute('data-auth');
      if (task.target) task.target.removeAttribute('aria-busy');
      state.tasks.delete(key);
    }
  }

  function clearEvidence() {
    for (const key of ['chat', 'compare', 'sql', 'benchmark', 'citation', 'documents', 'audit']) abortTask(key);
    $('chat-messages').replaceChildren();
    $('chat-empty').hidden = false;
    empty('compare-results', 'Run a question to compare real authorized responses.');
    empty('sql-results', 'Select a metric and run it to see live data.');
    empty('document-list', 'Refresh the library to load documents.');
    empty('audit-results', 'Load the audit trail to inspect server-recorded events and integrity.');
    empty('benchmark-results', 'No benchmark has been run in this session. All metrics will come from the live evaluator.');
    closeEvidence();
  }

  function resetSession(message) {
    state.generation += 1;
    // Public configuration is independent of the active authenticated session.
    for (const key of [...state.tasks.keys()]) if (key !== 'config') abortTask(key);
    state.token = null;
    state.user = null;
    clearEvidence();
    for (const id of ['login-form', 'chat-form', 'compare-form', 'sql-form', 'upload-form']) $(id).reset();
    $('credentials').open = false;
    updateSession();
    selectTab('assistant');
    status(message);
  }

  function updateSession() {
    const user = state.user;
    document.body.classList.toggle('signed-in', Boolean(user));
    document.querySelectorAll('[data-admin]').forEach((item) => { item.hidden = user?.role !== 'admin'; });
    document.querySelectorAll('[data-auth]').forEach((button) => { button.disabled = !user; });
    $('logout').hidden = !user;
    $('session-label').textContent = user ? `${user.role} workspace` : 'Signed out';
    $('session-scopes').textContent = user ? user.id : 'Choose a demo persona to explore.';
    $('context-identity').textContent = user?.id ?? 'Not signed in';
    $('context-role').textContent = user?.role ?? '—';
    $('context-epoch').textContent = user?.acl_epoch ?? '—';
    $('context-scopes').replaceChildren(...(user?.scopes ?? ['Sign in to view']).map((scope) => node('span', scope, 'badge quiet')));
    $('demo-login').disabled = !state.config?.demo_enabled;
  }

  async function work(key, button, target, pending, execute, onError) {
    if (key === 'mutation' && state.tasks.has(key)) {
      status('A document change is in progress. Please wait.');
      return;
    }
    abortTask(key);
    const generation = state.generation;
    const token = state.token;
    const task = { controller: new AbortController(), button, target };
    const current = () => (key === 'config' || generation === state.generation) && state.tasks.get(key) === task && !task.controller.signal.aborted;
    state.tasks.set(key, task);
    if (button) button.disabled = true;
    if (target) target.setAttribute('aria-busy', 'true');
    status(pending);
    const request = async (path, { method = 'GET', body, publicRequest = false } = {}) => {
      const headers = { Accept: 'application/json' };
      if (!publicRequest) {
        if (!token) throw new Error('Sign in to continue.');
        headers.Authorization = `Bearer ${token}`;
      }
      const isForm = body instanceof FormData;
      if (body !== undefined && !isForm) headers['Content-Type'] = 'application/json';
      const response = await fetch(path, {
        method, headers, body: body === undefined ? undefined : (isForm ? body : JSON.stringify(body)),
        signal: task.controller.signal, cache: 'no-store', credentials: 'omit', redirect: 'error'
      });
      if (!current()) throw new DOMException('Superseded request', 'AbortError');
      let data = null;
      if (response.status !== 204) {
        const text = await response.text();
        if (!current()) throw new DOMException('Superseded request', 'AbortError');
        try { data = JSON.parse(text); } catch { /* Do not display HTML or server error bodies. */ }
      }
      if (!response.ok) {
        if (response.status === 401 && !publicRequest) {
          resetSession('Session expired or identity changed. Sign in again.');
          throw new DOMException('Session ended', 'AbortError');
        }
        const detail = typeof data?.detail === 'string' && response.status < 500 ? data.detail : null;
        throw new Error(detail || (response.status >= 500
          ? (key === 'benchmark'
            ? 'Benchmark failed. It requires the seeded Acme corpus and original persona permissions. Check the server and restore any modified prerequisites before retrying.'
            : 'The server could not complete this operation. Please retry or check the server logs.')
          : `Request failed (${response.status}). Check your inputs and access permissions.`));
      }
      if (response.status !== 204 && data === null) throw new Error('The server returned an invalid response.');
      return data;
    };
    try {
      await execute(request);
    } catch (error) {
      if (current() && error.name !== 'AbortError') {
        const message = error instanceof TypeError ? 'Unable to reach the API. Check the server connection and retry.' : error.message;
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
    if (!state.user || (admin && state.user.role !== 'admin')) {
      status(admin ? 'Sign in with a backend administrator identity to use this tool.' : 'Choose a demo persona and enter the workspace first.', true);
      return false;
    }
    return true;
  }

  function selectTab(name, focus = false) {
    const tab = $(`tab-${name}`);
    if (!tab || tab.hidden) return;
    state.tab = name;
    document.querySelectorAll('[role="tab"]').forEach((item) => {
      const active = item === tab;
      item.setAttribute('aria-selected', String(active));
      item.tabIndex = active ? 0 : -1;
      $(item.getAttribute('aria-controls')).hidden = !active;
    });
    $('breadcrumb').textContent = tabNames[name];
    if (focus) tab.focus();
  }

  document.querySelectorAll('[role="tab"]').forEach((tab) => {
    tab.addEventListener('click', () => selectTab(tab.dataset.tab));
    tab.addEventListener('keydown', (event) => {
      const visible = [...document.querySelectorAll('[role="tab"]')].filter((item) => !item.hidden);
      let index = visible.indexOf(tab);
      if (['ArrowRight', 'ArrowDown'].includes(event.key)) index = (index + 1) % visible.length;
      else if (['ArrowLeft', 'ArrowUp'].includes(event.key)) index = (index - 1 + visible.length) % visible.length;
      else if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = visible.length - 1;
      else return;
      event.preventDefault();
      selectTab(visible[index].dataset.tab, true);
    });
  });

  function signedIn(data) {
    if (!data.access_token || !data.user?.id || !Array.isArray(data.user.scopes)) throw new Error('Invalid authentication response.');
    state.token = data.access_token;
    state.user = data.user;
    $('password').value = '';
    $('credentials').open = false;
    updateSession();
    status(`Signed in as ${data.user.id}. Permissions are enforced by the backend.`);
  }

  $('persona').addEventListener('change', () => resetSession('Persona changed. Previous session content cleared; select Enter demo workspace to sign in.'));
  $('logout').addEventListener('click', () => {
    resetSession('Signed out. The token and workspace results have been cleared.');
    if (Object.hasOwn(state.config?.personas ?? {}, 'general_user')) $('persona').value = 'general_user';
  });
  $('demo-form').addEventListener('submit', (event) => {
    event.preventDefault();
    if (!state.config?.demo_enabled) return;
    const persona = $('persona').value;
    resetSession('Starting a new demo session…');
    work('auth', $('demo-login'), null, 'Signing in to the demo workspace…', async (request) => {
      signedIn(await request('/api/auth/demo-persona', { method: 'POST', body: { persona_id: persona }, publicRequest: true }));
    });
  });
  $('login-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const credentials = { username: $('username').value, password: $('password').value };
    const button = event.submitter || $('login-form').querySelector('button');
    resetSession('Starting a new session…');
    work('auth', button, null, 'Signing in…', async (request) => {
      try { signedIn(await request('/api/auth/login', { method: 'POST', body: credentials, publicRequest: true })); }
      finally { credentials.password = ''; }
    });
  });
  $('theme-toggle').addEventListener('click', () => {
    const dark = document.body.classList.toggle('dark');
    $('theme-toggle').setAttribute('aria-pressed', String(dark));
    $('theme-toggle').setAttribute('aria-label', dark ? 'Use light theme' : 'Use dark theme');
  });

  function answerContent(result) {
    const fragment = document.createDocumentFragment();
    const badges = node('div', null, 'badge-row');
    badges.append(node('span', `Mode: ${result.generation_mode}`, 'badge violet'), node('span', `ACL epoch: ${result.acl_epoch}`, 'badge quiet'));
    fragment.append(badges);
    if (result.injection_warning) fragment.append(node('p', 'Injection warning: suspicious source instructions were detected. Review the evidence carefully.', 'notice'));
    fragment.append(node('p', result.answer, 'answer-text'));
    if (!result.citations?.length) fragment.append(node('p', 'No cited evidence was returned. This may be an insufficient-evidence or access-limited response.', 'notice'));
    else {
      const citations = node('div', null, 'citations');
      for (const citation of result.citations) {
        const button = node('button', `${citation.title} · p. ${citation.page} ↗`, 'citation');
        button.type = 'button';
        button.setAttribute('aria-label', `Inspect ${citation.title}, page ${citation.page}, chunk ${citation.chunk_id}`);
        button.addEventListener('click', () => inspectCitation(citation));
        citations.append(button);
      }
      fragment.append(citations);
    }
    return fragment;
  }

  document.querySelectorAll('[data-query]').forEach((button) => button.addEventListener('click', () => {
    $('chat-query').value = button.dataset.query;
    $('chat-query').focus();
  }));
  $('chat-form').addEventListener('submit', (event) => {
    event.preventDefault();
    if (!authenticated()) return;
    const query = $('chat-query').value.trim();
    if (query.length < 2) { status('Enter a question with at least two non-space characters.', true); return; }
    if (state.tasks.has('chat')) return;
    $('chat-empty').hidden = true;
    const question = node('article', null, 'message question');
    question.append(node('h3', 'YOU'), node('p', query, 'question-text'));
    const answer = node('article', null, 'message');
    answer.append(node('h3', 'ORGMIND'), node('p', 'Retrieving authorized evidence…', 'small'));
    $('chat-messages').append(question, answer);
    work('chat', event.submitter || $('chat-form').querySelector('button'), answer, 'Retrieving authorized evidence…', async (request) => {
      const result = await request('/api/chat', { method: 'POST', body: { query } });
      answer.replaceChildren(node('h3', 'ORGMIND'), answerContent(result));
      $('context-epoch').textContent = result.acl_epoch;
      $('chat-messages').scrollTop = $('chat-messages').scrollHeight;
      status('Answer received. Inspect the citations to review the evidence.');
    }, (message) => answer.replaceChildren(node('h3', 'REQUEST NOT COMPLETED'), node('p', message, 'notice')));
  });

  function closeEvidence() {
    abortTask('citation');
    if ($('evidence-dialog').open) $('evidence-dialog').close();
    $('evidence-title').textContent = 'Source evidence';
    $('evidence-meta').textContent = '';
    $('evidence-text').textContent = '';
  }
  $('close-evidence').addEventListener('click', closeEvidence);
  $('evidence-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeEvidence(); });
  $('evidence-dialog').addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { event.preventDefault(); closeEvidence(); }
  });
  $('evidence-dialog').addEventListener('click', (event) => {
    if (event.target !== $('evidence-dialog')) return;
    const box = $('evidence-dialog').getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) closeEvidence();
  });
  function inspectCitation(citation) {
    if (!authenticated()) return;
    closeEvidence();
    $('evidence-text').textContent = 'Loading source evidence…';
    $('evidence-dialog').showModal();
    work('citation', null, $('evidence-text'), 'Inspecting cited passage…', async (request) => {
      const source = await request(`/api/documents/${encodeURIComponent(citation.document_id)}/chunks/${encodeURIComponent(citation.chunk_id)}`);
      $('evidence-title').textContent = source.title;
      $('evidence-meta').textContent = `${source.document_id} · Page ${source.page} · Chunk ${source.id}`;
      $('evidence-text').textContent = source.text;
      status('Citation loaded from the authorized chunk endpoint.');
    }, (message) => { $('evidence-text').textContent = message; });
  }

  $('compare-form').addEventListener('submit', (event) => {
    event.preventDefault();
    if (!authenticated(true)) return;
    const query = $('compare-query').value.trim();
    if (query.length < 2) { status('Enter a question with at least two non-space characters.', true); return; }
    empty('compare-results', 'Generating five live, permission-aware responses…');
    work('compare', event.submitter || $('compare-form').querySelector('button'), $('compare-results'), 'Comparing live backend personas…', async (request) => {
      const data = await request('/api/chat/compare-roles', { method: 'POST', body: { query } });
      $('compare-results').replaceChildren(...data.results.map((result) => {
        const card = node('article', null, 'card result-card');
        card.append(node('h2', result.role), node('p', result.persona_id, 'small'), answerContent(result));
        return card;
      }));
      status(`${data.results.length} live role responses returned.`);
    }, (message) => empty('compare-results', message));
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
    if (!authenticated()) return;
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

  function scopeFields(fieldset, selected = []) {
    const legend = fieldset.querySelector('legend');
    fieldset.replaceChildren(legend || node('legend', 'Allowed scopes (select at least one)'));
    for (const scope of state.config?.scopes ?? []) {
      const label = node('label', null, 'scope-option');
      const checkbox = node('input');
      checkbox.type = 'checkbox';
      checkbox.name = 'allowed_scopes';
      checkbox.value = scope;
      checkbox.checked = selected.includes(scope);
      label.append(checkbox, node('span', scope));
      fieldset.append(label);
    }
  }
  const selectedScopes = (container) => [...container.querySelectorAll('input[name="allowed_scopes"]:checked')].map((input) => input.value);

  function documentChanged(epoch, message) {
    clearEvidence();
    if (epoch !== undefined) {
      state.user.acl_epoch = epoch;
      $('context-epoch').textContent = epoch;
    } else $('context-epoch').textContent = 'Refresh on next response';
    status(`${message} Previous evidence and benchmark results have been cleared.`);
    loadDocuments();
    loadAudit();
  }
  function renderDocuments(documents) {
    const target = $('document-list');
    target.replaceChildren();
    if (!documents.length) { empty('document-list', 'No documents are currently visible. Upload a synthetic PDF to begin.'); return; }
    for (const doc of documents) {
      const card = node('article', null, 'card document-card');
      card.append(node('h3', doc.title), node('p', `${doc.id} · ${doc.pages} pages · ${doc.chunks} indexed chunks`, 'small'));
      const form = node('form');
      const fields = node('fieldset');
      scopeFields(fields, doc.allowed_scopes);
      const actions = node('div', null, 'document-actions');
      const save = node('button', 'Save scopes', 'button secondary');
      save.type = 'submit';
      const remove = node('button', 'Delete document', 'button danger');
      remove.type = 'button';
      actions.append(save, remove);
      form.append(fields, actions);
      card.append(form);
      form.addEventListener('submit', (event) => {
        event.preventDefault();
        if (!authenticated(true)) return;
        const scopes = selectedScopes(fields);
        if (!scopes.length) { status('Select at least one allowed scope before saving.', true); return; }
        work('mutation', save, card, 'Saving document scopes…', async (request) => {
          const result = await request(`/api/documents/${encodeURIComponent(doc.id)}/permissions`, { method: 'PATCH', body: { allowed_scopes: scopes } });
          documentChanged(result.acl_epoch, 'Document scopes saved.');
        });
      });
      remove.addEventListener('click', () => {
        if (!authenticated(true) || !window.confirm(`Delete “${doc.title}” and its indexed chunks? This cannot be undone. Deleting a seeded document can invalidate the fixed benchmark.`)) return;
        work('mutation', remove, card, 'Deleting document and indexed chunks…', async (request) => {
          await request(`/api/documents/${encodeURIComponent(doc.id)}`, { method: 'DELETE' });
          documentChanged(undefined, 'Document deleted.');
        });
      });
      target.append(card);
    }
  }
  function loadDocuments() {
    if (!authenticated(true)) return;
    empty('document-list', 'Loading the document library…');
    work('documents', $('refresh-documents'), $('document-list'), 'Loading current document access…', async (request) => {
      const data = await request('/api/documents');
      renderDocuments(data.documents);
      status(`${data.documents.length} documents loaded with current scopes.`);
    }, (message) => empty('document-list', message));
  }
  $('refresh-documents').addEventListener('click', loadDocuments);
  $('upload-form').addEventListener('submit', (event) => {
    event.preventDefault();
    if (!authenticated(true)) return;
    const file = $('pdf-file').files[0];
    const scopes = selectedScopes($('upload-scopes'));
    if (!file || !scopes.length) { status('Choose a PDF and at least one allowed scope.', true); return; }
    if (file.type && file.type !== 'application/pdf') { status('Only PDF documents are accepted.', true); return; }
    const form = new FormData();
    form.append('file', file.type ? file : new File([file], file.name, { type: 'application/pdf' }));
    form.append('allowed_scopes', JSON.stringify(scopes));
    work('mutation', event.submitter || $('upload-form').querySelector('button'), $('upload-form'), 'Uploading and indexing the PDF…', async (request) => {
      const result = await request('/api/documents/upload', { method: 'POST', body: form });
      $('upload-form').reset();
      documentChanged(result.acl_epoch, `Document ${result.document_id} uploaded and indexed.`);
    });
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

  // Authentication never persists across reloads or browser history restoration.
  window.addEventListener('pagehide', () => resetSession('Signed out. Reload or choose a persona to begin a new session.'));
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) {
      resetSession('Restored page is signed out. Choose a persona to begin a new session.');
      if (!state.config) loadConfig();
    }
  });
  function loadConfig() {
    work('config', null, null, 'Loading workspace configuration…', async (request) => {
      const config = await request('/api/config', { publicRequest: true });
      state.config = config;
      const select = $('persona');
      select.replaceChildren();
      for (const [id, role] of Object.entries(config.personas)) {
        const option = node('option', `${role} · ${id}`);
        option.value = id;
        select.append(option);
      }
      if (Object.hasOwn(config.personas, 'general_user')) select.value = 'general_user';
      select.disabled = !config.demo_enabled || !select.options.length;
      $('demo-form').hidden = !config.demo_enabled;
      $('demo-note').textContent = config.demo_enabled ? 'Choose a persona, then explicitly enter the demo.' : 'Demo access is disabled by the server. Sign in with your credentials.';
      $('credentials').open = !config.demo_enabled && !state.user;
      scopeFields($('upload-scopes'));
      updateSession();
      if (!state.tasks.has('auth')) {
        status(state.user ? `Signed in as ${state.user.id}. Workspace configuration loaded.`
          : (config.demo_enabled ? 'Signed out · Choose a demo persona and enter the workspace to begin.' : 'Signed out · Use your credentials to access this workspace.'));
      }
    }, () => {
      $('persona').replaceChildren(node('option', 'Configuration unavailable'));
      $('demo-note').textContent = 'Configuration could not be loaded. Reload to retry, or sign in with credentials.';
      $('credentials').open = true;
    });
  }
  updateSession();
  loadConfig();
})();
