# SIP Tracker

SIP Tracker is a Flask application for student internship attendance. Students can clock in and out, record lunch time-out and afternoon time-in punches, review progress against an internship-hour goal, request attendance corrections or absences, message their assigned supervisor, view filtered history, and export reports. New attendance entries use the four daily punches (morning time-in, lunch time-out, afternoon time-in, final time-out); coffee punches are disabled while legacy coffee data remains available in existing records. Missed lunch punches can be submitted with actual times for supervisor review. Supervisors can enroll students, approve or reject attendance and requests, message assigned students, monitor completion, and export scoped reports. SIP center supervisors can monitor center-wide students. Administrators can manage accounts and roster CSVs, configure attendance policy and holidays, and review the audit history.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:SECRET_KEY = 'replace-with-a-long-random-value'
python migrate_db.py
python app.py
```

To create or update the administrator during migration, set `ADMIN_USERNAME` and `ADMIN_PASSWORD` before running `migrate_db.py`:

```powershell
$env:ADMIN_USERNAME = 'admin'
$env:ADMIN_PASSWORD = 'change-this-password'
python migrate_db.py
```

The admin account can manage every user from the Admin Dashboard. Admin accounts cannot be created through public registration. The migration's legacy fallback credentials are `admin` / `admin`; always provide a unique `ADMIN_USERNAME` and strong `ADMIN_PASSWORD` before a production migration.

Open `http://127.0.0.1:5000`. Public registration supports Student, Supervisor, and SIP Center Supervisor accounts. In a public deployment, restrict elevated-role registration with an invitation or administrator approval workflow.

## Configuration

- `SECRET_KEY`: required in production; use a long random value.
- `DATABASE_URL`: optional SQLAlchemy URL. The default is `instance/interns.db`.
- `FLASK_HOST` and `FLASK_PORT`: optional bind settings for local runs.
- `FLASK_DEBUG`: set to `true` only for local development.

Run `python migrate_db.py` after pulling schema changes and before starting a production process. The migration creates new feature tables, adds compatible columns and lookup indexes to existing SQLite or PostgreSQL databases, backfills legacy attendance dates, and ensures the default attendance policy. Dashboard summaries use database aggregates, and records, center rosters, and conversations are paginated to avoid loading an unbounded history into memory. Uploaded profile images are stored in `instance/profile_photos` and are served only to their owning authenticated user.

### Email reminders

In-app notifications work without an email provider. To send opted-in email alerts, configure SMTP in the process environment; do not store credentials in source control or commit a `.env` file:

```powershell
$env:SMTP_HOST = 'smtp.example.com'
$env:SMTP_PORT = '587'
$env:SMTP_FROM = 'attendance@example.com'
$env:APP_BASE_URL = 'https://attendance.example.com'
$env:SMTP_USERNAME = 'attendance@example.com'
$env:SMTP_PASSWORD = '<set through your deployment secret manager>'
$env:SMTP_USE_TLS = 'true'
```

For an implicit-TLS provider, set `SMTP_USE_SSL=true` and `SMTP_USE_TLS=false`. `SMTP_USERNAME`, `SMTP_PASSWORD`, and `SMTP_PORT` are optional when the provider does not require authentication or uses a different port. `APP_BASE_URL` is optional but recommended so email notifications contain a full link to the deployment. The application does not send email until `SMTP_HOST` and `SMTP_FROM` are configured. Users must explicitly opt in to email notifications; imported roster rows remain opted out unless the optional `email_notifications` column is set to `yes`, `true`, `1`, or `on`.

Set the timezone, work schedule, reminder time, grace period, break rules, and holidays in **Admin → Attendance policy**. Schedule the following Flask CLI commands through the host's scheduler (Windows Task Scheduler, cron, or an equivalent service):

```powershell
flask --app app generate-attendance-reminders
flask --app app generate-lunch-reminders
flask --app app dispatch-notification-emails
```

Run `generate-attendance-reminders` once a day after the configured reminder time. Schedule `generate-lunch-reminders` every 5 minutes from 5 minutes before the configured lunch window through 5 minutes after it ends; adjust the scheduler window when the lunch policy changes. Lunch reminder generation is idempotent, skips non-workdays and holidays, reminds students to record lunch-out and return punches, and alerts both students and their assigned supervisors when lunch-out was missed. Reminder generation also alerts students and supervisors about earlier attendance records that have no clock-out. Run email dispatch regularly to drain the queue. SMTP failures are logged and retained with a failed status in the notification queue. After correcting SMTP configuration, an operator can explicitly requeue up to 100 failed messages with `flask --app app retry-failed-notification-emails`, then run email dispatch again.

### Roster CSV format

The admin roster import accepts UTF-8 CSV with the required columns `username,password,role,email,supervisor_username`; `email_notifications` is optional and defaults to opt-out. Imports are validated before writing and limited to 500 accounts per file. Supported roles are `student`, `supervisor`, `sip_center_supervisor`, and `admin`. Student supervisor usernames must refer to existing supervisor accounts. Exported roster values are escaped to prevent spreadsheet formula execution.

Student and supervisor attendance reports accept date and status filters. CSV exports retain those filters; supervisors can only export records belonging to their assigned students. Attendance corrections, missed-lunch corrections, and absences remain pending until the assigned supervisor reviews them. Lunch time-out is accepted only during the configured lunch window; the afternoon time-in may occur after the window closes. Final time-out is blocked while a lunch break is still open. Only approved, completed attendance contributes to internship-hour progress.

## Tests

```powershell
pytest -q
```

The test suite covers password hashing, student-only registration, CSRF rejection, supervisor ownership, scoped exports, account management, attendance and missed-lunch requests/review, lunch window enforcement, message access, report filters, notification ownership, reminder idempotency, and role-specific page rendering. The `client/` directory is currently an unused Vite starter and is not part of the Flask runtime.

## Production

Set a production `SECRET_KEY`, use HTTPS, keep `FLASK_DEBUG` unset, run the migration command during deployment, and serve the Flask app through a production WSGI server. SQLite is suitable for a small single-process deployment; use PostgreSQL for concurrent or larger installations.
