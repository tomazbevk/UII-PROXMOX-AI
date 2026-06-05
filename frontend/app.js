async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${txt}`);
  }
  return res.json();
}

function el(tag, props = {}, ...children) {
  const e = document.createElement(tag);
  Object.entries(props).forEach(([k, v]) => e.setAttribute(k, v));
  children.flat().forEach(c => e.append(typeof c === 'string' ? document.createTextNode(c) : c));
  return e;
}

function hasUnresolvedPlaceholder(command) {
  return typeof command === 'string' && /<[^>]+>/.test(command);
}

async function fetchApprovals() {
  const items = await api('/approvals');
  if (Array.isArray(items)) {
    items.forEach(it => updateChatApprovalCard(it));
  }
}

// Chat integration
async function sendChatQuery(query) {
  const modelEl = document.getElementById('model_select');
  const model = modelEl ? modelEl.value || null : null;
  const payload = { query: query, include_logs: true, log_limit: 20, model };
  return api('/chat', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
}

async function streamChatQuery(query) {
  const modelEl = document.getElementById('model_select');
  const model = modelEl ? modelEl.value || null : null;
  const payload = { query: query, include_logs: true, log_limit: 20, model };

  const res = await fetch('/chat/stream', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${txt}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let finalPayload = null;
  const container = document.getElementById('chat_messages');
  const msg = el('div', {}, el('strong', {}, 'Assistant: '), el('span', {}, ''));
  container.append(msg);
  const span = msg.querySelector('span');

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    const chunk = decoder.decode(value, { stream: true });
    buffer += chunk;

    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      let event;
      try {
        event = JSON.parse(trimmed);
      } catch (e) {
        continue;
      }
      if (event.type === 'chunk' && typeof event.text === 'string') {
        span.textContent += event.text;
      } else if (event.type === 'tool_call') {
        const tool = event.tool || 'unknown';
        const args = event.args || {};
        const action = {
          action: typeof args.action === 'string' && args.action.trim() ? args.action.trim() : tool.replace(/_/g, ' '),
          command: typeof args.command === 'string' && args.command.trim() ? args.command.trim() : null,
          target: typeof args.target === 'string' && args.target.trim() ? args.target.trim() : null,
          risk: typeof args.risk === 'string' && args.risk.trim() ? args.risk.trim() : 'medium',
        };

        await createApprovalFromAction(action, query, false);
      } else if (event.type === 'final' && event.payload) {
        finalPayload = event.payload;
      } else if (event.type === 'error' && event.error) {
        throw new Error(event.error);
      }
    }
  }

  if (buffer.trim()) {
    try {
      const event = JSON.parse(buffer.trim());
      if (event.type === 'chunk' && typeof event.text === 'string') {
        span.textContent += event.text;
      } else if (event.type === 'final' && event.payload) {
        finalPayload = event.payload;
      } else if (event.type === 'error' && event.error) {
        throw new Error(event.error);
      }
    } catch (e) {
      // ignore trailing decode noise
    }
  }

  if (!finalPayload) {
    finalPayload = { summary: span.textContent, reasoning: '', confidence: 0.0, suggested_actions: [] };
  }

  if (finalPayload.summary) {
    span.textContent = finalPayload.summary;
  }

  return finalPayload;
}

async function fetchModels() {
  try {
    const models = await api('/models');
    const sel = document.getElementById('model_select');
    if (!sel) return;
    // Clear existing options except the first
    const keep = sel.firstElementChild ? sel.firstElementChild.value : '';
    sel.innerHTML = '';
    sel.append(el('option', {value: ''}, 'default model'));
    if (Array.isArray(models)) {
      models.forEach(m => {
        sel.append(el('option', {value: m}, m));
      });
    }
  } catch (e) {
    console.warn('Failed to load models', e);
  }
}

function renderChatMessage(role, text) {
  const box = el('div', {}, el('strong', {}, role+': '), el('span', {}, text));
  const container = document.getElementById('chat_messages');
  container.append(box);
  container.scrollTop = container.scrollHeight;
}

function renderApprovalInChat(item) {
  const elId = `chat_approval_${item.id}`;
  // Avoid rendering duplicate cards
  if (document.getElementById(elId)) return;

  const container = document.getElementById('chat_messages');
  const card = el('div', {class: 'card', id: elId});
  card.append(el('div', {}, el('strong', {}, item.action || 'Approval request')));
  card.append(el('div', {}, `id: ${item.id} status: ${item.status}`));
  if (item.requested_by) card.append(el('div', {}, `requested_by: ${item.requested_by}`));
  if (item.risk) card.append(el('div', {}, `risk: ${item.risk}`));
  if (item.source_query) card.append(el('div', {}, `source: ${item.source_query}`));
  if (item.command) card.append(el('pre', {}, item.command));

  const actions = el('div');
  if (item.status === 'pending') {
    const approveBtn = el('button', {class: 'btn btn-primary'}, 'Approve');
    approveBtn.onclick = () => decide(item.id, 'approved');
    const rejectBtn = el('button', {class: 'btn btn-danger'}, 'Reject');
    rejectBtn.onclick = () => decide(item.id, 'rejected');
    actions.append(approveBtn, rejectBtn);
  }
  if (item.status === 'approved' && item.command) {
    const execBtn = el('button', {class: 'btn btn-secondary'}, 'Execute');
    if (hasUnresolvedPlaceholder(item.command)) {
      execBtn.disabled = true;
      actions.append(el('div', {style: 'color:#a33'}, 'This approved command still contains a placeholder and cannot be executed as-is.'));
    } else {
      execBtn.onclick = () => executeInline(item.id);
    }
    actions.append(execBtn);
  }

  card.append(actions);
  container.append(card);
  container.scrollTop = container.scrollHeight;
}

function updateChatApprovalCard(item) {
  const elId = `chat_approval_${item.id}`;
  const existing = document.getElementById(elId);
  if (!existing) {
    renderApprovalInChat(item);
    return;
  }
  // replace content
  existing.innerHTML = '';
  existing.append(el('div', {}, el('strong', {}, item.action || 'Approval request')));
  existing.append(el('div', {}, `id: ${item.id} status: ${item.status}`));
  if (item.requested_by) existing.append(el('div', {}, `requested_by: ${item.requested_by}`));
  if (item.risk) existing.append(el('div', {}, `risk: ${item.risk}`));
  if (item.source_query) existing.append(el('div', {}, `source: ${item.source_query}`));
  if (item.command) existing.append(el('pre', {}, item.command));

  const actions = el('div');
  if (item.status === 'pending') {
    const approveBtn = el('button', {class: 'btn btn-primary'}, 'Approve');
    approveBtn.onclick = () => decide(item.id, 'approved');
    const rejectBtn = el('button', {class: 'btn btn-danger'}, 'Reject');
    rejectBtn.onclick = () => decide(item.id, 'rejected');
    actions.append(approveBtn, rejectBtn);
  }
  if (item.status === 'approved' && item.command) {
    const execBtn = el('button', {class: 'btn btn-secondary'}, 'Execute');
    if (hasUnresolvedPlaceholder(item.command)) {
      execBtn.disabled = true;
      existing.append(el('div', {style: 'color:#a33'}, 'This approved command still contains a placeholder and cannot be executed as-is.'));
    } else {
      execBtn.onclick = () => executeInline(item.id);
    }
    actions.append(execBtn);
  }
  
  existing.append(actions);
}

function showApprovalPreview(item) {
}

document.addEventListener('DOMContentLoaded', () => {
  const send = document.getElementById('chat_send');
  const input = document.getElementById('chat_input');
  fetchModels();
  send.onclick = async () => {
    const q = input.value.trim();
    if (!q) return;
    renderChatMessage('You', q);
    input.value = '';
    try {
      await streamChatQuery(q);
    } catch (e) {
      // remove thinking and show error
      const container = document.getElementById('chat_messages');
      container.removeChild(container.lastChild);
      renderChatMessage('Assistant', 'Error: '+e.message);
    }
  };
});

async function createApprovalFromAction(action, source_query, autoApprove = false) {
  const payload = {
    action: action.action || 'action',
    command: action.command || null,
    target: action.target || null,
    risk: action.risk || 'medium',
    source_query: source_query,
    requested_by: 'web-ui',
  };
  try {
    const created = await api('/approvals', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    await fetchApprovals();
    renderApprovalInChat(created);
    if (autoApprove) {
      try {
        const updated = await api(`/approvals/${created.id}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify({decision: 'approved', reviewer: 'web-ui', note: 'approved via chat'})});
        await fetchApprovals();
        updateChatApprovalCard(updated);
      } catch (e) {
        renderChatMessage('System', `Failed to auto-approve: ${e.message}`);
      }
    }
  } catch (e) { renderChatMessage('System', `Failed to create approval: ${e.message}`); }
}

// approvals are rendered inline in chat; legacy list removed

async function decide(id, decision) {
  try {
    const updated = await api(`/approvals/${id}`, {method: 'PATCH', headers: {'Content-Type':'application/json'}, body: JSON.stringify({decision, reviewer: 'web-ui', note: ''})});
    await fetchApprovals();
    updateChatApprovalCard(updated);
  } catch (e) { alert(e.message); }
}

async function execute(id) {
  try {
    const res = await api('/execute', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({approval_id: id})});
    document.getElementById('output').innerHTML = `<pre>${escapeHtml(JSON.stringify(res, null, 2))}</pre>`;
  } catch (e) { document.getElementById('output').innerText = e.message; }
}

async function executeInline(id) {
  try {
    const res = await api('/execute', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({approval_id: id})});
    const card = document.getElementById(`chat_approval_${id}`);
    if (card) {
      const cleanOutput = (res.stdout || '').trim();
      const resultDiv = el('div', {style: 'margin-top:8px'});
      resultDiv.append(el('div', {}, el('strong', {}, `Result (exit code ${res.returncode ?? 'N/A'}):`)));
      resultDiv.append(el('pre', {}, cleanOutput || '(no output)'));
      if (res.stderr) {
        resultDiv.append(el('div', {}, el('strong', {}, 'Stderr:')));
        resultDiv.append(el('pre', {}, res.stderr));
      }
      card.append(resultDiv);
      card.scrollIntoView({behavior:'smooth'});

      // Send execution result back to AI as injected context
      const followupQuery = `[System: The command "${res.command}" was executed on "${res.target || 'host'}". Exit code: ${res.returncode}. Output:\n${cleanOutput}]\n\nPlease briefly summarize what this output means for the user.`;
      try {
        const aiReply = await streamChatQuery(followupQuery);
        // AI response is already rendered by streamChatQuery
      } catch (aiErr) {
        renderChatMessage('Assistant', `Error processing result: ${aiErr.message}`);
      }
    }
    await fetchApprovals();
  } catch (e) {
    const card = document.getElementById(`chat_approval_${id}`);
    if (card) card.append(el('div', {}, `Execution failed: ${e.message}`));
  }
}

function escapeHtml(s) { return s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

fetchApprovals();

// ===========================================================================
// Settings sidebar
// ===========================================================================

const SETTING_FIELDS = [
  'app_env', 'app_host', 'app_port',
  'proxmox_url', 'proxmox_host_ip', 'proxmox_port', 'proxmox_realm',
  'proxmox_user', 'proxmox_token_id', 'proxmox_token_secret', 'proxmox_verify_ssl',
  'ollama_url', 'ollama_model',
  'qdrant_url', 'qdrant_api_key', 'qdrant_current_collection_name', 'qdrant_history_collection_name',
  'loki_url', 'prometheus_url',
  'approval_db_path',
];

function settingsEl(id) {
  return document.getElementById('cfg_' + id);
}

async function loadSettings() {
  try {
    const data = await api('/settings');
    for (const field of SETTING_FIELDS) {
      const el = settingsEl(field);
      if (!el) continue;
      const val = data[field];
      if (val === null || val === undefined) continue;
      if (el.type === 'checkbox') {
        el.checked = !!val;
      } else {
        el.value = String(val);
      }
    }
  } catch (e) {
    console.warn('Failed to load settings', e);
    showSettingsMsg('Failed to load settings: ' + e.message, 'err');
  }
}

async function saveSettings() {
  const payload = {};
  for (const field of SETTING_FIELDS) {
    const el = settingsEl(field);
    if (!el) continue;
    if (el.type === 'checkbox') {
      payload[field] = el.checked;
    } else if (el.type === 'number') {
      const num = parseInt(el.value, 10);
      if (!isNaN(num)) payload[field] = num;
    } else {
      if (el.value.trim() !== '') payload[field] = el.value.trim();
    }
  }

  try {
    const res = await api('/settings', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    showSettingsMsg(res.message || 'Settings saved.', 'ok');
  } catch (e) {
    showSettingsMsg('Save failed: ' + e.message, 'err');
  }
}

function showSettingsMsg(text, type) {
  const msgEl = document.getElementById('settings_msg');
  if (!msgEl) return;
  msgEl.textContent = text;
  msgEl.className = 'sidebar-msg ' + (type === 'ok' ? 'ok' : type === 'err' ? 'err' : '');
}

function resetSettingsForm() {
  loadSettings();
  showSettingsMsg('Form reset to current values.', '');
}

// ===========================================================================
// Tab switching
// ===========================================================================
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    const tabId = btn.getAttribute('data-tab');
    const tabEl = document.getElementById(tabId);
    if (tabEl) tabEl.classList.add('active');
  });
});

// ===========================================================================
// Sidebar open/close
// ===========================================================================
const sidebar = document.getElementById('settings_sidebar');
const toggleBtn = document.getElementById('settings_toggle');
const closeBtn = document.getElementById('settings_close');

function openSidebar() {
  sidebar.classList.remove('collapsed');
  document.body.classList.remove('sidebar-collapsed');
}

function closeSidebar() {
  sidebar.classList.add('collapsed');
  document.body.classList.add('sidebar-collapsed');
}

if (toggleBtn) toggleBtn.addEventListener('click', openSidebar);
if (closeBtn) closeBtn.addEventListener('click', closeSidebar);

// Save / Reset buttons
document.getElementById('settings_save').addEventListener('click', saveSettings);
document.getElementById('settings_reset').addEventListener('click', resetSettingsForm);

// ===========================================================================
// Containers tab
// ===========================================================================
function statusClass(status) {
  const s = (status || '').toLowerCase();
  if (s === 'running') return 'running';
  if (s === 'stopped') return 'stopped';
  return 'other';
}

function renderContainerItem(c) {
  const div = document.createElement('div');
  div.className = 'container-item';
  const statusCls = statusClass(c.status);
  div.innerHTML = `
    <div class="c-name">${escapeHtml(c.name || 'unknown')} <span class="c-status ${statusCls}">${escapeHtml(c.status || '?')}</span></div>
    <div class="c-meta">
      <span>ID: ${c.vmid ?? '—'}</span>
      <span>Type: ${escapeHtml(c.type || '—')}</span>
      <span>Node: ${escapeHtml(c.node || '—')}</span>
      ${c.ip ? `<span>IP: ${escapeHtml(c.ip)}</span>` : ''}
      ${c.hostname ? `<span>Host: ${escapeHtml(c.hostname)}</span>` : ''}
    </div>
  `;
  return div;
}

async function loadContainers() {
  const listEl = document.getElementById('container_list');
  const countEl = document.getElementById('container_count');
  listEl.innerHTML = '<div style="color:#888;font-size:12px">Loading…</div>';
  try {
    const containers = await api('/containers');
    if (!Array.isArray(containers) || containers.length === 0) {
      listEl.innerHTML = '<div style="color:#888;font-size:12px">No containers found. Try scanning.</div>';
      countEl.textContent = '';
      return;
    }
    countEl.textContent = `${containers.length} container${containers.length !== 1 ? 's' : ''} discovered`;
    listEl.innerHTML = '';
    containers.forEach(c => listEl.appendChild(renderContainerItem(c)));
  } catch (e) {
    listEl.innerHTML = `<div style="color:#f44336;font-size:12px">Error: ${escapeHtml(e.message)}</div>`;
    countEl.textContent = '';
  }
}

async function scanContainers() {
  const btn = document.getElementById('container_scan_btn');
  const listEl = document.getElementById('container_list');
  const countEl = document.getElementById('container_count');
  btn.disabled = true;
  btn.textContent = '⏳ Scanning…';
  listEl.innerHTML = '<div style="color:#888;font-size:12px">Scanning Proxmox…</div>';
  countEl.textContent = '';
  try {
    const result = await api('/scan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (result && result.containers && result.containers.length > 0) {
      countEl.textContent = `${result.containers.length} container${result.containers.length !== 1 ? 's' : ''} found · scanned ${result.scanned_nodes || 0} node(s)`;
      listEl.innerHTML = '';
      result.containers.forEach(c => listEl.appendChild(renderContainerItem(c)));
    } else {
      listEl.innerHTML = '<div style="color:#888;font-size:12px">No containers returned from scan.</div>';
      countEl.textContent = '';
    }
  } catch (e) {
    listEl.innerHTML = `<div style="color:#f44336;font-size:12px">Scan failed: ${escapeHtml(e.message)}</div>`;
    countEl.textContent = '';
  } finally {
    btn.disabled = false;
    btn.textContent = '🔄 Scan Now';
  }
}

document.getElementById('container_scan_btn').addEventListener('click', scanContainers);

// Auto-load containers when switching to that tab
document.querySelector('[data-tab="tab_containers"]').addEventListener('click', () => {
  loadContainers();
});

// ===========================================================================
// Init
// ===========================================================================
loadSettings();
