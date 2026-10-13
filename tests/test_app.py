import os
import re
import tempfile
import io
from datetime import date, datetime, time, timedelta

import pytest

DATABASE_DIR = tempfile.TemporaryDirectory()
DATABASE_PATH = os.path.join(DATABASE_DIR.name, 'test.db').replace('\\', '/')
os.environ['DATABASE_URL'] = f'sqlite:///{DATABASE_PATH}'
os.environ['SECRET_KEY'] = 'test-secret'

import app as app_module
from app import Attendance, AttendancePolicy, AttendanceRequest, Message, Notification, User, app, db, aggregate_attendance, ensure_default_policy, net_work_hours, send_queued_emails
from werkzeug.security import generate_password_hash, check_password_hash


def csrf(client):
    response = client.get('/login')
    return re.search(r'name="_csrf_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)


def login(client, username, password):
    token = csrf(client)
    return client.post('/login', data={
        '_csrf_token': token,
        'username': username,
        'password': password,
    })


def setup_module():
    app.config.update(TESTING=True)
    with app.app_context():
        db.create_all()


def teardown_module():
    with app.app_context():
        db.drop_all()
        db.session.remove()
        db.engine.dispose()
    DATABASE_DIR.cleanup()


def test_registration_hashes_password_and_accepts_supported_role():
    client = app.test_client()
    token = csrf(client)
    response = client.post('/register', data={
        '_csrf_token': token,
        'username': 'new-student',
        'password': 'long-enough-password',
        'role': 'supervisor',
    })
    assert response.status_code == 302
    with app.app_context():
        user = User.query.filter_by(username='new-student').one()
        assert user.role == 'supervisor'
        assert user.password != 'long-enough-password'


def test_supervisor_can_only_read_edit_export_owned_records():
    with app.app_context():
        owner = User(username='owner', password=generate_password_hash('password123'), role='supervisor')
        other = User(username='other', password=generate_password_hash('password123'), role='supervisor')
        db.session.add_all([owner, other])
        db.session.flush()
        owned_student = User(username='owned-student', password=generate_password_hash('password123'), role='student', supervisor_id=owner.id)
        foreign_student = User(username='foreign-student', password=generate_password_hash('password123'), role='student', supervisor_id=other.id)
        db.session.add_all([owned_student, foreign_student])
        db.session.flush()
        owned = Attendance(user_id=owned_student.id, time_in=datetime.now() - timedelta(hours=2), time_out=datetime.now())
        foreign = Attendance(user_id=foreign_student.id, time_in=datetime.now() - timedelta(hours=2), time_out=datetime.now())
        db.session.add_all([owned, foreign])
        db.session.commit()
        owned_id = owned.id
        foreign_id = foreign.id

    client = app.test_client()
    login(client, 'owner', 'password123')
    assert client.get('/records?student=foreign-student').status_code == 302
    csv_response = client.get('/export_all_csv')
    csv_text = csv_response.get_data(as_text=True)
    assert 'owned-student' in csv_text
    assert 'foreign-student' not in csv_text
    with client.session_transaction() as session:
        token = session['_csrf_token']
    assert client.post(f'/supervisor_dashboard', data={
        '_csrf_token': token,
        'record_id': str(foreign_id),
        'edit_time_in': '2025-01-01 09:00',
    }).status_code == 302
    assert client.post(f'/attendance/{owned_id}/delete', data={'_csrf_token': token}).status_code == 302


def test_state_changing_request_requires_csrf():
    client = app.test_client()
    assert client.post('/register', data={'username': 'missing-token', 'password': 'password123'}).status_code == 400


def test_default_admin_credentials_are_admin_admin():
    with app.app_context():
        db.session.query(User).delete()
        db.session.commit()
        os.environ.pop('ADMIN_USERNAME', None)
        os.environ.pop('ADMIN_PASSWORD', None)

        from migrate_db import ensure_admin_user
        ensure_admin_user()

        admin = User.query.filter_by(username='admin').one()
        assert admin.role == 'admin'
        assert check_password_hash(admin.password, 'admin')


def test_admin_can_manage_users_and_cannot_remove_own_access():
    with app.app_context():
        admin = User(username='admin-user', password=generate_password_hash('password123'), role='admin')
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id

    client = app.test_client()
    assert login(client, 'admin-user', 'password123').status_code == 302
    assert client.get('/dashboard').headers['Location'].endswith('/admin_dashboard')
    assert client.get('/admin_dashboard').status_code == 200
    with client.session_transaction() as session:
        token = session['_csrf_token']

    response = client.post('/admin/users/create', data={
        '_csrf_token': token,
        'username': 'managed-student',
        'password': 'password123',
        'role': 'student',
    })
    assert response.status_code == 302
    with app.app_context():
        managed = User.query.filter_by(username='managed-student').one()
        managed_id = managed.id
        assert managed.password != 'password123'

    assert client.post(f'/admin/users/{managed_id}/update', data={
        '_csrf_token': token,
        'username': 'managed-supervisor',
        'password': '',
        'role': 'student',
    }).status_code == 302
    assert 'managed-supervisor' in client.get('/admin_dashboard').get_data(as_text=True)
    with app.app_context():
        assert db.session.get(User, managed_id).role == 'student'
        assert db.session.get(User, managed_id).username == 'managed-supervisor'

    assert client.post(f'/admin/users/{managed_id}/activate', data={'_csrf_token': token}).status_code == 302
    with app.app_context():
        assert db.session.get(User, managed_id).account_active is False
    assert client.post(f'/admin/users/{managed_id}/activate', data={'_csrf_token': token}).status_code == 302
    with app.app_context():
        assert db.session.get(User, managed_id).account_active is True

    assert client.post(f'/admin/users/{managed_id}/delete', data={'_csrf_token': token}).status_code == 302
    with app.app_context():
        assert db.session.get(User, managed_id) is None

    assert client.post(f'/admin/users/{admin_id}/delete', data={'_csrf_token': token}).status_code == 302
    assert client.post(f'/admin/users/{admin_id}/update', data={
        '_csrf_token': token,
        'username': 'admin-user',
        'password': '',
        'role': 'student',
    }).status_code == 302


def test_admin_routes_are_restricted_and_public_registration_rejects_admin():
    client = app.test_client()
    assert client.get('/admin_dashboard').status_code == 302
    token = csrf(client)
    response = client.post('/register', data={
        '_csrf_token': token,
        'username': 'public-admin',
        'password': 'password123',
        'role': 'admin',
    })
    assert response.status_code == 302
    with app.app_context():
        assert User.query.filter_by(username='public-admin').first() is None


def create_supervised_student(suffix):
    supervisor = User(
        username=f'supervisor-{suffix}',
        password=generate_password_hash('password123'),
        role='supervisor',
    )
    db.session.add(supervisor)
    db.session.flush()
    student = User(
        username=f'student-{suffix}',
        password=generate_password_hash('password123'),
        role='student',
        supervisor_id=supervisor.id,
    )
    db.session.add(student)
    db.session.commit()
    return supervisor, student


def client_csrf(client):
    with client.session_transaction() as session:
        return session['_csrf_token']


def test_absence_request_requires_assigned_supervisor_approval():
    with app.app_context():
        supervisor, student = create_supervised_student('absence')
        request_date = app_module.local_today() - timedelta(days=1)
        student_id = student.id
        supervisor_id = supervisor.id

    student_client = app.test_client()
    login(student_client, 'student-absence', 'password123')
    response = student_client.post('/attendance/requests', data={
        '_csrf_token': client_csrf(student_client),
        'request_type': 'absence',
        'requested_date': request_date.isoformat(),
        'reason': 'Documented appointment.',
    })
    assert response.status_code == 302
    with app.app_context():
        attendance_request = AttendanceRequest.query.filter_by(student_id=student_id).one()
        request_id = attendance_request.id
        assert attendance_request.status == 'pending'

    supervisor_client = app.test_client()
    login(supervisor_client, 'supervisor-absence', 'password123')
    response = supervisor_client.post(
        f'/attendance/requests/{request_id}/review',
        data={'_csrf_token': client_csrf(supervisor_client), 'decision': 'approve'},
    )
    assert response.status_code == 302
    with app.app_context():
        record = Attendance.query.filter_by(user_id=student_id, work_date=request_date).one()
        assert record.approval_status == 'approved'
        assert record.time_in is None
        assert Notification.query.filter_by(user_id=student_id).count() == 1
        assert db.session.get(User, supervisor_id) is not None


def test_attendance_correction_updates_only_assigned_student_record():
    with app.app_context():
        supervisor, student = create_supervised_student('correction')
        record_date = app_module.local_today() - timedelta(days=2)
        record = Attendance(
            user_id=student.id,
            work_date=record_date,
            time_in=datetime.combine(record_date, time(9, 10)),
            time_out=datetime.combine(record_date, time(16, 30)),
            approval_status='pending',
        )
        db.session.add(record)
        db.session.commit()
        student_id, supervisor_id, record_id = student.id, supervisor.id, record.id

    student_client = app.test_client()
    login(student_client, 'student-correction', 'password123')
    response = student_client.post('/attendance/requests', data={
        '_csrf_token': client_csrf(student_client),
        'request_type': 'correction',
        'requested_date': record_date.isoformat(),
        'attendance_id': str(record_id),
        'requested_time_in': f'{record_date.isoformat()}T08:55',
        'requested_time_out': f'{record_date.isoformat()}T16:45',
        'reason': 'Corrected to match the signed timesheet.',
    })
    assert response.status_code == 302

    supervisor_client = app.test_client()
    login(supervisor_client, 'supervisor-correction', 'password123')
    with app.app_context():
        request_id = AttendanceRequest.query.filter_by(student_id=student_id).one().id
    response = supervisor_client.post(
        f'/attendance/requests/{request_id}/review',
        data={'_csrf_token': client_csrf(supervisor_client), 'decision': 'approve', 'review_note': 'Verified.'},
    )
    assert response.status_code == 302
    with app.app_context():
        corrected = db.session.get(Attendance, record_id)
        assert corrected.approval_status == 'approved'
        assert corrected.approved_by == supervisor_id
        assert corrected.time_in == datetime.combine(record_date, time(8, 55))
        assert corrected.time_out == datetime.combine(record_date, time(16, 45))

def test_lunch_punch_window_and_afternoon_return_are_enforced(monkeypatch):
    with app.app_context():
        _, student = create_supervised_student('lunch-window')
        policy = ensure_default_policy()
        policy.lunch_window_start = '12:00'
        policy.lunch_window_end = '13:00'
        today = app_module.local_today()
        record = Attendance(
            user_id=student.id,
            work_date=today,
            time_in=datetime.combine(today, time(9)),
            approval_status='pending',
        )
        db.session.add(record)
        db.session.commit()
        record_id = record.id

    now = [datetime.combine(today, time(13, 1))]
    monkeypatch.setattr(app_module, 'local_now', lambda: now[0])
    client = app.test_client()
    login(client, 'student-lunch-window', 'password123')
    token = client_csrf(client)

    client.post('/break_start', data={'_csrf_token': token, 'break_type': 'coffee'})
    with app.app_context():
        assert db.session.get(Attendance, record_id).coffee_start is None

    client.post('/break_start', data={'_csrf_token': token, 'break_type': 'lunch'})
    with app.app_context():
        assert db.session.get(Attendance, record_id).lunch_start is None

    now[0] = datetime.combine(today, time(12, 30))
    client.post('/break_start', data={'_csrf_token': token, 'break_type': 'lunch'})
    with app.app_context():
        assert db.session.get(Attendance, record_id).lunch_start == now[0]

    now[0] = datetime.combine(today, time(13, 5))
    client.post('/time_out', data={'_csrf_token': token})
    with app.app_context():
        opened = db.session.get(Attendance, record_id)
        assert opened.time_out is None
        assert opened.lunch_end is None

    client.post('/break_end', data={'_csrf_token': token, 'break_type': 'lunch'})
    with app.app_context():
        resumed = db.session.get(Attendance, record_id)
        assert resumed.lunch_end == now[0]

def test_student_can_request_and_supervisor_can_approve_missed_lunch_punches(monkeypatch):
    with app.app_context():
        supervisor, student = create_supervised_student('lunch-correction')
        policy = ensure_default_policy()
        policy.lunch_window_start = '12:00'
        policy.lunch_window_end = '13:00'
        today = app_module.local_today()
        record = Attendance(
            user_id=student.id,
            work_date=today,
            time_in=datetime.combine(today, time(9)),
            time_out=datetime.combine(today, time(17)),
            approval_status='pending',
        )
        db.session.add(record)
        db.session.commit()
        student_id, record_id, supervisor_id = student.id, record.id, supervisor.id
        actual_lunch_out = datetime.combine(today, time(12, 10))
        actual_return = datetime.combine(today, time(13, 5))

    monkeypatch.setattr(
        app_module,
        'local_now',
        lambda: datetime.combine(today, time(17, 30)),
    )
    student_client = app.test_client()
    login(student_client, 'student-lunch-correction', 'password123')
    dashboard = student_client.get('/dashboard')
    assert dashboard.status_code == 200
    assert 'Request lunch correction' in dashboard.get_data(as_text=True)
    invalid = student_client.post('/attendance/requests', data={
        '_csrf_token': client_csrf(student_client),
        'request_type': 'lunch_correction',
        'requested_date': today.isoformat(),
        'attendance_id': str(record_id),
        'requested_lunch_start': datetime.combine(today, time(13)).strftime('%Y-%m-%dT%H:%M'),
        'requested_lunch_end': datetime.combine(today, time(13, 5)).strftime('%Y-%m-%dT%H:%M'),
        'reason': 'Testing an invalid lunch-out time.',
    })
    assert invalid.status_code == 302
    with app.app_context():
        assert AttendanceRequest.query.filter_by(
            student_id=student_id,
            request_type='lunch_correction',
        ).count() == 0
    response = student_client.post('/attendance/requests', data={
        '_csrf_token': client_csrf(student_client),
        'request_type': 'lunch_correction',
        'requested_date': today.isoformat(),
        'attendance_id': str(record_id),
        'requested_lunch_start': actual_lunch_out.strftime('%Y-%m-%dT%H:%M'),
        'requested_lunch_end': actual_return.strftime('%Y-%m-%dT%H:%M'),
        'reason': 'I forgot to record my lunch-out and return.',
    })
    assert response.status_code == 302
    with app.app_context():
        attendance_request = AttendanceRequest.query.filter_by(
            student_id=student_id,
            request_type='lunch_correction',
        ).one()
        request_id = attendance_request.id
        assert attendance_request.status == 'pending'

    supervisor_client = app.test_client()
    login(supervisor_client, 'supervisor-lunch-correction', 'password123')
    response = supervisor_client.post(
        f'/attendance/requests/{request_id}/review',
        data={
            '_csrf_token': client_csrf(supervisor_client),
            'decision': 'approve',
            'review_note': 'Verified against the signed timesheet.',
        },
    )
    assert response.status_code == 302
    with app.app_context():
        corrected = db.session.get(Attendance, record_id)
        assert corrected.lunch_start == actual_lunch_out
        assert corrected.lunch_end == actual_return
        assert corrected.approval_status == 'approved'
        assert corrected.approved_by == supervisor_id
        assert corrected.correction_note == 'I forgot to record my lunch-out and return.'
        approved_audit = app_module.AuditLog.query.filter_by(
            action='attendance_lunch_correction_approved',
            target_username='student-lunch-correction',
        ).one()
        assert 'lunch-out 12:10' in approved_audit.details
        assert 'afternoon time-in 13:05' in approved_audit.details

def test_student_can_clock_in_on_a_date_with_an_approved_absence():
    with app.app_context():
        _, student = create_supervised_student('approved-absence')
        today = app_module.local_today()
        record = Attendance(
            user_id=student.id,
            work_date=today,
            approval_status='approved',
        )
        db.session.add(record)
        db.session.commit()
        student_id, record_id = student.id, record.id

    client = app.test_client()
    login(client, 'student-approved-absence', 'password123')
    response = client.post('/time_in', data={'_csrf_token': client_csrf(client)})
    assert response.status_code == 302
    with app.app_context():
        records = Attendance.query.filter_by(user_id=student_id, work_date=today).all()
        assert len(records) == 1
        assert records[0].id == record_id
        assert records[0].time_in is not None
        assert records[0].approval_status == 'pending'


def test_supervisor_reviews_completed_attendance_and_cannot_review_foreign_records():
    with app.app_context():
        supervisor, student = create_supervised_student('direct-review')
        foreign_supervisor, foreign_student = create_supervised_student('direct-review-foreign')
        day = app_module.local_today() - timedelta(days=1)
        own = Attendance(
            user_id=student.id,
            work_date=day,
            time_in=datetime.combine(day, time(9)),
            time_out=datetime.combine(day, time(17)),
            approval_status='pending',
        )
        foreign = Attendance(
            user_id=foreign_student.id,
            work_date=day,
            time_in=datetime.combine(day, time(9)),
            time_out=datetime.combine(day, time(17)),
            approval_status='pending',
        )
        db.session.add_all([own, foreign])
        db.session.commit()
        own_id, foreign_id, student_id = own.id, foreign.id, student.id

    client = app.test_client()
    login(client, 'supervisor-direct-review', 'password123')
    response = client.post(
        f'/supervisor/attendance/{foreign_id}/review',
        data={'_csrf_token': client_csrf(client), 'decision': 'approve'},
    )
    assert response.status_code == 404
    response = client.post(
        f'/supervisor/attendance/{own_id}/review',
        data={'_csrf_token': client_csrf(client), 'decision': 'approve'},
    )
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Attendance, own_id).approval_status == 'approved'
        assert db.session.get(Attendance, foreign_id).approval_status == 'pending'
        assert Notification.query.filter_by(user_id=student_id, title='Attendance approved').count() == 1


def test_messages_are_scoped_to_assigned_student_and_supervisor():
    with app.app_context():
        supervisor, student = create_supervised_student('messages')
        create_supervised_student('foreign')
        supervisor_id, student_id = supervisor.id, student.id

    student_client = app.test_client()
    login(student_client, 'student-messages', 'password123')
    response = student_client.post('/messages', data={
        '_csrf_token': client_csrf(student_client),
        'partner_id': str(supervisor_id),
        'content': 'Please review my attendance.',
    })
    assert response.status_code == 302

    foreign_client = app.test_client()
    login(foreign_client, 'supervisor-foreign', 'password123')
    assert foreign_client.get(f'/messages?partner={student_id}').status_code == 302

    supervisor_client = app.test_client()
    login(supervisor_client, 'supervisor-messages', 'password123')
    response = supervisor_client.get(f'/messages?partner={student_id}')
    assert response.status_code == 200
    assert b'Please review my attendance.' in response.data
    with app.app_context():
        message = Message.query.filter_by(sender_id=student_id, receiver_id=supervisor_id).one()
        assert message.read_at is not None


def test_reminder_cli_is_idempotent_and_flags_missing_clock_out(monkeypatch):
    with app.app_context():
        policy = ensure_default_policy()
        today = app_module.local_today()
        policy.workdays = str(today.weekday())
        policy.reminders_enabled = True
        policy.reminder_time = '08:00'
        supervisor, student = create_supervised_student('reminder')
        yesterday = today - timedelta(days=1)
        incomplete = Attendance(
            user_id=student.id,
            work_date=yesterday,
            time_in=datetime.combine(yesterday, time(9)),
            approval_status='pending',
        )
        db.session.add(incomplete)
        db.session.commit()
        student_id, supervisor_id = student.id, supervisor.id

    monkeypatch.setattr(
        app_module,
        'local_now',
        lambda: datetime.combine(today, time(10)),
    )
    runner = app.test_cli_runner()
    first = runner.invoke(args=['generate-attendance-reminders'])
    second = runner.invoke(args=['generate-attendance-reminders'])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    with app.app_context():
        assert Notification.query.filter_by(user_id=student_id, title='Attendance reminder').count() == 1
        assert Notification.query.filter_by(user_id=student_id, title='Missing clock-out').count() == 1
        assert Notification.query.filter_by(
            user_id=supervisor_id,
            title='Missing clock-out: student-reminder',
        ).count() == 1


def test_lunch_reminders_repeat_safely_and_escalate_missed_punch(monkeypatch):
    with app.app_context():
        policy = ensure_default_policy()
        today = app_module.local_today()
        policy.workdays = str(today.weekday())
        policy.reminders_enabled = True
        policy.lunch_window_start = '12:00'
        policy.lunch_window_end = '13:00'
        supervisor, student = create_supervised_student('lunch-reminders')
        record = Attendance(
            user_id=student.id,
            work_date=today,
            time_in=datetime.combine(today, time(9)),
            approval_status='pending',
        )
        db.session.add(record)
        db.session.commit()
        student_id, supervisor_id, record_id = student.id, supervisor.id, record.id

    now = [datetime.combine(today, time(11, 55))]
    monkeypatch.setattr(app_module, 'local_now', lambda: now[0])
    runner = app.test_cli_runner()
    first = runner.invoke(args=['generate-lunch-reminders'])
    second = runner.invoke(args=['generate-lunch-reminders'])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    with app.app_context():
        assert Notification.query.filter_by(
            user_id=student_id,
            title='Lunch time-out reminder',
        ).count() == 1

    with app.app_context():
        record = db.session.get(Attendance, record_id)
        record.lunch_start = datetime.combine(today, time(12))
        db.session.commit()
    now[0] = datetime.combine(today, time(12, 50))
    return_reminder = runner.invoke(args=['generate-lunch-reminders'])
    assert return_reminder.exit_code == 0, return_reminder.output
    with app.app_context():
        assert Notification.query.filter_by(
            user_id=student_id,
            title='Lunch return reminder',
        ).count() == 1
        db.session.get(Attendance, record_id).lunch_start = None
        db.session.commit()

    now[0] = datetime.combine(today, time(13))
    third = runner.invoke(args=['generate-lunch-reminders'])
    fourth = runner.invoke(args=['generate-lunch-reminders'])
    assert third.exit_code == 0, third.output
    assert fourth.exit_code == 0, fourth.output
    with app.app_context():
        assert Notification.query.filter_by(
            user_id=student_id,
            title='Missed lunch time-out',
        ).count() == 1
        assert Notification.query.filter_by(
            user_id=supervisor_id,
            title='Missed lunch time-out: student-lunch-reminders',
        ).count() == 1


def test_role_dashboards_and_feature_pages_render():
    public_client = app.test_client()
    for path in ['/login', '/register']:
        response = public_client.get(path)
        assert response.status_code == 200
        assert 'data-theme-toggle' in response.get_data(as_text=True)

    with app.app_context():
        supervisor, student = create_supervised_student('surfaces')
        center = User(
            username='center-surfaces',
            password=generate_password_hash('password123'),
            role='sip_center_supervisor',
        )
        admin = User(
            username='admin-surfaces',
            password=generate_password_hash('password123'),
            role='admin',
        )
        db.session.add_all([center, admin])
        db.session.flush()
        from app import MonitoredStudent
        db.session.add(MonitoredStudent(student_id=student.id))
        db.session.commit()
        student_id, supervisor_id = student.id, supervisor.id

    student_client = app.test_client()
    login(student_client, 'student-surfaces', 'password123')
    for path in ['/dashboard', '/records', '/attendance/requests', '/notifications', f'/messages?partner={supervisor_id}']:
        response = student_client.get(path)
        assert response.status_code == 200, path
        assert 'data-theme-toggle' in response.get_data(as_text=True), path

    supervisor_client = app.test_client()
    login(supervisor_client, 'supervisor-surfaces', 'password123')
    for path in ['/supervisor_dashboard', '/attendance/requests', f'/messages?partner={student_id}']:
        response = supervisor_client.get(path)
        assert response.status_code == 200, path
        assert 'data-theme-toggle' in response.get_data(as_text=True), path

    center_client = app.test_client()
    login_response = login(center_client, 'center-surfaces', 'password123')
    assert login_response.status_code == 302
    center_response = center_client.get('/sip_center_supervisor_dashboard')
    assert center_response.status_code == 200
    assert 'data-theme-toggle' in center_response.get_data(as_text=True)

    admin_client = app.test_client()
    login(admin_client, 'admin-surfaces', 'password123')
    for path in ['/admin_dashboard', '/admin/policy', '/admin/audit']:
        response = admin_client.get(path)
        assert response.status_code == 200, path
        assert 'data-theme-toggle' in response.get_data(as_text=True), path


def test_admin_can_save_policy_and_add_holiday():
    with app.app_context():
        today = app_module.local_today()
        admin = User(
            username='admin-policy',
            password=generate_password_hash('password123'),
            role='admin',
        )
        db.session.add(admin)
        db.session.commit()

    client = app.test_client()
    login(client, 'admin-policy', 'password123')
    response = client.post('/admin/policy', data={
        '_csrf_token': client_csrf(client),
        'timezone_name': 'Asia/Manila',
        'work_start': '08:30',
        'work_end': '17:00',
        'grace_minutes': '10',
        'workdays': ['0', '1', '2', '3', '4'],
        'lunch_window_start': '12:00',
        'lunch_window_end': '13:00',
        'lunch_max_minutes': '60',
        'reminder_time': '09:00',
        'reminders_enabled': 'on',
    })
    assert response.status_code == 302
    holiday = today + timedelta(days=60)
    response = client.post('/admin/policy', data={
        '_csrf_token': client_csrf(client),
        'action': 'add_holiday',
        'holiday_date': holiday.isoformat(),
        'holiday_name': 'System test holiday',
    })
    assert response.status_code == 302
    with app.app_context():
        policy = db.session.get(AttendancePolicy, 1)
        assert policy.work_start == '08:30'
        from app import Holiday
        assert Holiday.query.filter_by(holiday_date=holiday).one().name == 'System test holiday'


def test_report_filters_and_notification_read_are_account_scoped():
    with app.app_context():
        supervisor, student = create_supervised_student('report')
        other = User(
            username='other-report',
            password=generate_password_hash('password123'),
            role='student',
        )
        db.session.add(other)
        db.session.flush()
        day = app_module.local_today() - timedelta(days=3)
        own_record = Attendance(
            user_id=student.id,
            work_date=day,
            time_in=datetime.combine(day, time(8, 30)),
            time_out=datetime.combine(day, time(16, 30)),
            approval_status='approved',
        )
        foreign_record = Attendance(
            user_id=other.id,
            work_date=day,
            time_in=datetime.combine(day, time(8, 30)),
            time_out=datetime.combine(day, time(16, 30)),
            approval_status='approved',
        )
        late_day = day - timedelta(days=1)
        late_record = Attendance(
            user_id=student.id,
            work_date=late_day,
            time_in=datetime.combine(late_day, time(9, 30)),
            time_out=datetime.combine(late_day, time(16, 30)),
            approval_status='approved',
        )
        own_notification = Notification(user_id=student.id, title='Own', body='Own update', url='/records')
        foreign_notification = Notification(user_id=other.id, title='Foreign', body='Private update', url='/records')
        db.session.add_all([own_record, foreign_record, late_record, own_notification, foreign_notification])
        db.session.commit()
        own_notification_id = own_notification.id
        foreign_notification_id = foreign_notification.id
        student_id = student.id

    client = app.test_client()
    login(client, f'student-report', 'password123')
    response = client.get(f'/records?start_date={day.isoformat()}&end_date={day.isoformat()}&status=approved')
    assert response.status_code == 200
    assert b'Total Records' in response.data
    late_response = client.get('/records?status=late')
    assert late_response.status_code == 200
    assert b'09:30:00 AM' in late_response.data
    csv_response = client.get(f'/export_csv?start_date={day.isoformat()}&end_date={day.isoformat()}&status=approved')
    csv_text = csv_response.get_data(as_text=True)
    assert 'Work Date,Review Status' in csv_text
    assert 'approved' in csv_text

    response = client.post(
        f'/notifications/{foreign_notification_id}/read',
        data={'_csrf_token': client_csrf(client)},
    )
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Notification, foreign_notification_id).read_at is None
        assert db.session.get(Notification, own_notification_id).read_at is None
        assert Attendance.query.filter_by(user_id=student_id).count() == 2


def test_email_queue_uses_configured_smtp_without_network(monkeypatch):
    sent_to = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            assert host == 'smtp.test.invalid'
            assert port == 587
            assert timeout == 20

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def starttls(self, context):
            assert context is not None

        def login(self, username, password):
            assert (username, password) == ('mail-user', 'mail-password')

        def send_message(self, message):
            sent_to.append(message['To'])

    monkeypatch.setattr(app_module.smtplib, 'SMTP', FakeSMTP)
    monkeypatch.setenv('SMTP_HOST', 'smtp.test.invalid')
    monkeypatch.setenv('SMTP_FROM', 'alerts@example.test')
    monkeypatch.setenv('SMTP_PORT', '587')
    monkeypatch.setenv('SMTP_USERNAME', 'mail-user')
    monkeypatch.setenv('SMTP_PASSWORD', 'mail-password')
    monkeypatch.setenv('SMTP_USE_TLS', 'true')
    monkeypatch.delenv('SMTP_USE_SSL', raising=False)
    with app.app_context():
        user = User(
            username='email-recipient',
            password=generate_password_hash('password123'),
            role='student',
            email='student@example.test',
            email_notifications=True,
        )
        db.session.add(user)
        db.session.flush()
        notification = Notification(
            user_id=user.id,
            title='Test reminder',
            body='A test notification.',
            url='/dashboard',
            email_status='queued',
        )
        db.session.add(notification)
        db.session.commit()
        notification_id = notification.id
        assert send_queued_emails(limit=1) == 1
        assert sent_to == ['student@example.test']
        assert db.session.get(Notification, notification_id).email_status == 'sent'


def test_admin_csv_import_is_validated_and_defaults_email_to_opt_out():
    with app.app_context():
        admin = User(
            username='admin-roster',
            password=generate_password_hash('password123'),
            role='admin',
        )
        supervisor = User(
            username='roster-supervisor',
            password=generate_password_hash('password123'),
            role='supervisor',
        )
        db.session.add_all([admin, supervisor])
        db.session.commit()

    client = app.test_client()
    login(client, 'admin-roster', 'password123')
    response = client.post(
        '/admin/users/import',
        data={
            '_csrf_token': client_csrf(client),
            'file': (
                io.BytesIO(
                    b'username,password,role,email,supervisor_username\n'
                    b'roster-student,password123,student,roster@example.test,roster-supervisor\n'
                ),
                'roster.csv',
            ),
        },
        content_type='multipart/form-data',
    )
    assert response.status_code == 302
    with app.app_context():
        imported = User.query.filter_by(username='roster-student').one()
        assert imported.email == 'roster@example.test'
        assert imported.email_notifications is False
        assert imported.supervisor_id == User.query.filter_by(username='roster-supervisor').one().id


def test_database_attendance_aggregation_matches_break_aware_calculation():
    with app.app_context():
        _, student = create_supervised_student('aggregation')
        day = app_module.local_today() - timedelta(days=2)
        completed = Attendance(
            user_id=student.id,
            work_date=day,
            time_in=datetime.combine(day, time(8, 30)),
            time_out=datetime.combine(day, time(16, 30)),
            lunch_start=datetime.combine(day, time(12)),
            lunch_end=datetime.combine(day, time(13)),
            coffee_start=datetime.combine(day, time(15)),
            coffee_end=datetime.combine(day, time(15, 15)),
            approval_status='approved',
        )
        pending = Attendance(
            user_id=student.id,
            work_date=day - timedelta(days=1),
            time_in=datetime.combine(day - timedelta(days=1), time(9)),
            time_out=datetime.combine(day - timedelta(days=1), time(17)),
            approval_status='pending',
        )
        absence = Attendance(
            user_id=student.id,
            work_date=day - timedelta(days=2),
            approval_status='approved',
        )
        db.session.add_all([completed, pending, absence])
        db.session.commit()
        metrics = aggregate_attendance(Attendance.query.filter_by(user_id=student.id))
        assert metrics['count'] == 3
        assert metrics['present'] == 1
        assert metrics['absent'] == 2
        assert metrics['hours'] == pytest.approx(net_work_hours(completed), abs=0.001)
        student_name = student.username

    client = app.test_client()
    login(client, student_name, 'password123')
    response = client.get('/records')
    assert response.status_code == 200
    assert b'6.75' in response.data


def test_message_conversation_is_paginated_to_recent_messages():
    with app.app_context():
        supervisor, student = create_supervised_student('message-pages')
        base = datetime(2025, 1, 1)
        for offset in range(51):
            message = Message(
                sender_id=student.id if offset % 2 == 0 else supervisor.id,
                receiver_id=supervisor.id if offset % 2 == 0 else student.id,
                content=f'Message {offset + 1}',
                timestamp=base + timedelta(minutes=offset),
            )
            db.session.add(message)
        db.session.commit()
        supervisor_id, student_id = supervisor.id, student.id

    client = app.test_client()
    login(client, 'supervisor-message-pages', 'password123')
    newest_page = client.get(f'/messages?partner={student_id}')
    assert newest_page.status_code == 200
    assert b'Message 51' in newest_page.data
    assert b'>Message 1<' not in newest_page.data
    older_page = client.get(f'/messages?partner={student_id}&page=2')
    assert older_page.status_code == 200
    assert b'>Message 1<' in older_page.data
    assert b'Message 51' not in older_page.data
