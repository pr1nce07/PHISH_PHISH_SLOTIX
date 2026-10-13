// Shared helpers. All DOM is built with h() + textContent, so server data is never parsed as HTML.
export const state = { user: null, csrf: '', terms: [], termId: null, clerk: null };
export const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
export const fmt = (m) => `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
export const toMin = (s) => { const [a, b] = String(s).split(':').map(Number); return a * 60 + (b || 0); };

export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style') Object.assign(el.style, v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k in el && k !== 'list' && k !== 'form') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

export function errText(d) {
  const x = d && d.detail !== undefined ? d.detail : d;
  if (typeof x === 'string') return x;
  if (Array.isArray(x)) return x.map((e) => (e.field ? `${e.field}: ` : '') + (e.message || JSON.stringify(e))).join('; ');
  if (x && x.message) return x.message + (x.conflicts && x.conflicts.length ? ' — ' + x.conflicts.map((c) => c.message).join(' | ') : '');
  return 'Request failed';
}

export async function api(method, path, body) {
  const opt = { method, headers: {}, credentials: 'same-origin' };
  if (method !== 'GET') opt.headers['X-CSRF-Token'] = state.csrf;
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const res = await fetch(path, opt);
  if (res.status === 204) return null;
  const data = (res.headers.get('content-type') || '').includes('json') ? await res.json() : await res.text();
  if (!res.ok) {
    if (res.status === 401 && state.user) { state.user = null; location.reload(); }
    const err = new Error(errText(data)); err.status = res.status; err.data = data; throw err;
  }
  return data;
}
export const get = (p) => api('GET', p);
export const post = (p, b) => api('POST', p, b === undefined ? {} : b);

export function toast(msg, kind = '') {
  const t = h('div', { class: `toast ${kind}`, role: 'status' }, msg);
  document.getElementById('toasts').append(t);
  setTimeout(() => t.remove(), kind === 'err' ? 9000 : 4000);
}
export async function guard(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg, 'ok'); return r; } catch (e) { toast(e.message, 'err'); return undefined; }
}

export function dialog(title, body, buttons = []) {
  const d = h('dialog', {});
  const close = () => { d.close(); d.remove(); };
  d.append(h('div', { class: 'dh' }, h('h2', { style: { margin: 0 } }, title), h('button', { class: 'right sm', onclick: close, 'aria-label': 'Close' }, '✕')),
    h('div', { class: 'db' }, body),
    h('div', { class: 'df' }, buttons.map((b) => h('button', { class: b.class || '', onclick: async () => { if ((await b.action(close)) !== false && !b.keep) close(); } }, b.label)),
      h('button', { onclick: close }, 'Close')));
  document.body.append(d); d.showModal();
  return { el: d, close };
}
export const confirmBox = (title, text, label, action, cls = 'danger') => dialog(title, h('p', {}, text), [{ label, class: cls, action }]);

export const badge = (text, cls) => h('span', { class: `badge ${cls || text}` }, text);
export function field(label, input, help) { return h('label', { class: 'f' }, label, input, help ? h('span', { class: 'help' }, help) : null); }
export function select(options, value, props = {}) {
  const s = h('select', props, options.map(([v, l]) => h('option', { value: v === null ? '' : String(v) }, l)));
  s.value = value === null || value === undefined ? '' : String(value);
  return s;
}
export function table(cols, rows, empty = 'Nothing here yet.') {
  if (!rows.length) return h('p', { class: 'muted' }, empty);
  return h('div', { class: 'tablewrap' }, h('table', {}, h('thead', {}, h('tr', {}, cols.map((c) => h('th', {}, c.label)))),
    h('tbody', {}, rows.map((r) => h('tr', {}, cols.map((c) => h('td', { class: c.class || '' }, c.render ? c.render(r) : r[c.key] ?? '')))))));
}
export function findings(list, emptyText = 'No findings.') {
  if (!list || !list.length) return h('p', { class: 'notice ok' }, emptyText);
  return h('div', {}, list.map((f) => h('div', { class: `finding ${f.severity}` },
    h('div', {}, badge(f.severity), ' ', h('b', {}, f.rule_key.replaceAll('_', ' ')), ' ', h('span', { class: 'meta' }, `· ${f.category}`)),
    h('div', {}, f.message),
    f.evidence && (f.evidence.expected !== undefined) ? h('div', { class: 'meta' }, `Required: ${f.evidence.expected} · Available/actual: ${f.evidence.available ?? f.evidence.actual}`) : null,
    f.evidence && f.evidence.note ? h('div', { class: 'meta' }, f.evidence.note) : null,
    f.suggestions && f.suggestions.length ? h('ul', {}, f.suggestions.map((s) => h('li', {}, s))) : null)));
}
export function download(url) { const a = h('a', { href: url, download: '' }); document.body.append(a); a.click(); a.remove(); }
export const when = (iso) => (iso ? new Date(iso).toLocaleString() : '—');
