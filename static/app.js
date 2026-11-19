'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    config: null, documents: [], selected: new Set(),
    conversation: null, session: 0, chatVersion: 0, sourceVersion: 0,
    mutating: false, chatting: false, requests: new Set(), previewUrl: null,
  };
  const element = (tag, text, className) => {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = String(text);
    if (className) result.className = className;
    return result;
  };
  const ready = (doc) => !['pending', 'processing', 'uploading', 'failed', 'error'].includes(doc.status);
  const selectedDocuments = () => state.documents.filter((doc) => ready(doc) && state.selected.has(doc.id));
  const encoded = (value) => encodeURIComponent(String(value));
  const cancelled = () => new DOMException('Request no longer current', 'AbortError');
  const errorMessage = (error) => error instanceof TypeError ? 'Unable to reach OrgMind. Check your connection and try again.' : error.message;

  function status(message, error = false) {
    $('global-status').textContent = message;
    $('global-status').classList.toggle('error', error);
  }

  async function request(path, { method = 'GET', body, image = false } = {}) {
    const session = state.session;
    const controller = new AbortController();
    state.requests.add(controller);
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 120000);
    const headers = { Accept: image ? 'image/png' : 'application/json' };
    if (method !== 'GET') headers['X-OrgMind-Request'] = '1';
    const multipart = body instanceof FormData;
    if (body !== undefined && !multipart) headers['Content-Type'] = 'application/json';
    try {
      const response = await fetch(path, {
        method, headers, credentials: 'same-origin', cache: 'no-store',
        body: body === undefined ? undefined : multipart ? body : JSON.stringify(body),
        signal: controller.signal,
      });
      if (session !== state.session) throw cancelled();
      let data;
      if (response.status === 204) {
        data = null;
      } else if (image && response.ok) {
        if (response.headers.get('Content-Type')?.split(';')[0] !== 'image/png') throw new Error('The PDF page preview could not be loaded.');
        data = await response.blob();
      } else {
        try { data = await response.json(); } catch { throw new Error(`OrgMind returned an unexpected response (${response.status}). Try again.`); }
      }
      if (session !== state.session) throw cancelled();
      if (!response.ok) {
        const detail = typeof data?.detail === 'string' ? data.detail : null;
        const error = new Error(detail || (response.status === 429 ? 'Too many requests. Please wait a moment and retry.' : `Request failed (${response.status}). Please try again.`));
        error.status = response.status;
        throw error;
      }
      return data;
    } catch (error) {
      if (timedOut && session === state.session) throw new Error('This request took too long. Please try again.');
      throw error;
    } finally {
      clearTimeout(timer);
      state.requests.delete(controller);
    }
  }

  function closeSource() {
    state.sourceVersion += 1;
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.previewUrl = null;
    $('source-image').removeAttribute('src');
    $('source-image').hidden = true;
    $('source-text').textContent = '';
    $('source-title').textContent = 'Document source';
    $('source-status').textContent = '';
    $('preview-status').textContent = '';
    if ($('source-dialog').open) $('source-dialog').close();
  }

  function resetChat() {
    state.chatVersion += 1;
    state.chatting = false;
    state.conversation = null;
    $('chat-messages').replaceChildren();
    $('chat-empty').hidden = false;
    $('chat-progress').hidden = true;
    $('query').value = '';
    closeSource();
    updateControls();
  }

  function updateControls() {
    const docs = selectedDocuments();
    const busy = state.mutating || !state.config;
    const canChat = !busy && !state.chatting && docs.length > 0 && state.config?.generation_available !== false;
    $('pdf-files').disabled = busy;
    $('load-sample').disabled = busy || state.documents.some((doc) => doc.is_sample);
    $('load-sample').hidden = state.documents.some((doc) => doc.is_sample);
    $('query').disabled = !canChat;
    $('send-button').disabled = !canChat || !$('query').value.trim();
    $('clear-conversation').disabled = busy || (!state.conversation && !$('chat-messages').children.length && !state.chatting);
    $('select-all').disabled = busy;
    $('select-all').hidden = !state.documents.length || docs.length === state.documents.filter(ready).length;
    $('chat-selection').textContent = !state.documents.length ? 'Add a PDF to get started' : !docs.length ? 'Select a document to ask about' : `${docs.length > 1 && docs.length === state.documents.filter(ready).length ? 'All' : docs.length} ${docs.length === 1 ? 'document' : 'documents'} selected`;
    $('sample-suggestions').hidden = !docs.some((doc) => doc.is_sample);
    document.querySelectorAll('.suggestion').forEach((button) => { button.disabled = !canChat; });
    document.querySelectorAll('.document-check, .remove-document').forEach((control) => { control.disabled = busy || (control.classList.contains('document-check') && control.dataset.ready !== 'true'); });
  }

  function renderDocuments() {
    $('app-shell').classList.toggle('is-empty', !state.documents.length);
    $('main').classList.toggle('has-documents', state.documents.length > 0);
    $('chat-panel').hidden = !state.documents.length;
    $('document-count').textContent = String(state.documents.length);
    $('documents-empty').hidden = state.documents.length > 0;
    $('document-list').replaceChildren(...state.documents.map((doc) => {
      const item = element('li', undefined, `document-item${state.selected.has(doc.id) ? ' selected' : ''}`);
      const label = element('label', undefined, 'document-label');
      const check = element('input', undefined, 'document-check');
      check.type = 'checkbox';
      check.checked = state.selected.has(doc.id);
      check.dataset.ready = String(ready(doc));
      check.dataset.documentId = doc.id;
      check.setAttribute('aria-label', `Use ${doc.filename} for questions`);
      check.addEventListener('change', () => {
        if (check.checked) state.selected.add(doc.id); else state.selected.delete(doc.id);
        resetChat();
        renderDocuments();
        document.querySelectorAll('.document-check').forEach((control) => {
          if (control.dataset.documentId === doc.id) control.focus();
        });
        status('Document selection updated. A fresh conversation is ready.');
      });
      const detail = element('span');
      detail.append(element('span', doc.filename, 'document-name'));
      const pageLabel = `${doc.pages} ${doc.pages === 1 ? 'page' : 'pages'}`;
      detail.append(element('span', `${pageLabel} · ${doc.status}${doc.is_sample ? ' · Sample' : ''}`, 'document-meta'));
      label.append(check, detail);
      const remove = element('button', '×', 'remove-document');
      remove.type = 'button';
      remove.setAttribute('aria-label', `Remove ${doc.filename}`);
      remove.title = `Remove ${doc.filename}`;
      remove.addEventListener('click', () => removeDocument(doc));
      item.append(label, remove);
      return item;
    }));
    updateControls();
  }

  async function loadDocuments(selectAll = false, current = () => true) {
    const data = await request('/api/documents');
    if (!current()) return;
    state.documents = data.documents;
    const available = new Set(state.documents.filter(ready).map((doc) => doc.id));
    state.selected = selectAll ? available : new Set([...state.selected].filter((id) => available.has(id)));
    renderDocuments();
  }

  async function start() {
    const session = ++state.session;
    for (const controller of state.requests) controller.abort();
    state.config = null;
    state.mutating = true;
    updateControls();
    $('retry-config').hidden = true;
    status('Opening OrgMind…');
    try {
      const config = await request('/api/config');
      state.config = config;
      $('storage-notice').hidden = !config.ephemeral_storage;
      $('generation-notice').hidden = config.generation_available !== false;
      $('upload-limit').textContent = `PDF files · up to ${Math.max(1, Math.round(config.max_upload_bytes / 1024 / 1024))} MB each`;
      $('query').maxLength = config.max_query_chars;
      const retention = Number.isInteger(config.session_ttl_days) && config.session_ttl_days > 0
        ? ` Workspace access expires after ${config.session_ttl_days} days without activity.` : '';
      $('session-notice').textContent = `Your documents are private to this browser’s cookie. Cookie access does not prove identity.${retention} There are no accounts or account recovery; clearing cookies loses access. Keep your original files.`;
      await loadDocuments(true);
      resetChat();
      $('app-shell').hidden = false;
      status(state.documents.length ? 'Your PDFs are ready.' : 'Ready when you are. Add a PDF or try the sample.');
    } catch (error) {
      if (session === state.session && error.name !== 'AbortError') {
        state.config = null;
        status(errorMessage(error), true);
        $('retry-config').hidden = false;
      }
    } finally {
      if (session === state.session) { state.mutating = false; updateControls(); }
    }
  }

  async function mutate(action) {
    if (state.mutating || !state.config) return;
    const session = state.session;
    state.mutating = true;
    updateControls();
    try { await action(); }
    catch (error) { if (session === state.session && error.name !== 'AbortError') status(errorMessage(error), true); }
    finally {
      if (session === state.session) { state.mutating = false; updateControls(); }
    }
  }

  function addDocument(doc) {
    state.documents = [...state.documents.filter((item) => item.id !== doc.id), doc];
    if (ready(doc)) state.selected.add(doc.id);
    renderDocuments();
  }

  async function uploadFiles(files) {
    if (!files.length) return;
    await mutate(async () => {
      resetChat();
      let uploaded = 0;
      const failures = [];
      for (let index = 0; index < files.length; index += 1) {
        const file = files[index];
        const progress = `Uploading ${index + 1} of ${files.length}: ${file.name}`;
        $('upload-status').textContent = progress;
        status(progress);
        if (!file.name.toLowerCase().endsWith('.pdf')) { failures.push(`${file.name}: choose a PDF file.`); continue; }
        if (file.size > state.config.max_upload_bytes) { failures.push(`${file.name}: exceeds the upload limit.`); continue; }
        try {
          const body = new FormData();
          body.append('file', file);
          addDocument(await request('/api/documents/upload', { method: 'POST', body }));
          uploaded += 1;
        } catch (error) {
          if (error.name === 'AbortError') throw error;
          failures.push(`${file.name}: ${errorMessage(error)}`);
        }
      }
      const message = `${uploaded} ${uploaded === 1 ? 'PDF' : 'PDFs'} added.${failures.length ? ` ${failures.join(' ')}` : ' Ready for your questions.'}`;
      $('upload-status').textContent = message;
      $('upload-status').classList.toggle('error', failures.length > 0);
      status(message, failures.length > 0);
      $('pdf-files').value = '';
    });
    if (!$('query').disabled) $('query').focus();
  }

  async function removeDocument(doc) {
    await mutate(async () => {
      // Invalidate evidence before deletion, including any in-flight answer or preview.
      resetChat();
      status(`Removing ${doc.filename}…`);
      try { await request(`/api/documents/${encoded(doc.id)}`, { method: 'DELETE' }); }
      catch (error) {
        if (error.status !== 404) throw error;
        await loadDocuments(false);
        status('This document is no longer available. Your library has been refreshed.', true);
        return;
      }
      state.documents = state.documents.filter((item) => item.id !== doc.id);
      state.selected = new Set(state.documents.filter(ready).map((item) => item.id));
      renderDocuments();
      status(`${doc.filename} removed. Conversation cleared.`);
    });
    const remaining = document.querySelectorAll('.document-check');
    if (remaining.length) remaining[0].focus(); else $('pdf-files').focus();
  }

  function scrollChat() { $('chat-scroll').scrollTop = $('chat-scroll').scrollHeight; }

  function addMessage(kind, text) {
    const message = element('article', undefined, `message ${kind}`);
    message.append(element('span', kind === 'user' ? 'YOU' : 'ORGMIND', 'message-label'));
    message.append(element('p', text, 'message-text'));
    $('chat-messages').append(message);
    $('chat-empty').hidden = true;
    scrollChat();
    return message;
  }

  async function askQuestion() {
    const query = $('query').value.trim();
    const docs = selectedDocuments();
    if (!query || !docs.length || state.chatting || state.mutating || !state.config || state.config.generation_available === false) return;
    if (query.length > state.config.max_query_chars) { status(`Keep your question under ${state.config.max_query_chars} characters.`, true); return; }
    const version = ++state.chatVersion;
    const session = state.session;
    const current = () => version === state.chatVersion && session === state.session;
    state.chatting = true;
    addMessage('user', query);
    $('query').value = '';
    $('chat-progress').hidden = false;
    updateControls();
    scrollChat();
    status('Finding an answer in your selected PDFs…');
    try {
      const answer = await request('/api/chat', { method: 'POST', body: {
        query, document_ids: docs.map((doc) => doc.id), conversation_id: state.conversation,
      } });
      if (!current()) return;
      state.conversation = answer.conversation_id;
      const message = addMessage('assistant', answer.answer);
      if (answer.status === 'insufficient_evidence') {
        message.insertBefore(element('p', 'Not enough evidence in the selected documents.', 'message-note'), message.lastChild);
      }
      if (answer.citations?.length) {
        const citations = element('div', undefined, 'citations');
        answer.citations.forEach((citation, index) => {
          const button = element('button', `${index + 1}. ${citation.filename} · p. ${citation.page}`, 'citation');
          button.type = 'button';
          button.setAttribute('aria-label', `View source ${index + 1}: ${citation.filename}, page ${citation.page}`);
          button.addEventListener('click', () => openSource(citation));
          citations.append(button);
        });
        message.append(citations);
      }
      status(answer.status === 'insufficient_evidence' ? 'The selected documents do not provide enough evidence for this question.' : 'Answer ready. Open a source to check the original page.');
    } catch (error) {
      if (current() && error.name !== 'AbortError') {
        let message = errorMessage(error);
        if (error.status === 404) {
          state.conversation = null;
          message = 'This conversation or document is no longer available. It may have expired. Send your question again to start a new conversation with the available documents.';
          try { await loadDocuments(false, current); }
          catch (refreshError) { message += ` Could not refresh your library: ${errorMessage(refreshError)}`; }
          if (!current()) return;
        }
        const failure = element('p', `No answer was generated. ${message}`, 'message error');
        failure.setAttribute('role', 'alert');
        $('chat-messages').append(failure);
        $('query').value = query;
        status(message, true);
      }
    } finally {
      if (current()) {
        state.chatting = false;
        $('chat-progress').hidden = true;
        updateControls();
        scrollChat();
        $('query').focus();
      }
    }
  }

  async function openSource(citation) {
    closeSource();
    if (!state.documents.some((doc) => doc.id === citation.document_id)) return;
    const version = state.sourceVersion;
    const session = state.session;
    const current = () => version === state.sourceVersion && session === state.session && state.documents.some((doc) => doc.id === citation.document_id);
    $('source-title').textContent = `${citation.filename} · page ${citation.page}`;
    $('source-status').textContent = 'Loading the cited passage…';
    $('preview-status').textContent = 'Loading page preview…';
    $('source-dialog').showModal();
    try {
      const source = await request(`/api/documents/${encoded(citation.document_id)}/sources/${encoded(citation.chunk_id)}`);
      if (!current()) return;
      $('source-text').textContent = source.text;
      $('source-status').textContent = `${source.filename} · page ${source.page}`;
      try {
        const blob = await request(`/api/documents/${encoded(citation.document_id)}/preview?page=${encoded(source.page)}`, { image: true });
        if (!current()) return;
        state.previewUrl = URL.createObjectURL(blob);
        $('source-image').src = state.previewUrl;
        $('source-image').alt = `${source.filename}, original PDF page ${source.page}`;
        $('source-image').hidden = false;
        $('preview-status').textContent = '';
      } catch (error) {
        if (current() && error.name !== 'AbortError') $('preview-status').textContent = `Preview unavailable. ${errorMessage(error)}`;
      }
    } catch (error) {
      if (current() && error.name !== 'AbortError') {
        $('source-status').textContent = errorMessage(error);
        $('preview-status').textContent = 'Preview unavailable.';
      }
    }
  }

  $('retry-config').addEventListener('click', start);
  $('upload-form').addEventListener('submit', (event) => event.preventDefault());
  $('pdf-files').addEventListener('change', (event) => uploadFiles(Array.from(event.target.files)));
  $('drop-zone').addEventListener('dragover', (event) => { event.preventDefault(); if (!state.mutating) $('drop-zone').classList.add('dragging'); });
  $('drop-zone').addEventListener('dragleave', () => $('drop-zone').classList.remove('dragging'));
  $('drop-zone').addEventListener('drop', (event) => {
    event.preventDefault();
    $('drop-zone').classList.remove('dragging');
    uploadFiles(Array.from(event.dataTransfer.files));
  });
  $('load-sample').addEventListener('click', async () => {
    await mutate(async () => {
      resetChat();
      status('Loading the Personal Finance & Budgeting Guide…');
      $('upload-status').textContent = 'Preparing the sample PDF…';
      $('upload-status').classList.remove('error');
      try {
        addDocument(await request('/api/documents/sample', { method: 'POST' }));
        $('upload-status').textContent = 'Sample ready. Try a question in the chat.';
        status('Sample loaded. Choose a suggested question or write your own.');
      } catch (error) { $('upload-status').textContent = ''; throw error; }
    });
    if (!$('query').disabled) $('query').focus();
  });
  $('select-all').addEventListener('click', () => {
    state.selected = new Set(state.documents.filter(ready).map((doc) => doc.id));
    resetChat();
    renderDocuments();
    status('All ready documents selected. A fresh conversation is ready.');
    if (!$('query').disabled) $('query').focus();
  });
  $('clear-conversation').addEventListener('click', async () => {
    await mutate(async () => {
      const conversation = state.conversation;
      resetChat();
      status('Clearing conversation…');
      try { await request('/api/conversations/clear', { method: 'POST', body: { conversation_id: conversation } }); }
      catch (error) {
        if (error.status !== 404) throw error;
        status('That conversation is already unavailable. A fresh chat is ready.');
        return;
      }
      status('Conversation cleared. Ask a new question.');
    });
    if (!$('query').disabled) $('query').focus();
  });
  $('chat-form').addEventListener('submit', (event) => { event.preventDefault(); askQuestion(); });
  $('query').addEventListener('input', updateControls);
  $('query').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); askQuestion(); }
  });
  document.querySelectorAll('.suggestion').forEach((button) => button.addEventListener('click', () => {
    if (!selectedDocuments().some((doc) => doc.is_sample)) return;
    $('query').value = button.dataset.query;
    updateControls();
    askQuestion();
  }));
  $('close-source').addEventListener('click', closeSource);
  $('source-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeSource(); });
  $('source-dialog').addEventListener('click', (event) => { if (event.target === $('source-dialog')) closeSource(); });
  window.addEventListener('pagehide', closeSource);
  start();
})();
