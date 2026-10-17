/* Book-Keeper web app. Plain JavaScript, no build step.
   Being logged in is an httpOnly session cookie: the page never sees it, the browser just
   sends it. A 401 means the session ended, so the sign-in screen comes back. */

const $ = (id) => document.getElementById(id);
const naira = (n) => '₦' + Number(n || 0).toLocaleString('en-NG', { maximumFractionDigits: 0 });
const noKobo = (text) => String(text).replace(/(₦[\d,]+)\.00/g, '$1');  // "₦36,000.00" → "₦36,000"
const SUGGESTIONS = [
  'How much I make this week?', 'Wetin I spend this month?', 'Who owe me?', 'Who I owe?',
  'I sold 20 bags of rice to Ahmed for 900k, he paid 600k', 'Paid 45k for fuel today',
  'Bola come, I give am the usual', "Chidi's mum take 1 bag rice, 45k, she go pay Monday",
  'who I suppose chase?', 'Cancel that',
];
let me = null;
let whatsappInfo = { enabled: false };
let challengeRun = 0;     // bumping this stops an older WhatsApp code's polling
let period = 'month';
let debtSide = 'owed_to_you';
let debtData = null;
let peopleData = { people: [], memory: 'off' };

// ── helpers ──────────────────────────────────────────────────────────────────
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
/* The assistant writes WhatsApp-style text: **bold** or *bold*, "- " bullets, line breaks.
   Escape first, then add only those few tags, so nothing in a reply can inject HTML. */
function rich(text) {
  const lines = esc(text).split('\n');
  let html = '', list = false;
  for (const raw of lines) {
    const line = raw.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/(^|\W)\*(\S[^*]*?)\*(?=\W|$)/g, '$1<strong>$2</strong>');
    const bullet = line.match(/^\s*(?:[-•]|\d+\.)\s+(.*)$/);
    if (bullet) { if (!list) { html += '<ul>'; list = true; } html += `<li>${bullet[1]}</li>`; continue; }
    if (list) { html += '</ul>'; list = false; }
    html += line.trim() ? `<p>${line}</p>` : '';
  }
  return html + (list ? '</ul>' : '');
}
function errorText(detail, fallback) {
  if (Array.isArray(detail)) return detail.map(d => d.msg.replace(/^Value error, /, '')).join(' ');
  return detail || fallback;
}
function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, 4500);
}
function shortDate(iso) {
  const d = new Date(iso.length === 10 ? iso + 'T12:00:00' : iso);
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
}
function timeOf(iso) { return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
function localPhone(e164) { const d = '0' + e164.replace(/^\+234/, ''); return d.slice(0, 4) + ' ' + d.slice(4, 7) + ' ' + d.slice(7); }

async function api(path, options = {}) {
  const res = await fetch(path, { ...options, credentials: 'include' });
  if (res.status === 401) { showAuth('Your session has ended. Please log in again.'); throw new Error('signed out'); }
  return res;
}
async function getJSON(path) {
  const res = await api(path);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

// ── sign in ──────────────────────────────────────────────────────────────────
function showAuth(message = '') {
  $('auth-error').textContent = message;
  $('shell').hidden = true; $('auth').hidden = false;
}
function showAuthTab(which) {
  $('login-panel').hidden = which !== 'login';
  $('register-form').hidden = which !== 'register';
  $('tab-login').classList.toggle('active', which === 'login');
  $('tab-register').classList.toggle('active', which === 'register');
  $('auth-error').textContent = '';
}
async function submitAuth(event, path, body) {
  event.preventDefault();
  const button = event.target.querySelector('.auth-submit');
  button.disabled = true; $('auth-error').textContent = '';
  try {
    const res = await fetch(path, { method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { $('auth-error').textContent = errorText(data.detail, 'Something went wrong. Please try again.'); return; }
    await enterApp();
  } catch { $('auth-error').textContent = 'Could not reach the server.'; }
  finally { button.disabled = false; }
}
function submitLogin(e) {
  return submitAuth(e, '/login', { email: $('login-email').value.trim(), password: $('login-password').value });
}
function submitRegister(e) {
  return submitAuth(e, '/register', {
    firstname: $('reg-firstname').value.trim(), lastname: $('reg-lastname').value.trim(),
    business_name: $('reg-business').value.trim(), email: $('reg-email').value.trim(), password: $('reg-password').value,
  });
}

async function loadDemoButtons() {
  let info;
  try { info = await (await fetch('/demo/info')).json(); } catch { return; }
  if (!info.enabled || !info.accounts.length) return;
  const box = $('demo-quick-buttons'); box.replaceChildren();
  for (const a of info.accounts) {
    const b = el('button', a.id === 'day30' ? 'btn btn-primary' : 'btn btn-ghost');
    b.type = 'button';
    b.append(el('span', '', a.label), el('small', '', a.about));
    b.onclick = async () => {
      b.disabled = true;
      const res = await fetch('/demo/login', { method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ account: a.id }) });
      if (res.ok) await enterApp(); else { $('auth-error').textContent = 'Could not open the demo.'; b.disabled = false; }
    };
    box.append(b);
  }
  $('demo-quick').hidden = false;
}

async function loadWhatsAppInfo() {
  try { const res = await fetch('/auth/whatsapp/info'); if (res.ok) whatsappInfo = await res.json(); } catch { /* stays hidden */ }
  document.querySelectorAll('.wa-only').forEach(n => { n.hidden = !whatsappInfo.enabled; });
  if (whatsappInfo.signup_link) $('wa-signup-link').href = whatsappInfo.signup_link;
  $('email-login').open = !whatsappInfo.enabled;  // without WhatsApp, email is the only way in
}

// Start a WhatsApp code challenge and show it in `panel`; poll until approved, refused or expired.
async function runChallenge(panel, form, startPath, phone, useAuth, onApproved) {
  const run = ++challengeRun;
  const start = useAuth ? api : (p, o) => fetch(p, { credentials: 'include', ...o });
  const res = await start(startPath, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ phone }) });
  const data = await res.json().catch(() => ({}));
  panel.replaceChildren(); panel.hidden = false;
  const again = () => { panel.hidden = true; form.hidden = false; };
  if (!res.ok) {
    panel.append(el('div', 'problem', errorText(data.detail, 'Something went wrong. Please try again.')));
    const retry = el('button', 'linkish', 'Try again'); retry.type = 'button'; retry.onclick = again; panel.append(retry);
    return;
  }
  form.hidden = true;
  panel.append(el('div', '', 'Send this message to Book-Keeper on WhatsApp, from ' + phone + ':'), el('div', 'code-box', data.message));
  if (data.whatsapp_link) { const a = el('a', 'wa-button', 'Open WhatsApp'); a.href = data.whatsapp_link; a.target = '_blank'; a.rel = 'noopener'; panel.append(a); }
  const status = el('div', 'waiting', 'Waiting for your message…'); panel.append(status);
  const other = el('button', 'linkish', 'Use a different number'); other.type = 'button';
  other.onclick = () => { challengeRun++; again(); }; panel.append(other);
  const deadline = Date.now() + (data.expires_in || 600) * 1000;
  while (run === challengeRun && Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 2000));
    if (run !== challengeRun) return;
    const poll = await fetch('/auth/whatsapp/challenges/' + encodeURIComponent(data.challenge_id),
                             { method: 'POST', credentials: 'include' }).catch(() => null);
    if (!poll || !poll.ok) continue;
    const result = await poll.json();
    if (result.status === 'approved') { panel.hidden = true; form.hidden = false; await onApproved(result); return; }
    if (result.status === 'rejected' || result.status === 'expired') {
      status.className = 'problem'; status.textContent = result.message || 'That code has expired. Please get a new one.'; return;
    }
  }
  if (run === challengeRun) { status.className = 'problem'; status.textContent = 'That code has expired. Please get a new one.'; }
}
function submitWhatsAppLogin(e) {
  e.preventDefault(); $('auth-error').textContent = '';
  return runChallenge($('wa-login-challenge'), $('wa-login-form'), '/auth/whatsapp/login', $('wa-login-phone').value.trim(), false, enterApp);
}
function submitConnectWhatsApp(e) {
  e.preventDefault();
  return runChallenge($('connect-wa-challenge'), $('connect-wa-form'), '/auth/whatsapp/link', $('connect-wa-phone').value.trim(), true, enterApp);
}

async function enterApp() {
  let res;
  try { res = await fetch('/api/v1/me', { credentials: 'include' }); } catch { showAuth('Could not reach the server.'); return; }
  if (!res.ok) { showAuth(); return; }
  me = await res.json();
  const name = (me.firstname + ' ' + (me.lastname || '')).trim();
  $('account-btn').textContent = (me.firstname || '?')[0].toUpperCase();
  $('account-name').textContent = name;
  $('account-email').textContent = me.email || '';
  $('account-whatsapp').textContent = me.phone ? 'WhatsApp: ' + localPhone(me.phone) + ' ✓' : '';
  $('connect-wa-form').hidden = !whatsappInfo.enabled || !!me.phone;
  $('biz-name').textContent = me.business_name || '';
  document.title = (me.business_name ? me.business_name + ' · ' : '') + 'Book-Keeper';
  $('auth').hidden = true; $('shell').hidden = false;
  showView(location.hash.replace('#', '') || 'books');  // the books first; chat history loads behind it
  loadHistory();
  loadMemoryStatus();
}
async function logout() {
  await fetch('/logout', { method: 'POST', credentials: 'include' }).catch(() => {});
  me = null; $('account-menu').hidden = true; $('messages').replaceChildren();
  showAuthTab('login'); showAuth(); loadDemoButtons();
}

// ── navigation ───────────────────────────────────────────────────────────────
const LOADERS = { books: loadOverview, debts: loadDebts, chase: loadChase, people: loadPeople };
function showView(name) {
  if (!document.querySelector(`.navbtn[data-view="${name}"]`)) name = 'books';
  document.querySelectorAll('.navbtn').forEach(b => b.classList.toggle('active', b.dataset.view === name));
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
  history.replaceState(null, '', name === 'books' ? location.pathname : '#' + name);
  if (LOADERS[name]) LOADERS[name]();
  if (name === 'chat') $('msg-input').focus();
}
document.querySelectorAll('.navbtn').forEach(b => b.addEventListener('click', () => showView(b.dataset.view)));
$('account-btn').addEventListener('click', (e) => {
  e.stopPropagation(); const m = $('account-menu'); m.hidden = !m.hidden; $('account-btn').setAttribute('aria-expanded', !m.hidden);
});
document.addEventListener('click', (e) => { if (!$('account-menu').contains(e.target)) $('account-menu').hidden = true; });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

async function loadMemoryStatus() {
  const pill = $('memory-pill');
  try {
    const h = await (await fetch('/health')).json();
    const on = h.graph === 'ok';
    pill.className = 'pill ' + (on ? '' : h.graph === 'off' ? 'grey' : 'amber');
    pill.lastChild.textContent = on ? ' Memory on' : h.graph === 'off' ? ' Memory off' : ' Memory unavailable';
    pill.title = on ? 'Graph memory (FalkorDB) is connected' : 'Bookkeeping works; memory features are paused';
  } catch { /* leave as is */ }
}

// ── chat ─────────────────────────────────────────────────────────────────────
function welcome() {
  const box = el('div', 'welcome');
  box.innerHTML = `<div class="brand-mark"><svg><use href="#i-logo"/></svg></div>
    <h2>Wetin happen for shop today?</h2>
    <p>Tell me what you sold, bought, borrowed or collected, in English or Pidgin. Ask who owes you, who to chase,
       or how the business is doing.</p>`;
  return box;
}
function addRow(role, text, at, isError = false, into = null) {
  const box = into || $('messages');
  const w = $('messages').querySelector('.welcome'); if (w) w.remove();
  const row = el('div', 'msgrow ' + role + (isError ? ' error' : ''));
  const who = el('div', 'who');
  if (role === 'ai') who.innerHTML = '<svg><use href="#i-logo"/></svg>'; else who.textContent = (me?.firstname || 'Y')[0].toUpperCase();
  const wrap = el('div', 'bubble-wrap');
  const bubble = el('div', 'bubble rich');
  if (role === 'ai') bubble.innerHTML = rich(text); else bubble.textContent = text;
  wrap.append(bubble, el('div', 'meta', timeOf(at || new Date().toISOString())));
  row.append(who, wrap); box.append(row);
  if (!into) box.scrollTop = box.scrollHeight;
  return row;
}
function addTyping() {
  const row = el('div', 'msgrow ai'); row.id = 'typing';
  row.innerHTML = '<div class="who"><svg><use href="#i-logo"/></svg></div><div class="bubble-wrap"><div class="bubble"><div class="dots"><span></span><span></span><span></span></div></div></div>';
  $('messages').append(row); $('messages').scrollTop = $('messages').scrollHeight;
}
function removeTyping() { const t = $('typing'); if (t) t.remove(); }

async function loadHistory() {
  // Clear now, then put the history *above* anything sent while it was loading, so a quick
  // first message isn't wiped when the history arrives.
  const box = $('messages'); box.replaceChildren();
  let turns = [];
  try { turns = (await getJSON('/api/v1/history')).turns; } catch { /* start fresh */ }
  if (!turns.length) { if (!box.children.length) box.append(welcome()); return; }
  const older = el('div');
  let lastDay = '';
  for (const t of turns) {
    const day = new Date(t.at).toDateString();
    if (day !== lastDay) { older.append(el('div', 'day-sep', new Date(t.at).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' }))); lastDay = day; }
    addRow(t.role === 'user' ? 'user' : 'ai', t.content, t.at, false, older);
  }
  box.prepend(...older.childNodes);
  box.scrollTop = box.scrollHeight;
}
function renderSuggestions() {
  const box = $('suggestions');
  for (const s of SUGGESTIONS) {
    const c = el('button', 'chip', s); c.type = 'button';
    c.onclick = () => { const i = $('msg-input'); i.value = s; autoResize(i); i.focus(); };
    box.append(c);
  }
}
function autoResize(t) { t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 140) + 'px'; }
function handleKey(e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }
function uid() { return (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2)); }

async function sendMessage(textOverride) {
  const input = $('msg-input');
  const text = (textOverride ?? input.value).trim();
  if (!text) return;
  if (!$('view-chat').classList.contains('active')) showView('chat');
  $('send-btn').disabled = true; input.value = ''; autoResize(input);
  addRow('user', text); addTyping();
  try {
    const res = await api('/api/v1/message', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': uid() },
      body: JSON.stringify({ message: text }), signal: AbortSignal.timeout(120000),
    });
    removeTyping();
    const data = await res.json().catch(() => ({}));
    if (!res.ok) addRow('ai', errorText(data.detail, 'Something went wrong. Please try again.'), null, true);
    else {
      addRow('ai', data.reply);
      (data.clarifications || []).forEach(addClarify);
      debtData = null;  // the books may have changed: reload them next time they're opened
    }
  } catch (err) {
    removeTyping();
    if (err.message !== 'signed out') addRow('ai', err.name === 'TimeoutError' ? 'That took too long. Please try again.' : 'Could not reach the server.', null, true);
  }
  $('send-btn').disabled = false; input.focus();
}
function addClarify(c) {
  const row = el('div', 'msgrow ai');
  row.innerHTML = '<div class="who"><svg><use href="#i-logo"/></svg></div>';
  const card = el('div', 'clarify');
  card.append(el('b', '', 'Amount needed'), el('div', '', c.message));
  const line = el('div', 'clarify-row');
  const inp = el('input', 'input'); inp.type = 'number'; inp.min = '0'; inp.step = '0.01'; inp.placeholder = 'Amount in ₦';
  const btn = el('button', 'btn btn-primary btn-sm', 'Save');
  btn.onclick = async () => {
    const amount = parseFloat(inp.value);
    if (!(amount > 0)) { toast('Please enter an amount.'); return; }
    btn.disabled = true;
    try {
      const res = await api('/api/v1/clarify', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ transaction_id: c.transaction_id, amount }) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { toast(errorText(data.detail, 'Could not save that.')); btn.disabled = false; return; }
      row.remove(); addRow('ai', data.reply);
    } catch { btn.disabled = false; }
  };
  line.append(inp, btn); card.append(line);
  const wrap = el('div', 'bubble-wrap'); wrap.append(card); row.append(wrap);
  $('messages').append(row); $('messages').scrollTop = $('messages').scrollHeight;
}

// ── overview ─────────────────────────────────────────────────────────────────
document.querySelectorAll('#ov-period button').forEach(b => b.addEventListener('click', () => {
  period = b.dataset.period;
  document.querySelectorAll('#ov-period button').forEach(x => x.classList.toggle('active', x === b));
  loadOverview();
}));
const TYPE_LABEL = { sale: 'Sale', expense: 'Expense', loan_taken: 'Loan taken', loan_given: 'Loan given', debt_repayment: 'Repayment' };
function personLink(name) {
  const b = el('button', '', name); b.type = 'button'; b.onclick = () => openPerson(name); return b;
}
function balanceList(target, people, emptyText) {
  target.replaceChildren();
  if (!people.length) { target.append(el('div', 'empty', emptyText)); return; }
  for (const p of people.slice(0, 8)) {
    const li = el('div', 'line-item'); li.append(personLink(p.name), el('span', 'amount', naira(p.amount))); target.append(li);
  }
}
async function loadOverview() {
  let d;
  try { d = await getJSON('/api/v1/overview?period=' + period); } catch (e) { if (e.message !== 'signed out') toast(e.message); return; }
  $('ov-period-label').textContent = d.period + (d.waiting_for_amount ? ` · ${d.waiting_for_amount} entr${d.waiting_for_amount === 1 ? 'y' : 'ies'} waiting for an amount` : '');
  const k = $('ov-kpis'); k.replaceChildren();
  const kpi = (label, value, cls, note) => {
    const c = el('div', 'card kpi'); c.append(el('div', 'label', label), el('div', 'value ' + (cls || ''), value));
    if (note) c.append(el('div', 'note', note)); k.append(c);
  };
  kpi('Money made (sales)', naira(d.sales), '', d.sales_on_credit ? naira(d.sales_on_credit) + ' of it not yet paid' : 'all paid');
  kpi('Expenses', naira(d.expenses), '', 'what you spent');
  kpi('Profit', naira(d.profit), d.profit >= 0 ? 'pos' : 'neg', 'sales minus expenses');
  const people = (n) => `${n} ${n === 1 ? 'person' : 'people'}, right now`;
  kpi('Owed to you', naira(d.owed_to_you.total), '', people(d.owed_to_you.people.length));
  kpi('You owe', naira(d.you_owe.total), '', people(d.you_owe.people.length));
  itemBars($('ov-sales-items'), d.sales_by_item, 'No sales in this period.', false);
  itemBars($('ov-exp-items'), d.expenses_by_item, 'No expenses in this period.', true);
  $('ov-sales-total').textContent = naira(d.sales);
  $('ov-exp-total').textContent = naira(d.expenses);
  balanceList($('ov-owed'), d.owed_to_you.people, 'Nobody owes you anything. 🎉');
  balanceList($('ov-owe'), d.you_owe.people, "You don't owe anyone.");
  const t = $('ov-recent'); t.replaceChildren();
  if (!d.recent.length) { t.innerHTML = '<tr><td class="empty">Nothing recorded yet. Tell the chat what you sold today.</td></tr>'; return; }
  t.innerHTML = '<thead><tr><th>Date</th><th>Entry</th><th>Who</th><th class="num">Amount</th><th class="num">Unpaid</th></tr></thead>';
  const body = el('tbody');
  for (const r of d.recent) {
    const tr = el('tr');
    const what = [TYPE_LABEL[r.type] || r.type, r.quantity ? `${r.quantity} ${r.unit || ''}`.trim() : '', r.item || ''].filter(Boolean).join(' · ');
    tr.append(el('td', '', shortDate(r.date)), el('td', '', what));
    const who = el('td'); if (r.party) who.append(personLink(r.party)); else who.textContent = '—'; tr.append(who);
    tr.append(el('td', 'num', r.amount === null ? 'needs amount' : naira(r.amount)), el('td', 'num', r.outstanding ? naira(r.outstanding) : '—'));
    body.append(tr);
  }
  t.append(body);
}

function itemBars(target, items, emptyText, isExpense) {
  target.replaceChildren();
  if (!items.length) { target.append(el('div', 'empty', emptyText)); return; }
  const max = Math.max(...items.map(i => i.amount)) || 1;
  for (const i of items) {
    const row = el('div', 'bar-item');
    const top = el('div', 'bar-top');
    const left = el('span', '', i.item);
    const detail = [i.quantity ? `${i.quantity} ${i.unit || ''}`.trim() : '', `${i.entries} entr${i.entries === 1 ? 'y' : 'ies'}`].filter(Boolean).join(' · ');
    left.append(el('span', 'sub', detail));
    top.append(left, el('span', 'amount', naira(i.amount)));
    const bar = el('div', 'bar' + (isExpense ? ' exp' : '')); const fill = el('span'); fill.style.width = (100 * i.amount / max) + '%';
    bar.append(fill); row.append(top, bar); target.append(row);
  }
}

// ── owed & owing ─────────────────────────────────────────────────────────────
document.querySelectorAll('#debt-side button').forEach(b => b.addEventListener('click', () => {
  debtSide = b.dataset.side;
  document.querySelectorAll('#debt-side button').forEach(x => x.classList.toggle('active', x === b));
  renderDebts();
}));
function dueText(e) {
  if (!e.due_date) return el('span', 'muted', 'no date given');
  if (e.days_overdue) return el('span', 'due-late', `${shortDate(e.due_date)} · ${e.days_overdue} day${e.days_overdue === 1 ? '' : 's'} late`);
  return el('span', 'due-ok', shortDate(e.due_date));
}
async function loadDebts() {
  try { debtData = await getJSON('/api/v1/debts'); } catch (e) { if (e.message !== 'signed out') toast(e.message); return; }
  renderDebts();
}
function renderDebts() {
  if (!debtData) return;
  const d = debtData[debtSide], owed = debtSide === 'owed_to_you';
  const k = $('debt-kpis'); k.replaceChildren();
  const kpi = (label, value, note, cls) => { const c = el('div', 'card kpi'); c.append(el('div', 'label', label), el('div', 'value ' + (cls || ''), value), el('div', 'note', note)); k.append(c); };
  kpi(owed ? 'Owed to you' : 'You owe', naira(d.total), `${d.people.length} ${d.people.length === 1 ? 'person' : 'people'}`);
  kpi('Overdue', naira(d.overdue), 'past the date they gave', d.overdue ? 'neg' : '');
  const dated = d.people.filter(p => p.next_due && !p.overdue).sort((a, b) => a.next_due.localeCompare(b.next_due))[0];
  kpi('Next due', dated ? shortDate(dated.next_due) : '—', dated ? dated.name : 'no upcoming dates');
  const list = $('debt-list'); list.replaceChildren();
  if (!d.people.length) { list.append(el('div', 'card panel empty', owed ? 'Nobody owes you anything right now.' : "You don't owe anyone right now.")); return; }
  for (const p of d.people) {
    const card = el('div', 'card debtor');
    const head = el('div', 'debtor-head');
    const name = el('div', 'name'); name.append(personLink(p.name));
    if (p.overdue) name.append(el('span', 'pill red', 'overdue'));
    head.append(name, el('div', 'bal amount ' + (owed ? 'balance-owes' : 'balance-owed'), naira(p.balance)));
    card.append(head);
    const wrap = el('div', 'table-wrap'); const t = el('table', 'table');
    t.innerHTML = '<thead><tr><th>Date</th><th>For</th><th class="num">Amount</th><th class="num">Paid</th><th class="num">Balance</th><th>Due</th></tr></thead>';
    const tb = el('tbody');
    for (const e of p.entries) {
      const what = e.type === 'loan_given' ? 'Money lent' : e.type === 'loan_taken' ? 'Money borrowed'
        : [e.quantity ? `${e.quantity} ${e.unit || ''}`.trim() : '', e.item || (owed ? 'Goods' : 'Purchase')].filter(Boolean).join(' ');
      const tr = el('tr');
      const due = el('td'); due.append(dueText(e));
      tr.append(el('td', '', shortDate(e.date)), el('td', '', what), el('td', 'num', naira(e.amount)),
                el('td', 'num', e.paid ? naira(e.paid) : '—'), el('td', 'num', naira(e.balance)), due);
      tb.append(tr);
    }
    t.append(tb); wrap.append(t); card.append(wrap);
    const actions = el('div', 'debtor-actions');
    if (owed) {
      const st = el('button', 'btn btn-ghost btn-sm', 'Send statement'); st.onclick = () => sendMessage('send me ' + p.name + ' statement');
      const rem = el('button', 'btn btn-ghost btn-sm', 'Draft reminder'); rem.onclick = () => sendMessage('remind ' + p.name);
      actions.append(st, rem);
    }
    const paid = el('button', 'btn btn-ghost btn-sm', owed ? 'Record a payment' : 'Record what you paid');
    paid.onclick = () => { showView('chat'); const i = $('msg-input'); i.value = owed ? `${p.name} pay ` : `I pay ${p.name} `; autoResize(i); i.focus(); };
    actions.append(paid); card.append(actions);
    list.append(card);
  }
}

// ── who to chase ─────────────────────────────────────────────────────────────
async function loadChase() {
  let d;
  try { d = await getJSON('/api/v1/chase'); } catch (e) { if (e.message !== 'signed out') toast(e.message); return; }
  $('chase-rule').textContent = 'Ranked by ' + d.ranked_by + '.';
  const list = $('chase-list'); list.replaceChildren();
  if (!d.chase.length) list.append(el('div', 'card panel empty', 'Nobody to chase right now. Everyone who owes you is either paid up or skipped.'));
  d.chase.forEach((p, i) => {
    const card = el('div', 'card chase-card');
    card.append(el('div', 'rank', String(i + 1)));
    const mid = el('div');
    const h = el('h3'); h.append(el('span', '', p.name), el('span', 'amount', noKobo(p.owes))); mid.append(h);
    const ul = el('ul', 'reasons'); p.reasons.forEach(r => ul.append(el('li', '', noKobo(r)))); mid.append(ul);
    card.append(mid);
    const actions = el('div', 'chase-actions');
    const remind = el('button', 'btn btn-primary btn-sm', 'Draft reminder'); remind.onclick = () => sendMessage('remind ' + p.name);
    const why = el('button', 'btn btn-ghost btn-sm', 'History'); why.onclick = () => openPerson(p.name);
    actions.append(remind, why); card.append(actions);
    list.append(card);
  });
  const sk = $('chase-skipped'); sk.replaceChildren();
  if (d.skipped.length) {
    const box = el('div', 'skipped'); box.append(el('h3', '', 'Skipped'));
    d.skipped.forEach(s => {
      const li = el('div', 'line-item'); const left = el('div');
      left.append(personLink(s.name), el('div', 'muted', s.reason)); li.append(left, el('span', 'amount', noKobo(s.owes))); box.append(li);
    });
    sk.append(box);
  }
  if (d.more) sk.append(el('p', 'muted', `+ ${d.more} more owe you money.`));
}

// ── people & memory ──────────────────────────────────────────────────────────
async function loadPeople() {
  try { peopleData = await getJSON('/api/v1/people'); } catch (e) { if (e.message !== 'signed out') toast(e.message); return; }
  renderPeople();
}
function renderPeople() {
  const q = $('people-search').value.trim().toLowerCase();
  const grid = $('people-list'); grid.replaceChildren();
  if (peopleData.memory === 'off') grid.append(el('div', 'memory-off', 'Memory is switched off on this server: balances only.'));
  const shown = peopleData.people.filter(p => !q || p.name.toLowerCase().includes(q) || p.also_called.some(a => a.toLowerCase().includes(q)));
  if (!shown.length) { grid.append(el('div', 'empty', q ? 'No one matches that.' : 'No one in your books yet.')); return; }
  for (const p of shown) {
    const card = el('button', 'card person'); card.type = 'button'; card.onclick = () => openPerson(p.name);
    const top = el('div', 'person-top'); top.append(el('b', '', p.name));
    if (p.owes_you) top.append(el('span', 'amount balance-owes', 'owes ' + naira(p.owes_you)));
    else if (p.you_owe) top.append(el('span', 'amount balance-owed', 'you owe ' + naira(p.you_owe)));
    else top.append(el('span', 'muted', 'settled'));
    card.append(top);
    const tags = el('div', 'tags');
    p.roles.forEach(r => tags.append(el('span', 'pill ' + (r === 'supplier' ? 'blue' : ''), r)));
    if (p.promises && (p.promises.kept || p.promises.broken)) {
      tags.append(el('span', 'pill ' + (p.promises.broken > p.promises.kept ? 'red' : p.promises.broken ? 'amber' : ''),
        `promises kept ${p.promises.kept} · broken ${p.promises.broken}`));
    }
    if (p.remembered.some(f => f.kind === 'do_not_remind')) tags.append(el('span', 'pill red', "don't remind"));
    card.append(tags);
    const facts = el('div', 'facts');
    if (p.also_called.length) facts.append(el('div', '', 'Also called: ' + p.also_called.join(', ')));
    p.remembered.filter(f => f.kind === 'usual_order').forEach(f => facts.append(el('div', '', 'Usual order: ' + f.summary + (f.how === 'inferred' ? ' (learned from sales)' : ''))));
    if (p.promises && p.promises.open) facts.append(el('div', '', `${p.promises.open} promise${p.promises.open > 1 ? 's' : ''} to pay still open`));
    facts.append(el('div', 'prov', `${p.entries} entr${p.entries === 1 ? 'y' : 'ies'} · last ${shortDate(p.last_seen)}`));
    card.append(facts);
    grid.append(card);
  }
}

// ── person drawer ────────────────────────────────────────────────────────────
function closeDrawer() { $('drawer').hidden = true; $('drawer-backdrop').hidden = true; }
const OUTCOME_PILL = { kept: '', broken: 'red', 'still open': 'amber', 'replaced by a later date': 'grey' };
function provenance(item) {
  const wrap = el('div');
  if (item.you_said) wrap.append(el('div', 'quote', '“' + item.you_said + '”'));
  const bits = [];
  if (item.on) bits.push('you said this on ' + shortDate(item.on));
  if (item.written_by) bits.push('saved by the ' + item.written_by.replace('_', ' '));
  if (item.decided_by) bits.push('outcome by ' + item.decided_by.replace('_', ' ') + (item.decided_on ? ' on ' + shortDate(item.decided_on) : ''));
  if (bits.length) wrap.append(el('div', 'prov', bits.join(' · ')));
  return wrap;
}
async function openPerson(name) {
  $('drawer-title').textContent = name;
  const body = $('drawer-body'); body.replaceChildren(el('div', 'muted', 'Loading…'));
  $('drawer').hidden = false; $('drawer-backdrop').hidden = false;
  let d;
  try { d = await getJSON('/api/v1/people/' + encodeURIComponent(name)); }
  catch (e) { body.replaceChildren(el('div', 'empty', e.message === 'signed out' ? '' : e.message)); return; }
  $('drawer-title').textContent = d.name;
  body.replaceChildren();
  const m = d.memory || {};

  const ask = el('div', 'tags');
  const why = el('button', 'btn btn-ghost btn-sm', 'Ask about ' + d.name); why.onclick = () => { closeDrawer(); sendMessage('wetin you know about ' + d.name + '?'); };
  const stmt = el('button', 'btn btn-ghost btn-sm', 'Statement'); stmt.onclick = () => { closeDrawer(); sendMessage('send me ' + d.name + ' statement'); };
  ask.append(why, stmt); body.append(ask);

  if (m.error) body.append(el('div', 'memory-off', m.error));
  else {
    const sec = (title) => { const s = el('section'); s.append(el('h3', '', title)); body.append(s); return s; };
    if (m.also_called?.length || m.roles?.length) {
      const s = sec('Who they are'); const tags = el('div', 'tags');
      (m.roles || []).filter(r => r.status === 'current').forEach(r => tags.append(el('span', 'pill ' + (r.role === 'supplier' ? 'blue' : ''), r.role + (r.how === 'you said so' ? ' · you said so' : ''))));
      s.append(tags);
      const tl = el('div', 'timeline');
      (m.also_called || []).filter(a => a.status === 'current').forEach(a => {
        const item = el('div', 'tl'); const top = el('div', 'tl-top');
        top.append(el('b', '', 'Also called “' + a.name + '”'), el('span', 'pill grey', a.kind)); item.append(top, provenance(a));
        const wrong = el('button', 'linkish', 'Not the same person');
        wrong.onclick = async () => {
          if (!confirm(`Stop treating “${a.name}” as ${d.name}?`)) return;
          const res = await api('/api/v1/people/' + encodeURIComponent(d.name) + '/aliases/remove', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ alias: a.name }) });
          const out = await res.json().catch(() => ({}));
          toast(res.ok ? `“${a.name}” is no longer linked to ${d.name}.` : errorText(out.detail, 'Could not change that.'));
          openPerson(d.name); if ($('view-people').classList.contains('active')) loadPeople();
        };
        item.append(wrong); tl.append(item);
      });
      s.append(tl);
    }
    if (m.remembered?.length) {
      const s = sec('Remembered'); const tl = el('div', 'timeline');
      m.remembered.forEach(f => {
        const item = el('div', 'tl'); const top = el('div', 'tl-top');
        top.append(el('b', '', f.what === 'do not remind' ? "Don't remind" : 'Usual order: ' + f.detail),
                   el('span', 'pill ' + (f.status === 'in force' ? '' : 'grey'), f.status));
        item.append(top, el('div', 'prov', f.how), provenance(f)); tl.append(item);
      });
      s.append(tl);
    }
    const s = sec('Promises to pay · ' + (m.promise_record || 'none'));
    if (!m.promises?.length) s.append(el('div', 'empty', 'No promises on record.'));
    else {
      const tl = el('div', 'timeline');
      m.promises.slice().reverse().forEach(p => {
        const item = el('div', 'tl'); const top = el('div', 'tl-top');
        top.append(el('b', '', 'Pay by ' + shortDate(p.due)), el('span', 'pill ' + (OUTCOME_PILL[p.outcome] ?? 'grey'), p.outcome + (p.paid ? ` · ${p.paid} paid` : '')));
        item.append(top, provenance(p)); tl.append(item);
      });
      s.append(tl);
    }
    if (m.reminders_sent?.length) {
      const r = sec('Reminders sent'); m.reminders_sent.forEach(x => r.append(el('div', 'line-item', `${shortDate(x.on)} · because of ${x.because_of_promises.length} promise(s)`)));
    }
  }

  if (!m.error) {
    const forget = el('button', 'btn btn-ghost btn-sm danger', 'Forget what’s remembered about ' + d.name);
    forget.onclick = async () => {
      if (!confirm(`Erase everything Book-Keeper remembers about ${d.name}: nicknames, promises, usual order, notes? Their entries in your books stay.`)) return;
      const res = await api('/api/v1/people/' + encodeURIComponent(d.name) + '/forget', { method: 'POST' });
      const out = await res.json().catch(() => ({}));
      toast(res.ok ? `Forgotten. ${d.name}'s entries in your books are unchanged.` : errorText(out.detail, 'Could not forget that.'));
      openPerson(d.name); if ($('view-people').classList.contains('active')) loadPeople();
    };
    const fs = el('section'); fs.append(el('h3', '', 'Privacy'), forget); body.append(fs);
  }

  const es = el('section'); es.append(el('h3', '', 'Entries'));
  if (!d.entries.length) es.append(el('div', 'empty', 'No entries.'));
  else {
    const table = el('table', 'table');
    table.innerHTML = '<thead><tr><th>Date</th><th>Entry</th><th class="num">Amount</th><th class="num">Unpaid</th></tr></thead>';
    const tb = el('tbody');
    d.entries.forEach(r => {
      const tr = el('tr');
      tr.append(el('td', '', shortDate(r.date)), el('td', '', [TYPE_LABEL[r.type] || r.type, r.item].filter(Boolean).join(' · ') + (r.due_date ? ` (due ${shortDate(r.due_date)})` : '')),
                el('td', 'num', r.amount === null ? '—' : naira(r.amount)), el('td', 'num', r.outstanding ? naira(r.outstanding) : '—'));
      tb.append(tr);
    });
    table.append(tb); es.append(table);
  }
  body.append(es);
}

// ── wiring (no inline handlers, so the Content-Security-Policy can be strict) ───
const ACTIONS = {
  'auth-tab': (el) => showAuthTab(el.dataset.tab),
  'logout': () => logout(),
  'view': (el) => showView(el.dataset.viewTarget),
  'reload-chase': () => loadChase(),
  'close-drawer': () => closeDrawer(),
  'send': () => sendMessage(),
};
const SUBMITS = { 'wa-login': submitWhatsAppLogin, 'login': submitLogin, 'register': submitRegister,
                  'wa-connect': submitConnectWhatsApp };
document.addEventListener('click', (e) => {
  const target = e.target.closest('[data-action]');
  if (target && ACTIONS[target.dataset.action]) ACTIONS[target.dataset.action](target, e);
});
document.querySelectorAll('form[data-submit]').forEach(f => f.addEventListener('submit', (e) => SUBMITS[f.dataset.submit](e)));
$('people-search').addEventListener('input', renderPeople);
$('msg-input').addEventListener('input', (e) => autoResize(e.target));
$('msg-input').addEventListener('keydown', handleKey);

// ── start ────────────────────────────────────────────────────────────────────
renderSuggestions();
if (location.hash === '#signup') showAuthTab('register');
Promise.all([loadWhatsAppInfo(), loadDemoButtons()]).then(enterApp);
