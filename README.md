# SIP Tracker

SIP Tracker is a Flask application for student internship attendance. Students can clock in and out, record lunch and coffee breaks, view history, send messages, and export their records. Supervisors can enroll students, review and edit assigned attendance, and export their own students' records. SIP center supervisors can monitor center-wide students.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:SECRET_KEY = 'replace-with-a-long-random-value'
python migrate_db.py
python app.py
```

Open `http://127.0.0.1:5000`. Public registration supports Student, Supervisor, and SIP Center Supervisor accounts. In a public deployment, restrict elevated-role registration with an invitation or administrator approval workflow.

## Configuration

- `SECRET_KEY`: required in production; use a long random value.
- `DATABASE_URL`: optional SQLAlchemy URL. The default is `instance/interns.db`.
- `FLASK_HOST` and `FLASK_PORT`: optional bind settings for local runs.
- `FLASK_DEBUG`: set to `true` only for local development.

Run `python migrate_db.py` after pulling schema changes and before starting a production process. Uploaded profile images are stored in `instance/profile_photos` and are served only to their owning authenticated user.

## Tests

```powershell
pytest -q
```

The test suite covers password hashing, student-only registration, CSRF rejection, supervisor ownership, scoped exports, and attendance deletion. The `client/` directory is currently an unused Vite starter and is not part of the Flask runtime.

## Production

Set a production `SECRET_KEY`, use HTTPS, keep `FLASK_DEBUG` unset, run the migration command during deployment, and serve the Flask app through a production WSGI server. SQLite is suitable for a small single-process deployment; use PostgreSQL for concurrent or larger installations.
