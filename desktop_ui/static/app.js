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
    content.textContent = text;
    bubble.append(head, content);
    row.append(avatar, bubble);
    $('thread').append(row);
    $('thread').scrollTop = $('thread').scrollHeight;
    return bubble;
  }

  function setMode(value) {
    mode = value;
    for (const kind of ['chat', 'run', 'new']) {
      const button = $('mode-' + kind);
      button.classList.toggle('active', kind === value);
      button.setAttribute('aria-pressed', String(kind === value));
    }
    $('prompt').placeholder = value === 'chat' ? 'Ask Coding Brain anything...' :
      value === 'new' ? 'Describe the app you want Coding Brain to create...' : 'Describe a coding goal...';
  }

  function updateProject(project) {
    currentProject = project;
    $('project-label').textContent = project ? project.name : 'Choose project';
    $('project-breadcrumb').textContent = project ? project.path : 'Global conversation';
    $('explorer-folder').textContent = project ? project.name : 'No project selected';
    $('branch-name').textContent = project && project.git ? '◇ ' + project.git : '◇ No repository';
    $('file-count').textContent = '';
    $('profile-project').textContent = project ? project.name : 'No project selected';
    expanded.clear();
    $('preview').hidden = true;
    if (project) loadTree();
    else renderEmptyExplorer();
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
      const diff = document.createElement('pre');
      diff.className = 'proposal-diff';
      diff.textContent = details.diff;
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
      if (job.project && (!currentProject || currentProject.path !== job.project) && mode === 'new') {
        try { const project = await api('/api/project', 'POST', {path: job.project}); updateProject(project); }
        catch (e) { toast('Project created; select its folder to view files.'); }
      }
      for (const event of response.events) {
        cursor = Math.max(cursor, event.seq);
        if (displayedEvents.has(event.seq)) continue;
        displayedEvents.add(event.seq);
        activityEntry(event);
        if (event.kind === 'approval') approval(job.id, job.approval || {summary: event.message});
        if (event.kind === 'output') message(job.kind === 'chat' ? 'CODING BRAIN' : 'CODING BRAIN · ACTIVITY', event.message, 'assistant');
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
        // A chat reply already appears as an output event; don't add a redundant
        // 'Session finished' bubble after every conversational answer.
        if (job.kind !== 'chat' || job.status !== 'completed') {
          const text = job.status === 'completed' ? 'Coding task completed.' : `Session ${job.status}.`;
          message('CODING BRAIN', text + (job.error ? ` ${job.error}` : ''), 'system');
        }
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
  $('control-close').addEventListener('click', () => $('control-dialog').close());
  $('control-tab-setup').addEventListener('click', () => showControl('setup'));
  $('control-tab-providers').addEventListener('click', () => showControl('providers'));
  $('control-tab-updates').addEventListener('click', () => showControl('updates'));
  $('setup-check').addEventListener('click', reloadControl);
  $('deep-check').addEventListener('click', () => { showControl('updates'); systemAction('doctor'); });
  $('setup-continue').addEventListener('click', async () => {
    try { await api('/api/setup/completed', 'POST', {completed:true}); $('control-dialog').close(); }
    catch(e) { toast(e.message); }
  });
  $('full-setup').addEventListener('click', () => systemAction('install_full'));
  $('check-updates').addEventListener('click', () => systemAction('check_updates'));
  $('apply-update').addEventListener('click', () => systemAction('update_engine'));
  $('repair-install').addEventListener('click', () => systemAction('repair'));
  $('new-project-empty').addEventListener('click', () => { setMode('new'); $('prompt').focus(); });
  let setupCache = null;
  let maintenancePoll = null;

  function showControl(tab) {
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
    const list = $('provider-list');
    list.replaceChildren();
    const logos = {ollama:'◈',claude:'C',codex:'O',gemini:'G',grok:'X',meta:'M',muse:'✧'};
    for (const provider of providers) {
      const row = document.createElement('article'); row.className = 'provider-row';
      const left = document.createElement('div'); left.className = 'provider-identity';
      const mark = document.createElement('div'); mark.className = 'provider-logo'; mark.textContent = logos[provider.id] || '•';
      const inner = document.createElement('div');
      const heading = document.createElement('div'); heading.className = 'provider-name'; heading.textContent = provider.name;
      const desc = document.createElement('p'); desc.className = 'provider-description'; desc.textContent = provider.description;
      const badges = document.createElement('div'); badges.className = 'provider-badges';
      const badge = document.createElement('span'); badge.className = 'provider-badge'; badge.textContent = provider.status.replaceAll('-', ' ');
      const extra = document.createElement('span'); extra.className = 'provider-badge subdued';
      extra.textContent = provider.core_enabled ? 'Core integration' : 'Connector pending';
      badges.append(badge,extra); inner.append(heading,desc,badges); left.append(mark,inner);
      const actions = document.createElement('div'); actions.className = 'provider-actions';
      if (provider.sign_in_available) {
        const sign = document.createElement('button'); sign.textContent = 'Sign in / reconnect';
        sign.addEventListener('click', async () => {
          if (!confirm(`Open the official ${provider.name} CLI sign-in?`)) return;
          try { const response = await api(`/api/providers/${provider.id}/signin`, 'POST', {confirmed:true}); toast(response.message); }
          catch(e) { toast(e.message); }
        }); actions.append(sign);
      }
      if (['claude','codex'].includes(provider.id)) {
        const toggle = document.createElement('button'); toggle.textContent = provider.supervisor_enabled ? 'Disable supervisor' : 'Enable supervisor';
        toggle.addEventListener('click', async () => {
          const enabling = !provider.supervisor_enabled;
          if (!confirm(`${enabling ? 'Enable' : 'Disable'} ${provider.name} as a governed Coding Brain supervisor? Existing budget limits remain in effect.`)) return;
          try { const res=await api(`/api/providers/${provider.id}/configure`,'POST',{enabled:enabling}); toast(res.message); await reloadControl(); }
          catch(e) { toast(e.message); }
        }); actions.append(toggle);
      }
      if (provider.docs) {
        const link=document.createElement('a'); link.href=provider.docs; link.target='_blank'; link.rel='noopener noreferrer'; link.textContent='Official docs ↗'; actions.append(link);
      }
      row.append(left,actions); list.append(row);
    }
  }

  async function reloadControl() {
    try {
      setupCache = await api('/api/setup');
      renderSetup(setupCache.readiness);
      renderProviders(setupCache.providers);
    } catch(e) { $('setup-message').textContent = 'Cannot check the local engine: ' + e.message; }
  }

  async function systemAction(action) {
    const explanations = {
      install_full:'Run the official full installer? It may download software, require administrator approval and ask for subscription sign-in.',
      update_engine:'Update Coding Brain to the latest verified stable release? The desktop should be restarted afterward.',
      repair:'Open the official installer to diagnose and repair your current setup?',
      check_updates:'Check GitHub for a newer verified stable Coding Brain release?',
      doctor:'Run deep checks of your local model, Docker sandbox and providers? This can take a minute.'
    };
    if (!confirm(explanations[action] || 'Continue?')) return;
    try {
      const data = await api(`/api/system/action?action=${encodeURIComponent(action)}`,'POST',{confirmed:true});
      if (data.opened_terminal) {
        $('maintenance-log').textContent = data.message;
        toast('Official Coding Brain console opened');
      } else if (data.job) {
        $('maintenance-log').textContent = 'Checking…';
        if (maintenancePoll) clearInterval(maintenancePoll);
        let seen=0;
        const fetchLog=async () => {
          try {
            const update=await api(`/api/jobs/${data.job.id}?after=${seen}`);
            for (const event of update.events) {
              seen=Math.max(seen,event.seq);
              if (event.kind==='output' || event.kind==='error') $('maintenance-log').textContent += '\n' + event.message;
            }
            $('maintenance-log').scrollTop=$('maintenance-log').scrollHeight;
            if (!['starting','running','approval_required'].includes(update.job.status)) {
              clearInterval(maintenancePoll); maintenancePoll=null;
            }
          } catch(e) { clearInterval(maintenancePoll); maintenancePoll=null; toast(e.message); }
        };
        await fetchLog();
        if (maintenancePoll === null && !['completed','failed','cancelled'].includes((await api(`/api/jobs/${data.job.id}?after=${seen}`)).job.status)) maintenancePoll = setInterval(fetchLog,650);
      }
    } catch(e) { toast(e.message); $('maintenance-log').textContent = e.message; }
  }

  async function firstRun() {
    try {
      const data=await api('/api/setup');
      setupCache=data;
      if (!data.readiness.onboarding_completed) {
        // Never automatically install; only show the guided setup panel.
        renderSetup(data.readiness); renderProviders(data.providers);
        showControl('setup'); $('control-dialog').showModal();
      }
    } catch(e) { /* Main chat stays usable even if readiness probes fail. */ }
  }

  setInterval(tickTime, 1000);
  if (!token) message('CODING BRAIN', 'Missing local session token. Launch this page using python -m desktop_ui.', 'system');
  else { renderEmptyExplorer(); refresh(); firstRun(); }
})();
