// Timetable rendering: a day/period grid for one entity and a faculty-wise matrix like the sample sheet.
import { DAYS, fmt, h } from './core.js';

function rowsOf(cal) {
  const rows = cal.periods.map((p) => ({ kind: 'period', ...p }));
  for (const b of cal.breaks) if (b.day_of_week === null) rows.push({ kind: 'break', ...b });
  return rows.sort((a, b) => a.start_minute - b.start_minute);
}

function lanes(list) {
  // greedy interval colouring so partially overlapping sessions (e.g. two batches) sit side by side
  const ends = [];
  const sorted = [...list].sort((a, b) => a.start_minute - b.start_minute || b.end_minute - a.end_minute);
  for (const s of sorted) {
    let i = ends.findIndex((e) => e <= s.start_minute);
    if (i < 0) { i = ends.length; ends.push(0); }
    ends[i] = s.end_minute; s._lane = i;
    s._alone = !list.some((o) => o !== s && o.start_minute < s.end_minute && s.start_minute < o.end_minute);
  }
  return Math.max(1, ends.length);
}

export function sessionCard(s, lines, opt = {}) {
  const el = h('button', {
    type: 'button', class: `sess ${s.session_type}${s.locked ? ' locked' : ''}${s.mine ? ' mine' : ''}`,
    title: `${s.subject_name} · ${s.session_type}${s.locked ? ' · locked' : ''}`,
    onclick: () => opt.onClick && opt.onClick(s),
  }, h('span', { class: 'c1' }, s.subject_code, h('i', {}, (s.session_type === 'practical' ? 'PRACTICAL' : 'THEORY') + (s.locked ? ' · LOCKED' : ''))),
  lines(s).map((t, i) => h('span', { class: i === 0 ? 'c2' : 'c3' }, t)));
  if (opt.editable && !s.locked) {
    el.draggable = true;
    el.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/plain', String(s.id)); e.dataTransfer.effectAllowed = 'move'; });
  }
  return el;
}

/** opt: { lines(s) -> [text], onClick(s), editable, onDrop(sessionId, day, startMinute), days } */
export function timetableGrid(cal, sessions, opt = {}) {
  const days = (opt.days || cal.days);
  const rows = rowsOf(cal);
  if (!rows.length || !days.length) return h('p', { class: 'muted' }, 'Working days and periods are not configured yet.');
  const rowOf = new Map(rows.map((r, i) => [r.start_minute, i + 2]));
  const perDay = new Map(days.map((d) => [d, sessions.filter((s) => s.day_of_week === d)]));
  const laneCount = new Map(days.map((d) => [d, lanes(perDay.get(d))]));
  const colStart = new Map();
  let col = 2;
  for (const d of days) { colStart.set(d, col); col += laneCount.get(d); }
  const grid = h('div', { class: 'tt', role: 'table', 'aria-label': 'Timetable',
    style: { gridTemplateColumns: `92px ${days.map((d) => `repeat(${laneCount.get(d)}, minmax(${laneCount.get(d) > 1 ? 84 : 120}px, 1fr))`).join(' ')}` } });
  grid.append(h('div', { class: 'hd', style: { gridRow: '1', gridColumn: '1' } }, 'Time'));
  for (const d of days) grid.append(h('div', { class: 'hd', style: { gridRow: '1', gridColumn: `${colStart.get(d)} / span ${laneCount.get(d)}` } }, DAYS[d]));
  rows.forEach((r, i) => {
    const row = String(i + 2);
    if (r.kind === 'break') {
      grid.append(h('div', { class: 'brk', style: { gridRow: row, gridColumn: `1 / span ${col - 1}` } }, `${r.name} · ${fmt(r.start_minute)}–${fmt(r.end_minute)}`));
      return;
    }
    grid.append(h('div', { class: 'time', style: { gridRow: row, gridColumn: '1' } }, h('b', {}, `${fmt(r.start_minute)}–${fmt(r.end_minute)}`), r.label || ''));
    for (const d of days) {
      const bg = h('div', { class: 'bg', style: { gridRow: row, gridColumn: `${colStart.get(d)} / span ${laneCount.get(d)}` } });
      if (opt.editable && opt.onDrop) {
        bg.addEventListener('dragover', (e) => { e.preventDefault(); bg.classList.add('over'); });
        bg.addEventListener('dragleave', () => bg.classList.remove('over'));
        bg.addEventListener('drop', (e) => { e.preventDefault(); bg.classList.remove('over'); opt.onDrop(Number(e.dataTransfer.getData('text/plain')), d, r.start_minute); });
      }
      grid.append(bg);
    }
  });
  for (const d of days) {
    for (const s of perDay.get(d)) {
      const row = rowOf.get(s.start_minute);
      if (!row) continue;
      const card = sessionCard(s, opt.lines || (() => []), opt);
      card.style.gridRow = `${row} / span ${Math.max(1, s.period_count)}`;
      card.style.gridColumn = s._alone ? `${colStart.get(d)} / span ${laneCount.get(d)}` : String(colStart.get(d) + s._lane);
      grid.append(card);
    }
  }
  return h('div', { class: 'tablewrap' }, grid);
}

export const legend = () => h('div', { class: 'legend' },
  h('span', {}, h('i', {}), 'Theory'), h('span', {}, h('i', { class: 'p' }), 'Practical'), h('span', {}, h('i', { class: 'l' }), 'Locked'));

/** Sheet-style overview: entities grouped vertically, time rows, day columns (like the sample faculty timetable). */
export function matrixTable(cal, sessions, keyOf, nameOf, cellText) {
  const groups = new Map();
  for (const s of sessions) { if (!groups.has(keyOf(s))) groups.set(keyOf(s), { name: nameOf(s), list: [] }); groups.get(keyOf(s)).list.push(s); }
  const ents = [...groups.values()].sort((a, b) => a.name.localeCompare(b.name));
  if (!ents.length) return h('p', { class: 'muted' }, 'No sessions.');
  const body = [];
  for (const e of ents) {
    cal.periods.forEach((p, i) => {
      body.push(h('tr', { class: i === 0 ? 'first' : '' },
        i === 0 ? h('td', { class: 'name', rowSpan: cal.periods.length }, e.name) : null,
        h('td', { class: 'nowrap' }, fmt(p.start_minute)),
        cal.days.map((d) => h('td', {}, e.list.filter((s) => s.day_of_week === d && s.start_minute <= p.start_minute && p.start_minute < s.end_minute)
          .map((s) => h('div', {}, cellText(s)))))));
    });
  }
  return h('div', { class: 'tablewrap' }, h('table', { class: 'matrix' },
    h('thead', {}, h('tr', {}, h('th', {}, 'Faculty / group'), h('th', {}, 'Time'), cal.days.map((d) => h('th', {}, DAYS[d])))), h('tbody', {}, body)));
}

/** Viewer with programme/group-wise, faculty-wise and room-wise switching plus day / type filters. */
export function timetableViewer(data, opt = {}) {
  const { calendar: cal, sessions } = data;
  const groups = data.groups || [];
  const parent = new Map(groups.map((g) => [g.id, g.parent_group_id]));
  const chain = (id) => { const out = []; const seen = new Set(); while (id != null && !seen.has(id)) { out.push(id); seen.add(id); id = parent.get(id); } return out; };
  const uniq = (key, label) => [...new Map(sessions.map((s) => [s[key], s[label]])).entries()].sort((a, b) => String(a[1]).localeCompare(String(b[1])));
  const st = { view: opt.view || 'group', id: opt.id ?? '', day: '', type: '' };
  const host = h('div', {});
  const controls = h('div', { class: 'row noprint', style: { marginBottom: '10px' } });
  const out = h('div', {});
  const views = [['group', 'Programme / class'], ['faculty', 'Faculty'], ['room', 'Room / lab'], ['matrix', 'All faculty (sheet)']];
  if (opt.mineFacultyId) views.unshift(['mine', 'My timetable']);
  if (opt.mineFacultyId && !opt.view) st.view = 'mine';

  function entities() {
    if (st.view === 'faculty') return uniq('faculty_id', 'faculty_name');
    if (st.view === 'room') return uniq('room_id', 'room_code');
    const ids = new Set(sessions.flatMap((s) => chain(s.group_id)));
    const named = groups.filter((g) => ids.has(g.id)).map((g) => [g.id, g.name]);
    return (named.length ? named : uniq('group_id', 'group_name')).sort((a, b) => a[1].localeCompare(b[1]));
  }
  function draw() {
    controls.replaceChildren();
    const sel = (opts, val, on) => { const s = h('select', { onchange: (e) => on(e.target.value) }, opts.map(([v, l]) => h('option', { value: v }, l))); s.value = String(val); return s; };
    controls.append(h('label', { class: 'f' }, 'View', sel(views, st.view, (v) => { st.view = v; st.id = ''; draw(); })));
    let list = sessions;
    let lines = (s) => [s.group_name, `${s.faculty_code} · ${s.room_code}`];
    if (['group', 'faculty', 'room'].includes(st.view)) {
      const ents = entities();
      if (!ents.some((e) => String(e[0]) === String(st.id))) st.id = ents.length ? ents[0][0] : '';
      controls.append(h('label', { class: 'f' }, { group: 'Class / batch', faculty: 'Faculty member', room: 'Room' }[st.view], sel(ents, st.id, (v) => { st.id = v; draw(); })));
      const id = Number(st.id);
      if (st.view === 'group') {
        // a class view also shows its batches; a batch view also shows the whole-class sessions
        list = sessions.filter((s) => s.group_id === id || chain(s.group_id).includes(id) || chain(id).includes(s.group_id));
        lines = (s) => [s.group_id === id ? null : s.group_name, `${s.faculty_code} · ${s.room_code}`].filter(Boolean);
      } else if (st.view === 'faculty') { list = sessions.filter((s) => s.faculty_id === id); lines = (s) => [s.group_name, s.room_code]; }
      else { list = sessions.filter((s) => s.room_id === id); lines = (s) => [s.group_name, s.faculty_code]; }
    } else if (st.view === 'mine') { list = sessions.filter((s) => s.faculty_id === opt.mineFacultyId); lines = (s) => [s.group_name, s.room_code]; }
    controls.append(h('label', { class: 'f' }, 'Day', sel([['', 'All days'], ...cal.days.map((d) => [d, DAYS[d]])], st.day, (v) => { st.day = v; draw(); })),
      h('label', { class: 'f' }, 'Session type', sel([['', 'Theory + practical'], ['theory', 'Theory only'], ['practical', 'Practical only']], st.type, (v) => { st.type = v; draw(); })));
    if (st.type) list = list.filter((s) => s.session_type === st.type);
    const days = st.day === '' ? cal.days : [Number(st.day)];
    list = list.filter((s) => days.includes(s.day_of_week));
    out.replaceChildren(st.view === 'matrix'
      ? matrixTable({ ...cal, days }, list, (s) => s.faculty_id, (s) => s.faculty_name, (s) => `${s.subject_code} ${s.group_name}${s.session_type === 'practical' ? ' (Lab)' : ''} · ${s.room_code}`)
      : timetableGrid(cal, list.map((s) => ({ ...s })), { ...opt, lines, days }),
    legend(), h('p', { class: 'muted small' }, `${list.length} session(s) shown.`));
  }
  draw();
  host.append(controls, out);
  host.redraw = draw;
  return host;
}
