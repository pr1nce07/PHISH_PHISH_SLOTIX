# Slotix: full project guide

Slotix is a web application that builds a weekly academic timetable. A coordinator enters the
teaching requirements, rooms, faculty availability and working hours; a constraint solver produces
a timetable that satisfies every mandatory rule, or reports exactly why it cannot. Faculty and
students then see the published result.

This guide covers how to run it, how each role uses it, how the scheduling works, and how to
configure, secure and maintain it. `README.md` is the short technical summary; this is the long form.

## Contents

1. [Start the application](#1-start-the-application)
2. [Sign in and roles](#2-sign-in-and-roles)
3. [Coordinator guide](#3-coordinator-guide)
4. [Faculty guide](#4-faculty-guide)
5. [Student guide](#5-student-guide)
6. [How scheduling works](#6-how-scheduling-works)
7. [Configuration](#7-configuration)
8. [Clerk sign-in (Google and authenticator)](#8-clerk-sign-in-google-and-authenticator)
9. [Security](#9-security)
10. [Maintenance](#10-maintenance)
11. [Project layout](#11-project-layout)
12. [API reference](#12-api-reference)
13. [Tests](#13-tests)
14. [Troubleshooting](#14-troubleshooting)
15. [Known limitations](#15-known-limitations)

---

## 1. Start the application

### On this machine (already installed)

```powershell
cd C:\Users\princ\Downloads\academic-timetable
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Open http://localhost:8000. Leave the terminal open while you use the app; press Ctrl+C to stop it.

### On a fresh machine

Requirements: Python 3.14 and [uv](https://docs.astral.sh/uv/). No Node.js, Docker or database
server is needed.

```powershell
uv venv .venv --python 3.14
uv pip install --python .venv\Scripts\python.exe -r requirements.lock
copy .env.example .env
.venv\Scripts\python.exe -m alembic upgrade head     # creates data\timetable.db
.venv\Scripts\python.exe -m app.seed                 # optional: demo data and demo logins
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

### After changing code

* Python files (`app/`): stop and start the server.
* Pages (`web/`): refresh the browser.
* Database tables: add an Alembic revision, then run `alembic upgrade head`.

---

## 2. Sign in and roles

### Demo accounts

`python -m app.seed` creates fictitious demo data and these accounts. Passwords are random and are
written to `data/demo_credentials.txt`, one line per account (login, password, description). They
are never stored in the code.

| Login | Role | Linked to |
|---|---|---|
| `demo.admin` | Coordinator | - |
| `demo.login` | Coordinator | - |
| `demo.faculty` | Faculty | Dr. Parag Shukla |
| `demo.faculty2` | Faculty | Mr. Sarang Rajvansh |
| `demo.student` | Student | B.Tech CS Sem III, Batch A |
| `demo.student2` | Student | M.Sc. DFIS Sem I, Batch B |

`python -m app.seed --reset-passwords` gives every `demo.*` account a new random password and
rewrites the file.

### What each role can do

| | Coordinator | Faculty | Student |
|---|---|---|---|
| Enter inputs, rules, run the solver, edit, publish | yes | no | no |
| See drafts, sandboxes, the audit log | yes | no | no |
| See the published timetable | all of it | all of it, own sessions outlined | own class and batch only |
| My preferences | read-only list | own only | no |
| Leave and time-change requests | review | own only | no |

Roles are stored on the server. The browser cannot grant itself a role, and every request is
checked again on the server, so hiding a tab is never the only protection.

### Sign-in methods

* **Local login** (login ID and password). Meant for development and the demo accounts. Passwords
  need at least 12 characters with upper case, lower case and a digit. Five wrong passwords for one
  login block that login for five minutes.
* **Clerk** (Google, email, authenticator app). See [section 8](#8-clerk-sign-in-google-and-authenticator).
  Not yet connected on this machine.

### Screen layout

* **Brand bar:** shows whether the selected term has a published timetable, the **Term** selector,
  your name and role, and **Sign out**.
* **Tabs:** depend on your role.
* **Light and dark mode:** follow your system setting.

---

## 3. Coordinator guide

The coordinator's tabs are: Dashboard, Inputs, Rules lab, Generate, Timetable, What-if lab, Request
inbox, Review & publish, Users, Audit log, My account.

### 3.1 The usual workflow

1. Enter the inputs (once per term).
2. **Generate → Run preflight validation** and fix any blocking problems.
3. **Generate → Generate**. You get up to three candidate timetables.
4. Open a candidate in **Timetable**, review it, adjust and lock sessions if needed.
5. **Validate**, then **Publish**. Faculty and students are notified and can see it.
6. Later changes (leave, a room closing): simulate in the **What-if lab**, or run a **Repair**, then
   validate and publish the new version.

### 3.2 Inputs

Enter the sub-tabs roughly in this order, because later ones refer to earlier ones.

| Sub-tab | What to enter | Notes |
|---|---|---|
| Departments | Code and name | Needed before anything else. |
| Academic terms | Department, name, dates | Pick the term in the brand bar afterwards. |
| Working days | One row per teaching day | |
| Periods | Order number, start, end, label | Two periods are "consecutive" only if one ends exactly when the next starts. |
| Breaks | Name, start, end, optional day | A blank day means every day. No session can touch a break. |
| Programmes | Code and name | |
| Classes, sections & batches | Programme, name, semester, type, parent, strength | A practical batch is a group whose **parent** is its class. |
| Faculty | Code, name, optional daily and weekly period limits | |
| Classrooms & laboratories | Room number, type, capacity | Type is free text such as `classroom` or `lab`. |
| Room features | Room, feature code, description | For example `computers`. Each feature row belongs to one room. |
| Subjects & weekly requirements | See below | |
| Teaching assignments | Subject offering, faculty, theory or practical | One per offering and session type. |
| Faculty availability | Faculty, day, from, to, kind | See below. |
| Fixed commitments | Faculty, title, day, from, to, optional pinned session and room | See below. |
| Faculty preferences (submitted) | Read-only | What faculty entered under My preferences. |

**Subjects are "offerings".** One row is one subject for one group in one term, and it carries its
own weekly requirements: theory sessions per week, periods per theory session, practical sessions
per week, consecutive periods per practical, plus the room type and features each needs.

* Put whole-class **theory** on the class's offering.
* Add **one practical offering per batch**. Each batch then gets its own practical sessions.

Example from the problem statement (M.Sc. DFIS Sem I, 40 students, two batches of 20):

| Offering for | Theory / week | Practical / week | Consecutive periods |
|---|---|---|---|
| M.Sc. DFIS Sem I (class) | 3 | 0 | - |
| Batch A | 0 | 1 | 2 |
| Batch B | 0 | 1 | 2 |

**Faculty availability kinds:**

* `available`: the person may teach only inside these windows. If none are entered, all working
  hours count as available.
* `unavailable`: never teach here.
* `preferred`: a preference entered by the coordinator (faculty use My preferences instead).

**Fixed commitments** come in two forms. With a teaching assignment selected, one session of that
subject is pinned to that slot (and room, if given). Without one, the faculty member is simply
busy then.

**Deleting.** A record that a timetable still uses cannot be deleted. Deactivate it instead.

### 3.3 Import and export (CSV)

Every Inputs sub-tab has **Import CSV** and **Export CSV**.

* The header row uses the field names shown in the import dialog.
* References are numeric ids (export the related table to find them).
* Times are minutes after midnight (10:30 is `630`). Days are `0` for Monday to `6` for Sunday.
* Feature lists are separated with `;`.
* **Preview / validate** checks every row and saves nothing. **Import** saves only if every row is
  valid.
* Cells that start with `=`, `+`, `-` or `@` are rejected, because spreadsheets treat them as
  formulas. Limit: 2,000 rows.

### 3.4 Rules lab

* **Mandatory constraints** are listed for reference. They cannot be switched off or weighted.
* **Configurable rules** can be enabled, weighted, and in two cases made hard. Each change creates a
  new version of the rule set, and every generation run records the version it used.
* You can keep several rule sets (**+ New rule set** copies the current one) and choose one when
  generating.

The full list is in [section 6](#6-how-scheduling-works).

### 3.5 Generate

* **Run preflight validation** checks the inputs without running the solver.
* **Generate** starts a background job. The page shows queued, running, then the result. No
  percentage is shown because the solver does not report one.
* **Time limit:** the search stops there. Capped at 60 seconds by default (`TT_SOLVER_MAX_SECONDS`).
* **Alternatives:** up to three genuinely different timetables, each differing from the others in
  at least about 15% of session times.
* **Keep locked sessions from:** choose an existing version to keep its locked sessions fixed and
  reschedule everything else.
* **Cancel run** stops a job in progress.

Possible results:

| Status | Meaning |
|---|---|
| completed | At least one valid timetable. "Proven optimal" or "feasible, optimality not proven" is stated per candidate. |
| infeasible | Either preflight found blocking problems, or the solver proved that no timetable exists. Findings explain why. |
| unknown | The time limit ran out with no answer. This is not a proof of impossibility; try a longer limit. |
| failed | A technical problem. Details are in the server log. |
| cancelled | You stopped it. |

### 3.6 When a timetable is impossible

Nothing incomplete is ever presented as a success. You get findings instead, each with the rule, a
plain-language message, the numbers involved and suggested fixes. For example:

> Subject DFIS103 requires 3 weekly period(s), but its assigned faculty member Prof X is available
> for only 2 period(s).

Findings are labelled by how certain they are:

* **preflight:** a fact computed from the inputs.
* **proven:** the solver proved infeasibility.
* **diagnostic:** evidence gathered afterwards. The system tests which family of rules, if ignored,
  would make the timetable possible, and lists the sessions left unassigned in the best partial
  timetable it could find. A different combination of sessions could be left out instead.
* **timeout:** no conclusion.

### 3.7 Timetable (the editor)

Pick a version at the top. The grid can be viewed by class or batch, by faculty member, by room, or
as an all-faculty sheet, and filtered by day and session type.

* **Click a session** to see its details.
  * **Why here?** lists the mandatory rules it satisfies and the preference cost of this placement.
  * **Move / why not elsewhere?** shows every slot as valid or blocked. A blocked slot tells you
    why. A valid slot lets you pick a free suitable room and move the session.
  * **Lock / Unlock.** A locked session never moves during regeneration or repair.
  * **Remove** deletes the session from a draft.
* **Drag a session** onto another slot to move it and keep its room.
* Every edit is checked on the server before it is saved. An edit that would break a mandatory rule
  is rejected and the conflicts are listed.
* If someone else changed the version in the meantime, your edit is refused and you are asked to
  reload.

Buttons:

| Button | What it does |
|---|---|
| Validate | Re-checks every mandatory rule independently of the solver. A clean draft becomes "validated". |
| Publish | Only for a validated version. The previous published version is archived, not deleted. |
| Duplicate / Restore as new draft | Copies a version into a new draft. This is how you revert. |
| Lock all / Unlock all | |
| Regenerate keeping locks | New run that keeps this version's locked sessions. |
| Repair (minimal change) | Fixes this version against the current inputs while moving as few sessions as possible. Also refills a session you removed. |
| Archive | Withdraws the published version. |
| Delete | Drafts only. |
| Export CSV, Print | |

Tabs under the buttons: **Findings** (the audit result), **Score** (penalty per preference rule),
**Analytics** (load heatmap, room utilisation, faculty load per day, free periods per class).

Published and archived versions cannot be edited. Editing a validated version returns it to draft.

### 3.8 What-if lab

Simulates a disruption against a copy of a timetable. Your data and the published timetable are
never touched.

1. Choose the base timetable.
2. Add one or more changes: a faculty member unavailable (one day or all), a room unavailable, a
   change in student strength, more or fewer weekly sessions, or a different preference weight.
3. **Run simulation.**

The result lists which existing sessions the scenario breaks, the repair that moves the fewest
sessions, a before-and-after table, and the new score. The result is a **sandbox**. A sandbox can
never be published.

### 3.9 Request inbox

Faculty raise leave and time-change requests. For each you can approve or reject with a note.

* Approving records the decision and notifies the faculty member. It never edits the published
  timetable.
* For leave you can tick "mark the faculty member unavailable on that weekday". That adds an
  availability record, which is a controlled input change.
* If published sessions are affected, the dialog lists them and offers **Run minimum-change repair
  now**. Review the resulting draft, validate it and publish it.

### 3.10 Review & publish

Lists every version with its status, session count, score and dates. Tick two to five versions and
choose **Compare selected** to see them side by side: audit result, total score, penalty per rule,
how many sessions differ, room utilisation and the busiest faculty day.

### 3.11 Users

Access is default-deny: only people listed here can sign in.

* For Clerk sign-in, enter the person's email and leave the password empty.
* For local sign-in, set a password.
* Link faculty accounts to a faculty profile, and student accounts to their class or batch. A
  student linked to a batch sees the batch's sessions and the whole-class sessions.
* You cannot deactivate yourself or remove your own coordinator role.

### 3.12 Audit log

An append-only record of sign-ins, refused requests, input changes, runs, edits, approvals,
publications and exports. It cannot be edited or deleted, even directly in the database.

---

## 4. Faculty guide

Tabs: My timetable, My preferences, My requests, Notifications, My account.

### My timetable

The published timetable. "My timetable" shows your own sessions; you can also switch to any class,
colleague or room. Export CSV and Print are available.

### My preferences

Tell the solver what you would like. Preferences are weighed, not enforced: a mandatory rule such
as your availability, a clash or room capacity always wins.

* **Mark your week:** click a slot to cycle neutral → preferred → avoid. Greyed slots are outside
  your recorded availability.
* **Standing requests:** avoid the first period, avoid the last period, spread my load across the
  week.
* **How strongly:** low, normal or high. A high priority is honoured before a colleague's low one
  when both want the same slot.
* **Note to the coordinator:** anything the grid cannot express.
* **Submit preferences** saves everything. Nothing is saved until you submit; an "Unsaved changes"
  marker reminds you.

Your preferences are used the next time the coordinator generates a timetable. Submitting does not
change a published timetable. Once a timetable is published, the page shows what share of your
periods match what you asked for.

If you want preferences considered before the first timetable exists, you can still submit them:
active terms of your department appear in the Term selector before anything is published.

### My requests

* **Leave:** choose the affected weekday, optional dates, and a reason.
* **Timetable / time change:** choose one of your own published sessions, optionally a proposed day
  and time, and a reason.

You can cancel a request while it is pending. The coordinator's decision and note appear here and
under Notifications. An approval does not change the timetable by itself; you see the change after
a new version is published.

---

## 5. Student guide

Tabs: My timetable, Notifications, My account.

Students see the published timetable for their own class and batch, read-only, with Export CSV and
Print. They cannot see drafts, other classes, faculty notes or any administrative data.

---

## 6. How scheduling works

### 6.1 The four stages

1. **Preflight.** Deterministic checks on the inputs: missing faculty assignments, no room of the
   required type or features, rooms too small, no block of consecutive periods, faculty available
   for fewer periods than they must teach, workload limits, contradictory fixed or locked sessions.
2. **Solve.** Google OR-Tools CP-SAT. There is one yes/no decision per (session, day, start period,
   room). Mandatory rules are constraints of the model. Preferences appear only in the objective,
   so no preference weight can buy a mandatory violation.
3. **Independent audit.** What was saved is re-checked from scratch by separate code that shares
   nothing with the solver. A candidate that fails is never shown as valid.
4. **Score.** Each preference rule contributes `violations × weight`. Lower total is better.

### 6.2 Mandatory rules (always enforced)

| Rule | Meaning |
|---|---|
| within hours | Sessions fall on a working day, exactly on configured periods. |
| break overlap | No session touches a scheduled break. |
| faculty availability | Faculty teach only when available and outside unavailable and fixed slots. |
| faculty overlap | A faculty member teaches one session at a time. |
| room overlap | A room hosts one session at a time. |
| group overlap | A class, its parent and its batches attend one session at a time. Two sibling batches may run in parallel. |
| room capacity | Seats cover the strength of the class or batch. |
| room suitability | The room is active, of the required type, with every required feature. |
| consecutive periods | A session occupies its required number of consecutive periods. |
| weekly requirement | Every offering gets exactly its weekly theory and practical sessions. |
| fixed commitment | Fixed sessions stay in their slot (and room). |
| locked session | Locked sessions do not move. If an input change makes one impossible, that is reported; it is never moved silently. |
| faculty workload | Daily and weekly period limits are respected. |
| valid assignment | Every session has a consistent subject, group, term and faculty assignment. |

### 6.3 Configurable rules

| Rule | Mode | Default | Parameters |
|---|---|---|---|
| Spread a subject across days | soft or hard | on, weight 6 | - |
| Avoid student gaps | soft | on, weight 4 | - |
| Avoid faculty idle gaps | soft | on, weight 2 | - |
| Balance faculty load across days | soft | on, weight 3 | - |
| Faculty preferred slots (entered by the coordinator) | soft | on, weight 3 | - |
| Faculty self-service preferences (My preferences) | soft | on, weight 2 | - |
| Avoid late slots | soft | on, weight 1 | first late period index: 5 |
| Avoid early slots | soft | off, weight 1 | last early period index: 0 |
| Preferred laboratory window | soft | off, weight 2 | period indexes 3 to 5 |
| Right-sized rooms | soft | on, weight 1 | unused seats per penalty point: 20 |
| Keep a subject in one room | soft | on, weight 2 | - |
| Limit consecutive teaching periods | soft or hard | on, weight 4 | maximum in a row: 3 |
| Daily period limit per student group | hard | off | maximum per day: 6 |
| Blocked teaching slot | hard | off | day (blank for every day), period index |
| Minimise changes during repair | soft | on, weight 40 | - |

Period indexes count from 0 in order of start time.

**How My preferences are scored.** Per period taught: 3 points in a slot marked avoid; 1 point in
an unmarked slot if the person marked at least one slot as preferred; 2 points for a first or last
period they asked to avoid. "Spread my load" adds 1 point per period above their even daily share.
All of this is multiplied by their chosen strength (low 1, normal 2, high 4) and then by the rule's
weight.

Rules are configured only through validated fields. No formula, expression or code entered by a
user is ever executed.

### 6.4 Versions

| Status | Meaning |
|---|---|
| draft | Editable. |
| validated | Passed the audit; ready to publish. Any edit returns it to draft. |
| published | The live timetable. One per term, immutable. |
| archived | A former published version, kept as history. |
| sandbox | A what-if copy. Never publishable. |

Publishing re-runs the audit first, because inputs may have changed since validation.

---

## 7. Configuration

Settings come from environment variables or a `.env` file in the project folder. Real environment
variables take precedence. `.env.example` lists the common ones.

| Variable | Default | Purpose |
|---|---|---|
| `TT_ENV` | `development` | `production` turns on secure cookies, HSTS and admin MFA, and turns off API docs and local login. |
| `TT_ORG_NAME` | empty | School name shown under the product name. |
| `TT_DATABASE_URL` | `sqlite:///data/timetable.db` | Database file. |
| `TT_COOKIE_SECURE` | 0 (1 in production) | Must be 1 behind HTTPS. |
| `TT_SESSION_MINUTES` | 480 | Maximum session length. |
| `TT_SESSION_IDLE_MINUTES` | 60 | Sign-out after this much inactivity. |
| `TT_LOCAL_LOGIN` | 1 (0 in production) | Allow login ID and password. |
| `TT_LOGIN_MAX_ATTEMPTS` | 5 | Wrong passwords before a login is throttled. |
| `TT_LOGIN_WINDOW_SECONDS` | 300 | Throttle window. |
| `CLERK_PUBLISHABLE_KEY` | empty | Enables Clerk sign-in. Public by design. |
| `CLERK_SECRET_KEY` | empty | Server-side only. Used to read a user's verified email on first sign-in. |
| `TT_ALLOWED_ORIGINS` | `http://localhost:8000,http://127.0.0.1:8000` | Addresses the app is served from. Clerk tokens from other origins are refused. |
| `TT_REQUIRE_ADMIN_MFA` | 0 (1 in production) | Coordinators must sign in with a second factor. |
| `TT_SOLVER_MAX_SECONDS` | 60 | Upper bound on the time limit of a run. |
| `TT_SOLVER_WORKERS` | 4 | Solver threads. |
| `TT_SOLVER_MAX_VARS` | 400000 | Refuse models larger than this. |
| `TT_MAX_QUEUED_RUNS` | 3 | Runs allowed to wait or run at once. |
| `TT_MAX_BODY_BYTES` | 1000000 | Largest accepted request. |
| `TT_DOCS` | 1 (0 in production) | Serve interactive API docs at `/docs`. |

---

## 8. Clerk sign-in (Google and authenticator)

Clerk proves who someone is. What they may do always comes from Slotix's own Users table.

**Current state on this machine:** the code is in place and tested with locally signed tokens, but
no Clerk application is linked yet, so only local login is active. The Clerk CLI is installed and
signed in, but the signed-in Clerk account does not own the application id that was supplied.

To connect it:

1. Sign the CLI in with the Clerk account that owns the application: `clerk auth login`.
2. Pull the keys: `clerk env pull --file .env --app <your app id>`. Or copy the publishable key and
   secret key from the Clerk dashboard into `.env`.
3. Add the address you serve the app from to `TT_ALLOWED_ORIGINS`.
4. Restart the server. The sign-in page now shows Clerk's sign-in box.
5. Under **Users**, register each person's email with a role.

How a sign-in works:

1. The person signs in with Clerk in the browser.
2. The browser sends the Clerk session token once to `POST /api/auth/clerk`.
3. The server verifies the signature against Clerk's published keys, the issuer, the expiry and the
   origin the token was issued for.
4. First time: the server reads the person's verified email from Clerk and binds the Clerk identity
   to the matching user row. An unregistered email is refused.
5. The server issues Slotix's own session cookie. From here everything works as with local login.

Google sign-in and the authenticator app are switched on in the Clerk dashboard, not in Slotix. Set
`TT_REQUIRE_ADMIN_MFA=1` to refuse coordinators who signed in without a second factor, and
`TT_LOCAL_LOGIN=0` to remove password login.

---

## 9. Security

| Area | What is in place |
|---|---|
| Access control | Default-deny. Every coordinator endpoint requires the coordinator role; faculty endpoints act only on the signed-in person's own records; students have read-only endpoints. Refusals are audited. |
| Sessions | Random session id, only its hash is stored, HttpOnly and SameSite=Strict cookie, idle and absolute timeouts, revoked on sign-out, password change or role change. |
| Request forgery | A per-session token is required on every change, plus a same-origin check. |
| Passwords | Hashed with scrypt. Login throttling and temporary lockout. The same error for a wrong password and an unknown login. |
| Input | Every request body is validated against a strict schema that rejects unknown fields, so protected fields such as role or status cannot be set by a client. All database access is parameterised. |
| Browser | Content Security Policy, no framing, no content sniffing. Pages are built with text nodes, never by inserting HTML. |
| Files | CSV import is validated row by row and rejects formulas; exports neutralise them. |
| Solver | Queue limit, time limit, model-size limit, rate limit and cancellation. |
| Integrity | Published timetables and the audit log are immutable at database level. One published version per term is enforced by a unique index. Edits use version checks so two people cannot overwrite each other. |
| Errors | Clients get a generic message. Stack traces and SQL stay in the server log. |

For production: serve over HTTPS, set `TT_ENV=production`, run a single server process, keep `.env`
and `data/` out of version control (they are in `.gitignore`), and restrict the database file to
the service account.

---

## 10. Maintenance

**Back up.** The database is one file. With the server stopped, copy `data\timetable.db`. With it
running, use SQLite's online backup, then encrypt the copy and store it elsewhere:

```powershell
sqlite3 data\timetable.db ".backup backup.db"
```

**Restore.** Stop the server, replace `data\timetable.db` with the backup, start the server.
Practise this before you need it.

**Upgrade the database** after pulling new code:

```powershell
.venv\Scripts\python.exe -m alembic upgrade head
```

**Start over with clean demo data.** Stop the server, delete `data\timetable.db` (and the
`-wal` / `-shm` files next to it), then run `alembic upgrade head` and `python -m app.seed`.

**Add a coordinator without the UI.** Sign in as an existing coordinator and use the Users tab. On
an empty database, run the seed and use `demo.admin`, then create real accounts and deactivate the
demo ones.

---

## 11. Project layout

```
academic-timetable/
├─ app/
│  ├─ main.py           entry point, security headers, error handling, static files
│  ├─ config.py         settings from the environment / .env
│  ├─ db.py             SQLite engine and sessions
│  ├─ models.py         tables, constraints, triggers
│  ├─ schemas.py        request validation
│  ├─ security.py       passwords, sessions, CSRF, rate limits, roles, audit
│  ├─ clerk_auth.py     Clerk token verification
│  ├─ api_admin.py      coordinator API
│  ├─ api_public.py     sign-in, faculty and student API
│  ├─ services.py       run execution, versions, analytics, explanations
│  ├─ seed.py           demo data
│  └─ solver/
│     ├─ problem.py     snapshot of the inputs used by everything below
│     ├─ rules.py       rule registry
│     ├─ preflight.py   input checks and infeasibility diagnostics
│     ├─ engine.py      the CP-SAT model
│     ├─ auditor.py     independent check of mandatory rules
│     └─ score.py       preference scoring
├─ web/                 the user interface (plain HTML, CSS, JavaScript modules)
├─ migrations/          Alembic revisions
├─ tests/               automated tests
├─ data/                database and demo credentials (not in version control)
├─ README.md, GUIDE.md
├─ requirements.txt, requirements.lock
└─ .env.example
```

---

## 12. API reference

All paths start with `/api`. Changes need the session cookie and the `X-CSRF-Token` header (the
token is returned by sign-in and by `/api/auth/me`). In development, interactive docs are at
http://localhost:8000/docs.

### Everyone signed in

| Method and path | Purpose |
|---|---|
| `GET /auth/config` | Which sign-in methods are enabled (no sign-in needed). |
| `POST /auth/login`, `POST /auth/clerk` | Sign in. |
| `POST /auth/logout`, `GET /auth/me`, `POST /auth/change-password` | Session. |
| `GET /terms` | Terms you may see. |
| `GET /timetable/published?term_id=` | Published timetable, limited to what your role may see. |
| `GET /timetable/published/export.csv?term_id=` | The same, as CSV. |
| `GET /notifications`, `POST /notifications/read-all` | Notifications. |

### Faculty

| Method and path | Purpose |
|---|---|
| `GET /faculty/preferences?term_id=`, `PUT /faculty/preferences` | Own preferences. |
| `GET /faculty/requests`, `POST /faculty/requests` | Own requests. |
| `GET /faculty/requests/{id}`, `POST /faculty/requests/{id}/cancel` | One own request. |

### Coordinator (`/api/admin/...`)

| Method and path | Purpose |
|---|---|
| `GET, POST /data/{table}`, `PUT, DELETE /data/{table}/{id}` | Inputs. |
| `POST /data/{table}/import`, `GET /data/{table}/export.csv` | CSV. |
| `GET /rules/registry`, `GET, POST /rule-sets`, `PATCH /rule-sets/{id}`, `PUT /rule-sets/{id}/rules/{key}` | Rules. |
| `POST /preflight` | Check inputs. |
| `GET, POST /runs`, `GET /runs/{id}`, `POST /runs/{id}/cancel` | Generation, repair and what-if runs. |
| `GET /versions`, `GET, DELETE /versions/{id}`, `GET /versions/compare?ids=` | Versions. |
| `POST /versions/{id}/validate`, `/publish`, `/archive`, `/clone`, `/lock-all`, `/sessions` | Version actions. |
| `GET /versions/{id}/analytics`, `GET /versions/{id}/export.csv` | Analytics and export. |
| `PATCH, DELETE /sessions/{id}`, `POST /sessions/{id}/lock` | Edit one session. |
| `GET /sessions/{id}/options`, `GET /sessions/{id}/explain` | Why-not and why-here. |
| `GET /requests`, `POST /requests/{id}/decision` | Request inbox. |
| `GET /faculty-preferences?term_id=` | Submitted preferences (read-only). |
| `GET, POST /users`, `PATCH /users/{id}` | Users and roles. |
| `GET /audit`, `GET /dashboard?term_id=` | Audit log and dashboard. |

Input table names: `departments`, `academic_terms`, `programmes`, `student_groups`,
`faculty_profiles`, `subjects`, `teaching_assignments`, `rooms`, `room_features`, `working_days`,
`period_slots`, `scheduled_breaks`, `faculty_availability`, `fixed_commitments`.

---

## 13. Tests

```powershell
.venv\Scripts\python.exe -m pytest tests -q
```

31 tests, about 30 seconds. Each test runs against its own temporary database.

| File | Covers |
|---|---|
| `tests/test_solver.py` | The problem statement's example, overlaps, class and batch overlap, capacity, room type and features, consecutive practicals, weekly counts, availability shortfall, fixed and locked sessions, impossible inputs, every solver status, preference weights, distinct alternatives, minimum-change repair, rule validation. |
| `tests/test_security.py` | Anonymous access, student read-only scope, faculty boundaries and id tampering, request approval, CSRF, login hardening, malformed and injection input, manual edit validation and locking, publish rules and immutability, concurrent publishing, what-if isolation, Clerk sign-in. |
| `tests/test_preferences.py` | Faculty-only access, ownership from the session, the solver honouring preferences, preferences never overriding mandatory rules, unsubmitted or disabled preferences ignored. |

---

## 14. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "Address already in use" when starting | The server is already running. Open the URL, or stop the other one. |
| The page loads but nothing works; `/readyz` says not ready | The database has not been created. Run `alembic upgrade head`. |
| "Too many sign-in attempts" | Five wrong passwords. Wait five minutes. |
| Sign-in works, then you are signed out | Sessions end after 60 minutes of inactivity or 8 hours in total. |
| A code change has no effect | Restart the server for Python changes. |
| "No timetable has been published for this term yet" | A coordinator has to validate and publish a version. |
| A run ends as "unknown" | The time limit ran out. Raise it, or reduce the number of alternatives. |
| A run ends as "infeasible" | Read the findings; they name the sessions and the rule. Fix the inputs and generate again. |
| An edit is refused with "changed by someone else" | Reload the version and repeat the edit. |
| Cannot delete a room, subject or faculty member | A timetable still refers to it. Deactivate it, or delete the draft versions that use it. |
| "Too many generation jobs are queued" | Wait for a run to finish or cancel one. |
| Fonts look plain | Google Fonts is unreachable; system fonts are used instead. Nothing else is affected. |
| Clerk sign-in box does not appear | `CLERK_PUBLISHABLE_KEY` is not set, or the server was not restarted. |
| Clerk sign-in succeeds but Slotix refuses | The email is not registered under Users, or the address is missing from `TT_ALLOWED_ORIGINS`. |

---

## 15. Known limitations

* **One process.** The job queue and rate limits live in memory, so run a single server process. A
  long solve shares the CPU with the web server.
* **No Docker setup**, and no separate worker.
* **Imports.** CSV only. Excel files and timetable images cannot be imported.
* **Editor.** There is no button to add a session by hand. Use Repair to refill a removed session.
* **Leave is per weekday**, because the timetable is weekly, not per calendar date.
* **What-if "split or merge a batch"** is simulated as a strength change.
* **Rooms are per department.** Sharing rooms across departments is not modelled.
* **Clerk** has automated tests but has not been run against a live Clerk application.
* **Browser checks so far** covered sign-in, generation, the editor's move dialog, validation,
  publishing, the student view, My preferences and the submitted-preferences list. The what-if lab,
  request inbox, Inputs forms, Users and Audit log screens are covered by API tests but have not
  been clicked through.
* **Demo data is fictitious.** It is loosely modelled on the sample timetables and is not official.
