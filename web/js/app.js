// Boot, sign-in (Clerk and/or local), role-based navigation, faculty and student screens.
import { DAYS, api, badge, download, field, fmt, get, guard, h, post, select, state, table, toMin, toast, when } from './core.js';
import { timetableViewer } from './grid.js';
import * as admin from './admin.js';

const app = document.getElementById('app');
let authCfg = { local_login: true };

// ---------------------------------------------------------------- Clerk
function addScript(src, attrs = {}) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src; s.async = true; s.crossOrigin = 'anonymous';
    for (const [k, v] of Object.entries(attrs)) s.setAttribute(k, v);
    s.onload = resolve; s.onerror = () => reject(new Error('Could not load the sign-in service.'));
    document.head.append(s);
  });
}
async function loadClerk() {
  if (state.clerk) return state.clerk;
  const host = authCfg.clerk_frontend_api;
  await addScript(`https://${host}/npm/@clerk/ui@1/dist/ui.browser.js`);
  await addScript(`https://${host}/npm/@clerk/clerk-js@6/dist/clerk.browser.js`, { 'data-clerk-publishable-key': authCfg.clerk_publishable_key });
  await window.Clerk.load({ ui: { ClerkUI: window.__internal_ClerkUICtor } });
  state.clerk = window.Clerk;
  return state.clerk;
}

// ---------------------------------------------------------------- sign-in
function signedIn(me) { state.user = me; state.csrf = me.csrf_token; start(); }
const crest = (sub) => h('div', { class: 'crest' }, h('div', { class: 'crest-mark', 'aria-hidden': 'true' }, 'S'), sub);

async function loginPage() {
  const cap = (n, title, text) => h('div', { class: 'capability' }, h('div', { class: 'n' }, n), h('div', {}, h('b', {}, title), h('span', {}, text)));
  const card = h('div', { class: 'card' }, h('div', { class: 'eyebrow' }, 'Sign in'), h('h2', {}, 'Welcome back'));
  app.replaceChildren(h('div', { class: 'login-grid' },
    h('div', { class: 'brand-panel' },
      crest(h('div', { class: 'crest-text' }, authCfg.org_name || 'Academic timetable generator')),
      h('div', {}, h('div', { class: 'eyebrow' }, 'Automated academic scheduling'), h('h1', {}, 'Slot', h('span', { class: 'mk' }, 'ix')),
        h('p', { class: 'lede' }, 'The coordinator enters rooms, student groups, teaching requirements and faculty availability. The system builds a weekly timetable that satisfies every mandatory rule, or says exactly why it cannot.')),
      h('div', { class: 'caps' },
        cap('01', 'Mandatory rules are never bent', 'Working hours and breaks, faculty availability, no clash for any faculty member, room or student group, room capacity, lab type, consecutive practical periods, fixed commitments.'),
        cap('02', 'Preferences are scored, not enforced', 'Faculty preferences, spreading a subject across days, fewer idle gaps and right-sized rooms rank the valid timetables.'),
        cap('03', 'An impossible input is reported, not hidden', 'Unassigned sessions come back named, with the constraint that blocked them.'))),
    h('div', { class: 'login-side' }, card)));
  const msg = h('div', {});
  card.append(msg);

  if (authCfg.clerk_publishable_key) {
    const mount = h('div', { id: 'clerk-sign-in' }, h('p', { class: 'muted' }, 'Loading secure sign-in…'));
    card.append(mount);
    try {
      const clerk = await loadClerk();
      let busy = false;
      const exchange = async () => {
        if (busy || !clerk.session) return;
        busy = true;
        try {
          const token = await clerk.session.getToken();
          signedIn(await post('/api/auth/clerk', { token }));
        } catch (e) {
          clerk.unmountSignIn(mount);
          mount.replaceChildren();
          msg.replaceChildren(h('div', { class: 'notice err' }, e.message),
            h('button', { onclick: async () => { await clerk.signOut(); location.reload(); } }, 'Sign in with a different account'));
        }
      };
      if (clerk.isSignedIn) await exchange();
      else {
        mount.replaceChildren();
        clerk.mountSignIn(mount);
        clerk.addListener(({ session }) => { if (session && !state.user) exchange(); });
      }
    } catch (e) {
      mount.replaceChildren(h('div', { class: 'notice err' }, e.message));
    }
  }
  if (authCfg.local_login) {
    const u = h('input', { type: 'text', autocomplete: 'username', required: true, maxLength: 160 });
    const p = h('input', { type: 'password', autocomplete: 'current-password', required: true, maxLength: 200 });
    const err = h('div', {});
    if (authCfg.clerk_publishable_key) card.append(h('div', { class: 'divider' }, '— or a local account (development / demo) —'));
    card.append(h('form', { class: 'login-form', onsubmit: async (e) => {
      e.preventDefault();
      try { signedIn(await post('/api/auth/login', { username: u.value, password: p.value })); } catch (ex) { p.value = ''; err.replaceChildren(h('div', { class: 'notice err', role: 'alert' }, ex.message)); }
    } }, err, field('Login ID', u), field('Password', p), h('button', { class: 'primary', type: 'submit' }, 'Sign in'),
    h('p', { class: 'hint' }, 'What you see after signing in depends on your role: coordinator, faculty or student.')));
  }
  if (!authCfg.clerk_publishable_key && !authCfg.local_login) card.append(h('div', { class: 'notice err' }, 'No sign-in method is configured.'));
}

async function logout() {
  await guard(() => post('/api/auth/logout'));
  if (authCfg.clerk_publishable_key) { try { await (await loadClerk()).signOut(); } catch { /* local session is already closed */ } }
  location.hash = ''; location.reload();
}

// ---------------------------------------------------------------- faculty / student screens
const noTerm = (text) => h('div', { class: 'empty' }, h('b', {}, 'Nothing to show yet'), h('div', {}, text));

async function publishedPage(root) {
  if (!state.termId) return root.append(noTerm('Your timetable will appear here once the coordinator publishes it.'));
  let d;
  try { d = await get(`/api/timetable/published?term_id=${state.termId}`); } catch (e) { return root.append(noTerm(e.message)); }
  const student = state.user.roles.includes('student') && state.user.roles.length === 1;
  root.append(h('div', { class: 'card' },
    h('div', { class: 'row' }, h('h2', { style: { margin: 0 } }, d.version.label), badge('published'), h('span', { class: 'muted small' }, `since ${when(d.version.published_at)}`),
      h('button', { class: 'right noprint', onclick: () => download(`/api/timetable/published/export.csv?term_id=${state.termId}`) }, 'Export CSV'), h('button', { class: 'noprint', onclick: () => window.print() }, 'Print')),
    student ? h('p', { class: 'help gap' }, 'Read-only view of the published timetable for your class and batch.') : null,
    h('div', { class: 'gap' }, timetableViewer(d, { mineFacultyId: state.user.faculty_id || undefined, onClick: (s) => toast(`${s.subject_name} · ${s.group_name} · ${s.faculty_name} · room ${s.room_code} · ${DAYS[s.day_of_week]} ${fmt(s.start_minute)}–${fmt(s.end_minute)}`) }))));
  return undefined;
}

// "My preferences" - faculty only. Soft constraints the solver weighs on the next generation.
async function preferencesPage(root) {
  if (!state.termId) return root.append(noTerm('No academic term is open for your department yet.'));
  const d = await get(`/api/faculty/preferences?term_id=${state.termId}`);
  const { days, periods } = d.calendar;
  if (!days.length || !periods.length) return root.append(noTerm('The coordinator has not configured working days and periods for this term yet.'));
  const marks = { ...d.marks };
  const off = new Set(d.unavailable);
  const LABEL = { prefer: 'Preferred', avoid: 'Avoid' };
  const dirty = h('span', { class: 'badge warning hidden' }, 'Unsaved changes');
  const counts = h('div', { class: 'legend' });
  const touch = () => {
    const n = (k) => Object.values(marks).filter((v) => v === k).length;
    counts.replaceChildren(h('span', {}, h('i', { class: 'g' }), `Preferred — ${n('prefer')} slots`), h('span', {}, h('i', { class: 'r' }), `Avoid — ${n('avoid')} slots`), h('span', {}, 'Everything else is neutral.'));
  };
  const changed = () => { dirty.classList.remove('hidden'); touch(); };
  const paint = (b, v) => { b.className = `slot ${v || ''}`; b.textContent = LABEL[v] || 'Neutral'; b.setAttribute('aria-pressed', v ? 'true' : 'false'); };
  const buttons = [];
  const cell = (day, p) => {
    const key = `${day}-${p.start_minute}`;
    if (off.has(key)) return h('td', { class: 'off' }, 'unavailable');
    const b = h('button', { type: 'button', title: `${DAYS[day]} ${fmt(p.start_minute)}–${fmt(p.end_minute)}: click to cycle neutral → preferred → avoid`, onclick: () => {
      const next = marks[key] === 'prefer' ? 'avoid' : marks[key] === 'avoid' ? null : 'prefer';
      if (next) marks[key] = next; else delete marks[key];
      paint(b, next); changed();
    } });
    paint(b, marks[key]); buttons.push([key, b]);
    return h('td', {}, b);
  };
  const first = h('input', { type: 'checkbox', checked: d.avoid_first, onchange: changed }), last = h('input', { type: 'checkbox', checked: d.avoid_last, onchange: changed });
  const spread = h('input', { type: 'checkbox', checked: d.spread_evenly, onchange: changed });
  const strength = select([['low', 'Low — a mild nudge'], ['normal', 'Normal — the usual weighting'], ['high', 'High — accommodate wherever possible']], d.strength, { onchange: changed });
  const notes = h('textarea', { rows: 5, maxLength: 1000, value: d.notes, placeholder: 'e.g. Lab sessions work better before lunch.', oninput: changed });
  const std = (input, title, hint) => h('label', { class: 'std' }, input, h('span', {}, h('b', {}, title), h('span', { class: 'hint' }, hint)));
  const r = d.report;
  const breached = r && (r.in_avoided || r.first_breach || r.last_breach);

  root.append(h('div', {},
    h('div', { class: 'sec-head' }, h('p', {}, 'Preferences are weighed, not enforced. A mandatory rule — your availability, a clash, room capacity — always wins.')),
    r ? h('div', { class: `report ${breached ? '' : 'good'}` }, h('h3', {}, `${r.honoured_pct}% of your scheduled periods match what you asked for`),
      h('p', {}, `${r.in_preferred} of your ${r.periods} periods in the published timetable landed in a slot you marked preferred`,
        r.in_avoided ? `, ${r.in_avoided} landed in a slot you asked to avoid` : '', r.first_breach ? `, ${r.first_breach} in a first period` : '', r.last_breach ? `, ${r.last_breach} in a last period` : '',
        breached ? '. Either that timetable was generated before you submitted these preferences, or a mandatory rule or fixed commitment required those slots. Preferences are applied the next time the coordinator generates a timetable.' : '. Everything you asked to avoid was avoided.')) : null,
    h('div', { class: 'card' },
      h('div', { class: 'sec-head' }, h('h2', {}, 'Mark your week'), h('p', {}, 'Click a slot to cycle it: neutral → preferred → avoid. Greyed slots are outside your recorded availability.')),
      h('div', { class: 'tablewrap' }, h('table', { class: 'pgrid' },
        h('thead', {}, h('tr', {}, h('th', {}, 'Day'), periods.map((p) => h('th', {}, p.label || `P${p.idx + 1}`, h('div', { class: 'mono', style: { fontWeight: '400', textTransform: 'none', letterSpacing: '0', fontSize: '10px' } }, fmt(p.start_minute)))))),
        h('tbody', {}, days.map((day) => h('tr', {}, h('td', { class: 'day' }, DAYS[day]), periods.map((p) => cell(day, p))))))),
      counts),
    h('div', { class: 'grid2' },
      h('div', { class: 'editpanel' }, h('div', {}, h('div', { class: 'eyebrow' }, 'Standing requests'), h('h3', {}, 'Rules that apply every week')),
        std(first, 'Avoid the first period', `No class at ${fmt(periods[0].start_minute)}.`),
        std(last, 'Avoid the last period', `No class ending at ${fmt(periods[periods.length - 1].end_minute)}.`),
        std(spread, 'Spread my load across the week', 'Rather than stacking periods into fewer days.'),
        field('How strongly should these be weighed?', strength, 'High priority is honoured before a colleague\'s low priority when the two compete for the same slot.')),
      h('div', { class: 'editpanel' }, h('div', {}, h('div', { class: 'eyebrow' }, 'Note to the coordinator'), h('h3', {}, 'Anything the grid cannot say')),
        notes,
        h('div', { class: 'kv' }, h('div', {}, h('span', {}, 'Subjects assigned to me'), h('span', {}, d.info.subjects_assigned)),
          h('div', {}, h('span', {}, 'Daily cap set by the office'), h('span', {}, d.info.max_periods_per_day ? `${d.info.max_periods_per_day} periods` : 'None')),
          h('div', {}, h('span', {}, 'Last submitted'), h('span', {}, d.submitted_at ? when(d.submitted_at) : 'Not submitted yet'))),
        h('div', { class: 'toolbar' },
          h('button', { class: 'primary', onclick: async () => {
            const body = { term_id: state.termId, marks: Object.entries(marks).map(([k, mark]) => { const [dd, s] = k.split('-').map(Number); return { day_of_week: dd, start_minute: s, mark }; }),
              avoid_first: first.checked, avoid_last: last.checked, spread_evenly: spread.checked, strength: strength.value, notes: notes.value.trim() };
            if (await guard(() => api('PUT', '/api/faculty/preferences', body), 'Preferences submitted')) state.rerender();
          } }, 'Submit preferences'),
          h('button', { onclick: () => { for (const k of Object.keys(marks)) delete marks[k]; buttons.forEach(([, b]) => paint(b, null)); changed(); } }, 'Clear all marks'), dirty),
        h('p', { class: 'note' }, 'The coordinator sees these and the solver uses them on the next generation. Submitting does not change a published timetable on its own.')))));
  touch();
  return undefined;
}

async function facultyRequests(root) {
  const rows = await get('/api/faculty/requests');
  let mine = [];
  if (state.termId) { try { mine = (await get(`/api/timetable/published?term_id=${state.termId}`)).sessions.filter((s) => s.mine); } catch { mine = []; } }
  const type = select([['leave', 'Leave'], ['time_change', 'Timetable / time change']], 'leave');
  const sess = select([[null, '—'], ...mine.map((s) => [s.id, `${s.subject_code} ${s.session_type} · ${s.group_name} · ${DAYS[s.day_of_week]} ${fmt(s.start_minute)}`])], null);
  const day = select(DAYS.map((d, i) => [i, d]), 0), from = h('input', { type: 'date' }), to = h('input', { type: 'date' });
  const pday = select([[null, '—'], ...DAYS.map((d, i) => [i, d])], null), ptime = h('input', { type: 'time' });
  const reason = h('textarea', { maxLength: 1000, placeholder: 'Reason (required)' });
  const fLeave = h('div', { class: 'form gap' }, field('Affected weekday', day), field('From date', from), field('To date', to));
  const fMove = h('div', { class: 'form gap hidden' }, field('Your session', sess), field('Proposed day', pday), field('Proposed start time', ptime));
  type.addEventListener('change', () => { fLeave.classList.toggle('hidden', type.value !== 'leave'); fMove.classList.toggle('hidden', type.value === 'leave'); });
  root.append(h('div', { class: 'card' }, h('h2', {}, 'New request'),
    h('p', { class: 'help' }, 'Requests are reviewed by the coordinator. An approval does not change the timetable by itself - you will see the change after a new version is published.'),
    h('div', { class: 'form' }, field('Request type', type)), fLeave, fMove, h('div', { class: 'gap' }, reason),
    h('button', { class: 'primary gap', onclick: async () => {
      if (!state.termId) return toast('No term available', 'err');
      const body = { term_id: state.termId, request_type: type.value, reason: reason.value };
      if (type.value === 'leave') Object.assign(body, { day_of_week: Number(day.value), ...(from.value ? { leave_from: from.value } : {}), ...(to.value ? { leave_to: to.value } : {}) });
      else Object.assign(body, { ...(sess.value ? { session_id: Number(sess.value) } : {}), ...(pday.value ? { proposed_day_of_week: Number(pday.value) } : {}), ...(ptime.value ? { proposed_start_minute: toMin(ptime.value) } : {}) });
      if (await guard(() => post('/api/faculty/requests', body), 'Request submitted')) state.rerender();
      return undefined;
    } }, 'Submit request')),
  h('div', { class: 'card' }, h('h2', {}, 'My requests'),
    table([{ label: '#', key: 'id' }, { label: 'Type', render: (r) => r.request_type.replace('_', ' ') },
      { label: 'Details', render: (r) => [r.session_label, r.day_of_week != null ? ` ${DAYS[r.day_of_week]}` : '', h('div', { class: 'muted' }, r.reason)] },
      { label: 'Status', render: (r) => badge(r.status) }, { label: 'Coordinator note', render: (r) => r.decision_note || '—' }, { label: 'Raised', render: (r) => when(r.created_at) },
      { label: '', render: (r) => (r.status === 'pending' ? h('button', { class: 'sm', onclick: async () => { if (await guard(() => post(`/api/faculty/requests/${r.id}/cancel`), 'Request cancelled')) state.rerender(); } }, 'Cancel') : null) }], rows, 'You have not raised any requests.')));
}

async function notificationsPage(root) {
  const rows = await get('/api/notifications');
  root.append(h('div', { class: 'card' }, h('div', { class: 'row' }, h('h2', { style: { margin: 0 } }, 'Notifications'),
    h('button', { class: 'right', onclick: async () => { await guard(() => post('/api/notifications/read-all')); state.rerender(); } }, 'Mark all read')),
  h('div', { class: 'gap' }, table([{ label: 'When', class: 'nowrap', render: (n) => when(n.created_at) }, { label: 'Message', render: (n) => (n.read ? n.message : h('b', {}, n.message)) }], rows, 'No notifications.'))));
}

async function accountPage(root) {
  const cur = h('input', { type: 'password', autocomplete: 'current-password' }), nw = h('input', { type: 'password', autocomplete: 'new-password' });
  root.append(h('div', { class: 'card' }, h('h2', {}, 'My account'), h('p', {}, `${state.user.full_name} · ${state.user.username} · role: ${state.user.roles.join(', ')}`),
    authCfg.clerk_publishable_key ? h('p', { class: 'help' }, 'If you sign in with Google / authenticator, manage two-step verification from the sign-in provider\'s account menu.') : null,
    authCfg.local_login ? [h('h3', {}, 'Change local password'), h('div', { class: 'form' }, field('Current password', cur), field('New password (min. 12, upper + lower + digit)', nw)),
      h('button', { class: 'primary gap', onclick: async () => { if (await guard(() => post('/api/auth/change-password', { current_password: cur.value, new_password: nw.value }), 'Password changed; other devices were signed out')) { cur.value = ''; nw.value = ''; } } }, 'Change password')] : null));
}

// ---------------------------------------------------------------- shell + router
const NAV = {
  admin: [['dashboard', 'Dashboard', admin.dashboard], ['setup', 'Inputs', admin.setup], ['rules', 'Rules lab', admin.rules], ['generate', 'Generate', admin.generate],
    ['timetable', 'Timetable', admin.timetable], ['whatif', 'What-if lab', admin.whatif], ['requests', 'Request inbox', admin.requests], ['versions', 'Review & publish', admin.versionsPage],
    ['users', 'Users', admin.users], ['audit', 'Audit log', admin.auditPage], ['compare', null, admin.compare]],
  faculty: [['published', 'My timetable', publishedPage], ['prefs', 'My preferences', preferencesPage], ['myrequests', 'My requests', facultyRequests], ['notifications', 'Notifications', notificationsPage]],
  student: [['published', 'My timetable', publishedPage], ['notifications', 'Notifications', notificationsPage]],
};
const ROLE_LABEL = { admin: 'Coordinator', faculty: 'Faculty', student: 'Student' };

async function start() {
  const role = ['admin', 'faculty', 'student'].find((r) => state.user.roles.includes(r));
  const routes = [...(NAV[role] || []), ['account', 'My account', accountPage]];
  const main = h('main', { class: 'main' });
  const links = h('nav', { class: 'navtabs noprint', 'aria-label': 'Main' });
  const status = h('span', {});
  const termSel = h('select', { 'aria-label': 'Academic term', onchange: (e) => { state.termId = Number(e.target.value) || null; localStorage.setItem('tt_term', e.target.value); render(); } });

  state.reloadTerms = async () => {
    state.terms = await get('/api/terms');
    const saved = Number(localStorage.getItem('tt_term'));
    state.termId = (state.terms.find((t) => t.id === (state.termId || saved)) || state.terms[0] || {}).id || null;
    termSel.replaceChildren(...(state.terms.length ? state.terms.map((t) => h('option', { value: t.id }, t.name)) : [h('option', { value: '' }, 'No term yet')]));
    if (state.termId) termSel.value = String(state.termId);
  };
  async function render() {
    if (state.cleanup) { state.cleanup(); state.cleanup = null; }
    const [name, arg] = location.hash.replace(/^#\//, '').split('/');
    const route = routes.find((r) => r[0] === name) || routes[0];
    links.replaceChildren(...routes.filter((r) => r[1]).map((r) => h('a', { href: `#/${r[0]}`, class: r === route ? 'active' : '', 'aria-current': r === route ? 'page' : null }, r[1])));
    const t = state.terms.find((x) => x.id === state.termId);
    status.replaceChildren(t ? badge(t.published ? 'Published' : 'No timetable published', t.published ? 'ok' : '') : '');
    const page = h('div', {});
    main.replaceChildren(h('div', { class: 'top' }, h('h1', {}, route[1] || 'Compare candidates'), t ? h('p', {}, t.name) : null), page);
    try { await route[2](page, arg ? decodeURIComponent(arg) : undefined); } catch (e) { page.append(h('div', { class: 'notice err' }, e.message)); }
  }
  state.rerender = async () => { await state.reloadTerms(); return render(); };
  app.replaceChildren(h('div', { class: 'shell' },
    h('header', { class: 'topbar noprint' },
      crest(h('div', {}, h('div', { class: 'crest-name' }, 'Slotix'), h('div', { class: 'crest-sub' }, authCfg.org_name || 'Academic timetable generator'))),
      status, h('label', { class: 'termpick' }, 'Term', termSel),
      h('div', { class: 'who' }, h('div', {}, h('b', {}, state.user.full_name), ROLE_LABEL[role] || role), h('button', { class: 'sm', onclick: logout }, 'Sign out'))),
    links, main));
  await state.reloadTerms();
  window.onhashchange = render;
  render();
}

(async function boot() {
  try { authCfg = await get('/api/auth/config'); } catch { /* keep defaults */ }
  try { signedIn(await get('/api/auth/me')); } catch { loginPage(); }
})();
