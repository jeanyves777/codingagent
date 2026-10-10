'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const token = new URLSearchParams(location.hash.slice(1)).get('token');
  let currentProject = null;
  let knownProjects = [];
  let mode = 'chat';
  let activeJob = null;
  let cursor = 0;
  let displayedEvents = new Set();
  let pollTimer = null;
  let startedAt = 0;
  let expanded = new Set();
  let activityCount = 0;

  async function api(path, method = 'GET', body) {
    const options = {method, headers: {'X-CodingBrain-Token': token || ''}};
    if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
    const response = await fetch(path, options);
    let result;
    try { result = await response.json(); } catch { throw new Error('Could not reach the Coding Brain UI bridge'); }
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Request failed');
    return result;
  }

  function toast(message) {
    $('toast').textContent = message;
    $('toast').hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => $('toast').hidden = true, 4400);
  }

  function message(author, text, variant = 'assistant') {
    $('welcome').hidden = true;
    const node = document.createElement('div');
    node.className = 'message ' + variant;
    const label = document.createElement('div');
    label.className = 'message-head';
    label.textContent = author;
    const content = document.createElement('div');
    content.textContent = text;
    node.append(label, content);
    $('thread').append(node);
    $('thread').scrollTop = $('thread').scrollHeight;
    return node;
  }

  function setMode(value) {
    mode = value;
    for (const kind of ['chat', 'run']) {
      const button = $('mode-' + kind);
      button.classList.toggle('active', kind === value);
      button.setAttribute('aria-pressed', String(kind === value));
    }
    $('prompt').placeholder = value === 'chat' ? 'Ask Coding Brain anything...' : 'Describe a coding goal...';
  }

  function updateProject(project) {
    currentProject = project;
    $('project-label').textContent = project ? project.name : 'Choose project';
    $('project-breadcrumb').textContent = project ? project.path : 'Global conversation';
    $('explorer-folder').textContent = project ? project.name : 'No project selected';
    $('branch-name').textContent = project && project.git ? '◇ ' + project.git : '◇ No repository';
    $('file-count').textContent = '';
    expanded.clear();
    $('preview').hidden = true;
    if (project) loadTree();
    else $('file-tree').textContent = 'Select a project to browse its files.';
  }

  function rowFor(entry, depth = 0) {
    const container = document.createElement('div');
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'tree-row' + (entry.dir ? ' folder' : '');
    row.style.paddingLeft = (9 + depth * 10) + 'px';
    const icon = document.createElement('span');
    icon.className = 'g';
    icon.textContent = entry.dir ? (expanded.has(entry.path) ? '▾' : '▸') : '⌁';
    const text = document.createElement('span');
    text.textContent = entry.name;
    row.append(icon, text);
    container.append(row);
    const children = document.createElement('div');
    children.className = 'tree-children';
    children.hidden = !expanded.has(entry.path);
    if (entry.dir) container.append(children);
    row.addEventListener('click', async () => {
      if (!entry.dir) {
        try {
          const file = await api('/api/file?path=' + encodeURIComponent(entry.path));
          $('preview-name').textContent = file.path;
          $('preview-content').textContent = file.content;
          $('preview').hidden = false;
        } catch (e) { toast(e.message); }
        return;
      }
      if (expanded.has(entry.path)) {
        expanded.delete(entry.path);
        children.hidden = true;
        icon.textContent = '▸';
      } else {
        expanded.add(entry.path);
        children.hidden = false;
        icon.textContent = '▾';
        await loadFolder(entry.path, children, depth + 1);
      }
    });
    return container;
  }

  async function loadFolder(folder = '', parent = $('file-tree'), depth = 0) {
    try {
      const response = await api('/api/tree?path=' + encodeURIComponent(folder));
      parent.replaceChildren();
      for (const entry of response.entries) {
        parent.append(rowFor(entry, depth));
      }
      if (!folder) $('file-count').textContent = `${response.entries.length} items`;
      if (response.entries.length === 0) {
        const empty = document.createElement('p');
        empty.className = 'muted';
        empty.textContent = 'Folder is empty.';
        parent.append(empty);
      }
    } catch (e) {
      parent.textContent = e.message;
    }
  }
  async function loadTree() { if (currentProject) await loadFolder(); }

  function openDialog() {
    $('picker-error').textContent = '';
    $('recent-projects').replaceChildren();
    if (!knownProjects.length) {
      const empty = document.createElement('span');
      empty.className = 'muted';
      empty.textContent = 'No recent projects yet.';
      $('recent-projects').append(empty);
    }
    for (const project of knownProjects) {
      const button = document.createElement('button');
      button.type = 'button';
      const label = document.createElement('strong');
      label.textContent = project.name;
      const path = document.createElement('small');
      path.textContent = project.path;
      button.append(label, path);
      button.addEventListener('click', () => choose(project.path));
      $('recent-projects').append(button);
    }
    $('project-dialog').showModal();
  }
  async function choose(path) {
    try {
      const project = await api('/api/project', 'POST', {path});
      updateProject(project);
      $('project-dialog').close();
      if (!knownProjects.some(p => p.path === project.path)) knownProjects.push(project);
      $('footer-status').textContent = project.git ? `${project.name} • ${project.git}` : `${project.name} • no Git branch`;
    } catch (e) { $('picker-error').textContent = e.message; }
  }

  function activityEntry(event) {
    const node = document.createElement('div');
    node.className = 'activity-entry';
    const clock = document.createElement('time');
    clock.textContent = new Date(event.at * 1000).toLocaleTimeString() + ' · ' + event.kind;
    const content = document.createElement('p');
    content.textContent = event.message;
    node.append(clock, content);
    const list = $('activity-list');
    if (activityCount === 0) list.replaceChildren();
    list.append(node);
    activityCount++;
    list.scrollTop = list.scrollHeight;
  }

  function approval(jobId, prompt) {
    const box = message('CODING BRAIN · APPROVAL REQUIRED', prompt, 'system');
    const buttons = document.createElement('div');
    buttons.className = 'decision-buttons';
    for (const [name, allow] of [['Approve', true], ['Decline', false]]) {
      const button = document.createElement('button');
      button.textContent = name;
      if (!allow) button.className = 'no';
      button.addEventListener('click', async () => {
        try {
          await api(`/api/jobs/${jobId}/decision`, 'POST', {allow});
          buttons.remove();
          message('YOU', allow ? 'Approved this step' : 'Declined this step', 'user');
        } catch (e) { toast(e.message); }
      });
      buttons.append(button);
    }
    box.append(buttons);
  }

  function tickTime() {
    if (activeJob && startedAt) {
      const seconds = Math.floor(Date.now() / 1000 - startedAt);
      $('status-time').textContent = Math.floor(seconds / 60) + 'm ' + String(seconds % 60).padStart(2, '0') + 's';
    }
  }

  async function poll() {
    if (!activeJob) return;
    try {
      const response = await api(`/api/jobs/${activeJob}?after=${cursor}`);
      const job = response.job;
      for (const event of response.events) {
        cursor = Math.max(cursor, event.seq);
        if (displayedEvents.has(event.seq)) continue;
        displayedEvents.add(event.seq);
        activityEntry(event);
        if (event.kind === 'approval') approval(job.id, event.message);
        if (event.kind === 'output') message('CODING BRAIN · OUTPUT', event.message, 'assistant');
        if (event.kind === 'error') message('CODING BRAIN · ERROR', event.message, 'system');
      }
      const running = ['starting','running','approval_required'].includes(job.status);
      $('run-status').hidden = !running;
      $('send').disabled = running;
      $('status-label').textContent = job.status === 'approval_required' ? 'Awaiting your approval' : `Running ${job.kind} — waiting for engine output`;
      $('footer-status').textContent = `Task ${job.status}`;
      tickTime();
      if (!running) {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = null;
        $('run-status').hidden = true;
        $('send').disabled = false;
        const text = job.status === 'completed' ? 'Session finished.' : `Session ${job.status}.`;
        message('CODING BRAIN', text + (job.error ? ` ${job.error}` : ''), 'system');
        activeJob = null;
        return;
      }
    } catch(e) { $('footer-status').textContent = 'Connection lost'; toast(e.message); }
  }

  async function submit() {
    const text = $('prompt').value.trim();
    if (!text) return;
    if (activeJob) { toast('Finish or stop the current session first.'); return; }
    if (mode === 'run' && (!currentProject || !currentProject.git)) {
      toast('Open an existing Git repository before running a coding task.');
      openDialog();
      return;
    }
    message('YOU', text, 'user');
    $('prompt').value = '';
    try {
      const job = await api('/api/start', 'POST', {message: text, mode});
      activeJob = job.id;
      cursor = 0;
      displayedEvents = new Set();
      startedAt = job.started;
      $('run-status').hidden = false;
      $('send').disabled = true;
      await poll();
      pollTimer = setInterval(poll, 750);
    } catch(e) {
      message('CODING BRAIN · CONNECTION', e.message, 'system');
      toast(e.message);
    }
  }

  async function refresh() {
    try {
      const data = await api('/api/state');
      knownProjects = data.projects;
      if (data.project) updateProject(data.project);
      if (data.active && ['starting','running','approval_required'].includes(data.active.status)) {
        activeJob = data.active.id;
        startedAt = data.active.started;
        await poll();
        if (!pollTimer) pollTimer = setInterval(poll, 750);
      }
    } catch(e) { message('CODING BRAIN', 'Unable to connect to the local workspace: ' + e.message, 'system'); }
  }

  $('mode-chat').addEventListener('click', () => setMode('chat'));
  $('mode-run').addEventListener('click', () => setMode('run'));
  $('send').addEventListener('click', submit);
  $('prompt').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) {e.preventDefault();submit();} });
  $('project-picker').addEventListener('click', openDialog);
  $('browse-project').addEventListener('click', openDialog);
  $('nav-projects').addEventListener('click', openDialog);
  $('open-path').addEventListener('click', () => choose($('path-input').value));
  $('native-picker').addEventListener('click', async () => {
    try {
      const result = await api('/api/project/pick', 'POST');
      if (!result.cancelled) {updateProject(result);$('project-dialog').close();}
    } catch(e) { $('picker-error').textContent = e.message; }
  });
  $('refresh-files').addEventListener('click', loadTree);
  $('refresh').addEventListener('click', refresh);
  $('preview-close').addEventListener('click', () => $('preview').hidden = true);
  $('stop-task').addEventListener('click', async () => {
    if (!activeJob || !confirm('Stop the active Coding Brain session?')) return;
    try { await api(`/api/jobs/${activeJob}/stop`, 'POST'); await poll(); }
    catch(e) { toast(e.message); }
  });
  for (const item of document.querySelectorAll('[data-suggestion]')) {
    item.addEventListener('click', () => {$('prompt').value = item.dataset.suggestion; $('prompt').focus();});
  }
  for (const kind of ['files','activity']) {
    $('tab-' + kind).addEventListener('click', () => {
      const fileMode = kind === 'files';
      $('file-panel').hidden = !fileMode;
      $('activity-panel').hidden = fileMode;
      $('tab-files').classList.toggle('active', fileMode);
      $('tab-activity').classList.toggle('active', !fileMode);
      $('tab-files').setAttribute('aria-selected', String(fileMode));
      $('tab-activity').setAttribute('aria-selected', String(!fileMode));
    });
  }
  $('collapse-explorer').addEventListener('click', () => {
    $('explorer').classList.toggle('show');
    toast('File explorer can be reopened from the project selector.');
  });
  $('nav-chat').addEventListener('click', () => $('prompt').focus());
  setInterval(tickTime, 1000);
  if (!token) message('CODING BRAIN', 'Missing local session token. Launch this page using python -m desktop_ui.', 'system');
  else refresh();
})();
