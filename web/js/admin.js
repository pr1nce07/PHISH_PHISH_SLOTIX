// Coordinator / administrator screens.
import { DAYS, api, badge, confirmBox, dialog, download, field, findings, fmt, get, guard, h, post, select, state, table, toMin, toast, when } from './core.js';
import { timetableViewer } from './grid.js';

const A = '/api/admin';
const term = () => state.terms.find((t) => t.id === state.termId);
const need = () => (state.termId ? null : h('div', { class: 'notice' }, 'Create a department and an academic term first (Academic setup → Departments / Academic terms), then pick the term at the top.'));

// ---------------------------------------------------------------- dashboard
export async function dashboard(root) {
  if (need()) return root.append(need());
  const d = await get(`${A}/dashboard?term_id=${state.termId}`);
  const c = d.counts;
  const stat = (n, l) => h('div', { class: 'stat' }, h('b', {}, n), h('span', {}, l));
  root.append(h('div', { class: 'cards' },
    stat(d.published ? 'Published' : 'Not published', d.published ? d.published.label : 'Timetable status'),
    stat(c.sessions_required, 'Weekly sessions to schedule'), stat(d.pending_requests, 'Pending faculty requests'),
    stat(d.unresolved_conflicts, 'Unresolved conflicts in drafts'), stat(`${c.drafts} / ${c.validated}`, 'Drafts / validated'),
    stat(`${c.groups} · ${c.subjects} · ${c.faculty} · ${c.rooms}`, 'Groups · subjects · faculty · rooms')));
  root.append(h('div', { class: 'card' }, h('h2', {}, 'Data-quality check (preflight)'),
    findings(d.data_quality, 'Inputs look consistent - ready to generate.')));
  root.append(h('div', { class: 'card' }, h('h2', {}, 'Recent generation runs'), runTable(d.recent_runs)));
  if (d.analytics) root.append(h('div', { class: 'card' }, h('h2', {}, 'Published timetable - utilisation'), analyticsView(d.analytics, null)));
}

function analyticsView(a, cal) {
  const max = Math.max(1, ...a.heatmap.map((x) => x.sessions));
  const days = [...new Set(a.heatmap.map((x) => x.day_of_week))];
  const periods = [...new Set(a.heatmap.map((x) => x.period_idx))];
  const heat = h('div', { class: 'heat', style: { gridTemplateColumns: `70px repeat(${days.length}, 1fr)` } },
    h('div', {}), days.map((d) => h('div', { class: 'muted' }, DAYS[d].slice(0, 3))),
    periods.map((p) => [h('div', { class: 'muted' }, cal ? fmt(cal.periods[p].start_minute) : `P${p + 1}`),
      days.map((d) => { const n = a.heatmap.find((x) => x.day_of_week === d && x.period_idx === p).sessions;
        return h('div', { style: { background: `rgba(20,82,156,${0.1 + 0.8 * n / max})`, color: n / max > 0.5 ? '#fff' : 'var(--ink)' }, title: `${n} session(s) running` }, n); })]));
  const bar = (pct) => h('div', { class: 'bar' }, h('i', { style: { width: `${Math.min(100, pct)}%` } }));
  return h('div', {}, h('h3', {}, 'Load heatmap (sessions running per slot)'), heat,
    h('h3', {}, 'Room bottlenecks'), table([{ label: 'Room', key: 'code' }, { label: 'Type', key: 'room_type' }, { label: 'Seats', key: 'capacity' },
      { label: 'Periods used', key: 'periods_used' }, { label: 'Utilisation', render: (r) => h('div', { class: 'row' }, bar(r.utilisation), `${r.utilisation}%`) }], a.rooms),
    h('h3', {}, 'Faculty load balance'), table([{ label: 'Faculty', key: 'name' }, { label: 'Per day', render: (f) => Object.entries(f.per_day).map(([d, n]) => `${DAYS[d].slice(0, 3)} ${n}`).join(' · ') },
      { label: 'Week', render: (f) => `${f.total}${f.max_week ? ' / ' + f.max_week : ''}` }], a.faculty),
    h('h3', {}, 'Free slots per class / batch'), table([{ label: 'Group', key: 'name' }, { label: 'Busy periods', key: 'busy_periods' }, { label: 'Free periods', key: 'free_periods' }], a.groups),
    h('p', { class: 'muted small' }, `Unused seat-periods (capacity minus strength, summed): ${a.unused_seat_periods}`));
}

// ---------------------------------------------------------------- generic master data
const T = { text: 'text', num: 'number', bool: 'bool', day: 'day', time: 'time', ref: 'ref', sel: 'select', tags: 'tags', date: 'date' };
const f = (name, label, type = T.text, extra = {}) => ({ name, label, type, ...extra });
const dept = f('department_id', 'Department', 'auto_dept');
const termF = f('term_id', 'Term', 'auto_term');
const RES = {
  departments: { title: 'Departments', fields: [f('code', 'Code'), f('name', 'Name')] },
  academic_terms: { title: 'Academic terms', fields: [f('department_id', 'Department', T.ref, { res: 'departments' }), f('name', 'Name'), f('start_date', 'Start date', T.date, { opt: true }), f('end_date', 'End date', T.date, { opt: true }), f('active', 'Active', T.bool, { def: true })] },
  working_days: { title: 'Working days', byTerm: true, fields: [termF, f('day_of_week', 'Day', T.day), f('active', 'Working', T.bool, { def: true })] },
  period_slots: { title: 'Periods', byTerm: true, help: 'Official teaching periods. Consecutive periods must touch (end = next start) to host multi-period practicals.',
    fields: [termF, f('idx', 'Order no.', T.num, { def: 0 }), f('start_minute', 'Start', T.time), f('end_minute', 'End', T.time), f('label', 'Label', T.text, { opt: true })] },
  scheduled_breaks: { title: 'Breaks', byTerm: true, fields: [termF, f('name', 'Name'), f('day_of_week', 'Day (blank = every day)', T.day, { opt: true }), f('start_minute', 'Start', T.time), f('end_minute', 'End', T.time)] },
  programmes: { title: 'Programmes', fields: [dept, f('code', 'Code'), f('name', 'Name')] },
  student_groups: { title: 'Classes, sections & batches', byTerm: true, help: 'A practical batch is a group whose parent is its class. A whole-class session can never overlap a session of any of its batches.',
    fields: [termF, f('programme_id', 'Programme', T.ref, { res: 'programmes' }), f('name', 'Name'), f('semester', 'Semester', T.num, { def: 1 }), f('section', 'Section', T.text, { opt: true }),
      f('group_type', 'Type', T.sel, { options: ['cohort', 'section', 'batch'], def: 'cohort' }), f('parent_group_id', 'Parent class', T.ref, { res: 'student_groups', opt: true }), f('strength', 'Student strength', T.num)] },
  faculty_profiles: { title: 'Faculty', fields: [dept, f('code', 'Short code'), f('name', 'Name'), f('email', 'E-mail', T.text, { opt: true }), f('max_periods_per_day', 'Max periods / day', T.num, { opt: true }), f('max_periods_per_week', 'Max periods / week', T.num, { opt: true }), f('active', 'Active', T.bool, { def: true })] },
  rooms: { title: 'Classrooms & laboratories', fields: [dept, f('code', 'Room no.'), f('name', 'Name'), f('room_type', 'Type (e.g. classroom, lab)', T.text, { def: 'classroom' }), f('capacity', 'Seating capacity', T.num), f('active', 'Available', T.bool, { def: true })] },
  room_features: { title: 'Room features', help: 'Each feature row belongs to one room (e.g. computers, projector). Subjects can require feature codes.', fields: [f('room_id', 'Room', T.ref, { res: 'rooms' }), f('code', 'Feature code (lowercase_with_underscores)'), f('name', 'Description')] },
  subjects: { title: 'Subjects & weekly requirements', byTerm: true,
    help: 'One row = a subject offering for one group. Put whole-class theory on the class offering, and add one practical offering per batch (each batch then gets its own practical sessions). Assign faculty under "Teaching assignments".',
    fields: [dept, termF, f('student_group_id', 'Class / batch', T.ref, { res: 'student_groups' }), f('code', 'Subject code'), f('name', 'Subject name'), f('subject_category', 'Category', T.text, { def: 'core' }),
      f('theory_sessions_per_week', 'Theory sessions / week', T.num, { def: 0 }), f('theory_periods_per_session', 'Periods per theory session', T.num, { def: 1 }), f('theory_required_room_type', 'Theory room type', T.text, { def: 'classroom', opt: true }), f('theory_required_features', 'Theory required features', T.tags),
      f('practical_sessions_per_week', 'Practical sessions / week', T.num, { def: 0 }), f('practical_periods_per_session', 'Consecutive periods per practical', T.num, { def: 2 }), f('practical_required_room_type', 'Laboratory type', T.text, { def: 'lab', opt: true }), f('practical_required_features', 'Lab required features', T.tags),
      f('active', 'Active', T.bool, { def: true })] },
  teaching_assignments: { title: 'Teaching assignments', termVia: 'subject_id', fields: [f('subject_id', 'Subject offering', T.ref, { res: 'subjects' }), f('faculty_id', 'Faculty', T.ref, { res: 'faculty_profiles' }), f('session_type', 'Session type', T.sel, { options: ['theory', 'practical'], def: 'theory' })] },
  faculty_availability: { title: 'Faculty availability', byTerm: true,
    help: '"available": may teach only inside these windows (none = all working hours). "unavailable": never. "preferred": a soft preference.',
    fields: [termF, f('faculty_id', 'Faculty', T.ref, { res: 'faculty_profiles' }), f('day_of_week', 'Day', T.day), f('start_minute', 'From', T.time), f('end_minute', 'To', T.time), f('kind', 'Kind', T.sel, { options: ['available', 'unavailable', 'preferred'], def: 'available' }), f('note', 'Note (private)', T.text, { opt: true })] },
  fixed_commitments: { title: 'Fixed commitments', byTerm: true, help: 'With a teaching assignment: one session of it is pinned to this slot (and room). Without: the faculty member is simply busy.',
    fields: [termF, f('faculty_id', 'Faculty', T.ref, { res: 'faculty_profiles' }), f('title', 'Title'), f('teaching_assignment_id', 'Pinned teaching session', T.ref, { res: 'teaching_assignments', opt: true }), f('room_id', 'Room', T.ref, { res: 'rooms', opt: true }), f('day_of_week', 'Day', T.day), f('start_minute', 'From', T.time), f('end_minute', 'To', T.time)] },
};
const LOOK = ['departments', 'programmes', 'student_groups', 'faculty_profiles', 'rooms', 'subjects', 'teaching_assignments'];
let look = {};
async function loadLookups() {
  const res = await Promise.all(LOOK.map((r) => get(`${A}/data/${r}${RES[r].byTerm && state.termId ? `?term_id=${state.termId}` : ''}`)));
  look = Object.fromEntries(LOOK.map((r, i) => [r, res[i]]));
  const subj = new Set(look.subjects.map((s) => s.id));
  if (state.termId) look.teaching_assignments = look.teaching_assignments.filter((a) => subj.has(a.subject_id));
  const d = term() && term().department_id;
  if (d) for (const r of ['programmes', 'faculty_profiles', 'rooms']) look[r] = look[r].filter((x) => x.department_id === d);
}
const by = (res, id) => (look[res] || []).find((x) => x.id === id);
function refLabel(res, id) {
  const o = by(res, id);
  if (!o) return id == null ? '—' : `#${id}`;
  if (res === 'subjects') return `${o.code} — ${refLabel('student_groups', o.student_group_id)}`;
  if (res === 'teaching_assignments') return `${refLabel('subjects', o.subject_id)} · ${o.session_type} · ${refLabel('faculty_profiles', o.faculty_id)}`;
  return o.name && res !== 'rooms' ? (res === 'faculty_profiles' ? `${o.name} (${o.code})` : o.name) : o.code;
}
function show(fd, v) {
  if (v === null || v === undefined || v === '') return '—';
  switch (fd.type) {
    case T.bool: return v ? 'Yes' : 'No';
    case T.day: return DAYS[v];
    case T.time: return fmt(v);
    case T.ref: return refLabel(fd.res, v);
    case T.tags: return v.join(', ') || '—';
    default: return String(v);
  }
}
function input(fd, v) {
  const val = v ?? fd.def ?? '';
  switch (fd.type) {
    case T.bool: return h('input', { type: 'checkbox', checked: v === undefined ? fd.def !== false : !!v });
    case T.num: return h('input', { type: 'number', value: val, min: 0 });
    case T.day: return select([...(fd.opt ? [[null, '—']] : []), ...DAYS.map((d, i) => [i, d])], v ?? (fd.opt ? null : 0));
    case T.time: return h('input', { type: 'time', value: v == null ? '' : fmt(v), required: true });
    case T.date: return h('input', { type: 'date', value: val });
    case T.sel: return select(fd.options.map((o) => [o, o]), val);
    case T.ref: return select([...(fd.opt ? [[null, '—']] : []), ...(look[fd.res] || []).map((o) => [o.id, refLabel(fd.res, o.id)])], v ?? null);
    case T.tags: return h('input', { type: 'text', value: (v || []).join(', '), placeholder: 'comma separated codes' });
    default: return h('input', { type: 'text', value: val, maxLength: 160 });
  }
}
function read(fd, el) {
  switch (fd.type) {
    case T.bool: return el.checked;
    case T.num: return el.value === '' ? null : Number(el.value);
    case T.day: case T.ref: return el.value === '' ? null : Number(el.value);
    case T.time: return el.value ? toMin(el.value) : null;
    case T.tags: return el.value.split(',').map((x) => x.trim()).filter(Boolean);
    default: return el.value.trim() === '' ? (fd.opt ? null : '') : el.value.trim();
  }
}

export async function setup(root, sub) {
  const name = RES[sub] ? sub : 'academic_terms';
  const def = RES[name];
  await loadLookups();
  const prefsView = sub === 'faculty_preferences';
  const nav = h('div', { class: 'card sub' }, Object.entries(RES).map(([k, r]) => h('a', { href: `#/setup/${k}`, class: k === name && !prefsView ? 'active' : '' }, r.title)),
    h('a', { href: '#/setup/faculty_preferences', class: prefsView ? 'active' : '' }, 'Faculty preferences (submitted)'));
  const pane = h('div', {});
  root.append(h('div', { class: 'split' }, nav, pane));
  if (prefsView) return need() ? pane.append(need()) : facultyPreferences(pane);
  if ((def.byTerm || def.termVia || def.fields.includes(dept)) && need()) return pane.append(need());
  const visible = def.fields.filter((x) => !x.type.startsWith('auto_'));
  const auto = () => Object.fromEntries(def.fields.filter((x) => x.type.startsWith('auto_')).map((x) => [x.name, x.type === 'auto_term' ? state.termId : term().department_id]));

  async function load() {
    let rows = await get(`${A}/data/${name}${def.byTerm ? `?term_id=${state.termId}` : ''}`);
    if (def.termVia) rows = rows.filter((r) => by('subjects', r[def.termVia]));
    if (def.fields.includes(dept) && !def.byTerm) rows = rows.filter((r) => r.department_id === term().department_id);
    if (name === 'room_features') rows = rows.filter((r) => by('rooms', r.room_id));
    return rows;
  }
  function form(rowData) {
    const els = Object.fromEntries(visible.map((x) => [x.name, input(x, rowData ? rowData[x.name] : undefined)]));
    const body = h('div', { class: 'form' }, visible.map((x) => field(x.label, els[x.name])));
    dialog(`${rowData ? 'Edit' : 'Add'} · ${def.title}`, body, [{ label: 'Save', class: 'primary', action: async () => {
      const data = { ...auto(), ...Object.fromEntries(visible.map((x) => [x.name, read(x, els[x.name])])) };
      try { await api(rowData ? 'PUT' : 'POST', `${A}/data/${name}${rowData ? '/' + rowData.id : ''}`, data); } catch (e) { toast(e.message, 'err'); return false; }
      toast('Saved', 'ok'); if (name === 'academic_terms') await state.reloadTerms(); render(); return true;
    } }]);
  }
  function importer() {
    const ta = h('textarea', { placeholder: `Header row with: ${def.fields.map((x) => x.name).join(', ')}`, rows: 8 });
    const res = h('div', {});
    const run = async (dry) => {
      const r = await guard(() => post(`${A}/data/${name}/import`, { csv: ta.value, dry_run: dry }));
      if (!r) return false;
      res.replaceChildren(h('p', { class: r.errors.length ? 'notice err' : 'notice ok' }, `${r.valid} of ${r.rows} row(s) valid. ${r.imported ? r.imported + ' imported.' : dry ? 'Nothing saved yet (preview).' : ''}`),
        r.errors.map((e) => h('div', { class: 'finding error' }, `Row ${e.row}: ${e.errors.join('; ')}`)));
      if (r.imported) render();
      return false;
    };
    dialog(`Import CSV · ${def.title}`, h('div', { class: 'col' }, h('p', { class: 'help' }, 'Paste CSV text. Every row is validated on the server; references use numeric ids; lists use ";". Cells starting with = + - @ are rejected. Nothing is saved unless all rows are valid.'), ta, res),
      [{ label: 'Preview / validate', action: () => run(true), keep: true }, { label: 'Import', class: 'primary', action: () => run(false), keep: true }]);
  }
  async function render() {
    await loadLookups();
    const rows = await load();
    pane.replaceChildren(h('div', { class: 'card' },
      h('div', { class: 'row' }, h('h2', { style: { margin: 0 } }, def.title), h('span', { class: 'muted' }, `${rows.length} record(s)`),
        h('button', { class: 'primary right', onclick: () => form(null) }, '+ Add'), h('button', { onclick: importer }, 'Import CSV'),
        h('button', { onclick: () => download(`${A}/data/${name}/export.csv`) }, 'Export CSV')),
      def.help ? h('p', { class: 'help gap' }, def.help) : null,
      h('div', { class: 'gap' }, table([...visible.map((x) => ({ label: x.label, render: (r) => show(x, r[x.name]) })),
        { label: '', class: 'nowrap', render: (r) => [h('button', { class: 'sm', onclick: () => form(r) }, 'Edit'), ' ',
          h('button', { class: 'sm danger', onclick: () => confirmBox('Delete record', 'Delete this record? Records used by a timetable cannot be deleted.', 'Delete', async () => { if (await guard(() => api('DELETE', `${A}/data/${name}/${r.id}`), 'Deleted') !== undefined) { if (name === 'academic_terms') await state.reloadTerms(); render(); } }) }, 'Delete')] }], rows))));
  }
  render();
}

// What faculty submitted under "My preferences" - read-only here; only the faculty member can change them.
async function facultyPreferences(pane) {
  const rows = await get(`${A}/faculty-preferences?term_id=${state.termId}`);
  const strength = { low: ['Low', ''], normal: ['Normal', 'acc'], high: ['High', 'error'] };
  const slot = (k) => { const [d, s] = k.split('-').map(Number); return `${DAYS[d].slice(0, 3)} ${fmt(s)}`; };
  pane.append(h('div', { class: 'card' }, h('div', { class: 'sec-head' }, h('h2', {}, 'Faculty preferences'), h('p', {}, `${rows.length} submitted`)),
    h('p', { class: 'help' }, 'Submitted by faculty under "My preferences". They are soft constraints: the solver weighs them on the next generation through the rule "Faculty self-service preferences" (Rules lab), and a mandatory rule always wins.'),
    h('div', { class: 'gap' }, table([{ label: 'Faculty', render: (r) => h('b', {}, r.faculty_name) },
      { label: 'Priority', render: (r) => badge(strength[r.strength][0], strength[r.strength][1]) },
      { label: 'Preferred slots', render: (r) => Object.keys(r.marks).filter((k) => r.marks[k] === 'prefer').map(slot).join(', ') || '—' },
      { label: 'Avoid slots', render: (r) => Object.keys(r.marks).filter((k) => r.marks[k] === 'avoid').map(slot).join(', ') || '—' },
      { label: 'Standing requests', render: (r) => [r.avoid_first ? 'no first period' : '', r.avoid_last ? 'no last period' : '', r.spread_evenly ? 'spread the week' : ''].filter(Boolean).join(' · ') || '—' },
      { label: 'Note', render: (r) => r.notes || '—' },
      { label: 'Honoured in published', render: (r) => (r.report ? badge(`${r.report.honoured_pct}%`, r.report.honoured_pct === 100 ? 'ok' : 'warning') : '—') },
      { label: 'Submitted', class: 'nowrap', render: (r) => when(r.submitted_at) }], rows, 'No faculty member has submitted preferences for this term yet.'))));
}

// ---------------------------------------------------------------- rules
export async function rules(root) {
  const [reg, sets] = await Promise.all([get(`${A}/rules/registry`), get(`${A}/rule-sets`)]);
  let cur = sets.find((s) => s.id === state.ruleSetId) || sets[0];
  const pane = h('div', {});
  function draw() {
    state.ruleSetId = cur.id;
    const spec = Object.fromEntries(reg.rules.map((r) => [r.rule_key, r]));
    pane.replaceChildren(
      h('div', { class: 'card' }, h('div', { class: 'row' }, h('h2', { style: { margin: 0 } }, 'Rule set'),
        select(sets.map((s) => [s.id, `${s.name} (v${s.version})`]), cur.id, { onchange: (e) => { cur = sets.find((s) => s.id === Number(e.target.value)); draw(); } }),
        h('button', { class: 'right', onclick: () => { const n = h('input', { type: 'text', placeholder: 'Name of the new rule set' });
          dialog('New rule set', field('Name (copies the current settings)', n), [{ label: 'Create', class: 'primary', action: async () => { const r = await guard(() => post(`${A}/rule-sets`, { name: n.value, copy_from: cur.id }), 'Rule set created'); if (!r) return false; sets.push(r); cur = r; draw(); return true; } }]); } }, '+ New rule set')),
      h('p', { class: 'help gap' }, 'Every change creates a new rule-set version; each generation run records the version it used.')),
      h('div', { class: 'card' }, h('h2', {}, 'Mandatory constraints (always enforced, cannot be disabled or weighted)'),
        table([{ label: 'Rule', render: (r) => h('b', {}, r.rule_key.replaceAll('_', ' ')) }, { label: 'Meaning', key: 'description' }, { label: '', render: () => badge('hard · locked', 'ok') }], reg.core_hard)),
      h('div', { class: 'card' }, h('h2', {}, 'Configurable rules and preferences'),
        h('p', { class: 'help' }, 'Soft rules only rank valid timetables: penalty = raw count × weight, lower total is better. A hard rule must never be broken. Rules are configured only through these validated fields - no formulas or code.'),
        table([{ label: 'Rule', render: (r) => [h('b', {}, spec[r.rule_key].name), h('div', { class: 'help' }, spec[r.rule_key].description)] },
          { label: 'Enabled', render: (r) => (r._en = h('input', { type: 'checkbox', checked: r.enabled, 'aria-label': 'enabled' })) },
          { label: 'Mode', render: (r) => (r._hard = select(spec[r.rule_key].modes.map((m) => [m, m === 'hard' ? 'Hard (mandatory)' : 'Soft (preference)']), r.is_hard ? 'hard' : 'soft', { disabled: spec[r.rule_key].modes.length < 2 })) },
          { label: 'Weight', render: (r) => (r._w = h('input', { type: 'number', min: 0, max: 1000, value: r.weight, style: { width: '80px' }, title: 'Penalty per violation (soft rules)' })) },
          { label: 'Parameters', render: (r) => { r._p = {}; return Object.entries(spec[r.rule_key].params).map(([k, p]) => field(`${k}${p.help ? ' — ' + p.help : ''}`, (r._p[k] = h('input', { type: 'number', min: p.min, max: p.max, value: r.params[k] ?? '', style: { width: '90px' } })))); } },
          { label: '', render: (r) => h('button', { class: 'sm primary', onclick: async () => {
            const params = Object.fromEntries(Object.entries(r._p).map(([k, el]) => [k, el.value === '' ? null : Number(el.value)]));
            const out = await guard(() => api('PUT', `${A}/rule-sets/${cur.id}/rules/${r.rule_key}`, { enabled: r._en.checked, is_hard: r._hard.value === 'hard', weight: Number(r._w.value), params }), 'Rule saved');
            if (out) { sets[sets.indexOf(cur)] = out; cur = out; draw(); } } }, 'Save') }], cur.rules)));
  }
  root.append(pane); draw();
}

// ---------------------------------------------------------------- generation
function runTable(runs, onOpen) {
  return table([{ label: '#', key: 'id' }, { label: 'Kind', key: 'kind' }, { label: 'Status', render: (r) => badge(r.status) },
    { label: 'Solver', render: (r) => r.solver_status || '—' }, { label: 'Elapsed', render: (r) => `${r.elapsed_seconds}s` },
    { label: 'Result', render: (r) => (r.summary && r.summary.message) || '—' }, { label: 'Started', render: (r) => when(r.created_at) },
    { label: '', render: (r) => (onOpen ? h('button', { class: 'sm', onclick: () => onOpen(r.id) }, 'Details') : h('a', { href: '#/generate' }, 'Open')) }], runs, 'No runs yet.');
}

export async function generate(root) {
  if (need()) return root.append(need());
  const [sets, versions] = await Promise.all([get(`${A}/rule-sets`), get(`${A}/versions?term_id=${state.termId}`)]);
  const rs = select(sets.map((s) => [s.id, `${s.name} (v${s.version})`]), state.ruleSetId || sets[0].id);
  const limit = h('input', { type: 'number', min: 1, max: 300, value: 20 });
  const cands = select([[1, '1 timetable'], [2, 'up to 2 alternatives'], [3, 'up to 3 alternatives']], 3);
  const base = select([[null, '— fresh generation —'], ...versions.filter((v) => !v.is_sandbox).map((v) => [v.id, `#${v.id} ${v.label} (${v.status})`])], null);
  const pre = h('div', {}), detail = h('div', {}), list = h('div', {});
  let timer = null;
  const stop = () => { if (timer) clearTimeout(timer); timer = null; };
  state.cleanup = stop;

  async function showRun(id) {
    stop();
    const r = await guard(() => get(`${A}/runs/${id}`));
    if (!r) return;
    const live = ['queued', 'running'].includes(r.status);
    detail.replaceChildren(h('div', { class: 'card' },
      h('div', { class: 'row' }, h('h2', { style: { margin: 0 } }, `Run #${r.id}`), badge(r.status), h('span', { class: 'muted' }, `solver: ${r.solver_status || '—'} · elapsed ${r.elapsed_seconds}s`),
        live ? h('button', { class: 'right danger', onclick: () => guard(() => post(`${A}/runs/${id}/cancel`), 'Cancellation requested') }, 'Cancel run') : null),
      live ? h('p', { class: 'notice gap' }, r.status === 'queued' ? 'Waiting in the queue…' : 'The solver is searching… (the solver reports no percentage, so none is shown)') : null,
      r.summary.message ? h('p', { class: `notice gap ${r.status === 'completed' ? 'ok' : r.status === 'unknown' ? '' : 'err'}` }, r.summary.message) : null,
      r.versions.length ? [h('h3', {}, 'Candidate timetables'), table([{ label: 'Version', render: (v) => `#${v.id} ${v.label}` }, { label: 'Soft score (lower = better)', key: 'score' },
        { label: 'Optimality', render: (v) => v.summary.optimality || '—' }, { label: '', render: (v) => h('a', { href: `#/timetable/${v.id}` }, 'Open in editor') }], r.versions),
      r.versions.length > 1 ? h('p', {}, h('a', { href: `#/compare/${r.versions.map((v) => v.id).join(',')}` }, 'Compare candidates side by side →')) : null] : null,
      h('h3', {}, 'Findings'), findings(r.findings, 'No findings - all inputs were consistent.')));
    if (live) timer = setTimeout(() => showRun(id), 1500); else refresh();
  }
  async function refresh() { list.replaceChildren(runTable(await get(`${A}/runs?term_id=${state.termId}`), showRun)); }

  root.append(h('div', { class: 'card' }, h('h2', {}, 'Generate a timetable'),
    h('div', { class: 'form' }, field('Rule set', rs), field('Time limit (seconds)', limit, 'The search stops here; a timeout is not a proof of impossibility.'), field('Alternatives', cands),
      field('Keep locked sessions from', base, 'Locked sessions of this version stay fixed; everything else is rescheduled.')),
    h('div', { class: 'row gap' },
      h('button', { onclick: async () => { const r = await guard(() => post(`${A}/preflight`, { term_id: state.termId, rule_set_id: Number(rs.value) }));
        if (r) pre.replaceChildren(h('h3', {}, `Preflight: ${r.ok ? 'no blocking problems' : 'blocking problems found'} · ${r.sessions_required} session(s) required`), findings(r.findings, 'All checks passed.')); } }, '1 · Run preflight validation'),
      h('button', { class: 'primary', onclick: async () => { const r = await guard(() => post(`${A}/runs`, { term_id: state.termId, rule_set_id: Number(rs.value), time_limit: Number(limit.value), candidates: Number(cands.value),
        base_version_id: base.value ? Number(base.value) : null }), 'Generation started'); if (r) { refresh(); showRun(r.id); } } }, '2 · Generate')), pre), detail,
  h('div', { class: 'card' }, h('h2', {}, 'Runs'), list));
  refresh();
}

// ---------------------------------------------------------------- compare
export async function compare(root, ids) {
  const d = await get(`${A}/versions/compare?ids=${encodeURIComponent(ids || '')}`);
  const keys = [...new Set(d.candidates.flatMap((c) => c.rules.map((r) => r.rule_key)))];
  const name = (k) => (d.candidates.flatMap((c) => c.rules).find((r) => r.rule_key === k) || {}).name || k;
  const col = (label, fn) => h('tr', {}, h('th', {}, label), d.candidates.map((c) => h('td', {}, fn(c))));
  root.append(h('div', { class: 'card' }, h('h2', {}, 'Candidate comparison'), h('p', { class: 'help' }, d.note),
    h('div', { class: 'tablewrap' }, h('table', {}, h('tbody', {},
      col('Version', (c) => [h('a', { href: `#/timetable/${c.version.id}` }, `#${c.version.id} ${c.version.label}`), c.version.id === d.best_version_id ? [' ', badge('best score', 'ok')] : null]),
      col('Hard-constraint audit', (c) => badge(c.valid ? 'valid' : `${c.hard_errors} violation(s)`, c.valid ? 'ok' : 'error')),
      col('Solver result', (c) => c.version.summary.optimality || '—'),
      col('Soft score (total penalty)', (c) => h('b', {}, c.score)),
      keys.map((k) => col(`· ${name(k)}`, (c) => { const r = c.rules.find((x) => x.rule_key === k); return r ? `${r.penalty} (${r.raw} × ${r.weight})` : '—'; })),
      col('Sessions placed differently vs first', (c) => c.moved_vs_first), col('Average room utilisation', (c) => `${c.avg_room_utilisation}%`),
      col('Busiest faculty day (periods)', (c) => c.max_faculty_day), col('Unused seat-periods', (c) => c.unused_seat_periods))))));
}

// ---------------------------------------------------------------- timetable editor
export async function timetable(root, id) {
  if (need()) return root.append(need());
  const versions = await get(`${A}/versions?term_id=${state.termId}`);
  if (!versions.length) return root.append(h('div', { class: 'notice' }, 'No timetable versions yet. Generate one first.'));
  let vid = Number(id) || (versions.find((v) => v.status === 'draft' && !v.is_sandbox) || versions[0]).id;
  const pane = h('div', {});
  root.append(pane);
  let viewState = {};

  async function load(payload) {
    const d = payload || await guard(() => get(`${A}/versions/${vid}`));
    if (!d) return;
    const v = d.version, editable = ['draft', 'validated'].includes(v.status);
    const errs = d.findings.filter((x) => x.severity === 'error');
    const act = async (fn, msg) => { const r = await guard(fn, msg); if (r && r.version) load(r); else if (r !== undefined) load(); };
    const viewer = timetableViewer(d, { ...viewState, editable, onClick: (s) => sessionDialog(d, s, editable, load),
      onDrop: (sid, day, start) => { const s = d.sessions.find((x) => x.id === sid); if (s) act(() => api('PATCH', `${A}/sessions/${sid}`, { day_of_week: day, start_minute: start, room_id: s.room_id, row_version: v.row_version }), 'Session moved'); } });
    const tab = h('div', {}, viewer);
    pane.replaceChildren(...[
      h('div', { class: 'card noprint' }, h('div', { class: 'row' },
        select(versions.map((x) => [x.id, `#${x.id} ${x.label} · ${x.status}${x.is_sandbox ? ' · sandbox' : ''}`]), vid, { onchange: (e) => { location.hash = `#/timetable/${e.target.value}`; } }),
        badge(v.status), v.is_sandbox ? badge('what-if sandbox', 'warning') : null,
        h('span', { class: 'muted' }, `${d.sessions.length} sessions · soft score ${v.score ?? '—'} · ${errs.length} hard violation(s)`)),
      h('div', { class: 'row gap' },
        editable ? h('button', { class: 'primary', onclick: () => act(() => post(`${A}/versions/${vid}/validate`), 'Validation finished') }, 'Validate') : null,
        v.status === 'validated' && !v.is_sandbox ? h('button', { class: 'primary', onclick: () => confirmBox('Publish timetable', 'Publish this validated timetable? The currently published version is archived (history is kept) and faculty/students are notified.', 'Publish', () => act(() => post(`${A}/versions/${vid}/publish`, { row_version: v.row_version }), 'Timetable published'), 'primary') }, 'Publish') : null,
        h('button', { onclick: async () => { const r = await guard(() => post(`${A}/versions/${vid}/clone`, { sandbox: false }), 'Draft copy created'); if (r) location.hash = `#/timetable/${r.version.id}`; } }, v.status === 'draft' ? 'Duplicate' : 'Restore as new draft'),
        editable ? h('button', { onclick: () => act(() => post(`${A}/versions/${vid}/lock-all`, { locked: true, row_version: v.row_version }), 'All sessions locked') }, 'Lock all') : null,
        editable ? h('button', { onclick: () => act(() => post(`${A}/versions/${vid}/lock-all`, { locked: false, row_version: v.row_version }), 'All sessions unlocked') }, 'Unlock all') : null,
        !v.is_sandbox ? h('button', { onclick: async () => { const r = await guard(() => post(`${A}/runs`, { term_id: state.termId, kind: 'generate', base_version_id: vid, time_limit: 20 }), 'Regeneration started (locked sessions stay)'); if (r) location.hash = '#/generate'; } }, 'Regenerate keeping locks') : null,
        !v.is_sandbox ? h('button', { onclick: async () => { const r = await guard(() => post(`${A}/runs`, { term_id: state.termId, kind: 'repair', base_version_id: vid, time_limit: 20 }), 'Minimum-change repair started'); if (r) location.hash = '#/generate'; } , title: 'Fix this timetable against the current inputs while moving as few sessions as possible' }, 'Repair (minimal change)') : null,
        v.status === 'published' ? h('button', { class: 'danger', onclick: () => confirmBox('Archive', 'Archive the published timetable? Students and faculty will no longer see it.', 'Archive', () => act(() => post(`${A}/versions/${vid}/archive`), 'Archived')) }, 'Archive') : null,
        editable ? h('button', { class: 'danger', onclick: () => confirmBox('Delete draft', 'Delete this draft version?', 'Delete', async () => { if (await guard(() => api('DELETE', `${A}/versions/${vid}`), 'Deleted') !== undefined) location.hash = '#/timetable'; }) }, 'Delete') : null,
        h('button', { class: 'right', onclick: () => download(`${A}/versions/${vid}/export.csv`) }, 'Export CSV'), h('button', { onclick: () => window.print() }, 'Print')),
      editable ? h('p', { class: 'help gap' }, 'Click a session to inspect, move or lock it, or drag it onto another slot. Every edit is validated on the server before it is saved.') : h('p', { class: 'help gap' }, 'This version is immutable. Use "Restore as new draft" to change it.')),
      v.summary.diff ? h('div', { class: 'card noprint' }, h('h2', {}, `Changes versus base version #${v.base_version_id} (${v.summary.diff.length} session(s))`),
        (v.summary.scenario_notes || []).length ? h('p', { class: 'help' }, `Scenario: ${v.summary.scenario_notes.join('; ')}`) : null,
        table([{ label: 'Session', key: 'session' }, { label: 'Change', render: (x) => badge(x.change, 'warning') }, { label: 'Before', key: 'before' }, { label: 'After', key: 'after' }], v.summary.diff, 'Nothing had to move.')) : null,
      h('div', { class: 'tabs noprint' }, ['Timetable', 'Findings', 'Score', 'Analytics'].map((t, i) => h('button', { class: i === 0 ? 'active' : '', onclick: async (e) => {
        e.target.parentNode.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === e.target));
        if (i === 0) tab.replaceChildren(viewer);
        if (i === 1) tab.replaceChildren(h('p', { class: 'help' }, 'Result of the independent audit of the saved sessions.'), findings(d.findings, 'The independent audit found no hard-constraint violation.'));
        if (i === 2) tab.replaceChildren(h('p', { class: 'help' }, 'Soft penalties only rank valid timetables. Hard violations are listed under Findings and invalidate a timetable regardless of score.'),
          table([{ label: 'Rule', key: 'name' }, { label: 'Violations (raw)', key: 'raw' }, { label: 'Weight', key: 'weight' }, { label: 'Penalty', key: 'penalty' }], (v.score_breakdown.rules || [])), h('p', {}, h('b', {}, `Total: ${v.score ?? 0}`)));
        if (i === 3) { const a = await guard(() => get(`${A}/versions/${vid}/analytics`)); if (a) tab.replaceChildren(analyticsView(a, d.calendar)); }
      } }, t + (i === 1 && errs.length ? ` (${errs.length})` : '')))),
      h('div', { class: 'card' }, h('h2', {}, `${term().name} — ${v.label}`), tab)].filter(Boolean));
  }
  load();
}

function sessionDialog(d, s, editable, reload) {
  const v = d.version;
  const body = h('div', { class: 'col' });
  const info = h('div', {}, h('p', {}, h('b', {}, `${s.subject_code} — ${s.subject_name}`), ' ', badge(s.session_type, 'ok'), s.locked ? [' ', badge('locked', 'warning')] : null),
    h('p', {}, `${s.group_name} · ${s.faculty_name} · room ${s.room_code}`), h('p', {}, `${DAYS[s.day_of_week]} ${fmt(s.start_minute)}–${fmt(s.end_minute)} (${s.period_count} period(s)) · source: ${s.source}`));
  const extra = h('div', {});
  body.append(info, extra);
  let dlg;
  const done = (r, msg) => { if (r) { toast(msg, 'ok'); dlg.close(); reload(r); } };
  const explain = async () => {
    const e = await guard(() => get(`${A}/sessions/${s.id}/explain`));
    if (!e) return false;
    extra.replaceChildren(h('h3', {}, 'Why is this session here?'),
      h('ul', {}, e.hard.map((x) => h('li', {}, x.ok ? '✔ ' : '✘ ', x.text))),
      e.violations.length ? findings(e.violations.map((m) => ({ severity: 'error', rule_key: 'violation', category: 'audit', message: m }))) : null,
      h('h3', {}, 'Preference cost of this placement'), e.soft.length ? table([{ label: 'Rule', render: (x) => x.rule_key.replaceAll('_', ' ') }, { label: 'Raw', key: 'raw' }, { label: 'Weight', key: 'weight' }, { label: 'Penalty', key: 'penalty' }], e.soft) : h('p', { class: 'muted' }, 'No soft penalty from this placement alone.'),
      h('p', { class: 'help' }, e.note));
    return false;
  };
  const options = async () => {
    const o = await guard(() => get(`${A}/sessions/${s.id}/options`));
    if (!o) return false;
    const rooms = Object.fromEntries(o.rooms.map((r) => [r.id, r]));
    const why = h('div', {});
    extra.replaceChildren(h('h3', {}, editable && !s.locked ? 'Move to… (green = valid slot)' : 'Where could this session go?'),
      h('div', { class: 'opt' }, o.options.map((x) => h('button', { class: `sm ${x.ok ? '' : 'bad'}`, disabled: x.current, onclick: () => {
        if (!x.ok) return why.replaceChildren(h('h3', {}, `Why not ${DAYS[x.day_of_week]} ${fmt(x.start_minute)}?`), h('ul', {}, x.reasons.map((r) => h('li', {}, r))));
        const room = select(x.room_ids.map((r) => [r, `${rooms[r].code} (${rooms[r].capacity} seats)`]), x.room_ids.includes(s.room_id) ? s.room_id : x.room_ids[0]);
        why.replaceChildren(h('h3', {}, `${DAYS[x.day_of_week]} ${fmt(x.start_minute)}–${fmt(x.end_minute)}`), h('div', { class: 'row' }, field('Free suitable room', room),
          editable && !s.locked ? h('button', { class: 'primary', onclick: async () => done(await guard(() => api('PATCH', `${A}/sessions/${s.id}`, { day_of_week: x.day_of_week, start_minute: x.start_minute, room_id: Number(room.value), row_version: v.row_version })), 'Session moved') }, 'Move here') : null));
        return undefined;
      } }, `${x.ok ? '✔' : '✘'} ${DAYS[x.day_of_week].slice(0, 3)} ${fmt(x.start_minute)}${x.current ? ' (now)' : ''}`))), why);
    return false;
  };
  const buttons = [{ label: 'Why here?', action: explain, keep: true }, { label: editable && !s.locked ? 'Move / why not elsewhere?' : 'Why not elsewhere?', action: options, keep: true }];
  if (editable) {
    buttons.push({ label: s.locked ? 'Unlock' : 'Lock', action: async () => { done(await guard(() => post(`${A}/sessions/${s.id}/lock`, { locked: !s.locked, row_version: v.row_version })), s.locked ? 'Unlocked' : 'Locked'); return false; }, keep: true });
    if (!s.locked) buttons.push({ label: 'Remove', class: 'danger', action: async () => { done(await guard(() => api('DELETE', `${A}/sessions/${s.id}?row_version=${v.row_version}`)), 'Session removed - the draft is now incomplete'); return false; }, keep: true });
  }
  dlg = dialog('Session details', body, buttons);
}

// ---------------------------------------------------------------- what-if lab
export async function whatif(root) {
  if (need()) return root.append(need());
  await loadLookups();
  const versions = (await get(`${A}/versions?term_id=${state.termId}`)).filter((v) => !v.is_sandbox);
  if (!versions.length) return root.append(h('div', { class: 'notice' }, 'Generate a timetable first - the what-if lab simulates changes against an existing version.'));
  const pub = versions.find((v) => v.status === 'published') || versions[0];
  const base = select(versions.map((v) => [v.id, `#${v.id} ${v.label} (${v.status})`]), pub.id);
  const changes = [];
  const listEl = h('div', {}), out = h('div', {});
  const kinds = {
    faculty_unavailable: { label: 'Faculty member unavailable', build: () => { const a = select(look.faculty_profiles.map((x) => [x.id, x.name])), b = select([[null, 'All days'], ...DAYS.map((d, i) => [i, d])], null);
      return { els: [field('Faculty', a), field('Day', b)], read: () => ({ faculty_id: Number(a.value), ...(b.value === '' ? {} : { day: Number(b.value) }) }), text: () => `${a.selectedOptions[0].text} unavailable (${b.selectedOptions[0].text})` }; } },
    room_unavailable: { label: 'Room / lab unavailable', build: () => { const a = select(look.rooms.map((x) => [x.id, x.code]));
      return { els: [field('Room', a)], read: () => ({ room_id: Number(a.value) }), text: () => `Room ${a.selectedOptions[0].text} unavailable` }; } },
    strength_change: { label: 'Student strength changes', build: () => { const a = select(look.student_groups.map((x) => [x.id, `${x.name} (${x.strength})`])), b = h('input', { type: 'number', min: 1, value: 30 });
      return { els: [field('Group', a), field('New strength', b)], read: () => ({ group_id: Number(a.value), strength: Number(b.value) }), text: () => `${a.selectedOptions[0].text} → ${b.value} students` }; } },
    extra_sessions: { label: 'More / fewer weekly sessions', build: () => { const a = select(look.subjects.map((x) => [x.id, refLabel('subjects', x.id)])), b = select([['theory', 'theory'], ['practical', 'practical']], 'theory'), c = h('input', { type: 'number', min: -10, max: 10, value: 1 });
      return { els: [field('Subject offering', a), field('Type', b), field('Change (+/-)', c)], read: () => ({ subject_id: Number(a.value), session_type: b.value, delta: Number(c.value) }), text: () => `${a.selectedOptions[0].text}: ${c.value > 0 ? '+' : ''}${c.value} ${b.value} session(s)` }; } },
    rule_override: { label: 'Change a preference weight', build: () => { const a = select(['spread_subject_days', 'student_gaps', 'faculty_gaps', 'balance_faculty_load', 'faculty_preferred_slots', 'room_stability', 'minimal_change'].map((k) => [k, k.replaceAll('_', ' ')])), b = h('input', { type: 'number', min: 0, max: 1000, value: 50 });
      return { els: [field('Soft rule', a), field('New weight', b)], read: () => ({ rule_key: a.value, enabled: true, is_hard: false, weight: Number(b.value), params: {} }), text: () => `${a.selectedOptions[0].text} weight → ${b.value}` }; } },
  };
  const kind = select(Object.entries(kinds).map(([k, v]) => [k, v.label]), 'faculty_unavailable');
  const builder = h('div', { class: 'row' });
  let cur;
  const rebuild = () => { cur = kinds[kind.value].build(); builder.replaceChildren(...cur.els, h('button', { onclick: () => { changes.push({ type: kind.value, ...cur.read(), _text: cur.text() }); drawList(); } }, '+ Add to scenario')); };
  kind.addEventListener('change', rebuild);
  const drawList = () => listEl.replaceChildren(changes.length ? h('ul', {}, changes.map((c, i) => h('li', {}, c._text, ' ', h('button', { class: 'link', onclick: () => { changes.splice(i, 1); drawList(); } }, 'remove')))) : h('p', { class: 'muted' }, 'No changes added yet.'));
  let timer;
  state.cleanup = () => clearTimeout(timer);
  async function poll(id) {
    const r = await guard(() => get(`${A}/runs/${id}`));
    if (!r) return;
    if (['queued', 'running'].includes(r.status)) { out.replaceChildren(h('p', { class: 'notice' }, 'Simulating…')); timer = setTimeout(() => poll(id), 1200); return; }
    const v = r.versions[0];
    out.replaceChildren(h('div', { class: 'card' }, h('h2', {}, 'Simulation result'), h('div', { class: 'row' }, badge(r.status), h('span', { class: 'muted' }, `solver: ${r.solver_status}`)),
      h('p', { class: `notice gap ${r.status === 'completed' ? 'ok' : 'err'}` }, r.summary.message || ''),
      (r.summary.affected_before_repair || []).length ? [h('h3', {}, `Sessions broken by the scenario (${r.summary.affected_before_repair.length})`), h('ul', {}, r.summary.affected_before_repair.slice(0, 30).map((m) => h('li', {}, m)))] : h('p', { class: 'muted' }, 'The scenario breaks no existing session.'),
      v ? [h('h3', {}, `Minimum-disruption repair: ${v.summary.diff.length} session(s) change · soft score ${v.score}`),
        table([{ label: 'Session', key: 'session' }, { label: 'Change', render: (x) => badge(x.change, 'warning') }, { label: 'Before', key: 'before' }, { label: 'After', key: 'after' }], v.summary.diff, 'Nothing has to move.'),
        h('p', { class: 'gap' }, h('a', { href: `#/timetable/${v.id}` }, 'Open the sandbox timetable →'), h('span', { class: 'muted' }, '  (a sandbox can never be published; the live timetable is untouched)'))] : [h('h3', {}, 'Why it cannot be repaired'), findings(r.findings)]));
  }
  root.append(h('div', { class: 'card' }, h('h2', {}, 'Timetable digital twin — what-if simulation'),
    h('p', { class: 'help' }, 'Simulate a disruption against a copy of a timetable. Nothing here changes your data or the published timetable. The repair keeps locked sessions and moves as few other sessions as possible.'),
    h('div', { class: 'form' }, field('Base timetable', base), field('Type of change', kind)), h('div', { class: 'gap' }, builder), h('h3', {}, 'Scenario'), listEl,
    h('button', { class: 'primary gap', onclick: async () => { if (!changes.length) return toast('Add at least one change', 'err');
      const r = await guard(() => post(`${A}/runs`, { term_id: state.termId, kind: 'whatif', base_version_id: Number(base.value), sandbox: true, time_limit: 20, scenario: changes.map(({ _text, ...c }) => c) })); if (r) poll(r.id); return undefined; } }, 'Run simulation')), out);
  rebuild(); drawList();
}

// ---------------------------------------------------------------- requests, versions, users, audit
export async function requests(root) {
  const rows = await get(`${A}/requests`);
  root.append(h('div', { class: 'card' }, h('h2', {}, 'Faculty request inbox'),
    h('p', { class: 'help' }, 'Approving a request records the decision (and optionally marks the faculty member unavailable). It never edits the published timetable - run a repair, validate and publish a new version.'),
    table([{ label: '#', key: 'id' }, { label: 'Faculty', key: 'faculty_name' }, { label: 'Type', render: (r) => r.request_type.replace('_', ' ') },
      { label: 'Details', render: (r) => [r.session_label ? h('div', {}, r.session_label) : null, r.day_of_week != null ? h('div', {}, `Day: ${DAYS[r.day_of_week]}${r.leave_from ? ` (${r.leave_from} → ${r.leave_to || r.leave_from})` : ''}`) : null,
        r.proposed_day_of_week != null ? h('div', {}, `Proposed: ${DAYS[r.proposed_day_of_week]} ${r.proposed_start_minute != null ? fmt(r.proposed_start_minute) : ''}`) : null, h('div', { class: 'muted' }, r.reason)] },
      { label: 'Status', render: (r) => [badge(r.status), r.decision_note ? h('div', { class: 'muted small' }, r.decision_note) : null] }, { label: 'Raised', render: (r) => when(r.created_at) },
      { label: '', render: (r) => (r.status !== 'pending' ? null : h('button', { class: 'sm primary', onclick: () => {
        const note = h('textarea', { placeholder: 'Decision note (shown to the faculty member)' }), apply = h('input', { type: 'checkbox', checked: r.request_type === 'leave' }), res = h('div', {});
        const decide = async (decision, close) => { const o = await guard(() => post(`${A}/requests/${r.id}/decision`, { decision, note: note.value, apply_unavailability: decision === 'approved' && apply.checked }), `Request ${decision}`);
          if (!o) return false;
          if (!o.affected_sessions.length) { close(); state.rerender(); return true; }
          res.replaceChildren(h('h3', {}, `${o.affected_sessions.length} published session(s) affected`), h('ul', {}, o.affected_sessions.map((s) => h('li', {}, `${s.subject_code} ${s.group_name} · ${DAYS[s.day_of_week]} ${fmt(s.start_minute)}`))), h('p', {}, o.next_step),
            h('button', { class: 'primary', onclick: async () => { const run = await guard(() => post(`${A}/runs`, { term_id: r.term_id, kind: 'repair', base_version_id: o.published_version_id, time_limit: 20 }), 'Repair started'); if (run) { close(); location.hash = '#/generate'; } } }, 'Run minimum-change repair now'));
          return false; };
        dialog(`Decide request #${r.id}`, h('div', { class: 'col' }, note, r.request_type === 'leave' ? h('label', { class: 'chk' }, apply, `On approval, mark the faculty member unavailable on ${DAYS[r.day_of_week]}s (controlled input change)`) : null, res),
          [{ label: 'Approve', class: 'primary', action: (c) => decide('approved', c), keep: true }, { label: 'Reject', class: 'danger', action: (c) => decide('rejected', c), keep: true }]); } }, 'Review')) }], rows, 'No requests.')));
}

export async function versionsPage(root) {
  if (need()) return root.append(need());
  const rows = await get(`${A}/versions?term_id=${state.termId}`);
  const picked = new Set();
  root.append(h('div', { class: 'card' }, h('div', { class: 'row' }, h('h2', { style: { margin: 0 } }, 'Timetable versions & publication history'),
    h('button', { class: 'right', onclick: () => (picked.size < 2 ? toast('Tick 2-5 versions to compare', 'err') : (location.hash = `#/compare/${[...picked].join(',')}`)) }, 'Compare selected')),
  h('p', { class: 'help gap' }, 'Published versions are never rewritten. To revert, restore an archived version as a new draft, validate it and publish it.'),
  table([{ label: '', render: (v) => h('input', { type: 'checkbox', 'aria-label': 'select for comparison', onchange: (e) => (e.target.checked ? picked.add(v.id) : picked.delete(v.id)) }) },
    { label: '#', key: 'id' }, { label: 'Label', key: 'label' }, { label: 'Status', render: (v) => [badge(v.status), v.is_sandbox ? [' ', badge('sandbox', 'warning')] : null] },
    { label: 'Sessions', key: 'session_count' }, { label: 'Score', render: (v) => v.score ?? '—' }, { label: 'Created', render: (v) => when(v.created_at) }, { label: 'Published', render: (v) => when(v.published_at) },
    { label: '', render: (v) => h('a', { href: `#/timetable/${v.id}` }, 'Open') }], rows, 'No versions yet.')));
}

export async function users(root) {
  await loadLookups();
  const rows = await get(`${A}/users`);
  const form = (u) => {
    const e = { username: h('input', { type: 'text', value: u ? u.username : '', disabled: !!u }), full_name: h('input', { type: 'text', value: u ? u.full_name : '' }), email: h('input', { type: 'email', value: u ? u.email || '' : '' }),
      password: h('input', { type: 'password', autocomplete: 'new-password' }), role: select([['admin', 'Coordinator / admin'], ['faculty', 'Faculty'], ['student', 'Student']], u ? u.roles[0] : 'faculty'),
      faculty_id: select([[null, '—'], ...look.faculty_profiles.map((x) => [x.id, x.name])], u ? u.faculty_id : null), student_group_id: select([[null, '—'], ...look.student_groups.map((x) => [x.id, x.name])], u ? u.student_group_id : null),
      active: h('input', { type: 'checkbox', checked: u ? u.active : true }) };
    dialog(u ? `Edit ${u.username}` : 'Add user', h('div', { class: 'form' }, field('Username', e.username), field('Full name', e.full_name), field('E-mail (used to match the Clerk / Google sign-in)', e.email),
      field(u ? 'New local password (optional)' : 'Local password (optional)', e.password, 'Leave empty for accounts that sign in with Google / authenticator. Min. 12 chars, upper+lower+digit.'),
      field('Role', e.role), field('Faculty profile (faculty)', e.faculty_id), field('Class / batch (students)', e.student_group_id), h('label', { class: 'chk' }, e.active, 'Active')),
    [{ label: 'Save', class: 'primary', action: async () => {
      const body = { full_name: e.full_name.value, roles: [e.role.value], ...(e.email.value ? { email: e.email.value } : {}), ...(e.password.value ? { password: e.password.value } : {}),
        ...(e.faculty_id.value ? { faculty_id: Number(e.faculty_id.value) } : {}), ...(e.student_group_id.value ? { student_group_id: Number(e.student_group_id.value) } : {}) };
      const r = await guard(() => (u ? api('PATCH', `${A}/users/${u.id}`, { ...body, active: e.active.checked, clear_links: !e.faculty_id.value && !e.student_group_id.value }) : post(`${A}/users`, { ...body, username: e.username.value })), 'User saved');
      if (!r) return false; state.rerender(); return true; } }]);
  };
  root.append(h('div', { class: 'card' }, h('div', { class: 'row' }, h('h2', { style: { margin: 0 } }, 'Users & roles'), h('button', { class: 'primary right', onclick: () => form(null) }, '+ Add user')),
    h('p', { class: 'help gap' }, 'Access is default-deny: only people listed here can sign in. Roles are stored on the server and are never taken from the browser or the identity provider.'),
    table([{ label: 'Username', key: 'username' }, { label: 'Name', key: 'full_name' }, { label: 'E-mail', render: (u) => u.email || '—' }, { label: 'Role', render: (u) => u.roles.join(', ') },
      { label: 'Linked to', render: (u) => (u.faculty_id ? refLabel('faculty_profiles', u.faculty_id) : u.student_group_id ? refLabel('student_groups', u.student_group_id) : '—') },
      { label: 'Sign-in', render: (u) => `${u.auth_provider}${u.mfa_enabled ? ' + MFA' : ''}` }, { label: 'Active', render: (u) => badge(u.active ? 'active' : 'disabled', u.active ? 'ok' : 'error') },
      { label: '', render: (u) => h('button', { class: 'sm', onclick: () => form(u) }, 'Edit') }], rows)));
}

export async function auditPage(root) {
  const rows = await get(`${A}/audit?limit=300`);
  root.append(h('div', { class: 'card' }, h('h2', {}, 'Audit log (append-only)'),
    table([{ label: 'When', class: 'nowrap', render: (e) => when(e.at) }, { label: 'Who', render: (e) => e.actor_name || 'anonymous' }, { label: 'Action', key: 'action' },
      { label: 'Outcome', render: (e) => badge(e.outcome, e.outcome === 'ok' ? 'ok' : 'error') }, { label: 'Target', render: (e) => (e.entity ? `${e.entity} ${e.entity_id || ''}` : '—') },
      { label: 'Detail', render: (e) => h('span', { class: 'small muted' }, JSON.stringify(e.detail)) }, { label: 'IP', key: 'ip' }], rows)));
}
