/* =========================================================================
 * app.js — Web Simulator for the sample repo
 *         "Zalo Restaurant Bot — a restaurant bot that remembers its guests"
 *
 * Simulates the Zalo OA chat UI so developers can test the agent without a real Zalo account.
 * - Plain vanilla JS, no framework, no build step.
 * - The Python SDK backend serves these files statically at GET / (same origin),
 *   so every fetch() uses a relative URL and CORS is not an issue.
 *
 * API contract (same origin):
 *   POST /invocations        send a message to the agent
 *   GET  /api/info           agent info (model, memory, zalo_configured…)
 *   GET  /api/memory?actor=  guest profile from the memory strategy
 *   GET  /api/history?actor=&session=  conversation history (oldest first)
 *   GET  /api/actors         guests and their sessions
 *   GET  /api/bookings       current bookings
 * ========================================================================= */
'use strict';

/* ----- Endpoint constants (exactly as the backend serves them) ----- */
const API = {
  INVOCATIONS: '/invocations',
  INFO:        '/api/info',
  MEMORY:      '/api/memory',
  HISTORY:     '/api/history',
  ACTORS:      '/api/actors',
  BOOKINGS:    '/api/bookings',
};

/* CSS class for each booking status (coloured badge) */
const STATUS_CLASS = { CONFIRMED: 'st-confirmed', CANCELLED: 'st-cancelled' };

/* ----- Global simulator state ----- */
const state = {
  currentActor: null,   // selected actorId (this is the Zalo user id)
  currentSession: null, // session id in use for that actor
  actors: [],           // cached /api/actors result
  sending: false,       // blocks a second send while the bot is answering
  timerId: null,        // interval counting the wait for the bot's answer
  pendingStart: 0,      // when the wait started
};

/* ----- Short DOM helper ----- */
const $ = (id) => document.getElementById(id);

/* escapeHtml: always escape API/user content before putting it into innerHTML */
function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* renderMarkdown: minimal **bold**, `code`, [link](url) on an ALREADY escaped string.
   Only http/https links are accepted, to avoid an injected javascript: URI. */
function renderMarkdown(raw) {
  let html = escapeHtml(raw);
  html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  html = html.replace(/^\s*[-*]\s+(.*)$/gm, '&bull; $1'); // dash list item -> bullet
  return html.replace(/\n/g, '<br>');
}

/* relativeTime: "vừa xong" (just now), "5 phút trước" (5 minutes ago)… from an ISO createdAt string */
function relativeTime(iso) {
  if (!iso) return '';
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) return String(iso);
  const diff = Date.now() - time;
  if (diff < 60_000) return 'vừa xong';
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 60) return `${minutes} phút trước`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} giờ trước`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} ngày trước`;
  return new Date(iso).toLocaleDateString('vi-VN');
}

/* ===== Error toast: red strip on top, hides after 8s or on the ✕ button ===== */
let toastTimer = null;

function showToast(message) {
  $('toast-message').textContent = message;
  $('toast-strip').classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, 8000);
}

function hideToast() {
  $('toast-strip').classList.add('hidden');
  clearTimeout(toastTimer);
}

/* API key (when the backend sets AGENT_API_KEY): kept in localStorage, attached to every request */
const KEY_STORAGE = 'zalo_bot_api_key';
function authHeaders(extra = {}) {
  const h = Object.assign({}, extra);
  let k = '';
  try { k = localStorage.getItem(KEY_STORAGE) || ''; } catch (e) { /* private mode */ }
  if (k) h['X-API-Key'] = k;
  return h;
}

/* fetchJson: fetch wrapper that throws readable (Vietnamese) errors for the toast */
async function fetchJson(url, options = {}) {
  options.headers = authHeaders(options.headers || {});
  let res;
  try {
    res = await fetch(url, options);
  } catch (err) {
    throw new Error(`Không gọi được ${url} — backend đã chạy chưa?`);
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body or not JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `Lỗi HTTP ${res.status} từ ${url}`);
  return data;
}

/* ===== Chat pane: render bubbles ===== */
function scrollToBottom() {
  const box = $('chat-messages');
  box.scrollTop = box.scrollHeight;
}

/* appendMessage: add one bubble to the chat area.
   role: 'user' | 'assistant'. memories: array of facts the bot just used (if any). */
function appendMessage(role, text, memories) {
  const wrap = document.createElement('div');
  wrap.className = role === 'user' ? 'msg msg-user' : 'msg msg-bot';

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = renderMarkdown(text);
  wrap.appendChild(bubble);

  // "✨ Bot nhớ: ..." (the bot remembers) callout right under the bot bubble when memories_used is set
  if (role !== 'user' && Array.isArray(memories) && memories.length > 0) {
    const note = document.createElement('div');
    note.className = 'memory-callout';
    note.innerHTML = `✨ Bot nhớ: ${memories.map((m) => `<em>${escapeHtml(m)}</em>`).join(', ')}`;
    wrap.appendChild(note);
  }

  $('chat-messages').appendChild(wrap);
  scrollToBottom();
}

/* "Bot is typing..." indicator: 3 blinking dots + a running seconds counter */
function showTyping() {
  const el = document.createElement('div');
  el.className = 'msg msg-bot typing-msg';
  el.id = 'typing-bubble'; // dynamic ID: this element creates itself and looks itself up again
  el.innerHTML = `
    <div class="bubble typing">
      <span class="typing-dots"><i></i><i></i><i></i></span>
      <span class="typing-timer">0.0s</span>
    </div>`;
  $('chat-messages').appendChild(el);
  scrollToBottom();

  state.pendingStart = Date.now();
  state.timerId = setInterval(() => {
    const seconds = (Date.now() - state.pendingStart) / 1000;
    const label = document.querySelector('#typing-bubble .typing-timer');
    if (label) label.textContent = `${seconds.toFixed(1)}s`;
  }, 100);
}

function hideTyping() {
  clearInterval(state.timerId);
  state.timerId = null;
  const el = $('typing-bubble');
  if (el) el.remove();
}

/* ===== Load data from the backend ===== */

/* loadAgentInfo: GET /api/info -> status dot + info line in the sidebar */
async function loadAgentInfo() {
  try {
    const info = await fetchJson(API.INFO);
    const configured = info && info.zalo_configured === true;
    $('agent-status').textContent = configured
      ? 'Đang hoạt động'
      : 'Chế độ giả lập · OA chưa cấu hình';
    $('status-dot').className = configured ? 'status-dot' : 'status-dot warn';
    $('info-line').innerHTML =
      `🤖 <strong>${escapeHtml(info.agent || 'agent')}</strong>` +
      ` · model <code>${escapeHtml(info.llm_model || '—')}</code>` +
      `<br>memory: <code>${escapeHtml(info.memory_id || '—')}</code>`;
  } catch (err) {
    showToast(err.message);
  }
}

/* loadActors: GET /api/actors -> guest list; selects the first guest the first time the page opens */
async function loadActors() {
  try {
    const data = await fetchJson(API.ACTORS);
    state.actors = (data && data.actors) || [];
    renderActorList();
    // First page load (nobody selected yet) -> pick the first guest so a conversation is ready
    if (!state.currentActor && state.actors.length > 0) {
      const first = state.actors[0];
      selectActor(first.actorId, (first.sessions || [])[0] || null);
    }
  } catch (err) {
    showToast(err.message);
  }
}

/* renderActorList: draw the guest list in the left column */
function renderActorList() {
  const list = $('actor-list');
  list.innerHTML = '';

  if (!state.actors.length) {
    list.innerHTML = '<p class="empty-note">Chưa có khách nào.<br>Thêm khách giả lập bên dưới 👇</p>';
    return;
  }

  for (const actor of state.actors) {
    const sessions = actor.sessions || [];
    const isActive = actor.actorId === state.currentActor;

    const item = document.createElement('button');
    item.type = 'button';
    item.className = isActive ? 'actor-item active' : 'actor-item';
    item.title = `actorId: ${actor.actorId}`;
    item.innerHTML = `
      <span class="avatar">${escapeHtml(avatarLabel(actor.actorId))}</span>
      <span class="actor-meta">
        <span class="actor-id">${escapeHtml(actor.actorId)}</span>
        <span class="actor-sub">${sessions.length} session</span>
      </span>`;
    // Select the guest: reuse an existing session if there is one, else start a new one
    item.addEventListener('click', () => selectActor(actor.actorId, sessions[0] || null));
    list.appendChild(item);
  }
}

/* avatarLabel: for a phone-number-like id, use the last 2 digits as the avatar "initials" */
function avatarLabel(actorId) {
  const str = String(actorId || '');
  return str.slice(-2) || '?';
}

/* newSessionId: a fresh session for a simulated guest (the backend records it at the first message) */
function newSessionId() {
  return `web-${Date.now()}`;
}

/* selectActor: switch the active guest -> reload history + memory profile */
function selectActor(actorId, sessionId) {
  state.currentActor = actorId;
  state.currentSession = sessionId || newSessionId();
  renderActorList();
  $('chat-messages').innerHTML = '';
  loadHistory();
  loadMemory();
}

/* loadHistory: GET /api/history?actor=&session= -> redraw every bubble (oldest first) */
async function loadHistory() {
  const box = $('chat-messages');
  box.innerHTML = '<p class="history-loading">Đang tải lịch sử hội thoại…</p>';
  try {
    const url = `${API.HISTORY}?actor=${encodeURIComponent(state.currentActor)}` +
      `&session=${encodeURIComponent(state.currentSession)}`;
    const data = await fetchJson(url);
    const events = (data && data.events) || [];

    box.innerHTML = '';
    if (!events.length) {
      // No message yet -> show a friendly welcome block
      box.innerHTML = `
        <div class="welcome">
          <p>👋 Xin chào <strong>${escapeHtml(state.currentActor)}</strong>!</p>
          <p>Đây là Zalo OA giả lập của <strong>Quán Ngon 123</strong>.<br>
             Hãy nhắn tin để đặt bàn, hoặc kể sở thích để bot ghi nhớ bạn.</p>
        </div>`;
      return;
    }
    for (const ev of events) {
      appendMessage(ev.role === 'user' ? 'user' : 'assistant', ev.message || '');
    }
  } catch (err) {
    box.innerHTML = '';
    showToast(err.message);
  }
}

/* loadMemory: GET /api/memory?actor= -> grouped by memory strategy, one card per record */
async function loadMemory() {
  if (!state.currentActor) return;
  try {
    const url = `${API.MEMORY}?actor=${encodeURIComponent(state.currentActor)}`;
    const data = await fetchJson(url);
    renderMemory((data && data.groups) || []);
  } catch (err) {
    showToast(err.message);
  }
}

function renderMemory(groups) {
  const panel = $('memory-panel');
  panel.innerHTML = '';

  if (!groups.length) {
    panel.innerHTML = '<p class="empty-note">Chưa có hồ sơ nào được ghi nhớ.<br>' +
      'Hãy trò chuyện — bot sẽ tự học sở thích của khách.</p>';
    return;
  }

  for (const group of groups) {
    const records = group.records || [];
    const block = document.createElement('div');
    block.className = 'memory-group';
    block.innerHTML = `
      <div class="memory-group-head">
        <span class="strategy-name">${escapeHtml(group.strategy || 'unknown')}</span>
        <span class="strategy-id">${escapeHtml(group.strategy_id || '')}</span>
      </div>`;

    if (!records.length) {
      block.insertAdjacentHTML('beforeend', '<p class="empty-note">Chưa có bản ghi nào trong nhóm này.</p>');
    }
    for (const record of records) {
      const card = document.createElement('div');
      card.className = 'memory-card';
      card.innerHTML = `
        <p class="memory-text">${escapeHtml(record.memory || '')}</p>
        <span class="memory-time">${escapeHtml(relativeTime(record.createdAt))}</span>`;
      block.appendChild(card);
    }
    panel.appendChild(block);
  }
}

/* loadBookings: GET /api/bookings -> bookings table in the right column */
async function loadBookings() {
  try {
    const data = await fetchJson(API.BOOKINGS);
    renderBookings((data && data.bookings) || []);
  } catch (err) {
    showToast(err.message);
  }
}

function renderBookings(bookings) {
  const tbody = $('bookings-table-body');
  tbody.innerHTML = '';

  if (!bookings.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-note">Chưa có lượt đặt bàn nào.</td></tr>';
    return;
  }

  for (const booking of bookings) {
    const statusClass = STATUS_CLASS[booking.status] || 'st-other';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${escapeHtml(booking.date || '—')}</td>
      <td>${escapeHtml(booking.time || '—')}</td>
      <td title="${escapeHtml(booking.notes || '')}">${escapeHtml(booking.customer || '—')}</td>
      <td>${escapeHtml(booking.party_size ?? '—')}</td>
      <td>${escapeHtml(booking.table || '—')}</td>
      <td><span class="status-badge ${statusClass}">${escapeHtml(booking.status || '?')}</span></td>`;
    tbody.appendChild(tr);
  }
}

/* ===== Send a message: POST /invocations with the User-Id / Session-Id headers ===== */
async function submitMessage() {
  const input = $('composer-input');
  const text = input.value.trim();

  if (!text) return;
  if (!state.currentActor) {
    showToast('Hãy chọn hoặc thêm một khách hàng trước khi nhắn tin.');
    return;
  }
  if (state.sending) return; // still waiting for the previous answer

  state.sending = true;
  $('send-btn').disabled = true;

  appendMessage('user', text);
  input.value = '';
  autoResizeTextarea();
  showTyping();

  try {
    const data = await fetchJson(API.INVOCATIONS, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        // Exactly the backend contract: the guest identity (Zalo user id) and the chat session
        'X-GreenNode-AgentBase-User-Id': state.currentActor,
        'X-GreenNode-AgentBase-Session-Id': state.currentSession,
      },
      body: JSON.stringify({ message: text }),
    });

    if (!data || data.status !== 'success') {
      throw new Error((data && data.error) || 'Agent trả về trạng thái không hợp lệ.');
    }

    hideTyping();
    appendMessage('assistant', data.response || '(Bot trả về nội dung rỗng)', data.memories_used);

    // Refresh after every bot answer: memory may have just been extracted, a booking may have
    // just been created, a new session may have appeared
    loadMemory();
    loadBookings();
    loadActors();
  } catch (err) {
    hideTyping();
    appendMessage('assistant', `⚠️ **Lỗi:** ${err.message}`);
    showToast(err.message);
  } finally {
    state.sending = false;
    $('send-btn').disabled = false;
    $('composer-input').focus();
  }
}

/* ===== Add a new simulated guest ===== */

/* normalizeActorId: normalise a phone number into an actorId.
   - Drop every non-digit character (including +).
   - A domestic number starting with 0 (e.g. 0901234567) gets the 0 replaced by 84 -> 84901234567.
   - Already international (84901…) or any other form -> kept as is. */
function normalizeActorId(raw) {
  const digits = String(raw || '').replace(/\D/g, '');
  if (!digits) return null;
  if (digits.startsWith('0')) return '84' + digits.slice(1);
  return digits;
}

function submitNewCustomer(event) {
  event.preventDefault();
  const input = $('new-customer-input');
  const actorId = normalizeActorId(input.value);

  if (!actorId || actorId.length < 9) {
    showToast('ActorId phải là số điện thoại hợp lệ (ít nhất 9 chữ số).');
    return;
  }

  // Guest already in the list -> just select again, do not add a duplicate
  const existing = state.actors.find((a) => a.actorId === actorId);
  if (existing) {
    selectActor(actorId, (existing.sessions || [])[0] || null);
    input.value = '';
    return;
  }

  // Add to the local list for now (the backend learns about this actor after the first message)
  state.actors.unshift({ actorId, sessions: [] });
  input.value = '';
  selectActor(actorId, null); // new guest -> a completely new session
}

/* ===== Wire up events & start ===== */
function autoResizeTextarea() {
  const ta = $('composer-input');
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 120) + 'px';
}

function bindEvents() {
  // Send a message: form submit or the Enter key (Shift+Enter still inserts a line break)
  $('composer').addEventListener('submit', (e) => { e.preventDefault(); submitMessage(); });
  $('composer-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submitMessage();
    }
  });
  $('composer-input').addEventListener('input', autoResizeTextarea);

  // Suggestion chips: only fill the composer, the user presses Send themselves
  document.querySelectorAll('#suggestion-chips .chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const input = $('composer-input');
      input.value = chip.textContent.trim();
      autoResizeTextarea();
      input.focus();
    });
  });

  // New simulated guest form + bookings refresh button + toast close button
  $('new-customer-form').addEventListener('submit', submitNewCustomer);
  $('bookings-refresh').addEventListener('click', loadBookings);
  $('toast-close').addEventListener('click', hideToast);
}

/* init: load agent info, guest list and bookings table in parallel */
async function init() {
  bindEvents();
  await Promise.allSettled([loadAgentInfo(), loadActors(), loadBookings()]);
}

document.addEventListener('DOMContentLoaded', init);
