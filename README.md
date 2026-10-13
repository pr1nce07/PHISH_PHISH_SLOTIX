# Slotix - Academic Timetable Generator

A web application in which a timetable coordinator enters teaching requirements, rooms, faculty
availability and working hours, and a constraint solver (Google OR-Tools CP-SAT) produces a valid
weekly timetable - or explains, with evidence, why none exists.

## Architecture

| Layer | Choice | Notes |
|---|---|---|
| API | FastAPI + Pydantic 2 | `app/api_admin.py` (admin only), `app/api_public.py` (auth, faculty, student) |
| Database | **SQLite** via SQLAlchemy 2 + Alembic | foreign keys ON, WAL, check/unique constraints, triggers |
| Solver | OR-Tools CP-SAT 9.15 | `app/solver/engine.py`; rules in `rules.py`; preflight + diagnostics in `preflight.py` |
| Independent auditor | plain Python | `app/solver/auditor.py` re-checks every saved timetable without the solver |
| Jobs | in-process worker thread | runs are queued, persisted and cancellable (`app/services.py`) |
| UI | dependency-free ES modules | `web/`; DOM built with `textContent` only, strict CSP |
| Sign-in | Clerk (Google + authenticator MFA) and/or local passwords | `app/clerk_auth.py`, `app/security.py` |

Deviations from the original brief, and why: SQLite instead of PostgreSQL (requested); no Docker /
Redis / Celery because Docker is not installed on the target machine, so the worker is a single
background thread inside the API process; the UI is plain JavaScript modules instead of React so
there is no build step and one origin serves everything.

Tested with Python 3.14, OR-Tools 9.15, FastAPI 0.143, SQLAlchemy 2.1 (exact pins in `requirements.lock`).

## Setup

```powershell
uv venv .venv --python 3.14
uv pip install --python .venv\Scripts\python.exe -r requirements.lock
copy .env.example .env                      # then edit
.venv\Scripts\python.exe -m alembic upgrade head     # creates the schema
.venv\Scripts\python.exe -m app.seed                 # optional demo data
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Open http://localhost:8000. The seed writes random demo passwords to `data/demo_credentials.txt`
(git-ignored); `python -m app.seed --reset-passwords` regenerates them. All seed data is fictitious.

Tests: `.venv\Scripts\python.exe -m pytest tests -q` (31 tests).

## Sign-in with Clerk

Clerk proves *who* someone is (Google, e-mail, authenticator app). *What* they may do always comes
from this application's own `app_users` / `app_roles` tables.

1. `clerk env pull --file .env` (or copy the two keys from the Clerk dashboard into `.env`).
2. In **Users**, register each person's e-mail with a role. Access is default-deny: a Clerk account
   whose verified e-mail is not registered is refused.
3. On first sign-in the Clerk identity is bound to that row; later sign-ins match on the identity.

The browser sends the Clerk session token once to `POST /api/auth/clerk`. The server verifies the
RS256 signature against the instance JWKS, the issuer, `exp`/`nbf` and the `azp` origin, then issues
the application's own HttpOnly session cookie. Set `TT_REQUIRE_ADMIN_MFA=1` to require a second
factor for administrators, and `TT_LOCAL_LOGIN=0` to disable password login.

## Faculty "My preferences"

Faculty (and only faculty) get a **My preferences** tab: mark each slot of the week preferred /
avoid, tick standing requests (avoid first period, avoid last period, spread my load), choose how
strongly to weigh them (low 1x, normal 2x, high 4x) and leave a note for the coordinator.

* Stored in `faculty_preferences`, one row per faculty member and term. The owner always comes from
  the signed-in session; the API has no faculty id parameter.
* The solver applies them through the soft rule **Faculty self-service preferences** (Rules lab),
  on the next generation. They never override a mandatory rule and never change a published
  timetable.
* The coordinator sees submissions read-only under **Inputs -> Faculty preferences (submitted)**.
* Tests: `tests/test_preferences.py`.

## Theme

The UI uses the Slotix theme (navy brand bar, green accent, Spectral + IBM Plex type, light and
dark mode following the system setting). The fonts load from Google Fonts, so the CSP allows
`fonts.googleapis.com` (stylesheet) and `fonts.gstatic.com` (font files); system fonts are used if
they are unreachable. Set `TT_ORG_NAME` to show your school's name under the product name.

## Data model conventions

* Weekly requirements are columns of `subjects`; there is **no** `weekly_requirements` table.
* `subjects.student_group_id` is a foreign key to `student_groups`; a trigger guarantees the group
  is in the same term.
* Room features are rows of `room_features` with a direct `room_id`; there is **no** mapping table.
* A `subjects` row is an offering for one group in one term. Put whole-class theory on the class's
  offering and add **one practical offering per batch**. Faculty are attached through
  `teaching_assignments` (one per offering and session type).
* A batch is a `student_groups` row whose `parent_group_id` is its class. Overlap checks follow the
  whole ancestor chain, so nested batches are handled.

## How generation works

1. **Preflight** (`preflight.py`) - deterministic checks: missing assignments, no suitable room,
   capacity, no consecutive block, faculty availability versus required periods, workload caps,
   contradictory fixed or locked sessions.
2. **CP-SAT model** - one boolean per (session, day, start period, room). Hard constraints are
   model constraints or domain filters; soft rules appear only in the objective.
3. **Independent audit** of what was saved. A candidate that fails is never shown as valid.
4. **Infeasible** results are labelled as proven; diagnostics relax one constraint family at a time
   and compute one maximal partial placement to list unassigned sessions. A timeout is reported as
   *unknown*, never as infeasible.

Configurable rules are an allow-listed registry with validated integer parameters. No code,
expression or SQL supplied by a user is ever executed.

## Roles

| | Admin | Faculty | Student |
|---|---|---|---|
| Master data, rules, runs, edits, publish, audit | yes | no | no |
| Published timetable | all | all (own highlighted) | own class + batch only |
| Requests | review | create / view / cancel own | no |
| Drafts, sandboxes | yes | no | no |

## Security controls (and where they are tested)

| Control | Code | Test |
|---|---|---|
| Default-deny role gate on every admin route, denials audited | `security.require_roles` | `test_student_is_strictly_read_only_and_scoped`, `test_faculty_boundaries_and_idor` |
| Object-level checks (own requests, own sessions) | `api_public.py` | `test_faculty_boundaries_and_idor` |
| No mass assignment (`extra="forbid"` DTOs) | `schemas.py` | same |
| Server sessions: random id, only hash stored, HttpOnly, SameSite=Strict, idle + absolute timeout, revocation | `security.py` | `test_csrf_and_cross_origin`, `test_login_hardening` |
| CSRF token + same-origin check on every unsafe request | `security.current_user`, `main.py` | `test_csrf_and_cross_origin` |
| Password hashing (scrypt), login throttling, lockout, no account enumeration | `security.py`, `api_public.login` | `test_login_hardening` |
| Clerk token verification, default-deny linking, admin MFA | `clerk_auth.py` | `test_clerk_login_is_default_deny_and_role_comes_from_our_db` |
| Parameterised SQL only, strict input validation, safe error bodies | everywhere | `test_malformed_and_injection_input_fails_safely` |
| CSV import: schema-validated, formulas rejected; export neutralises formulas | `api_admin.py` | same |
| CSP, frame/sniff/referrer headers, body-size cap | `main.py` | `test_csrf_and_cross_origin` |
| Published versions and audit log immutable (DB triggers); one published version per term (partial unique index) | `models.py` | `test_publish_state_machine_and_immutability`, `test_concurrent_publish_leaves_exactly_one_published` |
| Optimistic concurrency on edits | `api_admin.bump` | `test_manual_edit_validation_locking_and_concurrency` |
| Solver abuse limits: queue cap, time cap, variable cap, rate limit, cancel | `services.py`, `engine.py` | `test_solver_statuses_are_mapped_precisely` |

Solver requirements map to `tests/test_solver.py` one test per acceptance criterion (reference
example, overlaps, parent/child, capacity, features, consecutive, weekly counts, availability,
fixed/locked, impossible input, statuses, preferences, candidates/repair).

## Production notes

* Run behind HTTPS with `TT_ENV=production` (secure cookies, HSTS, docs off, local login off, admin MFA on).
* Run **one** API process: the job queue and rate limiter live in memory.
* SQLite is a single file: restrict its permissions to the service account, keep it off any web
  root, and back it up with `sqlite3 data/timetable.db ".backup backup.db"`, then encrypt and store
  the copy elsewhere. To restore, stop the service and replace the file. Test the restore.
* Secrets come from the environment; `.env` and `data/` are git-ignored.

## Known limitations

* No Docker files and no separate worker process; long solves share CPU with the API.
* The initial Alembic revision creates the schema from the models; later changes are separate revisions (`0002` adds faculty preferences).
* XLSX and timetable-image import are not implemented (CSV import/export and print are).
* What-if "split/merge a batch" is simulated through strength changes, not by restructuring groups.
* With Clerk enabled, the CSP must allow inline styles for Clerk's components.
* Leave is modelled per weekday (the timetable is weekly), not per calendar date.
* Clerk sign-in has automated tests with locally signed tokens but has not yet been exercised
  against a live Clerk instance.

## References

OR-Tools CP-SAT https://developers.google.com/optimization/cp/cp_solver ·
OWASP Top 10 https://owasp.org/Top10/ · OWASP API Top 10 https://owasp.org/API-Security/ ·
OWASP Session Management Cheat Sheet · Clerk manual JWT verification
https://clerk.com/docs/guides/sessions/manual-jwt-verification
