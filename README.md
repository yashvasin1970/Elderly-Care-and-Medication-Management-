# Hearth: a care circle the elder runs

Hearth is a medication and appointment platform for older people, shared with the family members
and caretakers who support them. Most care apps treat an older person as a patient to be monitored.
Hearth starts from the opposite end: **the elder owns the circle**. They decide who joins, what each
person can do, and whether any change to their medicines goes ahead.

> **Challenge vertical:** Elderly Care & Medication Management Platform (PromptWars × Catalyst)

| | |
|---|---|
| Stack | Python 3.11+, Flask, SQLite, server-rendered HTML, a small amount of plain JavaScript |
| Runtime dependencies | 3 (Flask, python-dotenv, tzdata), plus gunicorn in the container |
| Tests | 184 automated tests (`pytest`), lint and format checks (`ruff`), GitHub Actions CI |
| Google services | Gemini (optional, fills in a medicine from a sentence), Cloud Run + Cloud Scheduler deployment |

---

## Contents

1. [The problem and how Hearth answers it](#1-the-problem-and-how-hearth-answers-it)
2. [Four ideas that go beyond a reminder app](#2-four-ideas-that-go-beyond-a-reminder-app)
3. [Approach and decision logic](#3-approach-and-decision-logic)
4. [How it works](#4-how-it-works)
5. [Run it](#5-run-it)
6. [Deploy on Google Cloud](#6-deploy-on-google-cloud)
7. [Security](#7-security)
8. [Accessibility](#8-accessibility)
9. [Efficiency](#9-efficiency)
10. [Testing](#10-testing)
11. [Project structure](#11-project-structure)
12. [Assumptions and limits](#12-assumptions-and-limits)

---

## 1. The problem and how Hearth answers it

Every requirement in the problem statement, and where Hearth meets it:

| The problem statement asks for… | Hearth |
|---|---|
| Elderly people managing daily medicines, appointments and routines | A large-type **Today** screen with *Took it / Remind me in 15 minutes / Skip* for each dose; appointments; a daily routine that reminders follow |
| Family and caretakers kept informed | Role-based circle members, alert emails, a weekly **Care Weather** summary, and an **Outbox** showing every message sent |
| A shared ecosystem with secure, passcode-based access | One **circle code** per elder plus a **personal passcode** per member; four roles (owner, caretaker, family, viewer) |
| Authorized members can view, add, edit and manage medication schedules | Medicine list and form, with permissions per role and the elder's approval for changes |
| Track adherence | Every dose is recorded as taken, skipped or missed; adherence by day and by medicine; doctor visit summary |
| Monitor upcoming doctor appointments | Appointment list, reminders the day before and two hours before, printable visit summary |
| Proactive email reminders for missed medicines, upcoming doses and appointments | A reminder job sends a heads-up before doses, a nudge, alerts when a dose is missed, appointment and refill reminders, all sent at most once |
| Go beyond a basic reminder app: smarter, more accessible, reliable, collaborative | See the four ideas below |
| **Elderly people remain at the centre of their own healthcare decisions** | The core design rule: elder-approved changes, the elder is always told first, the elder controls sharing and check-ins |

## 2. Four ideas that go beyond a reminder app

### 1. The elder approves changes
When a daughter edits her mother's evening dose, nothing changes silently. The change becomes a
**suggestion** that the elder sees as a plain before-and-after table:

| Detail | Now | Suggested |
|---|---|---|
| When | At bedtime | **With dinner** |

The elder approves or declines. Every change, decision, sign-in and recorded dose goes into a
**History** that everyone in the circle can read. The elder can also mark a person as *trusted*, so
their edits apply directly. Handing over control is also the elder's decision.

### 2. Reminders tied to daily life that learn the real routine
Doses are tied to moments like "15 min after breakfast", not just clock times. The elder sets when
breakfast usually happens, and every reminder tied to it follows. Hearth compares scheduled times
with when doses are actually confirmed. If breakfast pills are consistently confirmed around
8:40 instead of 8:00, it suggests: *"Shall I move the reminders to 8:40?"* It only suggests; the
elder decides.

### 3. Alerts that escalate gradually, and the elder hears first
How quickly anyone else is told depends on how important the elder says the medicine is. Skipping
a dose on purpose is recorded as the elder's choice and never triggers an alert. The quiet daily
check-in (*"no sign of activity yet today"*) is opt-out and uses app activity only. There is no
location tracking or camera.

### 4. Care Weather and the doctor visit summary
Instead of a dashboard of percentages, family get a short weekly "forecast" with patterns worth
acting on:

> ☀️ **Sunny.** Kamala Rao took 35 of 38 doses (92%), 2 missed, 1 skipped by choice.
> *Tuesdays are the hardest day (5 of 9 missed doses). An extra reminder that day may help.*

Before every appointment, Hearth prepares a printable **visit summary**. It lists the elder's own
questions first, then how each medicine was taken since the last visit, noted symptoms, and the
current medicine list. The elder's questions get asked, and the doctor sees real data.

**Also included:**
- **Refill forecasts:** run-out dates from stock and daily use, with a reminder 5 days ahead.
- **Missed-dose instructions:** only what the label or pharmacist says; Hearth never makes up its own advice.
- **Read aloud:** uses the browser's built-in speech.
- **Pill pictures:** colour and shape, so the elder can recognise each medicine at a glance.
- **Fill in from a sentence** (optional Gemini): a person always checks the form before it is saved.

## 3. Approach and decision logic

All care rules live in [`hearth/engine/`](hearth/engine): pure Python with no web framework, no
database and no clock of its own. Each function takes plain values, including "now", and returns
plain values. That keeps every rule explainable and testable to the minute.

### Scheduling ([`schedule.py`](hearth/engine/schedule.py))
- A **slot** is either *anchor + offset* (e.g. `breakfast + 15 min`) or a fixed time. The anchor
  times come from the elder's routine, so changing breakfast to 8:40 moves every breakfast dose.
- A dose only exists on a day if the medicine is running on that date, the weekday is switched on,
  and the slot existed at that moment. So a medicine added at 3 pm never shows a "missed" 8 am dose.
- Slots are never edited in place. A schedule change retires the old slot and creates a new one,
  so doses already recorded keep pointing at the schedule that was true at the time.

### The escalation ladder ([`escalation.py`](hearth/engine/escalation.py))

| How important (set by the elder) | Heads-up to the elder | Second reminder to the elder | Caretakers | Family |
|---|---|---|---|---|
| **Critical** (e.g. insulin) | 10 min before | 20 min after | 30 min after | 60 min after |
| **Important** | 10 min before | 30 min after | 60 min after | never |
| **Routine** (e.g. vitamins) | 10 min before | 60 min after | never | never (weekly summary only) |

- **Taken or skipped** cancels everything still pending.
- **"Remind me in 15 minutes"** moves the rest of the ladder back to start from the end of the
  snooze, but by at most 60 minutes in total, so snoozing can't silence alerts for a critical medicine.
- **No caretaker in the circle?** Family are told at the caretaker step instead, and only once.
- **Alerts more than 3 hours overdue are dropped** rather than sent late and out of context, for
  example after a restart.
- **Doses due at the same time share one email.** A dose with no record after 3 hours counts as missed.

### Learning the routine ([`rhythm.py`](hearth/engine/rhythm.py))
It looks at confirmed doses from the last 14 days, grouped by anchor (or by fixed slot). It needs
at least 5 confirmed doses and a median gap of 20 minutes or more. At least 70% of the doses must
lean the same way, so erratic days don't count as a routine. Entries logged hours later are ignored.
The suggested time is rounded to 5 minutes, and a dismissed suggestion stays dismissed.

### Care Weather ([`insights.py`](hearth/engine/insights.py))
Adherence rate: taken ÷ (taken + skipped + missed). ≥90% is ☀️ sunny, ≥75% ⛅ mostly sunny, ≥50% ☁️
cloudy, otherwise ⛈️ stormy. Patterns are simple counting rules over 28 days, each with one
practical suggestion:
- **Time of day:** at least 60% of misses in one part of the day.
- **Weekday:** at least 40% of misses on one weekday.
- **Medicine:** one medicine accounts for at least half the misses.
- **Visit days:** 2 or more misses on days with a doctor visit.
- **Skipping:** a medicine skipped by choice 3 or more times, worth raising with the doctor.

### Other rules
- **Refill** ([`refill.py`](hearth/engine/refill.py)): days left = stock ÷ (doses per day ×
  units per dose). Stock goes down when a dose is taken and back up when that is undone.
- **Quiet check-in:** fires only if the elder has opted in, it is past their chosen hour, and
  neither the elder nor anyone else has recorded anything today.
- **Sentence parsing** ([`parsing.py`](hearth/engine/parsing.py)): rules for doses, meal
  timings, "twice daily", "at 9:30 pm", "for 30 days" and similar. Gemini output, when used,
  passes through the same validation.

## 4. How it works

```
 Browser (HTML forms, large type)          Scheduler (in-app timer or Cloud Scheduler)
          │                                              │  POST /tasks/reminders (bearer token)
          ▼                                              ▼
 hearth/web/        pages, form validation, CSRF, sessions, permission checks
          │
 hearth/services/   use cases: record a dose, submit/decide a change, run reminders, send mail
          │                        │
 hearth/engine/     pure rules     hearth/repositories/   parameterised SQL, always scoped to one circle
                                   │
                                   SQLite
```

- **Signing in:** the circle code finds the circle, and the passcode identifies the member and
  their role. The session stores only IDs. The member is re-checked on every request, so removing
  someone or changing their passcode takes effect immediately.
- **The reminder job** ([`services/reminders.py`](hearth/services/reminders.py)) looks at
  yesterday and today for every circle. It works out which ladder steps are due, groups doses due
  at the same time, and writes each message under a unique key before sending. Running it twice
  sends nothing new.
- **Email** goes out through SMTP when configured. Without it, Hearth runs in **demo mode**: every
  message is recorded on the **Outbox** page, so the whole flow can be seen with no setup.

## 5. Run it

You need Python 3.11 or newer.

```bash
git clone https://github.com/yashvasin1970/Elderly-Care-and-Medication-Management-.git
cd Elderly-Care-and-Medication-Management-
python -m venv .venv
```

Activate the virtual environment (Windows: `.venv\Scripts\activate`; macOS/Linux:
`source .venv/bin/activate`), then:

```bash
pip install -r requirements-dev.txt
cp .env.example .env        # optional (Windows: copy). Every setting has a safe default.
flask --app hearth run
```

Open <http://localhost:5000>. With `HEARTH_DEMO_MODE=1` (as in `.env.example`), a fictional example
circle with five weeks of history is loaded automatically. Sign in with circle code **`DEMO-2026`**
and one of these passcodes:

| Sign in as | Role | Passcode | Try this |
|---|---|---|---|
| Kamala Rao | Circle owner (elder) | `sunrise-2468` | Approve Priya's suggestion, accept the 8:40 routine suggestion, record a dose |
| Joseph | Caretaker (trusted) | `caring-1357` | Record doses, edit a medicine (applies directly) |
| Priya | Family | `family-8642` | Suggest a change and see it wait for Kamala |
| Dr. Mehta | Viewer | `doctor-9753` | Read-only: open a visit summary |

These passcodes are fictional, for the demo only. Other useful commands:

```bash
flask --app hearth send-reminders   # run the reminder job once
flask --app hearth seed-demo        # add the example circle to an existing database
pytest                              # run the tests
ruff check . && ruff format --check .
```

All settings are listed in [`.env.example`](.env.example). The main ones:

| Setting | Purpose |
|---|---|
| `HEARTH_SECRET_KEY` | Signs session cookies. Required in production. |
| `HEARTH_RUN_SCHEDULER` | Run the reminder check inside the app every minute. |
| `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD` | Send real email, for example through Gmail with an app password. |
| `GEMINI_API_KEY` | Enable Gemini for "fill in the form from a sentence". |
| `HEARTH_TASK_TOKEN` | Allow an external scheduler to call `POST /tasks/reminders`. |

## 6. Deploy on Google Cloud

The included [`Dockerfile`](Dockerfile) runs Hearth with gunicorn as a non-root user.

```bash
gcloud run deploy hearth --source . --region asia-south1 --allow-unauthenticated \
  --set-env-vars HEARTH_DEMO_MODE=1 \
  --set-secrets HEARTH_SECRET_KEY=hearth-secret:latest,HEARTH_TASK_TOKEN=hearth-task-token:latest,GEMINI_API_KEY=gemini-key:latest
```

Then set `HEARTH_BASE_URL` to the service URL, and have **Cloud Scheduler** run the reminder job
every minute:

```bash
gcloud scheduler jobs create http hearth-reminders --schedule "* * * * *" \
  --uri "https://YOUR-SERVICE-URL/tasks/reminders" --http-method POST \
  --headers "Authorization=Bearer YOUR-TASK-TOKEN"
```

## 7. Security

| Risk | What Hearth does |
|---|---|
| Stolen database | Passcodes are stored only as salted **scrypt** hashes; nothing secret is in the repo (`.env` is git-ignored) |
| Passcode guessing | Sign-in locks for 15 minutes after 5 failures per visitor per circle (also capped at 25 per circle and 20 per address); counters are kept in the database |
| Finding out which circle codes exist | One message for every failure, plus a decoy hash check when the circle code is unknown, so both cases take the same time |
| One circle reading another's data | Every query is filtered by the signed-in circle; IDs from another circle return 404 (tested) |
| Doing more than your role allows | Central role-to-permission table (`access.py`); every page and action checks it; the elder cannot be removed |
| Cross-site request forgery | A per-session token on every form, compared in constant time |
| Cross-site scripting | Jinja auto-escaping, plus a strict Content-Security-Policy (`script-src 'self'`, no inline scripts) |
| Session theft or misuse | `HttpOnly`, `SameSite=Lax` and (in production) `Secure` cookies; a new session on sign-in; removal or a new passcode ends sessions at once |
| Clickjacking, sniffing, caching | `frame-ancestors 'none'`, `X-Frame-Options`, `nosniff`, `Referrer-Policy`, `Cache-Control: no-store` on pages |
| Open redirects | Redirect targets are limited to paths on the same site |
| Oversized or malformed input | 64 KB request limit; every field validated and length-capped server-side |
| AI misuse | Gemini receives only the typed sentence, with the key sent in a header; output is schema-validated and only pre-fills a form; calls are rate-limited per member |
| Scheduler endpoint | Hidden unless a token is configured, and needs a bearer token compared in constant time |
| Health advice | Hearth never generates medical advice; it only shows instructions copied from the label |

## 8. Accessibility

Designed for older eyes and hands, aiming for **WCAG 2.2 AA**:

- **Type and targets:** 18 px base type, with Large and Extra large settings. Buttons are at least
  48 px tall, and the main dose actions are full width.
- **Contrast:** every text colour pair is at least 6.8:1 in both light and dark mode (AA asks for
  4.5:1), and borders are above 3:1. There is also a **High contrast** setting, and status is
  always written in words (never shown by colour alone).
- **Keyboard and screen readers:** a skip link, landmarks, one `h1` per page and a logical heading
  order, labels on every field, and hints and errors linked to each field with `aria-describedby`.
- **Form errors:** a summary at the top that receives focus. Each button names its medicine for
  screen readers ("Took it: Metformin 500 mg").
- **Read aloud** on the Today screen, using the browser's built-in speech.
- **Works without JavaScript:** every action is a plain HTML form. Script only adds conveniences.
- **Other details:** `prefers-reduced-motion` is respected, a print stylesheet formats the visit
  summary, and dates use `<time>` elements.

## 9. Efficiency

- **Small footprint:** three runtime dependencies, no front-end build step, no web fonts or
  images beyond one small SVG icon. The repository is a few hundred KB.
- **Bounded reminder job:** each run looks at only two days of doses per circle and loads each
  circle's data once. Indexes cover every lookup.
- **No wasted email:** doses due together share one email, and a unique key per message makes
  repeated runs cost nothing.
- **Light sessions:** checking a session costs a couple of indexed lookups per request; static
  files skip the database entirely.
- **Data kept in step:** stock changes and dose records are saved in one transaction.

## 10. Testing

184 tests run in a few seconds, each against a fresh database and a clock pinned to a known moment:

| File | Covers |
|---|---|
| `test_schedule.py`, `test_escalation.py` | Slot timing, routine anchors, the alert ladder, snooze caps, statuses, appointment and check-in rules |
| `test_rhythm.py`, `test_insights.py` | Routine learning thresholds; Care Weather ratings and every pattern rule |
| `test_refill_and_parsing.py` | Run-out forecasts; sentence parsing; validation of untrusted AI output |
| `test_reminders.py` | The full job: who gets what and when, sent once only, grouping, snooze, fallbacks, mail failures |
| `test_services.py` | Consent rules, schedule history, passcode rules, the Gemini client (with a fake network) |
| `test_security.py` | Sign-in, lockout, hashing, CSRF, role permissions, isolation between circles, escaping, headers, cookies |
| `test_web.py` | End-to-end page flows for every role, including the approval flow, doses, appointments and the demo |

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs lint, format and tests on Python 3.11
and 3.13 for every push.

## 11. Project structure

```
hearth/
  engine/          pure decision logic: schedule, escalation, rhythm, insights, refill, parsing
  repositories/    SQL for profiles, medicines, doses, appointments, journal/history/outbox
  services/        care (doses and views), consent, reminders, mailer, messages, passcodes,
                   throttle, gemini, demo
  web/             pages: auth, today, medications, appointments, circle, insights, tasks;
                   forms.py (validation) and security.py (sessions, CSRF, headers, permissions)
  templates/       Jinja templates (accessible markup, shared macros)
  static/          app.css, app.js, icon.svg
  schema.sql       database schema
tests/             184 tests (see above)
Dockerfile, .env.example, pyproject.toml, requirements*.txt
```

## 12. Assumptions and limits

- **One elder per circle.** A family supporting two parents creates two circles. Each member
  signs in to one circle at a time.
- **Passcodes are the access method,** as the problem statement asks. Each member has their own,
  so actions are attributable and access can be removed one person at a time. Passcodes must be
  unique within a circle because they identify the member.
- **Times use the elder's timezone,** set on the circle, since that is where the medicines are taken.
- **Email is the alert channel,** as specified. With no SMTP server configured, messages go to
  the in-app Outbox instead (demo mode).
- **SQLite is used for simplicity.** On Cloud Run the container's disk is temporary, so the
  hosted version is a demo. Durable hosting would put the database on a mounted volume or move
  the repository layer to Cloud SQL; only `hearth/repositories/` would change.
- **No medical advice.** Hearth passes on reminders and records. Missed-dose instructions are
  whatever was copied from the label, and every insight is a simple count.
- **The demo circle is entirely fictional** (people, medicines and history). Its history is
  generated relative to the current date each time a fresh database starts.
