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
  let submitting = false;
  let polling = false;
  let typingRow = null;
  let engineInfo = null;
  let engineNoticeDismissed = false;
  let engineRefresh = null;
  let controlLoad = null;
  let activeMaintenance = null;
  let gitReading = false;
  let gitGeneration = 0;
  let gitLastSignature = '';
  let gitLatest = null;
  let maintenancePoll = null;
  let feedbackTimer = null;
  let bridgeBoot = null;
  let installedVersion;
  let connectionLost = false;
  let heartbeatFailures = 0;
  let heartbeatTimer = null;
  let attachments = [];
  // The task's stages and agents, rebuilt only from the engine's structured events.
  let flow = {kind: null, stages: {}, agents: {}, progress: 0, current: null, status: null};
  const busyButtons = new WeakMap();

  function busyButton(button, working) {
    if (!button) return;
    if (working) {
      if (busyButtons.has(button)) return;
      busyButtons.set(button, button.disabled);
      button.classList.add('is-loading');
      button.setAttribute('aria-busy', 'true');
      button.disabled = true;
    } else if (busyButtons.has(button)) {
      const wasDisabled = busyButtons.get(button);
      busyButtons.delete(button);
      button.classList.remove('is-loading');
      button.removeAttribute('aria-busy');
      button.disabled = wasDisabled;
    }
  }

  function feedback(state, title, detail = '') {
    const box = $('control-feedback');
    if (!box.hidden && state && box.dataset.state === state &&
        $('control-feedback-title').textContent === title &&
        $('control-feedback-detail').textContent === detail) return;
    clearInterval(feedbackTimer);
    feedbackTimer = null;
    if (!state) { box.hidden = true; return; }
    box.hidden = false;
    box.dataset.state = state;
    $('control-feedback-title').textContent = title;
    $('control-feedback-detail').textContent = detail;
    const elapsed = $('control-feedback-elapsed');
    elapsed.hidden = state !== 'busy';
    if (state === 'busy') {
      const start = Date.now();
      const tick = () => { elapsed.textContent = `${Math.floor((Date.now() - start) / 1000)}s elapsed`; };
      tick();
      feedbackTimer = setInterval(tick, 1000);
    }
  }

  function loadingComponents() {
    const grid = $('setup-components');
    grid.setAttribute('aria-busy', 'true');
    $('full-setup').disabled = true;
    $('full-setup').title = 'Checking whether the official full installer is available';
    grid.replaceChildren();
    for (let index = 0; index < 6; index++) {
      const card = document.createElement('div'); card.className = 'component-card skeleton-card';
      const icon = document.createElement('span'); icon.className = 'skeleton-icon';
      const copy = document.createElement('div'); copy.className = 'skeleton-copy';
      const first = document.createElement('span'); first.className = 'skeleton-line';
      const second = document.createElement('span'); second.className = 'skeleton-line short';
      copy.append(first, second); card.append(icon, copy); grid.append(card);
    }
  }

  function showTyping() {
    if (typingRow) return;
    const bubble = message('CODING BRAIN', 'Thinking…', 'assistant');
    typingRow = bubble.closest('.message');
    typingRow.classList.add('typing-row');
    typingRow.setAttribute('role','status');
    typingRow.setAttribute('aria-label','Coding Brain is working');
    const dots = document.createElement('span'); dots.className = 'typing-dots'; dots.setAttribute('aria-hidden','true');
    for (let index=0;index<3;index++) dots.append(document.createElement('span'));
    bubble.append(dots);
  }

  function hideTyping() {
    if (typingRow) typingRow.remove();
    typingRow = null;
  }

  function busySend(working) {
    busyButton($('send'), working);
    $('send').setAttribute('aria-label', working ? 'Coding Brain is working' : 'Send message');
  }

  async function api(path, method = 'GET', body) {
    const options = {method, headers: {'X-CodingBrain-Token': token || ''}};
    if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
    // GETs are read-only, so a timeout cannot accidentally duplicate a task, and a hung read can
    // never wedge a poll loop. Never automatically timeout or retry mutating requests: they may
    // have taken effect.
    const engineStatus = method === 'GET' && path.startsWith('/api/engine/status');
    const controller = method === 'GET' ? new AbortController() : null;
    if (controller) options.signal = controller.signal;
    const deadline = controller ? setTimeout(() => controller.abort(), engineStatus ? 35000 : 15000) : null;
    try {
      let response;
      try { response = await fetch(path, options); }
      catch (error) {
        // fetch rejects with a TypeError only when the bridge cannot be reached; keep any other
        // error's own message.
        if (!(error instanceof TypeError)) throw error;
        const offline = new Error('Could not reach the Coding Brain UI bridge'); offline.offline = true; throw offline;
      }
      let result;
      try { result = await response.json(); }
      catch {
        // The bridge answered, but not with JSON (e.g. an internal error): say so instead of
        // claiming it is unreachable, so the real failure can be diagnosed.
        const failure = new Error(`The Coding Brain desktop service returned an error (HTTP ${response.status}) for ${path.split('?')[0]}. Retry; if it repeats, reopen Coding Brain.`);
        failure.status = response.status;
        throw failure;
      }
      if (!response.ok) {
        const failure = new Error(typeof result.detail === 'string' ? result.detail : 'Request failed');
        failure.status = response.status;
        throw failure;
      }
      return result;
    } catch (error) {
      if (error?.name === 'AbortError') {
        const timedOut = new Error(engineStatus ? 'Engine readiness check timed out. Open Setup to diagnose the installed engine, then retry.'
          : 'The Coding Brain UI bridge did not answer in time');
        timedOut.offline = !engineStatus;
        throw timedOut;
      }
      throw error;
    } finally {
      if (deadline !== null) clearTimeout(deadline);
    }
  }

  // Every privileged action uses a Coding Brain dialog, never a browser-native
  // confirm/alert/prompt. Dismissal, Escape and the default button all mean NO.
  function confirmAction(title, detail, confirmLabel = 'Continue', destructive = false) {
    const dialog = $('action-dialog');
    if (dialog.open) return Promise.resolve(false);
    $('action-heading').textContent = title;
    $('action-description').textContent = detail;
    $('action-approve').textContent = confirmLabel;
    $('action-approve').classList.toggle('destructive', destructive);
    dialog.returnValue = 'cancel';
    return new Promise(resolve => {
      const onClose = () => {
        dialog.removeEventListener('close', onClose);
        resolve(dialog.returnValue === 'proceed');
      };
      dialog.addEventListener('close', onClose);
      dialog.showModal();
      $('action-cancel').focus();
    });
  }

  $('action-dialog').addEventListener('click', event => {
    // Click outside the card closes the dialog without authorizing anything.
    if (event.target === $('action-dialog')) $('action-dialog').close('cancel');
  });

  function toast(message) {
    $('toast').textContent = message;
    $('toast').hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => $('toast').hidden = true, 4400);
  }

  function avatarIcon(kind) {
    const holder = document.createElement('span');
    holder.className = 'avatar avatar-' + kind;
    holder.setAttribute('aria-hidden', 'true');
    if (kind === 'user') {
      const ns = 'http://www.w3.org/2000/svg';
      const svg = document.createElementNS(ns, 'svg');
      for (const [key,value] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.8','stroke-linecap':'round','stroke-linejoin':'round'})) svg.setAttribute(key,value);
      const face = document.createElementNS(ns, 'circle');
      for (const [key,value] of Object.entries({cx:'12',cy:'8',r:'3.6'})) face.setAttribute(key,value);
      const shoulders = document.createElementNS(ns, 'path');
      shoulders.setAttribute('d', 'M5.2 19.1a6.8 6.8 0 0 1 13.6 0');
      svg.append(face, shoulders); holder.append(svg);
    } else if (kind === 'system') {
      holder.textContent = 'i';
    } else {
      holder.textContent = '◇';
    }
    return holder;
  }

  // ---- Code display: fenced blocks with a language label, copy button, syntax colors and diff
  // highlighting. Everything is built with textContent; model output is never parsed as HTML.
  const KEYWORDS = new Set(('def class return if elif else for while in not and or is import from as with try except ' +
    'finally raise pass break continue lambda yield async await None True False self function const let var new ' +
    'this typeof instanceof switch case default throw catch export extends static public private protected ' +
    'interface type enum implements null undefined true false void delete do of package struct impl fn pub use ' +
    'mut match where select insert update delete create table values into join on group order by').split(' '));
  const HASH_COMMENTS = new Set(['py', 'python', 'sh', 'bash', 'shell', 'yaml', 'yml', 'toml', 'ps1', 'powershell', 'r', 'rb', 'ruby', 'ini', 'cfg']);
  const LANGUAGE_BY_EXTENSION = {py:'python', js:'javascript', jsx:'javascript', ts:'typescript', tsx:'typescript',
    json:'json', css:'css', html:'html', md:'markdown', sh:'bash', ps1:'powershell', yml:'yaml', yaml:'yaml',
    toml:'toml', ini:'ini', cfg:'ini', rs:'rust', go:'go', java:'java', cs:'csharp', sql:'sql', rb:'ruby', diff:'diff', patch:'diff'};

  function tokenPattern(lang) {
    const comment = HASH_COMMENTS.has(lang) ? '#[^\\n]*' : lang === 'sql' ? '--[^\\n]*' : '\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/';
    return new RegExp(`(${comment})|("(?:\\\\.|[^"\\\\\\n])*"|'(?:\\\\.|[^'\\\\\\n])*'|\`(?:\\\\.|[^\`\\\\])*\`)|(\\b\\d+(?:\\.\\d+)?\\b)|([A-Za-z_$][\\w$]*)`, 'g');
  }

  function highlightInto(parent, code, lang) {
    lang = (lang || '').toLowerCase();
    if (lang === 'diff' || (!lang && /^(diff --git|--- |\+\+\+ |@@ )/m.test(code))) {
      for (const line of code.split('\n')) {
        const span = document.createElement('span');
        span.className = 'diff-line' + (line.startsWith('+') && !line.startsWith('+++') ? ' diff-add' :
          line.startsWith('-') && !line.startsWith('---') ? ' diff-del' : line.startsWith('@@') ? ' diff-hunk' :
          /^(diff --git|\+\+\+|---|index )/.test(line) ? ' diff-file' : '');
        span.textContent = line + '\n';
        parent.append(span);
      }
      return;
    }
    if (['text', 'txt', 'plain', 'markdown', 'md', 'output', 'console'].includes(lang) || code.length > 200000) {
      parent.append(code);
      return;
    }
    const pattern = tokenPattern(lang);
    let last = 0;
    for (const match of code.matchAll(pattern)) {
      if (match.index > last) parent.append(code.slice(last, match.index));
      const [token, comment, string, number, word] = match;
      if (word && !KEYWORDS.has(word)) { parent.append(token); last = match.index + token.length; continue; }
      const span = document.createElement('span');
      span.className = comment ? 'tok-c' : string ? 'tok-s' : number ? 'tok-n' : 'tok-k';
      span.textContent = token;
      parent.append(span);
      last = match.index + token.length;
    }
    if (last < code.length) parent.append(code.slice(last));
  }

  function codeBlock(code, lang, options = {}) {
    const block = document.createElement('div');
    block.className = 'code-block';
    const bar = document.createElement('div');
    bar.className = 'code-bar';
    const label = document.createElement('span');
    label.className = 'code-lang';
    label.textContent = options.title || lang || 'code';
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'code-copy';
    copy.textContent = 'Copy';
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(code); copy.textContent = 'Copied'; setTimeout(() => copy.textContent = 'Copy', 1500); }
      catch { toast('Copy unavailable'); }
    });
    bar.append(label, copy);
    const pre = document.createElement('pre');
    const element = document.createElement('code');
    if (options.lineNumbers) {
      pre.className = 'numbered';
      code.split('\n').forEach((line, index, lines) => {
        if (index === lines.length - 1 && line === '') return;
        const row = document.createElement('span');
        row.className = 'code-line';
        const number = document.createElement('span');
        number.className = 'line-no';
        number.textContent = String(index + 1);
        const text = document.createElement('span');
        highlightInto(text, line, lang);
        row.append(number, text, '\n');
        element.append(row);
      });
    } else {
      highlightInto(element, code.replace(/\n$/, ''), lang);
    }
    pre.append(element);
    block.append(bar, pre);
    return block;
  }

  function inlineText(parent, text) {
    const parts = text.split(/(`[^`\n]+`)/g);
    for (const part of parts) {
      if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) {
        const code = document.createElement('code');
        code.className = 'inline-code';
        code.textContent = part.slice(1, -1);
        parent.append(code);
      } else if (part) parent.append(part);
    }
  }

  function richText(parent, text) {
    const fence = /```([\w+#.-]*)[^\n]*\n?([\s\S]*?)(?:```|$)/g;
    let last = 0;
    for (const match of text.matchAll(fence)) {
      if (match.index > last) inlineText(parent, text.slice(last, match.index).replace(/\n+$/, '\n'));
      parent.append(codeBlock(match[2], match[1]));
      last = match.index + match[0].length;
    }
    if (last < text.length) inlineText(parent, text.slice(last).replace(/^\n+/, ''));
  }

  function message(author, text, variant = 'assistant') {
    $('welcome').hidden = true;
    const row = document.createElement('article');
    row.className = 'message ' + variant;
    row.setAttribute('aria-label', author + ' message');
    const avatar = avatarIcon(variant);
    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    const head = document.createElement('div');
    head.className = 'message-head';
    const name = document.createElement('span');
    name.className = 'message-name';
    name.textContent = variant === 'user' ? 'You' : author.replace(/^CODING BRAIN\s*·\s*/, 'Coding Brain · ').replace(/^CODING BRAIN$/, 'Coding Brain');
    const when = document.createElement('time');
    when.className = 'message-time';
    const now = new Date();
    when.dateTime = now.toISOString();
    when.textContent = now.toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
    const metadata = document.createElement('span');
    metadata.className = 'message-meta';
    metadata.append(name, when);
    head.append(metadata);
    if (variant !== 'user') {
      const actions = document.createElement('span');
      actions.className = 'message-actions';
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.className = 'message-copy';
      copy.textContent = '⧉';
      copy.title = 'Copy message';
      copy.setAttribute('aria-label','Copy message');
      copy.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(text); copy.textContent = '✓'; }
        catch { toast('Copy unavailable'); }
      });
      actions.append(copy);
      head.append(actions);
    }
    const content = document.createElement('div');
    content.className = 'message-content';
    if (variant !== 'user' && text.includes('`')) richText(content, text);
    else content.textContent = text;
    bubble.append(head, content);
    row.append(avatar, bubble);
    $('thread').append(row);
    $('thread').scrollTop = $('thread').scrollHeight;
    return bubble;
  }

  function showEngineStatus(info) {
    engineInfo = info;
    const available = info.engine?.available === true;
    const chat = info.chat_available === true;
    const model = info.model || {};
    const canCreateProject = info.engine?.new_project === true;
    const newProjectButton = $('mode-new');
    newProjectButton.setAttribute('aria-disabled', String(!canCreateProject));
    newProjectButton.title = canCreateProject ? 'Create a new project after your approval' :
      'Requires a newer installed Coding Brain release with goal-first project creation';
    const modelName = model.name || (model.provider === 'ollama' ? 'Local model not configured' : 'Model not configured');
    $('model-label').textContent = `◈  ${modelName}  ⌄`;
    $('model-label').title = `Primary worker: ${modelName}. Ollama model detected: ${model.ready ? 'yes' : 'no'}. Claude and Codex are separate optional supervisors; inspect them in AI Providers.`;
    $('readiness-local').textContent = `◈ ${modelName}: ${model.ready ? 'detected' : 'not verified'}`;
    $('readiness-local').dataset.ready = model.ready ? 'true' : 'false';
    $('engine-label').textContent = !available ? 'Engine unavailable' :
      !chat ? `Engine v${info.engine.version || '?'} · Chat update needed` :
      model.provider === 'ollama' && !model.ready ? 'Local model not ready' :
      `Engine v${info.engine.version || '?'} · Chat available`;
    $('engine-dot').classList.toggle('green', available && chat && (model.provider !== 'ollama' || model.ready));
    $('engine-dot').classList.toggle('amber', available && (!chat || (model.provider === 'ollama' && !model.ready)));
    $('engine-banner').hidden = !info.next_action || engineNoticeDismissed;
    $('engine-banner').dataset.state = available ? 'warning' : 'error';
    $('engine-banner-title').textContent = info.title || 'Coding Brain engine status';
    $('engine-banner-detail').textContent = info.detail || '';
    $('engine-banner-action').textContent = info.next_action === 'updates' ? 'View updates' : 'Open setup';
    if (!activeJob) $('footer-status').textContent = !available ? 'Engine unavailable' :
      !chat ? 'Engine update required for chat' :
      model.provider === 'ollama' && !model.ready ? 'Local model not ready' : 'Ready';
  }

  async function refreshEngine(options = {}) {
    if (engineRefresh) return engineRefresh;
    engineRefresh = (async () => {
      try {
        const info = await api('/api/engine/status' + (options.force ? '?refresh=true' : ''));
        showEngineStatus(info);
        return info;
      } catch (error) {
        if (!options.quiet) {
          $('engine-label').textContent = 'Engine status unavailable';
          $('engine-dot').classList.remove('green','amber');
          $('model-label').textContent = '◈  Model status unknown  ⌄';
          $('engine-banner').hidden = false;
          $('engine-banner-title').textContent = 'Could not verify the installed brain';
          $('engine-banner-detail').textContent = error.message;
          $('engine-banner-action').textContent = 'Open setup';
        }
        return null;
      } finally { engineRefresh = null; }
    })();
    return engineRefresh;
  }

  function setMode(value) {
    if (value === 'new' && engineInfo && engineInfo.engine?.new_project !== true) {
      toast('New project is not available in the installed Coding Brain engine. No task started. You can browse existing project folders.');
      return;
    }
    mode = value;
    for (const kind of ['chat', 'run', 'new']) {
      const button = $('mode-' + kind);
      button.classList.toggle('active', kind === value);
      button.setAttribute('aria-pressed', String(kind === value));
    }
    $('prompt').placeholder = value === 'chat' ? 'Ask Coding Brain anything...' :
      value === 'new' ? 'Describe the app you want Coding Brain to create...' : 'Describe a coding goal...';
  }

  function gitEmpty(messageText) {
    $('git-details').hidden = true;
    $('git-notice').hidden = false;
    $('git-notice').textContent = messageText || 'Select a Git project to see branches and recent commits.';
    $('git-updated').textContent = 'Local Git · Waiting for a project';
    if (!currentProject) $('branch-name').textContent = '◇ No repository';
  }

  function gitText(parent, tag, value, className) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = value;
    parent.append(element);
    return element;
  }

  function renderGitSnapshot(snapshot) {
    if (!snapshot || !snapshot.available) {
      gitEmpty(snapshot?.reason || 'Git is not available for this project.');
      $('branch-name').textContent = '◇ No repository';
      return;
    }
    $('git-notice').hidden = true;
    $('git-details').hidden = false;
    const signature = JSON.stringify({
      branch: snapshot.branch, head: snapshot.head, branches: snapshot.branches,
      commits: snapshot.commits, changes: snapshot.changes, counts: snapshot.counts, paused: snapshot.changes_paused
    });
    $('git-updated').textContent = 'Live · Checked ' +
      new Date().toLocaleTimeString([], {hour:'numeric',minute:'2-digit',second:'2-digit'});
    $('branch-name').textContent = '◇ ' + (snapshot.branch || 'Detached HEAD');
    if (signature === gitLastSignature) return;
    // Files changed on disk (by a task, an editor or a checkout): refresh the open file tree too.
    if (gitLastSignature && !$('file-panel').hidden) loadTree();
    gitLastSignature = signature;
    $('git-current-branch').textContent = snapshot.branch || 'Unknown branch';
    $('git-current-head').textContent = snapshot.head ?
      'HEAD ' + snapshot.head.slice(0, 10) + (snapshot.detached ? ' · Detached' : '') :
      'No commits yet';
    const counts = snapshot.counts || {};
    $('git-summary-staged').textContent = (counts.staged || 0) + ' staged';
    $('git-summary-modified').textContent = (counts.modified || 0) + ' modified';
    $('git-summary-untracked').textContent = (counts.untracked || 0) + ' untracked';
    $('git-changes-count').textContent = (snapshot.changes || []).length +
      (snapshot.changes_truncated ? '+' : '');
    const changed = $('git-changes');
    changed.replaceChildren();
    if (!snapshot.changes?.length) gitText(changed, 'p', snapshot.changes_paused || 'Working tree clean', 'git-empty');
    for (const item of snapshot.changes || []) {
      const row = document.createElement('div');
      row.className = 'git-file-row'; row.setAttribute('role', 'listitem');
      gitText(row, 'span', item.status.trim() || '??',
        'git-file-status' + (item.conflicted ? ' git-conflict' : item.untracked ? ' git-new' : ''));
      gitText(row, 'span', item.path, 'git-file-name');
      changed.append(row);
    }
    const branchList = $('git-branches');
    branchList.replaceChildren();
    const branches = snapshot.branches || [];
    $('git-branches-count').textContent = branches.length + (branches.length >= 25 ? '+' : '');
    if (!branches.length) gitText(branchList, 'p', 'No local branches yet', 'git-empty');
    for (const name of branches) {
      const row = document.createElement('div');
      row.className = 'git-branch-row' + (name === snapshot.branch ? ' current' : '');
      row.setAttribute('role', 'listitem');
      gitText(row, 'span', name === snapshot.branch ? '●' : '◇', 'git-branch-mark');
      gitText(row, 'span', name, 'git-branch-label');
      branchList.append(row);
    }
    const history = $('git-commits');
    history.replaceChildren();
    if (!snapshot.commits?.length) gitText(history, 'p', 'No commits yet', 'git-empty');
    for (const item of snapshot.commits || []) {
      const row = document.createElement('div');
      row.className = 'git-commit-row'; row.setAttribute('role', 'listitem');
      const main = gitText(row, 'span', item.subject || '(empty commit message)', 'git-commit-message');
      main.title = item.subject || '(empty commit message)';
      const secondary = document.createElement('span');
      secondary.className = 'git-commit-meta';
      const date = new Date(item.date);
      gitText(secondary, 'span', item.short || item.sha.slice(0, 7), 'git-hash');
      gitText(secondary, 'span', item.author || 'Unknown', 'git-author');
      gitText(secondary, 'time', Number.isNaN(date.getTime()) ?
        '' : date.toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}), '');
      row.append(secondary);
      history.append(row);
    }
  }

  async function refreshGit(force = false) {
    if (gitReading || !currentProject || (!force && document.hidden)) return;
    const projectPath = currentProject.path;
    const generation = gitGeneration;
    gitReading = true;
    try {
      const result = await api('/api/git/snapshot');
      // Ignore stale requests after switching projects. No response may overwrite the new repo.
      if (generation !== gitGeneration || currentProject?.path !== projectPath) return;
      gitLatest = result;
      renderGitSnapshot(result);
    } catch (error) {
      if (generation === gitGeneration && currentProject?.path === projectPath) {
        $('git-notice').hidden = false;
        $('git-notice').textContent = 'Git status temporarily unavailable. Refresh to retry.';
        $('git-updated').textContent = 'Could not refresh Git';
      }
    } finally { gitReading = false; }
  }

  function updateProject(project) {
    const switched = (currentProject?.path || '') !== (project?.path || '');
    currentProject = project;
    if (switched) {
      gitGeneration++;
      gitLastSignature = '';
      gitLatest = null;
      gitEmpty(project ? 'Reading local Git branches and history…' :
        'Choose a project to view its Git history.');
    }
    $('project-label').textContent = project ? project.name : 'Choose project';
    $('project-breadcrumb').textContent = project ? project.path : 'Global conversation';
    $('explorer-folder').textContent = project ? project.name : 'No project selected';
    $('branch-name').textContent = project && project.git ? '◇ ' + project.git : '◇ No repository';
    $('file-count').textContent = '';
    $('profile-project').textContent = project ? project.name : 'No project selected';
    expanded.clear();
    $('preview').hidden = true;
    if (project) {
      loadTree();
      refreshGit(true);
    } else renderEmptyExplorer();
  }

  function renderEmptyExplorer() {
    const list = $('file-tree');
    list.replaceChildren();
    const panel = document.createElement('div');
    panel.className = 'empty-panel';
    const illustration = document.createElement('div');
    illustration.className = 'empty-folder'; illustration.setAttribute('aria-hidden', 'true'); illustration.textContent = '▱';
    const heading = document.createElement('h3'); heading.textContent = 'No project selected';
    const description = document.createElement('p'); description.textContent = 'Open a project folder to browse its files and follow agent activity.';
    panel.append(illustration, heading, description);
    const browse = document.createElement('button');
    browse.type = 'button'; browse.textContent = '▱  Browse folder';
    browse.addEventListener('click', openDialog);
    const create = document.createElement('button');
    create.type = 'button'; create.className = 'empty-secondary'; create.textContent = '＋  New project';
    create.addEventListener('click', () => {setMode('new'); $('prompt').focus();});
    panel.append(browse, create);
    list.append(panel);
  }

  function setExplorerVisibility(visible) {
    const explorer = $('explorer');
    explorer.classList.toggle('collapsed', !visible);
    explorer.classList.toggle('show', visible);
    $('open-explorer').classList.toggle('is-visible', !visible);
    $('open-explorer').setAttribute('aria-expanded', String(visible));
    $('collapse-explorer').setAttribute('aria-expanded', String(visible));
    $('open-explorer').title = visible ? 'Project explorer is open' : 'Open project explorer';
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
          const extension = (file.path.split('.').pop() || '').toLowerCase();
          $('preview-content').replaceChildren(codeBlock(file.content, LANGUAGE_BY_EXTENSION[extension] || extension,
            {lineNumbers: true, title: file.path}));
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
      // Build off-screen, including folders the user has open, then swap in one step: a live
      // refresh never collapses the tree, flickers or jumps the scroll position.
      const rows = [];
      const nested = [];
      for (const entry of response.entries) {
        const row = rowFor(entry, depth);
        rows.push(row);
        if (entry.dir && expanded.has(entry.path)) {
          nested.push(loadFolder(entry.path, row.querySelector(':scope > .tree-children'), depth + 1));
        }
      }
      await Promise.all(nested);
      if (response.entries.length === 0) {
        const empty = document.createElement('p');
        empty.className = 'muted';
        empty.textContent = 'Folder is empty.';
        rows.push(empty);
      }
      parent.replaceChildren(...rows);
      if (!folder) $('file-count').textContent = `${response.entries.length} items`;
    } catch (e) {
      if (folder && e.status === 400) expanded.delete(folder);  // the folder was deleted or renamed
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

  function approval(jobId, details) {
    const prompt = details?.summary || 'Review the proposal before approving.';
    const box = message('CODING BRAIN · APPROVAL REQUIRED', prompt, 'system');
    if (details?.files?.length) {
      const files = document.createElement('p');
      files.className = 'muted';
      files.textContent = 'Proposed files: ' + details.files.join(', ');
      box.append(files);
    }
    if (details?.diff) {
      const reveal = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = 'Review exact diff';
      const diff = codeBlock(details.diff, 'diff', {title: 'Proposed changes'});
      diff.classList.add('proposal-diff');
      reveal.append(summary, diff);
      box.append(reveal);
    }
    const buttons = document.createElement('div');
    buttons.className = 'decision-buttons';
    for (const [name, allow] of [['Approve', true], ['Decline', false]]) {
      const button = document.createElement('button');
      button.textContent = name;
      if (!allow) button.className = 'no';
      button.addEventListener('click', async () => {
        try {
          await api(`/api/jobs/${jobId}/decision`, 'POST', {allow, approval_id: details?.id || null});
          buttons.remove();
          message('YOU', allow ? 'Approved this step' : 'Declined this step', 'user');
        } catch (e) { toast(e.message); }
      });
      buttons.append(button);
    }
    box.append(buttons);
  }

  // ---- Agent flow and progress. Stages and agents come only from the engine's structured events;
  // the percentage is an estimate from the stages completed so far, and never moves backward.
  const STAGES = [
    ['goal', 'Goal received', 4, true], ['project_validation', 'Project and Git check', 7], ['indexing', 'Repository indexing', 10],
    ['memory', 'Memory and knowledge', 14], ['decomposition', 'Goal decomposition', 18], ['supervisor_selection', 'Supervisor selection', 20],
    ['planning', 'Planning', 28, true], ['requirements', 'Requirements and tests', 34], ['proposal', 'Proposal', 44, true],
    ['proposal_validation', 'Proposal validation', 48], ['approval', 'Your approval', 52, true], ['implementation', 'Implementation', 60, true],
    ['tool', 'Tool use', 62], ['review', 'Code review', 68], ['sandbox', 'Sandbox run', 75, true], ['test_results', 'Test results', 82, true],
    ['repair', 'Diagnosis and repair', 84], ['retest', 'Retesting', 86], ['completion_verification', 'Completion check', 90],
    ['visual_verification', 'Visual check', 93], ['acceptance', 'Acceptance on a new branch', 100, true],
  ];
  const STAGE_INDEX = Object.fromEntries(STAGES.map((stage, index) => [stage[0], index]));
  // Role names say what each agent does in the flow.
  const AGENT_NAMES = {user: 'You', coding_brain: 'Orchestrator', coordinator: 'Coordinator (splits the goal)',
    implementer: 'Implementer', reviewer: 'Reviewer', supervisor: 'Supervisor', sandbox: 'Test sandbox',
    browser: 'Visual checker', installer: 'Installer'};
  const AGENT_ORDER = ['coding_brain', 'coordinator', 'implementer', 'reviewer', 'supervisor', 'sandbox', 'browser', 'installer', 'user'];

  // Which brain an agent belongs to, from the provider the engine reported (never guessed from a
  // name). Agents with no model call fall back to what they are by construction.
  function agentOwner(id, agent) {
    const provider = String(agent.provider || '').toLowerCase();
    if (provider === 'ollama' || provider === 'local') return {group: 'local', label: 'Local brain', detail: agent.model || 'Ollama'};
    if (provider.startsWith('claude') || provider === 'anthropic') return {group: 'provider', label: 'Claude', detail: agent.model || 'subscription CLI'};
    if (provider.startsWith('codex') || provider === 'openai') return {group: 'provider', label: 'Codex', detail: agent.model || 'subscription CLI'};
    if (provider) return {group: 'provider', label: provider, detail: agent.model || ''};
    if (id === 'user') return {group: 'you', label: 'You', detail: 'approvals'};
    if (id === 'sandbox') return {group: 'tools', label: 'Docker sandbox', detail: 'isolated tests'};
    if (id === 'browser') return {group: 'tools', label: 'Local browser', detail: 'screenshots'};
    if (id === 'coding_brain' || id === 'coordinator' || id === 'installer') return {group: 'local', label: 'Local brain', detail: 'orchestration'};
    return {group: 'local', label: 'Local brain', detail: agent.model || 'model not reported yet'};
  }
  const OWNER_GROUPS = [['local', 'Local brain · on this PC'], ['provider', 'Provider brains · Claude / Codex'],
    ['tools', 'Tools'], ['you', 'You']];
  const STATE_LABELS = {RUNNING: 'Working', COMPLETED: 'Done', FAILED: 'Failed', BLOCKED: 'Blocked', RETRYING: 'Retrying',
    WAITING_APPROVAL: 'Waiting for you', CANCELLED: 'Cancelled', PENDING: 'Pending'};

  function resetFlow(kind) {
    flow = {kind, stages: {}, agents: {}, progress: kind === 'chat' ? 0 : 2, current: null, status: 'running'};
    renderFlow();
  }

  function flowEvent(event) {
    const data = event.data;
    if (!data || flow.kind === 'chat') return;
    const status = String(data.status || '').toUpperCase();
    const summary = event.message.replace(/^[\w:]+ [A-Z_]+: /, '');
    if (data.phase && data.phase in STAGE_INDEX) {
      flow.stages[data.phase] = {status: status || 'RUNNING', at: event.at, summary, duration: data.duration};
      const index = STAGE_INDEX[data.phase];
      const reached = status === 'COMPLETED' ? STAGES[index][2] : index > 0 ? STAGES[index - 1][2] : 0;
      flow.progress = Math.min(99, Math.max(flow.progress, reached));  // 100 only when the task finishes
      if (status !== 'COMPLETED') flow.current = data.phase;
    }
    if (data.agent) {
      // Who the agent runs on (local brain or a provider) sticks once reported: later events of the
      // same agent often omit provider and model.
      const known = flow.agents[data.agent] || {};
      flow.agents[data.agent] = {status: status || 'RUNNING', at: event.at, phase: data.phase || known.phase, summary,
        model: data.model || known.model, provider: data.provider || known.provider};
    }
    renderFlow();
  }

  function finishFlow(job) {
    if (flow.kind === 'chat') return;
    flow.status = job.status;
    if (job.status === 'completed') flow.progress = 100;
    for (const agent of Object.values(flow.agents)) {
      if (agent.status === 'RUNNING' || agent.status === 'RETRYING') agent.status = job.status === 'completed' ? 'COMPLETED' : 'CANCELLED';
    }
    flow.current = null;
    renderFlow();
  }

  function renderFlow() {
    const running = flow.status === 'running';
    const showProgress = flow.kind && flow.kind !== 'chat';
    $('run-progress').hidden = !(showProgress && running);
    $('run-progress-fill').style.width = flow.progress + '%';
    $('run-progress-label').textContent = flow.progress + '%';
    $('flow-progress-fill').style.width = (showProgress ? flow.progress : 0) + '%';
    $('flow-percent').textContent = showProgress ? flow.progress + '%' : '–';
    const current = flow.current && STAGES[STAGE_INDEX[flow.current]];
    $('flow-title').textContent = !flow.kind ? 'No task running' : flow.kind === 'chat' ? 'Conversation' :
      running ? (current ? current[1] : 'Starting…') : `Task ${flow.status}`;
    $('flow-sub').textContent = !flow.kind ? 'Run a task to see each agent and stage live.' :
      flow.kind === 'chat' ? 'Chat replies come from one model; agents appear when you run a task.' :
      'Progress is estimated from the stages the engine has completed.';
    const lanes = $('agent-lanes');
    lanes.replaceChildren();
    const agents = Object.entries(flow.agents);
    if (!agents.length) {
      const empty = document.createElement('p'); empty.className = 'muted';
      empty.textContent = flow.kind && flow.kind !== 'chat' ? 'Waiting for the first agent event…' : 'No agents active.';
      lanes.append(empty);
    }
    agents.sort((a, b) => (AGENT_ORDER.indexOf(a[0]) + 99) % 99 - (AGENT_ORDER.indexOf(b[0]) + 99) % 99);
    for (const [group, heading] of OWNER_GROUPS) {
      const members = agents.filter(([id, agent]) => agentOwner(id, agent).group === group);
      if (!members.length) continue;
      const title = document.createElement('div');
      title.className = 'owner-heading owner-' + group;
      title.textContent = heading;
      lanes.append(title);
      for (const [id, agent] of members) {
        const owner = agentOwner(id, agent);
        const card = document.createElement('div');
        card.className = `agent-lane owner-${group} state-${agent.status.toLowerCase()}`;
        card.setAttribute('role', 'listitem');
        const top = document.createElement('div'); top.className = 'agent-top';
        const name = document.createElement('strong'); name.textContent = AGENT_NAMES[id] || id;
        const pill = document.createElement('span'); pill.className = 'agent-state'; pill.textContent = STATE_LABELS[agent.status] || agent.status;
        top.append(name, pill);
        const badge = document.createElement('div'); badge.className = 'owner-badge owner-' + group;
        const who = document.createElement('strong'); who.textContent = owner.label;
        badge.append(who, owner.detail ? ' · ' + owner.detail : '');
        const meta = document.createElement('div'); meta.className = 'agent-meta';
        const stage = agent.phase && STAGES[STAGE_INDEX[agent.phase]];
        meta.textContent = stage ? stage[1] : agent.phase || '';
        const text = document.createElement('p'); text.className = 'agent-summary'; text.textContent = agent.summary.slice(0, 240);
        card.append(top, badge, meta, text);
        lanes.append(card);
      }
    }
    const list = $('stage-list');
    list.replaceChildren();
    for (const [phase, label, , core] of STAGES) {
      const seen = flow.stages[phase];
      if (!seen && !core) continue;  // optional stages appear only when the engine actually runs them
      const item = document.createElement('li');
      const status = seen ? seen.status : 'PENDING';
      item.className = 'stage state-' + status.toLowerCase() + (flow.current === phase ? ' current' : '');
      const mark = document.createElement('span'); mark.className = 'stage-mark';
      mark.textContent = {COMPLETED: '✓', FAILED: '✕', BLOCKED: '!', CANCELLED: '–', WAITING_APPROVAL: '⏸', RETRYING: '↻', RUNNING: '●'}[status] || '○';
      const name = document.createElement('span'); name.className = 'stage-name'; name.textContent = label;
      const time = document.createElement('span'); time.className = 'stage-time';
      time.textContent = seen?.duration ? `${Number(seen.duration).toFixed(1)}s` : seen ? STATE_LABELS[status] || '' : '';
      item.append(mark, name, time);
      list.append(item);
    }
  }

  function tickTime() {
    if (activeJob && startedAt) {
      const seconds = Math.floor(Date.now() / 1000 - startedAt);
      $('status-time').textContent = Math.floor(seconds / 60) + 'm ' + String(seconds % 60).padStart(2, '0') + 's';
    }
  }

  async function poll() {
    if (!activeJob || polling) return;
    polling = true;
    try {
      const response = await api(`/api/jobs/${activeJob}?after=${cursor}`);
      const job = response.job;
      if (job.project && (!currentProject || currentProject.path !== job.project) && mode === 'new') {
        try { const project = await api('/api/project', 'POST', {path: job.project}); updateProject(project); }
        catch (e) { toast('Project created; select its folder to view files.'); }
      }
      for (const event of response.events) {
        cursor = Math.max(cursor, event.seq);
        if (displayedEvents.has(event.seq)) continue;
        displayedEvents.add(event.seq);
        activityEntry(event);
        flowEvent(event);
        if (event.kind === 'approval') approval(job.id, job.approval || {summary: event.message});
        if (event.kind === 'output') { hideTyping(); message(job.kind === 'chat' ? 'CODING BRAIN' : 'CODING BRAIN · ACTIVITY', event.message, 'assistant'); }
        if (event.kind === 'error') { hideTyping(); message('CODING BRAIN · ERROR', event.message, 'system'); }
      }
      const running = ['starting','running','approval_required'].includes(job.status);
      $('run-status').hidden = !running;
      busySend(running);
      const stage = flow.current && STAGES[STAGE_INDEX[flow.current]];
      $('status-label').textContent = job.status === 'approval_required' ? 'Awaiting your approval' :
        stage ? `${stage[1]}…` : job.kind === 'chat' ? 'Coding Brain is replying…' : 'Starting — waiting for the first engine event';
      $('footer-status').textContent = job.kind === 'chat' && engineInfo?.chat_available === false ?
        'Engine update required for chat' : `Task ${job.status}`;
      tickTime();
      if (!running) {
        finishFlow(job);
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = null;
        $('run-status').hidden = true;
        busySend(false);
        hideTyping();
        // A chat reply already appears as an output event; don't add a redundant
        // 'Session finished' bubble after every conversational answer.
        if (job.kind !== 'chat' || job.status !== 'completed') {
          const text = job.status === 'completed' ? 'Coding task completed.' : `Session ${job.status}.`;
          message('CODING BRAIN', text + (job.error ? ` ${job.error}` : ''), 'system');
        }
        activeJob = null;
        if (currentProject) {
          refreshGit(true);
          if (!$('file-panel').hidden) loadTree();
        }
        return;
      }
    } catch(e) {
      if (e.status === 404) {
        // The bridge restarted and no longer knows this job; stop following it instead of
        // polling a missing task forever.
        endLostJob('This session is no longer tracked by the desktop service (it restarted). Check Activity or the task list before retrying.');
      } else {
        // The heartbeat owns reconnecting and its single notice; don't toast every 750 ms.
        $('footer-status').textContent = e.offline ? 'Connection lost — retrying' : 'Task status unavailable — retrying';
        if (!e.offline) toast(e.message);
      }
    }
    finally { polling = false; }
  }

  function endLostJob(text) {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
    activeJob = null;
    $('run-status').hidden = true;
    busySend(false);
    hideTyping();
    message('CODING BRAIN', text, 'system');
  }

  function setConnection(online, label) {
    $('connection-dot').classList.toggle('green', online);
    $('connection-dot').classList.toggle('red', !online);
    $('connection-label').textContent = label;
  }

  function scheduleHeartbeat() {
    clearTimeout(heartbeatTimer);
    // Back off while disconnected (1 s, 2 s, 4 s … 30 s); idle slowly while the window is hidden.
    const delay = connectionLost ? Math.min(30000, 1000 * 2 ** Math.max(0, heartbeatFailures - 1))
      : document.hidden ? 30000 : 5000;
    heartbeatTimer = setTimeout(heartbeat, delay);
  }

  async function heartbeat() {
    try {
      const health = await api('/api/health');
      const restarted = bridgeBoot !== null && health.boot !== bridgeBoot;
      const recovered = connectionLost;
      bridgeBoot = health.boot;
      heartbeatFailures = 0;
      connectionLost = false;
      setConnection(true, 'Desktop connected');
      if (restarted && activeJob) endLostJob('The desktop service restarted; this session is no longer tracked. Check Activity before retrying.');
      if (installedVersion !== undefined && health.installed_version !== installedVersion) {
        // An update or rollback switched the engine underneath us: re-read readiness now
        // instead of showing the old version's banner until someone clicks Refresh.
        toast(`Coding Brain engine is now v${health.installed_version || '?'}`);
        engineNoticeDismissed = false;
        refreshEngine({force: true});
      }
      installedVersion = health.installed_version;
      if (recovered || restarted) { toast('Reconnected to Coding Brain'); refresh(); }
    } catch (error) {
      if (error.status === 403) {
        setConnection(false, 'Session expired — reopen Coding Brain');
        return;  // a new bridge has a new token; retrying with the old one cannot succeed
      }
      heartbeatFailures++;
      if (!connectionLost) toast('Lost connection to the Coding Brain desktop service. Reconnecting…');
      connectionLost = true;
      setConnection(false, `Reconnecting… (attempt ${heartbeatFailures})`);
      if (!activeJob) $('footer-status').textContent = 'Desktop service unreachable';
    }
    scheduleHeartbeat();
  }

  async function submit() {
    const text = $('prompt').value.trim();
    if (!text) return;
    if (activeJob || submitting) { toast('Finish or stop the current session first.'); return; }
    if (mode === 'run' && (!currentProject || !currentProject.git)) {
      toast('Open an existing Git repository before running a coding task.');
      openDialog();
      return;
    }
    if (attachments.length && mode === 'chat') {
      toast('Attachments are read by Run task and New project. Switch mode to send these files.');
      return;
    }
    submitting = true;
    busySend(true);
    try {
      // A new desktop can start before the local engine has answered its
      // readiness probe. Do not draw a "Thinking" bubble or clear the user's
      // message until the INSTALLED engine confirms this operation exists.
      if (mode === 'chat' || mode === 'new') {
        $('run-status').hidden = false;
        $('status-label').textContent = 'Checking installed Coding Brain engine…';
        const info = engineInfo || await refreshEngine({force:true});
        if (!info || info.engine?.available !== true) {
          const detail = info?.engine?.detail || 'Engine status could not be verified.';
          message('CODING BRAIN · SETUP', detail + ' Open Setup to diagnose your engine. No task was started.', 'system');
          return;
        }
        if (mode === 'chat' && info.chat_available !== true) {
          message('CODING BRAIN · UPDATE', 'Your installed Coding Brain engine does not support conversational chat. A development PR is not an installed release. Update to a verified conversational version first; your message has been kept.', 'system');
          return;
        }
        if (mode === 'new' && info.engine?.new_project !== true) {
          message('CODING BRAIN · UPDATE', 'Your installed engine does not support new-project creation yet. No task started. To inspect existing projects, choose Projects or Browse folder.', 'system');
          return;
        }
      }
      message('YOU', text, 'user');
      $('prompt').value = '';
      showTyping();
      $('run-status').hidden = false;
      $('status-label').textContent = 'Connecting to Coding Brain…';
      const job = await api('/api/start', 'POST', {message: text, mode, attachments: attachments.map(item => item.path)});
      attachments = []; renderAttachments();
      resetFlow(job.kind);
      activeJob = job.id;
      cursor = 0;
      displayedEvents = new Set();
      startedAt = job.started;
      await poll();
      if (activeJob && !pollTimer) pollTimer = setInterval(poll, 750);
    } catch(e) {
      hideTyping();
      message('CODING BRAIN · CONNECTION', e.message, 'system');
      toast(e.message);
    } finally {
      submitting = false;
      if (!activeJob) { busySend(false); $('run-status').hidden = true; }
    }
  }

  async function refresh() {
    try {
      const data = await api('/api/state');
      knownProjects = data.projects;
      if (data.project) updateProject(data.project);
      await refreshEngine({force:true});
      if (data.active && ['starting','running','approval_required'].includes(data.active.status)) {
        activeJob = data.active.id;
        startedAt = data.active.started;
        if (flow.kind !== data.active.kind || cursor === 0) { resetFlow(data.active.kind); cursor = 0; displayedEvents = new Set(); }
        await poll();
        if (!pollTimer) pollTimer = setInterval(poll, 750);
      }
    } catch(e) { message('CODING BRAIN', 'Unable to connect to the local workspace: ' + e.message, 'system'); }
  }

  $('mode-chat').addEventListener('click', () => setMode('chat'));
  $('mode-run').addEventListener('click', () => setMode('run'));
  $('mode-new').addEventListener('click', () => setMode('new'));
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
  $('engine-banner-close').addEventListener('click', () => { engineNoticeDismissed = true; $('engine-banner').hidden = true; });
  $('engine-banner-action').addEventListener('click', () => openControl(engineInfo?.next_action === 'updates' ? 'updates' : 'setup'));
  $('preview-close').addEventListener('click', () => $('preview').hidden = true);
  $('stop-task').addEventListener('click', async () => {
    if (!activeJob || !await confirmAction('Stop the active task?', 'Coding Brain will request cancellation at a safe boundary. Unsaved task progress may be interrupted.', 'Stop task', true)) return;
    try { await api(`/api/jobs/${activeJob}/stop`, 'POST'); await poll(); }
    catch(e) { toast(e.message); }
  });
  for (const item of document.querySelectorAll('[data-suggestion]')) {
    item.addEventListener('click', () => {$('prompt').value = item.dataset.suggestion; $('prompt').focus();});
  }
  const PANELS = {files: 'file-panel', activity: 'activity-panel', flow: 'flow-panel', git: 'git-panel'};
  for (const kind of Object.keys(PANELS)) {
    $('tab-' + kind).addEventListener('click', () => {
      for (const tab of Object.keys(PANELS)) {
        $('tab-' + tab).classList.toggle('active', tab === kind);
        $('tab-' + tab).setAttribute('aria-selected', String(tab === kind));
        $(PANELS[tab]).hidden = tab !== kind;
      }
      if (kind === 'git') {
        if (gitLatest) renderGitSnapshot(gitLatest);
        else gitEmpty(currentProject ? 'Checking local Git…' : 'Choose a project to view Git.');
        refreshGit(true);
      }
    });
  }
  $('git-refresh').addEventListener('click', () => refreshGit(true));

  // ---- Attachments: chosen with the native picker, or pasted / dropped and staged privately by the
  // bridge. Only paths travel to the engine, which re-checks and parses each file in isolation.
  function formatBytes(bytes) {
    return bytes >= 1e6 ? (bytes / 1e6).toFixed(1) + ' MB' : bytes >= 1e3 ? Math.round(bytes / 1e3) + ' KB' : bytes + ' B';
  }

  function renderAttachments() {
    const list = $('attachment-list');
    list.replaceChildren();
    list.hidden = attachments.length === 0;
    for (const item of attachments) {
      const chip = document.createElement('span');
      chip.className = 'attachment';
      chip.title = item.path;
      const name = document.createElement('span'); name.className = 'attachment-name'; name.textContent = item.name;
      const size = document.createElement('span'); size.className = 'attachment-size'; size.textContent = formatBytes(item.bytes);
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'attachment-remove'; remove.textContent = '×';
      remove.setAttribute('aria-label', 'Remove ' + item.name);
      remove.addEventListener('click', () => { attachments = attachments.filter(other => other !== item); renderAttachments(); });
      chip.append(name, size, remove);
      list.append(chip);
    }
  }

  function addAttachments(files) {
    for (const file of files) {
      if (attachments.some(item => item.path === file.path)) continue;
      if (attachments.length >= 10) { toast('At most 10 attachments per message'); break; }
      attachments.push(file);
    }
    renderAttachments();
  }

  async function uploadAttachment(file) {
    const response = await fetch('/api/attachments/upload', {method: 'POST', body: file, headers: {
      'X-CodingBrain-Token': token || '', 'Content-Type': 'application/octet-stream',
      'X-Filename': encodeURIComponent(file.name || 'pasted-screenshot.png')}});
    let result = {};
    try { result = await response.json(); } catch { /* reported below */ }
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Attachment could not be added');
    return result;
  }

  async function uploadAll(files) {
    for (const file of files) {
      try { addAttachments([await uploadAttachment(file)]); }
      catch (e) { toast(e.message); }
    }
  }

  $('attach-files').addEventListener('click', async () => {
    busyButton($('attach-files'), true);
    try {
      const result = await api('/api/attachments/pick', 'POST');
      addAttachments(result.files || []);
      if (result.refused?.length) toast(result.refused.join(' · '));
    } catch (e) { toast(e.message); }
    finally { busyButton($('attach-files'), false); }
  });
  $('prompt').addEventListener('paste', event => {
    const files = [...(event.clipboardData?.files || [])];
    if (!files.length) return;  // ordinary text paste
    event.preventDefault();
    uploadAll(files);
  });
  const composer = document.querySelector('.composer');
  composer.addEventListener('dragover', event => {
    if ([...(event.dataTransfer?.types || [])].includes('Files')) { event.preventDefault(); composer.classList.add('dropping'); }
  });
  composer.addEventListener('dragleave', () => composer.classList.remove('dropping'));
  composer.addEventListener('drop', event => {
    composer.classList.remove('dropping');
    const files = [...(event.dataTransfer?.files || [])];
    if (!files.length) return;
    event.preventDefault();
    uploadAll(files);
  });
  $('branch-name').addEventListener('click', () => $('tab-git').click());
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    if (currentProject) refreshGit(true);
    heartbeat();  // check right away on return instead of waiting out the hidden-window interval
  });
  window.addEventListener('online', () => heartbeat());
  $('collapse-explorer').addEventListener('click', () => setExplorerVisibility(false));
  $('open-explorer').addEventListener('click', () => setExplorerVisibility(true));
  $('profile-trigger').addEventListener('click', () => {
    const show = $('profile-popover').hidden;
    $('profile-popover').hidden = !show;
    $('profile-trigger').setAttribute('aria-expanded', String(show));
  });
  document.addEventListener('pointerdown', event => {
    if (!event.target.closest('.profile-anchor')) {
      $('profile-popover').hidden = true;
      $('profile-trigger').setAttribute('aria-expanded', 'false');
    }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !$('profile-popover').hidden) {
      $('profile-popover').hidden = true;
      $('profile-trigger').setAttribute('aria-expanded', 'false');
      $('profile-trigger').focus();
    }
  });
  $('profile-providers').addEventListener('click', () => { $('profile-popover').hidden = true; $('profile-trigger').setAttribute('aria-expanded','false'); openControl('providers'); });
  $('profile-setup').addEventListener('click', () => { $('profile-popover').hidden = true; $('profile-trigger').setAttribute('aria-expanded','false'); openControl('setup'); });
  $('nav-chat').addEventListener('click', () => $('prompt').focus());
  $('nav-settings').addEventListener('click', () => openControl('setup'));
  $('nav-providers').addEventListener('click', () => openControl('providers'));
  $('model-label').addEventListener('click', () => openControl('providers'));
  $('header-setup').addEventListener('click', () => openControl('setup'));
  $('control-close').addEventListener('click', () => { stopProviderWatch(); $('control-dialog').close(); });
  $('control-tab-setup').addEventListener('click', () => showControl('setup'));
  $('control-tab-providers').addEventListener('click', () => showControl('providers'));
  $('control-tab-updates').addEventListener('click', () => showControl('updates'));
  $('setup-check').addEventListener('click', reloadControl);
  $('refresh-providers').addEventListener('click', async () => {
    const button = $('refresh-providers');
    busyButton(button,true);
    try { await reloadControl(); } finally {busyButton(button,false);}
  });
  $('deep-check').addEventListener('click', () => { showControl('updates'); systemAction('doctor'); });
  $('setup-continue').addEventListener('click', async () => {
    try { await api('/api/setup/completed', 'POST', {completed:true}); $('control-dialog').close(); }
    catch(e) { toast(e.message); }
  });
  $('full-setup').addEventListener('click', () => systemAction('install_full'));
  $('update-channel').addEventListener('change', () => {
    const preview = $('update-channel').value === 'preview';
    $('apply-update').textContent = preview ? 'Install verified preview' : 'Install latest stable';
    $('update-channel-description').textContent = preview ?
      'Preview: UNRELEASED Windows CI artifacts from the current preview branch, only after successful checks. Requires GitHub CLI sign-in.' :
      'Stable: owner-approved, tagged GitHub releases only. Draft PRs are never installed.';
    $('maintenance-log').textContent = 'Channel changed. Check for updates to verify the selected source.';
    $('apply-update').disabled = true;
  });
  $('check-updates').addEventListener('click', () => systemAction('check_updates'));
  $('apply-update').addEventListener('click', () => systemAction('update_engine'));
  $('repair-install').addEventListener('click', () => systemAction('repair'));
  $('new-project-empty').addEventListener('click', () => { setMode('new'); $('prompt').focus(); });
  let setupCache = null;
  let activeControlTab = 'setup';
  let providerWatchTimer = null;
  let providerWatchActive = false;

  function stopProviderWatch() {
    if (providerWatchTimer !== null) clearInterval(providerWatchTimer);
    providerWatchTimer = null;
  }

  function watchProviderSignIn(providerId, name) {
    stopProviderWatch();
    const started = Date.now();
    // Official vendor CLI owns credentials. Poll read-only status, never a sign-in
    // URL or session token. Only verified CLI authentication becomes connected.
    providerWatchTimer = setInterval(async () => {
      if (!$('control-dialog').open || activeControlTab !== 'providers') {
        stopProviderWatch();
        return;
      }
      if (providerWatchActive) return;
      providerWatchActive = true;
      try {
        const data = await reloadControl({quiet:true});
        const provider = data?.providers?.find(p => p.id === providerId);
        if (provider?.authenticated) {
          stopProviderWatch();
          feedback('success', `${name} sign-in verified`, provider.supervisor_enabled ?
            'Connected and enabled as a governed supervisor.' :
            'Signed in successfully. Enable the supervisor separately if you want it used for coding tasks.');
        } else if (Date.now() - started >= 120000) {
          stopProviderWatch();
          feedback('info', `${name} connection not yet verified`,
            'The sign-in terminal was opened, but the desktop cannot confirm authentication yet. Finish in the official CLI, then click Recheck connections.');
        }
      } catch {
        // A transient probe error is not a failed sign-in; a manual recheck remains available.
      } finally { providerWatchActive = false; }
    }, 8000);
  }

  function showControl(tab) {
    activeControlTab = tab;
    if (tab !== 'providers') stopProviderWatch();
    const headings={setup:['Set up your coding workspace','Check your local engine, install requirements and connect optional providers.'],providers:['Manage your AI providers','Connect subscriptions, inspect provider support and choose authorized supervisors.'],updates:['Installation and updates','Check verified releases, diagnose readiness and repair the coding environment.']};
    $('control-heading').textContent = headings[tab][0];
    $('control-subtitle').textContent = headings[tab][1];
    for (const part of ['setup','providers','updates']) {
      const shown = tab === part;
      $('control-' + part).hidden = !shown;
      $('control-tab-' + part).classList.toggle('active', shown);
    }
  }

  function openControl(tab='setup') {
    showControl(tab);
    if (!$('control-dialog').open) $('control-dialog').showModal();
    reloadControl();
  }

  function renderSetup(readiness) {
    const installed = readiness.components.filter(c => c.installed).length;
    const all = readiness.components.length;
    $('setup-message').textContent = `${installed} of ${all} key tools were detected. ` + readiness.full_installer_note;
    $('full-setup').disabled = !readiness.full_installer_available;
    $('full-setup').title = readiness.full_installer_available ? 'Run official guided full installation' : readiness.full_installer_note;
    const deep = readiness.deep_doctor_available !== false;
    $('deep-check').textContent = deep ? 'Deep system test' : 'Basic system check';
    $('deep-check').title = deep ? 'Verify the configured model and Docker sandbox' : 'The installed backend supports basic health checks only. Update Coding Brain for deep verification.';
    $('setup-components').setAttribute('aria-busy', 'false');
    $('setup-components').replaceChildren();
    for (const entry of readiness.components) {
      const row = document.createElement('div');
      row.className = 'component-card ' + (entry.installed ? '' : 'missing');
      const icon = document.createElement('span');
      icon.className = 'component-state';
      icon.textContent = entry.installed ? '✓' : '!';
      const content = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = entry.name;
      const note = document.createElement('small');
      note.textContent = entry.detail;
      content.append(title,note);
      row.append(icon,content);
      $('setup-components').append(row);
    }
  }

  function renderProviders(providers) {
    for (const providerId of ['claude','codex']) {
      const provider = providers.find(entry => entry.id === providerId);
      const node = $('readiness-' + providerId);
      const name = providerId === 'claude' ? 'Claude' : 'Codex';
      const connected = provider?.authenticated === true && provider?.supervisor_enabled === true;
      const signedIn = provider?.authenticated === true;
      node.textContent = `${name}: ${connected ? 'connected' : signedIn ? 'signed in (disabled)' :
        provider?.status === 'not-installed' ? 'CLI not found' :
        provider?.status === 'sign-in-needed' ? 'sign-in needed' : 'not verified'}`;
      node.dataset.ready = connected ? 'true' : 'false';
    }
    const list = $('provider-list');
    list.replaceChildren();
    const logos = {ollama:'◈',claude:'C',codex:'O',gemini:'G',grok:'X',meta:'M',muse:'✧'};
    for (const provider of providers) {
      const row = document.createElement('article'); row.className = 'provider-row';
      row.dataset.provider = provider.id;
      const left = document.createElement('div'); left.className = 'provider-identity';
      const mark = document.createElement('div'); mark.className = 'provider-logo'; mark.textContent = logos[provider.id] || '•';
      const inner = document.createElement('div');
      const heading = document.createElement('div'); heading.className = 'provider-name'; heading.textContent = provider.name;
      const desc = document.createElement('p'); desc.className = 'provider-description'; desc.textContent = provider.description;
      const isSubscription = ['claude','codex'].includes(provider.id);
      const authenticated = isSubscription && provider.authenticated === true;
      // Preserve compatibility with an older /api/setup response, without treating
      // an installed executable or a settings toggle as authenticated.
      const verified = authenticated || (isSubscription && provider.status === 'authenticated');
      const connected = verified && provider.supervisor_enabled === true;
      const state = isSubscription ?
        connected ? 'connected' : verified ? 'signed-in' :
        provider.status === 'not-installed' ? 'not-installed' : 'unverified' : provider.status;
      const badges = document.createElement('div'); badges.className = 'provider-badges';
      const badge = document.createElement('span'); badge.className = 'provider-badge ' + state;
      badge.textContent = provider.id === 'ollama' ?
        provider.model_ready ? 'Model detected' : provider.ollama_running ? 'Configured model missing' : 'Ollama not running' : isSubscription ?
        connected ? '✓ Connected' : verified ? '✓ Authenticated' :
        provider.status === 'not-installed' ? 'CLI not found' :
        provider.status === 'sign-in-needed' ? 'Sign-in needed' : 'Not verified' :
        provider.status.replaceAll('-', ' ');
      const extra = document.createElement('span'); extra.className = 'provider-badge subdued';
      extra.textContent = isSubscription ?
        provider.supervisor_enabled ? 'Supervisor enabled' : 'Supervisor disabled' :
        provider.core_enabled ? 'Core integration' : 'Connector pending';
      badges.append(badge,extra);
      inner.append(heading,desc,badges);
      if (provider.id === 'ollama') {
        const localState = document.createElement('div');
        localState.className = 'provider-connection ' + (provider.model_ready ? 'signed-in' : 'unverified');
        localState.textContent = provider.detail || 'The local model has not been verified.';
        inner.append(localState);
      }
      if (isSubscription) {
        const stateText = document.createElement('div');
        stateText.className = 'provider-connection ' + state;
        stateText.textContent = connected ?
          'Official CLI sign-in verified. Coding Brain may use this supervisor within your budgets.' :
          verified ? 'Official CLI sign-in verified. Enable supervisor to use it for coding tasks.' :
          provider.detail || (provider.status === 'not-installed' ? 'CLI not found on this desktop.' : 'Sign-in has not been verified.');
        inner.append(stateText);
      }
      left.append(mark,inner);
      const actions = document.createElement('div'); actions.className = 'provider-actions';
      if (provider.sign_in_available && !verified) {
        const sign = document.createElement('button'); sign.textContent = 'Sign in with ' + provider.name;
        sign.addEventListener('click', async () => {
          if (!await confirmAction(`Connect ${provider.name}`,
            'Open the official provider sign-in in a separate console. Only a successful CLI auth check will mark the account connected.',
            'Open sign-in')) return;
          busyButton(sign,true);
          feedback('busy',`Opening ${provider.name} sign-in`, 'Launch is not proof of authentication. Coding Brain will recheck the provider while this panel is open.');
          try {
            const response = await api(`/api/providers/${provider.id}/signin`,'POST',{confirmed:true});
            feedback('info',`${provider.name} sign-in opened`,
              'Finish sign-in in the official terminal. Checking the CLI for verified sign-in; you can also select Recheck connections.');
            toast(response.message);
            // CLI-based subscription sign-ins support a side-effect-free status check.
            if (isSubscription) watchProviderSignIn(provider.id,provider.name);
          } catch(e) { feedback('error','Could not start sign-in',e.message); toast(e.message); }
          finally { busyButton(sign,false); }
        });
        actions.append(sign);
      }
      if (isSubscription) {
        if (verified) {
          const recheck = document.createElement('button'); recheck.textContent = 'Recheck connection';
          recheck.addEventListener('click',async () => {
            busyButton(recheck,true);
            try {await reloadControl();}
            finally {busyButton(recheck,false);}
          });
          actions.append(recheck);
        }
        const toggle = document.createElement('button');
        toggle.textContent = provider.supervisor_enabled ? 'Disable supervisor' : 'Enable supervisor';
        // Installation, authentication and configuration are distinct. Never let an
        // unverified account silently become an enabled subscription supervisor.
        if (!verified && !provider.supervisor_enabled) {
          toggle.disabled = true;
          toggle.title = 'Sign in and verify the official CLI before enabling this supervisor';
        }
        toggle.addEventListener('click',async () => {
          const enabling = !provider.supervisor_enabled;
          if (enabling && !verified) {toast('Verify your sign-in before enabling this supervisor.');return;}
          if (!await confirmAction(`${enabling ? 'Enable' : 'Disable'} ${provider.name}`,
            `Change ${provider.name} supervisor routing? Existing budgets and approvals remain in effect.`,
            enabling ? 'Enable supervisor' : 'Disable supervisor', !enabling)) return;
          busyButton(toggle,true);
          feedback('busy',`Saving ${provider.name} routing`, 'Applying the setting through your installed Coding Brain engine.');
          try {
            const res = await api(`/api/providers/${provider.id}/configure`,'POST',{enabled:enabling});
            await reloadControl();
            feedback('success',`${provider.name} supervisor setting saved`,
              enabling ? 'Enabled in Coding Brain configuration. CLI authentication is verified separately.' : 'Supervisor disabled. Sign-in remains managed by the official provider CLI.');
            toast(res.message);
          } catch(e) {feedback('error','Provider setting failed',e.message);toast(e.message);}
          finally {busyButton(toggle,false);}
        });
        actions.append(toggle);
      }
      if (provider.docs) {
        const link=document.createElement('a');link.href=provider.docs;link.target='_blank';link.rel='noopener noreferrer';link.textContent='Official docs ↗';actions.append(link);
      }
      row.append(left,actions);list.append(row);
    }
  }

  async function reloadControl(options = {}) {
    const quiet = options.quiet === true;
    // Share a pending request: opening Setup and the first-run check must not race.
    if (controlLoad) return controlLoad;
    if (!setupCache) loadingComponents();
    if (!quiet && !activeMaintenance) feedback('busy', 'Checking your environment', 'Detecting installed tools and provider readiness. No changes are being made.');
    busyButton($('setup-check'), true);
    controlLoad = (async () => {
      try {
        setupCache = await api('/api/setup');
        renderSetup(setupCache.readiness);
        renderProviders(setupCache.providers);
        if (!quiet) await refreshEngine({force:true, quiet:true});
        if (!quiet && !activeMaintenance) {
          const connected = setupCache.providers.filter(p => ['claude','codex'].includes(p.id) && p.authenticated && p.supervisor_enabled);
          if (activeControlTab === 'providers') feedback('success', 'Provider connections refreshed',
            connected.length ? `Verified active supervisors: ${connected.map(p => p.name).join(', ')}.` :
            'No authenticated and enabled subscription supervisors were detected. The cards show sign-in and routing separately.');
          else feedback('success','Environment check finished','These are detected tools, not a guarantee that every model or sandbox is ready. Use Deep system test for verification.');
        }
        return setupCache;
      } catch(e) {
        $('setup-components').setAttribute('aria-busy', 'false');
        if (!setupCache) $('setup-components').replaceChildren();
        $('setup-message').textContent = 'Could not check the local engine: ' + e.message;
        if (!quiet && !activeMaintenance) feedback('error', 'Environment check failed', e.message + '. Use Refresh checks to retry.');
        return null;
      } finally {
        controlLoad = null;
        busyButton($('setup-check'), false);
        // renderSetup may have changed this availability while Refresh checks ran.
        if (setupCache) $('full-setup').disabled = !setupCache.readiness.full_installer_available;
      }
    })();
    return controlLoad;
  }

  const maintenanceButtons = {
    install_full:'full-setup', update_engine:'apply-update', repair:'repair-install',
    check_updates:'check-updates', doctor:'deep-check'
  };
  const maintenanceNames = {
    install_full:'Full system installation', update_engine:'Stable update', repair:'Installation repair',
    check_updates:'Update check', doctor:'Deep system test'
  };

  async function systemAction(action) {
    const updateChannel = $('update-channel').value === 'preview' ? 'preview' : 'stable';
    const explanations = {
      install_full:'Run the official full installer? It may download software, require administrator approval and ask for subscription sign-in.',
      update_engine: updateChannel === 'preview' ?
        'Install an UNRELEASED CI preview (not an official GitHub Release) from the current preview branch, only after successful Windows checks? GitHub CLI authentication is needed. You must close the desktop before installation.' :
        'Update both the installed Coding Brain engine and native desktop from the same owner-approved stable release? Close the desktop and follow the installer instructions. Older engine-only releases cannot update the desktop EXE.',
      repair:'Open the official installer to diagnose and repair your current setup?',
      check_updates:'Check GitHub for a newer verified stable Coding Brain release?',
      doctor:'Run deep checks of your local model, Docker sandbox and providers? This can take a minute.'
    };
    if (activeMaintenance) { toast('A setup or update operation is already running.'); return; }
    const legacyDoctor = action === 'doctor' && setupCache?.readiness?.deep_doctor_available === false;
    if (action !== 'check_updates' && !await confirmAction(
      legacyDoctor ? 'Run basic health check?' : (updateChannel === 'preview' && action === 'update_engine' ? 'Install UNRELEASED preview?' : (maintenanceNames[action] || 'System operation') + '?'),
      legacyDoctor ? 'Your installed Coding Brain does not support doctor --full. A basic health check will run instead; upgrade the backend for deep model and sandbox tests.' : (explanations[action] || 'Continue?'),
      action === 'doctor' ? 'Run check' : 'Continue',
      false)) return;
    activeMaintenance = action;
    let name = legacyDoctor ? 'Basic system check' : (maintenanceNames[action] || 'System operation');
    const button = $(maintenanceButtons[action]);
    busyButton(button, true);
    feedback('busy', `${name} starting`, 'Waiting for the official Coding Brain backend.');
    if (action === 'doctor' || action === 'check_updates' || action === 'repair' || action === 'update_engine') showControl('updates');
    if (action === 'doctor' || action === 'check_updates') $('maintenance-log').textContent = 'Connecting to Coding Brain…';
    try {
      if (action === 'check_updates') {
        const info = await api('/api/updates/status?channel=' + encodeURIComponent(updateChannel));
        const lines = [
          `Latest published stable: ${info.tag}`,
          `Installed engine: ${info.engine}  (${info.engine_update_available ? 'update available' : 'current'})`,
          `Installed desktop: ${info.desktop}  (${info.desktop_update_available ? 'update available' : info.desktop_in_release ? 'current' : 'no desktop installer in this release'})`
        ];
        if (info.unreleased) lines.push('UNRELEASED Windows preview, NOT a published GitHub release. Source: ' + info.preview_source_commit + '; CI run: ' + info.preview_run_id);
        if (info.engine_only) lines.push('This release updates the engine only. A packaged desktop update is NOT available.');
        $('maintenance-log').textContent = lines.join('\n');
        $('apply-update').disabled = !info.any_update_available;
        feedback(info.any_update_available ? 'info' : 'success',
          info.any_update_available ? 'Verified stable update available' : 'No newer supported release',
          info.unreleased ? 'Opt-in, successful Windows preview workflow artifact. It is not an official published release.' :
          info.engine_only ? 'GitHub has no matching desktop installer in this release; the update can only change the backend.' :
            'Both components are checked independently against the latest published stable release.');
        return;
      }
      const data = await api(`/api/system/action?action=${encodeURIComponent(action)}&channel=${encodeURIComponent(action === 'update_engine' ? updateChannel : 'stable')}`,'POST',{confirmed:true});
      // The backend decides the actual supported diagnostic level, not an optimistic UI assumption.
      if (action === 'doctor' && data.diagnostic_level === 'basic') {
        name = 'Basic system check';
        feedback('busy', `${name} running`, 'Your installed engine lacks deep verification. Checking the basic health indicators that it supports.');
      }
      if (data.opened_terminal) {
        $('maintenance-log').textContent = data.message || `${name} opened in a separate terminal.`;
        feedback('info', `${name} launched`, 'Continue in the official terminal. Completion cannot be verified from this window; refresh checks after it finishes.');
        toast('Official Coding Brain console opened');
        return;
      }
      if (!data.job || !data.job.id) throw new Error('Backend did not return an operation or an official terminal.');
      const jobId = data.job.id;
      let seen = 0;
      let requestPending = false;
      let errors = 0;
      $('maintenance-log').textContent = `${name} started. Waiting for verified output…`;
      feedback('busy', `${name} running`, 'Live output appears below. Elapsed time is shown; no completion percentage is estimated.');
      const stopPolling = () => {
        clearInterval(maintenancePoll); maintenancePoll = null;
        activeMaintenance = null;
        busyButton(button, false);
      };
      const fetchLog = async () => {
        if (requestPending || activeMaintenance !== action) return;
        requestPending = true;
        try {
          const update = await api(`/api/jobs/${jobId}?after=${seen}`);
          errors = 0;
          for (const event of update.events || []) {
            seen = Math.max(seen, event.seq);
            if (event.kind === 'output' || event.kind === 'error') {
              const existing = $('maintenance-log').textContent;
              $('maintenance-log').textContent = (existing + '\n' + event.message).slice(-65000);
            }
          }
          $('maintenance-log').scrollTop = $('maintenance-log').scrollHeight;
          const status = update.job.status;
          if (!['starting', 'running', 'approval_required'].includes(status)) {
            stopPolling();
            if (status === 'completed') {
              if (action === 'doctor' || action === 'check_updates') await reloadControl();
              feedback('success', `${name} completed`, action === 'doctor' && data.diagnostic_level === 'basic' ? 'Basic checks finished. Deep model and Docker verification requires a newer Coding Brain backend. Review the output below.' : 'The backend operation finished. Review its output below.');
            } else {
              feedback('error', `${name} ${status}`, update.job.error || 'See the operation output below. You can retry.');
            }
          } else if (status === 'approval_required') {
            feedback('busy', `${name} awaiting approval`, 'Complete the required confirmation in the official Coding Brain workflow.');
          }
        } catch(e) {
          errors++;
          if (errors >= 3) {
            stopPolling();
            feedback('error', `${name} connection interrupted`, 'The backend status could not be retrieved. Do not assume the operation stopped: check again before retrying. ' + e.message);
          } else {
            feedback('busy', `${name} reconnecting`, 'Waiting for the local engine to respond.');
          }
        } finally { requestPending = false; }
      };
      maintenancePoll = setInterval(fetchLog, 750);
      await fetchLog();
      // This asynchronous job holds activeMaintenance until a terminal status arrives.
      return;
    } catch(e) {
      feedback('error', `${name} could not start`, e.message);
      $('maintenance-log').textContent = e.message;
      toast(e.message);
    } finally {
      // An explicit external terminal launch is a handoff, not a completed installation.
      // Keep the button spinning only for an actual polled job still running.
      if (!maintenancePoll) {
        activeMaintenance = null;
        busyButton(button, false);
      }
    }
  }

  async function firstRun() {
    try {
      const data = await reloadControl();
      if (data && !data.readiness.onboarding_completed) {
        // Never automatically install; only show the guided setup panel.
        renderSetup(data.readiness); renderProviders(data.providers);
        showControl('setup'); $('control-dialog').showModal();
      }
    } catch(e) { /* Main chat stays usable even if readiness probes fail. */ }
  }

  setInterval(tickTime, 1000);
  // Poll only while this desktop is visible. This is near-live local state,
  // not a remote GitHub watcher or a mutating Git operation.
  setInterval(() => { if (currentProject && !document.hidden) refreshGit(); }, 3000);
  // Projects without Git get no change signal from the Git snapshot; re-read their open tree.
  setInterval(() => {
    if (currentProject && !currentProject.git && !document.hidden && !$('file-panel').hidden) loadTree();
  }, 8000);
  // Readiness changes on its own (Ollama stopped, a supervisor signed out): re-check quietly.
  setInterval(() => { if (!document.hidden && !connectionLost && !activeJob) refreshEngine({quiet: true}); }, 60000);
  if (!token) message('CODING BRAIN', 'Missing local session token. Launch this page using python -m desktop_ui.', 'system');
  else { renderEmptyExplorer(); renderFlow(); refresh(); firstRun(); heartbeat(); }
})();
