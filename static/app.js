'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    config: null, documents: [], selected: new Set(),
    conversation: null, session: 0, chatVersion: 0, sourceVersion: 0,
    mutating: false, chatting: false, requests: new Set(), previewUrl: null,
    suggestions: [], suggestionVersion: 0,
  };
  function installDotField() {
    const canvas = $('dot-field');
    const context = canvas.getContext?.('2d');
    if (!context) return;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    const compact = window.matchMedia('(max-width: 520px)');
    const points = [];
    const ripples = [];
    const pointer = { x: -10000, y: -10000, active: false, lastX: -10000, lastY: -10000 };
    let width = 0, height = 0, frame = 0, lastTime = 0;
    const spacing = 22;
    function resize() {
      const ratio = Math.min(window.devicePixelRatio || 1, 1.5);
      width = window.innerWidth; height = window.innerHeight;
      canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      points.length = 0;
      for (let y = -spacing; y < height + spacing; y += spacing) {
        for (let x = -spacing; x < width + spacing; x += spacing) points.push({ x, y, dx: 0, dy: 0 });
      }
      draw(performance.now(), true);
    }
    function draw(time, stationary = false) {
      context.clearRect(0, 0, width, height);
      const style = getComputedStyle(document.documentElement);
      const color = style.getPropertyValue('--dot').trim() || '#91c5d8';
      const activeColor = style.getPropertyValue('--dot-active').trim() || color;
      const strongColor = style.getPropertyValue('--dot-strong').trim() || activeColor;
      const forceScale = Number.parseFloat(style.getPropertyValue('--dot-force')) || 16;
      const motionOpacity = Number.parseFloat(style.getPropertyValue('--dot-motion-opacity')) || .64;
      const proximityOpacity = Number.parseFloat(style.getPropertyValue('--dot-proximity-opacity')) || 0;
      const rippleOpacity = Number.parseFloat(style.getPropertyValue('--dot-ripple-opacity')) || 0;
      const growth = Number.parseFloat(style.getPropertyValue('--dot-growth')) || 0;
      const motion = !reduced.matches && !compact.matches && !stationary;
      const dt = Math.min((time - (lastTime || time)) / 1000, 0.04);
      lastTime = time;
      const age = time / 1000;
      const radius = 138;
      for (const dot of points) {
        let tx = 0, ty = 0, proximity = 0, rippleStrength = 0;
        if (motion) {
          const wave = Math.sin(dot.x * .006 + age * .48) * Math.cos(dot.y * .007 - age * .38);
          tx += Math.cos(dot.x * .009 + dot.y * .004) * wave * 1.25;
          ty += Math.sin(dot.y * .008 - dot.x * .003) * wave * 1.25;
          if (pointer.active) {
            const vx = dot.x - pointer.x, vy = dot.y - pointer.y, distance = Math.hypot(vx, vy);
            if (distance < radius && distance > 0) {
              proximity = (1 - distance / radius) ** 2;
              const force = proximity * forceScale;
              tx += vx / distance * force; ty += vy / distance * force;
            }
          }
          for (const ripple of ripples) {
            const distance = Math.hypot(dot.x - ripple.x, dot.y - ripple.y);
            const phase = distance - (time - ripple.time) * .34;
            if (Math.abs(phase) < 34) {
              const strength = (1 - (time - ripple.time) / 950) * Math.exp(-(phase * phase) / 240);
              rippleStrength = Math.max(rippleStrength, strength);
              tx += (dot.x - ripple.x) / Math.max(distance, 1) * strength * 4;
              ty += (dot.y - ripple.y) / Math.max(distance, 1) * strength * 4;
            }
          }
        }
        const spring = 1 - Math.exp(-dt * 8.5);
        dot.dx += (tx - dot.dx) * spring; dot.dy += (ty - dot.dy) * spring;
        const depth = .72 + .28 * (Math.sin(dot.x * .004 + dot.y * .005 + age * .2) + 1) / 2;
        context.globalAlpha = Math.min(1, depth * (motion ? motionOpacity : .7)
          + proximity * proximityOpacity + rippleStrength * rippleOpacity);
        context.fillStyle = proximity > .52 || rippleStrength > .6 ? strongColor : proximity > .025 || rippleStrength > .12 ? activeColor : color;
        context.beginPath(); context.arc(dot.x + dot.dx, dot.y + dot.dy, 1.05 + proximity * growth + rippleStrength * growth * .47, 0, Math.PI * 2); context.fill();
      }
      context.globalAlpha = 1;
      while (ripples.length && time - ripples[0].time > 950) ripples.shift();
      frame = motion && !document.hidden ? requestAnimationFrame(draw) : 0;
    }
    function onPointer(event) {
      const dx = event.clientX - pointer.lastX, dy = event.clientY - pointer.lastY;
      pointer.x = event.clientX; pointer.y = event.clientY; pointer.active = true;
      if (Math.hypot(dx, dy) > 18 && ripples.length < 4) {
        ripples.push({ x: pointer.x, y: pointer.y, time: performance.now() });
      }
      pointer.lastX = pointer.x; pointer.lastY = pointer.y;
      if (!frame && !reduced.matches && !compact.matches && !document.hidden) frame = requestAnimationFrame(draw);
    }
    window.addEventListener('pointermove', onPointer, { passive: true });
    window.addEventListener('pointerout', (event) => { if (!event.relatedTarget) pointer.active = false; });
    window.addEventListener('resize', resize, { passive: true });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { cancelAnimationFrame(frame); frame = 0; }
      else if (!reduced.matches && !compact.matches) frame = requestAnimationFrame(draw);
      else draw(performance.now(), true);
    });
    reduced.addEventListener?.('change', resize); compact.addEventListener?.('change', resize);
    $('theme-toggle').addEventListener('click', () => {
      const theme = document.documentElement.dataset.theme === 'night' ? 'day' : 'night';
      document.documentElement.dataset.theme = theme;
      document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'night' ? '#080b0f' : '#eaf7fb');
      try { localStorage.setItem('orgmind-theme', theme); } catch { /* Theme remains active for this page. */ }
      $('theme-toggle').setAttribute('aria-label', `Switch to ${theme === 'night' ? 'day' : 'night'} theme`);
      $('theme-toggle').querySelector('.theme-label').textContent = theme === 'night' ? 'Day' : 'Night';
      $('theme-toggle').querySelector('[aria-hidden="true"]').textContent = theme === 'night' ? '☼' : '☾';
      draw(performance.now(), reduced.matches || compact.matches);
    });
    $('theme-toggle').setAttribute('aria-label', `Switch to ${document.documentElement.dataset.theme === 'night' ? 'day' : 'night'} theme`);
    $('theme-toggle').querySelector('.theme-label').textContent = document.documentElement.dataset.theme === 'night' ? 'Day' : 'Night';
    $('theme-toggle').querySelector('[aria-hidden="true"]').textContent = document.documentElement.dataset.theme === 'night' ? '☼' : '☾';
    resize();
    if (!reduced.matches && !compact.matches) frame = requestAnimationFrame(draw);
  }
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
    $('source-passages').replaceChildren();
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
    $('discovery-suggestions').hidden = !docs.length || !state.suggestions.length || $('chat-empty').hidden;
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
        void loadSuggestions();
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
    await loadSuggestions(current);
  }

  async function loadSuggestions(current = () => true) {
    const version = ++state.suggestionVersion;
    const ids = selectedDocuments().map((doc) => doc.id);
    state.suggestions = [];
    $('suggestion-list').replaceChildren();
    updateControls();
    if (!ids.length) return;
    try {
      const params = new URLSearchParams();
      ids.forEach((id) => params.append('document_ids', id));
      const result = await request(`/api/suggestions?${params.toString()}`);
      if (!current() || version !== state.suggestionVersion) return;
      state.suggestions = result.suggestions || [];
      $('suggestion-list').replaceChildren(...state.suggestions.map((item) => {
        const button = element('button', undefined, 'suggestion');
        button.type = 'button';
        button.append(element('span', item.question, 'suggestion-question'), element('span', '↗'));
        button.addEventListener('click', () => {
          if (button.disabled || state.chatting || state.mutating) return;
          $('query').value = item.question;
          updateControls();
          askQuestion(item.question, item.document_ids);
        });
        return button;
      }));
      updateControls();
    } catch (error) {
      if (current() && error.name !== 'AbortError') status(`Suggested questions are unavailable. ${errorMessage(error)}`, true);
    }
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
        } catch (error) {
          if (error.name === 'AbortError') throw error;
          failures.push(`${file.name}: ${errorMessage(error)}`);
        }
      }
      const libraryCount = state.documents.filter(ready).length;
      const message = `${libraryCount} ${libraryCount === 1 ? 'PDF' : 'PDFs'} in your library.${failures.length ? ` ${failures.join(' ')}` : ' Ready for your questions.'}`;
      $('upload-status').textContent = message;
      $('upload-status').classList.toggle('error', failures.length > 0);
      status(message, failures.length > 0);
      $('pdf-files').value = '';
    });
    await loadSuggestions();
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
    await loadSuggestions();
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

  async function askQuestion(questionOverride = null, documentIdsOverride = null) {
    const query = (questionOverride ?? $('query').value).trim();
    const selected = selectedDocuments();
    const docs = documentIdsOverride ? selected.filter((doc) => documentIdsOverride.includes(doc.id)) : selected;
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
        const grouped = new Map();
        answer.citations.forEach((citation) => {
          const key = `${citation.document_id}\u0000${citation.page}`;
          if (!grouped.has(key)) grouped.set(key, []);
          grouped.get(key).push(citation);
        });
        [...grouped.values()].forEach((sources, index) => {
          const citation = sources[0];
          const count = sources.length;
          const button = element('button', `${index + 1}. ${citation.filename} — Page ${citation.page}${count > 1 ? ` · ${count} supporting passages` : ''}`, 'citation');
          button.type = 'button';
          button.setAttribute('aria-label', `View ${count} supporting ${count === 1 ? 'passage' : 'passages'} from ${citation.filename}, page ${citation.page}`);
          button.addEventListener('click', () => openSource(sources));
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

  async function openSource(citations) {
    closeSource();
    const group = Array.isArray(citations) ? citations : [citations];
    const citation = group[0];
    if (!state.documents.some((doc) => doc.id === citation.document_id)) return;
    const version = state.sourceVersion;
    const session = state.session;
    const current = () => version === state.sourceVersion && session === state.session && state.documents.some((doc) => doc.id === citation.document_id);
    $('source-title').textContent = `${citation.filename} · page ${citation.page}`;
    $('source-status').textContent = 'Loading the cited passage…';
    $('preview-status').textContent = 'Loading page preview…';
    $('source-dialog').showModal();
    try {
      const sources = await Promise.all(group.map((item) => request(`/api/documents/${encoded(item.document_id)}/sources/${encoded(item.chunk_id)}`)));
      if (!current()) return;
      $('source-passages').replaceChildren(...sources.map((source, index) => {
        const passage = element('section', undefined, 'source-passage');
        passage.append(element('h4', `Supporting passage ${index + 1}`), element('p', source.text, 'source-text'));
        return passage;
      }));
      $('source-status').textContent = `${sources.length} supporting ${sources.length === 1 ? 'passage' : 'passages'} · ${citation.filename} · page ${citation.page}`;
      try {
        const blob = await request(`/api/documents/${encoded(citation.document_id)}/preview?page=${encoded(citation.page)}`, { image: true });
        if (!current()) return;
        state.previewUrl = URL.createObjectURL(blob);
        $('source-image').src = state.previewUrl;
        $('source-image').alt = `${citation.filename}, original PDF page ${citation.page}`;
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

  installDotField();
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
    await loadSuggestions();
    if (!$('query').disabled) $('query').focus();
  });
  $('select-all').addEventListener('click', () => {
    state.selected = new Set(state.documents.filter(ready).map((doc) => doc.id));
    resetChat();
    renderDocuments();
    void loadSuggestions();
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
  $('close-source').addEventListener('click', closeSource);
  $('source-dialog').addEventListener('cancel', (event) => { event.preventDefault(); closeSource(); });
  $('source-dialog').addEventListener('click', (event) => { if (event.target === $('source-dialog')) closeSource(); });
  window.addEventListener('pagehide', closeSource);
  start();
})();
