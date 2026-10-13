from flask import Flask, g, render_template, redirect, url_for, request, session, flash, Response, jsonify, send_from_directory, has_request_context
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from datetime import datetime, timedelta, date, time, timezone
import csv
from functools import wraps
import io
import logging
import os
import secrets
import smtplib
import ssl
from collections import defaultdict
from email.message import EmailMessage
from email.utils import parseaddr
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy import Index, and_, case, inspect, select, text, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased, joinedload

LUNCH_BREAK_MAX = timedelta(hours=1)
COFFEE_BREAK_MAX = timedelta(minutes=30)
LUNCH_WINDOW_START = 12
LUNCH_WINDOW_END = 13
DEFAULT_TIMEZONE = 'Asia/Manila'
DEFAULT_TARGET_HOURS = 400

def utc_now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)

app = Flask(__name__)
logger = logging.getLogger(__name__)
os.makedirs(app.instance_path, exist_ok=True)
secret_key = os.environ.get('SECRET_KEY')
if os.environ.get('FLASK_ENV') == 'production' and not secret_key:
    raise RuntimeError('SECRET_KEY must be set when FLASK_ENV=production.')
app.config['SECRET_KEY'] = secret_key or 'dev-only-change-this-secret'
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(app.instance_path, 'interns.db'),
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_ENV') == 'production'

UPLOAD_FOLDER = os.path.join(app.instance_path, 'profile_photos')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False, index=True)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(25), nullable=False, default='student', index=True)
    supervisor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    photo = db.Column(db.String(200), nullable=True)
    email = db.Column(db.String(254), nullable=True, unique=True, index=True)
    email_notifications = db.Column(db.Boolean, nullable=False, default=False)
    account_active = db.Column(db.Boolean, nullable=False, default=True, index=True)

    @property
    def is_active(self):
        return self.account_active

class Message(db.Model):
    __table_args__ = (
        Index('ix_message_sender_receiver_timestamp', 'sender_id', 'receiver_id', 'timestamp'),
        Index('ix_message_receiver_sender_timestamp', 'receiver_id', 'sender_id', 'timestamp'),
        Index('ix_message_receiver_read', 'receiver_id', 'read_at'),
    )
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    receiver_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    content = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=utc_now_naive)
    read_at = db.Column(db.DateTime, nullable=True)

class Attendance(db.Model):
    __table_args__ = (
        Index('ix_attendance_user_work_date', 'user_id', 'work_date'),
        Index('ix_attendance_user_status_time', 'user_id', 'approval_status', 'time_in'),
    )
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    time_in = db.Column(db.DateTime, index=True)
    time_out = db.Column(db.DateTime)
    lunch_start = db.Column(db.DateTime, nullable=True)
    lunch_end = db.Column(db.DateTime, nullable=True)
    coffee_start = db.Column(db.DateTime, nullable=True)
    coffee_end = db.Column(db.DateTime, nullable=True)
    work_date = db.Column(db.Date, nullable=True, index=True)
    approval_status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    approved_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    correction_note = db.Column(db.String(255), nullable=True)

class MonitoredStudent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True, nullable=False)

class EnrollmentInfo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), unique=True, nullable=False)
    supervisor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    course_section = db.Column(db.String(100), nullable=True)
    start_date = db.Column(db.Date, nullable=True)
    notes = db.Column(db.String(255), nullable=True)
    target_hours = db.Column(db.Float, nullable=False, default=DEFAULT_TARGET_HOURS)

class AttendanceRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    attendance_id = db.Column(db.Integer, db.ForeignKey('attendance.id'), nullable=True, index=True)
    request_type = db.Column(db.String(20), nullable=False)
    requested_date = db.Column(db.Date, nullable=False, index=True)
    requested_time_in = db.Column(db.DateTime, nullable=True)
    requested_time_out = db.Column(db.DateTime, nullable=True)
    requested_lunch_start = db.Column(db.DateTime, nullable=True)
    requested_lunch_end = db.Column(db.DateTime, nullable=True)
    reason = db.Column(db.String(1000), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    review_note = db.Column(db.String(1000), nullable=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now_naive)

class AttendancePolicy(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timezone_name = db.Column(db.String(64), nullable=False, default=DEFAULT_TIMEZONE)
    work_start = db.Column(db.String(5), nullable=False, default='09:00')
    work_end = db.Column(db.String(5), nullable=False, default='17:00')
    grace_minutes = db.Column(db.Integer, nullable=False, default=15)
    workdays = db.Column(db.String(20), nullable=False, default='0,1,2,3,4')
    lunch_max_minutes = db.Column(db.Integer, nullable=False, default=60)
    coffee_max_minutes = db.Column(db.Integer, nullable=False, default=30)
    lunch_window_start = db.Column(db.String(5), nullable=False, default='12:00')
    lunch_window_end = db.Column(db.String(5), nullable=False, default='13:00')
    coffee_window_start = db.Column(db.String(5), nullable=False, default='15:00')
    coffee_window_end = db.Column(db.String(5), nullable=False, default='15:30')
    reminder_time = db.Column(db.String(5), nullable=False, default='09:15')
    reminders_enabled = db.Column(db.Boolean, nullable=False, default=True)

class Holiday(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    holiday_date = db.Column(db.Date, nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)

class Notification(db.Model):
    __table_args__ = (
        Index('ix_notification_user_created', 'user_id', 'created_at'),
        Index('ix_notification_user_read', 'user_id', 'read_at'),
        Index('ix_notification_email_status_created', 'email_status', 'created_at'),
    )
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    body = db.Column(db.String(1000), nullable=False)
    url = db.Column(db.String(255), nullable=False, default='/dashboard')
    dedupe_key = db.Column(db.String(200), nullable=True, unique=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now_naive, index=True)
    read_at = db.Column(db.DateTime, nullable=True)
    email_status = db.Column(db.String(20), nullable=False, default='not_requested')
    email_error = db.Column(db.String(500), nullable=True)
    email_sent_at = db.Column(db.DateTime, nullable=True)
    user = db.relationship('User')

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_username = db.Column(db.String(150), nullable=False)
    action = db.Column(db.String(80), nullable=False, index=True)
    target_username = db.Column(db.String(150), nullable=True, index=True)
    details = db.Column(db.String(1000), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now_naive, index=True)

@app.context_processor
def inject_today():
    """Expose today's date in all templates (e.g. records status vs. clock-in date)."""
    unread_count = 0
    unread_message_count = 0
    if current_user.is_authenticated:
        notification_count = (
            select(db.func.count(Notification.id))
            .where(Notification.user_id == current_user.id, Notification.read_at.is_(None))
            .scalar_subquery()
        )
        message_count = (
            select(db.func.count(Message.id))
            .where(Message.receiver_id == current_user.id, Message.read_at.is_(None))
            .scalar_subquery()
        ) if current_user.role in {'student', 'supervisor'} else select(0).scalar_subquery()
        unread_count, unread_message_count = db.session.execute(
            select(notification_count, message_count)
        ).one()
    return {
        'today': local_today(),
        'csrf_token': csrf_token,
        'unread_notification_count': unread_count,
        'unread_message_count': unread_message_count,
    }

def csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token

@app.before_request
def _protect_post_requests():
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        expected = session.get('_csrf_token')
        supplied = request.form.get('_csrf_token') or request.headers.get('X-CSRF-Token')
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            return jsonify({'success': False, 'message': 'Invalid or missing CSRF token.'}), 400

@app.after_request
def _set_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    return response

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

def ensure_break_columns():
    """Add break columns to existing supported database schemas if missing."""
    dialect = db.engine.dialect.name
    if dialect not in {'sqlite', 'postgresql'}:
        raise RuntimeError(f'Break-column migration is not supported for database dialect {dialect}.')
    insp = inspect(db.engine)
    if not insp.has_table('attendance'):
        return
    existing = {c['name'] for c in insp.get_columns('attendance')}
    table_name = db.engine.dialect.identifier_preparer.quote('attendance')
    timestamp_type = 'DATETIME' if dialect == 'sqlite' else 'TIMESTAMP'
    for col, ddl in (
        ('lunch_start', timestamp_type),
        ('lunch_end', timestamp_type),
        ('coffee_start', timestamp_type),
        ('coffee_end', timestamp_type),
    ):
        if col not in existing:
            column_name = db.engine.dialect.identifier_preparer.quote(col)
            with db.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}'))

def ensure_feature_columns():
    """Add feature columns to supported existing databases without replacing user data."""
    dialect = db.engine.dialect.name
    if dialect not in {'sqlite', 'postgresql'}:
        raise RuntimeError(f'Feature migration is not supported for database dialect {dialect}.')
    insp = inspect(db.engine)
    additions = {
        'user': {
            'email': 'VARCHAR(254)',
            'email_notifications': 'BOOLEAN NOT NULL DEFAULT 0',
            'account_active': 'BOOLEAN NOT NULL DEFAULT 1',
        },
        'attendance': {
            'work_date': 'DATE',
            'approval_status': "VARCHAR(20) NOT NULL DEFAULT 'approved'",
            'approved_by': 'INTEGER',
            'approved_at': 'DATETIME',
            'correction_note': 'VARCHAR(255)',
        },
        'attendance_request': {
            'requested_lunch_start': 'DATETIME',
            'requested_lunch_end': 'DATETIME',
        },
        'enrollment_info': {
            'target_hours': f'FLOAT NOT NULL DEFAULT {DEFAULT_TARGET_HOURS}',
        },
        'message': {'read_at': 'DATETIME'},
    }
    with db.engine.begin() as conn:
        for table, columns in additions.items():
            if not insp.has_table(table):
                continue
            existing = {column['name'] for column in insp.get_columns(table)}
            for column, ddl in columns.items():
                if column not in existing:
                    if dialect == 'postgresql':
                        ddl = (
                            ddl.replace('DATETIME', 'TIMESTAMP')
                            .replace('BOOLEAN NOT NULL DEFAULT 0', 'BOOLEAN NOT NULL DEFAULT FALSE')
                            .replace('BOOLEAN NOT NULL DEFAULT 1', 'BOOLEAN NOT NULL DEFAULT TRUE')
                        )
                    table_name = db.engine.dialect.identifier_preparer.quote(table)
                    column_name = db.engine.dialect.identifier_preparer.quote(column)
                    conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}'))
        if insp.has_table('attendance'):
            table_name = db.engine.dialect.identifier_preparer.quote('attendance')
            date_expression = 'date(time_in)' if dialect == 'sqlite' else 'CAST(time_in AS DATE)'
            conn.execute(text(
                f'UPDATE {table_name} SET work_date = {date_expression} '
                'WHERE work_date IS NULL AND time_in IS NOT NULL'
            ))
        if insp.has_table('user'):
            user_table = db.engine.dialect.identifier_preparer.quote('user')
            conn.execute(text(
                f'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_email ON {user_table} (email)'
            ))
        insp.clear_cache()
        index_definitions = {
            'attendance': {
                'ix_attendance_user_work_date': ('user_id', 'work_date'),
                'ix_attendance_user_status_time': ('user_id', 'approval_status', 'time_in'),
            },
            'message': {
                'ix_message_sender_receiver_timestamp': ('sender_id', 'receiver_id', 'timestamp'),
                'ix_message_receiver_sender_timestamp': ('receiver_id', 'sender_id', 'timestamp'),
                'ix_message_receiver_read': ('receiver_id', 'read_at'),
            },
            'notification': {
                'ix_notification_user_created': ('user_id', 'created_at'),
                'ix_notification_user_read': ('user_id', 'read_at'),
                'ix_notification_email_status_created': ('email_status', 'created_at'),
            },
        }
        for table, indexes in index_definitions.items():
            if not insp.has_table(table):
                continue
            available_columns = {column['name'] for column in insp.get_columns(table)}
            quoted_table = db.engine.dialect.identifier_preparer.quote(table)
            for index_name, columns in indexes.items():
                if not set(columns).issubset(available_columns):
                    continue
                quoted_columns = ', '.join(
                    db.engine.dialect.identifier_preparer.quote(column)
                    for column in columns
                )
                quoted_index = db.engine.dialect.identifier_preparer.quote(index_name)
                conn.execute(text(
                    f'CREATE INDEX IF NOT EXISTS {quoted_index} '
                    f'ON {quoted_table} ({quoted_columns})'
                ))

def ensure_default_policy():
    policy = db.session.get(AttendancePolicy, 1)
    if policy:
        return policy
    policy = AttendancePolicy(id=1)
    db.session.add(policy)
    db.session.flush()
    return policy

def local_now():
    policy = get_attendance_policy()
    timezone_name = policy.timezone_name if policy else DEFAULT_TIMEZONE
    try:
        return datetime.now(ZoneInfo(timezone_name)).replace(tzinfo=None)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f'Invalid configured timezone: {timezone_name}') from exc

def local_today():
    return local_now().date()

def get_attendance_policy():
    if has_request_context() and hasattr(g, 'attendance_policy'):
        return g.attendance_policy
    policy = db.session.get(AttendancePolicy, 1)
    if has_request_context():
        g.attendance_policy = policy
    return policy

def duration_seconds_expression(start, end):
    dialect = db.engine.dialect.name
    if dialect == 'sqlite':
        return (db.func.julianday(end) - db.func.julianday(start)) * 86400
    if dialect == 'postgresql':
        return db.func.extract('epoch', end - start)
    raise RuntimeError(f'Attendance aggregation is not supported for database dialect {dialect}.')

def attendance_net_hours_expression():
    gross_seconds = duration_seconds_expression(Attendance.time_in, Attendance.time_out)
    lunch_seconds = case(
        (
            and_(Attendance.lunch_start.is_not(None), Attendance.lunch_end.is_not(None)),
            duration_seconds_expression(Attendance.lunch_start, Attendance.lunch_end),
        ),
        else_=0,
    )
    coffee_seconds = case(
        (
            and_(Attendance.coffee_start.is_not(None), Attendance.coffee_end.is_not(None)),
            duration_seconds_expression(Attendance.coffee_start, Attendance.coffee_end),
        ),
        else_=0,
    )
    net_seconds = gross_seconds - lunch_seconds - coffee_seconds
    return case((net_seconds > 0, net_seconds), else_=0) / 3600

def attendance_date_at_or_after(day):
    return or_(
        Attendance.work_date >= day,
        and_(
            Attendance.work_date.is_(None),
            Attendance.time_in >= datetime.combine(day, time.min),
        ),
    )

def attendance_date_before(day):
    return or_(
        Attendance.work_date < day,
        and_(
            Attendance.work_date.is_(None),
            Attendance.time_in < datetime.combine(day, time.min),
        ),
    )

def aggregate_attendance(query, today=None):
    today = today or local_today()
    approved = Attendance.approval_status == 'approved'
    completed = and_(
        approved,
        Attendance.time_in.is_not(None),
        Attendance.time_out.is_not(None),
    )
    absent = or_(
        Attendance.approval_status != 'approved',
        Attendance.time_in.is_(None),
        and_(Attendance.time_out.is_(None), attendance_date_before(today)),
    )
    policy = get_attendance_policy()
    shift_start = time.fromisoformat(policy.work_start if policy else '09:00')
    cutoff_minutes = shift_start.hour * 60 + shift_start.minute + (policy.grace_minutes if policy else 15)
    late = and_(
        approved,
        Attendance.time_in.is_not(None),
        (
            db.func.extract('hour', Attendance.time_in) * 60
            + db.func.extract('minute', Attendance.time_in)
        ) > cutoff_minutes,
    )
    count, present, absent_count, late_count, hours = query.with_entities(
        db.func.count(Attendance.id),
        db.func.coalesce(db.func.sum(case((completed, 1), else_=0)), 0),
        db.func.coalesce(db.func.sum(case((absent, 1), else_=0)), 0),
        db.func.coalesce(db.func.sum(case((late, 1), else_=0)), 0),
        db.func.coalesce(
            db.func.sum(case((completed, attendance_net_hours_expression()), else_=0)),
            0,
        ),
    ).one()
    return {
        'count': count,
        'present': present,
        'absent': absent_count,
        'late': late_count,
        'hours': float(hours),
    }

def policy_break_limits(policy=None):
    policy = policy or get_attendance_policy()
    if not policy:
        return LUNCH_BREAK_MAX, COFFEE_BREAK_MAX
    return timedelta(minutes=policy.lunch_max_minutes), timedelta(minutes=policy.coffee_max_minutes)

def validate_email_address(value):
    value = (value or '').strip()
    if not value:
        return None
    _, address = parseaddr(value)
    if address != value or '@' not in address or len(address) > 254:
        return None
    local_part, domain = address.rsplit('@', 1)
    if not local_part or '.' not in domain or domain.startswith('.') or domain.endswith('.'):
        return None
    return value

def write_audit(action, details, target_username=None, actor_username=None):
    actor = actor_username or (current_user.username if current_user.is_authenticated else 'system')
    db.session.add(AuditLog(
        actor_username=actor,
        action=action,
        target_username=target_username,
        details=details[:1000],
    ))

def queue_notification(user, title, body, endpoint='dashboard', endpoint_values=None, dedupe_key=None, queue_email=True):
    if not user or not user.account_active:
        return None
    if dedupe_key and Notification.query.filter_by(dedupe_key=dedupe_key).first():
        return None
    endpoint_values = endpoint_values or {}
    notification_url = (
        url_for(endpoint, **endpoint_values)
        if has_request_context()
        else app.url_map.bind('localhost').build(endpoint, endpoint_values, force_external=False)
    )
    status = 'queued' if queue_email and user.email and user.email_notifications else 'not_requested'
    notification = Notification(
        user_id=user.id,
        title=title[:160],
        body=body[:1000],
        url=notification_url,
        dedupe_key=dedupe_key,
        email_status=status,
    )
    db.session.add(notification)
    return notification

def send_queued_emails(limit=100):
    host = os.environ.get('SMTP_HOST', '').strip()
    sender = os.environ.get('SMTP_FROM', '').strip()
    if not host or not sender:
        raise RuntimeError('SMTP_HOST and SMTP_FROM must be configured before sending email notifications.')
    port = int(os.environ.get('SMTP_PORT', '587'))
    username = os.environ.get('SMTP_USERNAME', '')
    password = os.environ.get('SMTP_PASSWORD', '')
    use_ssl = os.environ.get('SMTP_USE_SSL', '').lower() in {'1', 'true', 'yes'}
    use_tls = os.environ.get('SMTP_USE_TLS', 'true').lower() in {'1', 'true', 'yes'}
    app_base_url = os.environ.get('APP_BASE_URL', '').strip().rstrip('/')
    if app_base_url and not app_base_url.startswith(('https://', 'http://')):
        raise RuntimeError('APP_BASE_URL must use http:// or https://.')
    pending = (
        Notification.query.options(joinedload(Notification.user))
        .filter_by(email_status='queued')
        .order_by(Notification.created_at.asc())
        .limit(limit)
        .all()
    )
    sent = 0
    for notification in pending:
        recipient = notification.user.email if notification.user else None
        if not recipient or not notification.user.email_notifications:
            notification.email_status = 'not_requested'
            continue
        message = EmailMessage()
        message['Subject'] = notification.title
        message['From'] = sender
        message['To'] = recipient
        link = f'{app_base_url}/{notification.url.lstrip("/")}' if app_base_url else None
        email_body = notification.body
        if link:
            email_body += f'\n\nOpen SIP Tracker: {link}'
        message.set_content(email_body)
        try:
            if use_ssl:
                with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=20) as smtp:
                    if username:
                        smtp.login(username, password)
                    smtp.send_message(message)
            else:
                with smtplib.SMTP(host, port, timeout=20) as smtp:
                    if use_tls:
                        smtp.starttls(context=ssl.create_default_context())
                    if username:
                        smtp.login(username, password)
                    smtp.send_message(message)
            notification.email_status = 'sent'
            notification.email_sent_at = local_now()
            notification.email_error = None
            sent += 1
        except (OSError, smtplib.SMTPException) as exc:
            notification.email_status = 'failed'
            notification.email_error = str(exc)[:500]
            logger.error(
                'Failed to send notification email id=%s recipient=%s: %s',
                notification.id,
                recipient,
                exc,
            )
    db.session.commit()
    return sent

@app.cli.command('generate-attendance-reminders')
def generate_attendance_reminders_command():
    policy = ensure_default_policy()
    if not policy.reminders_enabled:
        print('Attendance reminders are disabled by policy.')
        return
    now = local_now()
    try:
        reminder_time = time.fromisoformat(policy.reminder_time)
    except ValueError as exc:
        raise RuntimeError(f'Invalid reminder time configured: {policy.reminder_time}') from exc
    if now.time() < reminder_time:
        raise RuntimeError(f'Reminders are scheduled for {policy.reminder_time} or later in {policy.timezone_name}.')
    workdays = {int(day) for day in policy.workdays.split(',') if day.isdigit()}
    today = now.date()
    created = 0
    is_workday = today.weekday() in workdays and not Holiday.query.filter_by(holiday_date=today).first()
    if is_workday:
        students = User.query.filter_by(role='student', account_active=True).order_by(User.id).all()
        for student in students:
            attended = Attendance.query.filter(
                Attendance.user_id == student.id,
                or_(
                    Attendance.work_date == today,
                    and_(
                        Attendance.work_date.is_(None),
                        Attendance.time_in >= datetime.combine(today, time.min),
                        Attendance.time_in < datetime.combine(today + timedelta(days=1), time.min),
                    ),
                ),
            ).first()
            if attended:
                continue
            notification = queue_notification(
                student,
                'Attendance reminder',
                f'Remember to record your attendance for {today}.',
                endpoint='dashboard',
                dedupe_key=f'daily-reminder:{today.isoformat()}:{student.id}',
            )
            created += bool(notification)
    incomplete_records = Attendance.query.filter(
        Attendance.time_in.is_not(None),
        Attendance.time_out.is_(None),
        or_(
            Attendance.work_date < today,
            and_(
                Attendance.work_date.is_(None),
                Attendance.time_in < datetime.combine(today, time.min),
            ),
        ),
    ).all()
    for record in incomplete_records:
        student = db.session.get(User, record.user_id)
        if not student:
            continue
        record_date = record.work_date or record.time_in.date()
        notification_key = f'missing-punch:{record.id}:{student.id}'
        message = f'Your attendance on {record_date} has no clock-out time. Please contact your supervisor.'
        created += bool(queue_notification(
            student,
            'Missing clock-out',
            message,
            endpoint='records',
            dedupe_key=notification_key,
        ))
        supervisor = db.session.get(User, student.supervisor_id) if student.supervisor_id else None
        if supervisor and supervisor.account_active:
            created += bool(queue_notification(
                supervisor,
                f'Missing clock-out: {student.username}',
                f'{student.username} has no clock-out recorded for {record_date}.',
                endpoint='supervisor_dashboard',
                dedupe_key=f'missing-punch:{record.id}:{supervisor.id}',
            ))
    if created:
        write_audit('attendance_reminders_generated', f'Generated {created} reminders for {today}.', actor_username='system')
        db.session.commit()
    print(f'Created {created} attendance notification(s) for {today}.')

@app.cli.command('generate-lunch-reminders')
def generate_lunch_reminders_command():
    policy = ensure_default_policy()
    if not policy.reminders_enabled:
        print('Attendance reminders are disabled by policy.')
        return
    now = local_now()
    today = now.date()
    workdays = {int(day) for day in policy.workdays.split(',') if day.isdigit()}
    if today.weekday() not in workdays or Holiday.query.filter_by(holiday_date=today).first():
        print(f'No lunch reminders generated for non-workday {today}.')
        return

    try:
        lunch_window_start = time.fromisoformat(policy.lunch_window_start)
        lunch_window_end = time.fromisoformat(policy.lunch_window_end)
    except ValueError as exc:
        raise RuntimeError('Invalid configured lunch window.') from exc
    reminder_at = datetime.combine(today, lunch_window_start) - timedelta(minutes=5)
    window_end = datetime.combine(today, lunch_window_end)
    lunch_limit, _ = policy_break_limits(policy)
    created = 0
    students = User.query.filter_by(role='student', account_active=True).order_by(User.id).all()
    for student in students:
        record = Attendance.query.filter(
            Attendance.user_id == student.id,
            or_(
                Attendance.work_date == today,
                and_(
                    Attendance.work_date.is_(None),
                    Attendance.time_in >= datetime.combine(today, time.min),
                    Attendance.time_in < datetime.combine(today + timedelta(days=1), time.min),
                ),
            ),
        ).order_by(Attendance.id.desc()).first()
        if not record or not record.time_in or record.lunch_start:
            continue
        supervisor = db.session.get(User, student.supervisor_id) if student.supervisor_id else None
        if now >= window_end:
            notification = queue_notification(
                student,
                'Missed lunch time-out',
                f'No lunch time-out was recorded for {today}. Submit a lunch correction request with the actual lunch-out and afternoon-return times.',
                endpoint='attendance_requests',
                dedupe_key=f'missed-lunch:{today.isoformat()}:{student.id}',
            )
            created += bool(notification)
            if supervisor and supervisor.account_active:
                notification = queue_notification(
                    supervisor,
                    f'Missed lunch time-out: {student.username}',
                    f'{student.username} has no lunch time-out recorded for {today}.',
                    endpoint='attendance_requests',
                    dedupe_key=f'missed-lunch:{today.isoformat()}:{supervisor.id}:{student.id}',
                )
                created += bool(notification)
        elif now >= reminder_at:
            notification = queue_notification(
                student,
                'Lunch time-out reminder',
                f'Record your lunch time-out between {lunch_window_start.strftime("%I:%M %p")} and {lunch_window_end.strftime("%I:%M %p")}.',
                endpoint='dashboard',
                dedupe_key=f'lunch-reminder:{today.isoformat()}:{student.id}',
            )
            created += bool(notification)

    for record in Attendance.query.filter(
        or_(
            Attendance.work_date == today,
            and_(
                Attendance.work_date.is_(None),
                Attendance.time_in >= datetime.combine(today, time.min),
                Attendance.time_in < datetime.combine(today + timedelta(days=1), time.min),
            ),
        ),
        Attendance.lunch_start.is_not(None),
        Attendance.lunch_end.is_(None),
        Attendance.time_out.is_(None),
    ).all():
        if not record.time_in:
            continue
        return_reminder_at = record.lunch_start + max(
            timedelta(0),
            lunch_limit - timedelta(minutes=10),
        )
        if now < return_reminder_at:
            continue
        student = db.session.get(User, record.user_id)
        if student:
            notification = queue_notification(
                student,
                'Lunch return reminder',
                'Your lunch break is nearly over. Record your afternoon time-in when you return.',
                endpoint='dashboard',
                dedupe_key=f'lunch-return:{record.id}:{student.id}',
            )
            created += bool(notification)

    if created:
        write_audit(
            'lunch_reminders_generated',
            f'Generated {created} lunch reminder notification(s) for {today}.',
            actor_username='system',
        )
        db.session.commit()
    print(f'Created {created} lunch reminder notification(s) for {today}.')

@app.cli.command('dispatch-notification-emails')
def dispatch_notification_emails_command():
    sent = send_queued_emails()
    failed = Notification.query.filter_by(email_status='failed').count()
    print(f'Sent {sent} queued notification email(s); {failed} notification email(s) have failed.')

@app.cli.command('retry-failed-notification-emails')
def retry_failed_notification_emails_command():
    failed = (
        Notification.query.options(joinedload(Notification.user))
        .filter_by(email_status='failed')
        .order_by(Notification.created_at.asc())
        .limit(100)
        .all()
    )
    queued = 0
    for notification in failed:
        if not notification.user or not notification.user.email or not notification.user.email_notifications:
            notification.email_status = 'not_requested'
            continue
        notification.email_status = 'queued'
        notification.email_error = None
        queued += 1
    db.session.commit()
    print(f'Requeued {queued} failed notification email(s).')

def completed_break_seconds(rec):
    """Total seconds of finished lunch + coffee breaks for a record."""
    total = 0.0
    if rec.lunch_start and rec.lunch_end:
        total += (rec.lunch_end - rec.lunch_start).total_seconds()
    if rec.coffee_start and rec.coffee_end:
        total += (rec.coffee_end - rec.coffee_start).total_seconds()
    return total

def net_work_hours(rec):
    """Hours between clock in/out minus completed breaks."""
    if not rec.time_in or not rec.time_out:
        return 0.0
    gross = (rec.time_out - rec.time_in).total_seconds()
    return max(0.0, gross - completed_break_seconds(rec)) / 3600.0

def flash_if_overbreak(label, duration, allowed=None):
    """Flash a warning when break duration exceeds allowance. Returns True if overbreak."""
    if allowed is None:
        lunch_limit, coffee_limit = policy_break_limits()
        allowed = lunch_limit if label.lower().startswith('lunch') else coffee_limit
    if duration <= allowed:
        return False
    mins = int((duration - allowed).total_seconds() // 60)
    flash(
        f'Overbreak warning: {label} exceeded the allowed time by {mins} minute(s).',
        'warning',
    )
    return True

def finalize_open_breaks(attendance, end_time):
    """Close any open break at clock-out; flash overbreak warnings."""
    lunch_limit, coffee_limit = policy_break_limits()
    if attendance.lunch_start and not attendance.lunch_end:
        attendance.lunch_end = end_time
        flash_if_overbreak('Lunch break', end_time - attendance.lunch_start, lunch_limit)
    if attendance.coffee_start and not attendance.coffee_end:
        attendance.coffee_end = end_time
        flash_if_overbreak('Coffee break', end_time - attendance.coffee_start, coffee_limit)

def format_break_range(start, end):
    if not start:
        return ''
    if end:
        return f"{start.strftime('%Y-%m-%d %H:%M')}–{end.strftime('%H:%M')}"
    return f"{start.strftime('%Y-%m-%d %H:%M')} (in progress)"

def format_duration(start, end=None):
    if not start:
        return ''
    elapsed = (end or local_now()) - start
    minutes = max(0, int(elapsed.total_seconds() // 60))
    return f'{minutes // 60}h {minutes % 60:02d}m'

def attendance_status(rec):
    if not rec.time_in:
        return 'No clock-in', 'neutral'
    now = rec.time_out or local_now()
    lunch_duration = (rec.lunch_end or now) - rec.lunch_start if rec.lunch_start else timedelta()
    coffee_duration = (rec.coffee_end or now) - rec.coffee_start if rec.coffee_start else timedelta()
    lunch_limit, coffee_limit = policy_break_limits()
    overbreak = lunch_duration > lunch_limit or coffee_duration > coffee_limit
    if overbreak:
        return 'Overbreak', 'warning'
    if rec.approval_status == 'pending':
        return 'Awaiting review', 'warning'
    if rec.approval_status == 'rejected':
        return 'Rejected', 'warning'
    if not rec.time_out:
        return ('Incomplete' if rec.time_in.date() < local_today() else 'Active'), 'active'
    policy = get_attendance_policy()
    work_start = time.fromisoformat(policy.work_start if policy else '09:00')
    grace_minutes = policy.grace_minutes if policy else 15
    cutoff = datetime.combine(rec.time_in.date(), work_start) + timedelta(minutes=grace_minutes)
    if rec.time_in > cutoff:
        return 'Late', 'warning'
    return 'Complete', 'complete'

def attendance_events(rec):
    events = []
    if rec.time_in:
        events.append({'label': 'Clock in', 'time': rec.time_in, 'detail': 'Attendance started', 'icon': 'fa-fingerprint', 'tone': 'teal'})
    if rec.lunch_start:
        events.append({'label': 'Lunch break', 'time': rec.lunch_start, 'detail': f"{format_duration(rec.lunch_start, rec.lunch_end)}{' · in progress' if not rec.lunch_end else ''}", 'icon': 'fa-utensils', 'tone': 'amber'})
        if rec.lunch_end:
            events.append({'label': 'Lunch resumed', 'time': rec.lunch_end, 'detail': 'Break ended', 'icon': 'fa-play', 'tone': 'amber'})
    if rec.coffee_start:
        events.append({'label': 'Coffee break', 'time': rec.coffee_start, 'detail': f"{format_duration(rec.coffee_start, rec.coffee_end)}{' · in progress' if not rec.coffee_end else ''}", 'icon': 'fa-mug-hot', 'tone': 'cyan'})
        if rec.coffee_end:
            events.append({'label': 'Coffee resumed', 'time': rec.coffee_end, 'detail': 'Break ended', 'icon': 'fa-play', 'tone': 'cyan'})
    if rec.time_out:
        events.append({'label': 'Clock out', 'time': rec.time_out, 'detail': f'Net {net_work_hours(rec):.2f} hours', 'icon': 'fa-fingerprint', 'tone': 'red'})
    return sorted(events, key=lambda event: event['time'])

def scheduled_break_status(rec, start_hour, end_hour, label):
    if not rec.time_in:
        return {'label': label, 'status': 'Not scheduled', 'detail': 'No clock-in recorded.'}
    start_minutes = round(start_hour * 60)
    end_minutes = round(end_hour * 60)
    window_start = rec.time_in.replace(
        hour=start_minutes // 60,
        minute=start_minutes % 60,
        second=0,
        microsecond=0,
    )
    window_end = rec.time_in.replace(
        hour=end_minutes // 60,
        minute=end_minutes % 60,
        second=0,
        microsecond=0,
    )
    session_end = rec.time_out or local_now()
    if session_end < window_start:
        status = 'Pending'
    elif rec.time_in > window_end:
        status = 'Not covered'
    elif rec.time_in <= window_end and session_end >= window_start:
        status = 'Covered'
    else:
        status = 'Not covered'
    return {
        'label': label,
        'status': status,
        'detail': f'{window_start.strftime("%I:%M %p")} - {window_end.strftime("%I:%M %p")}',
    }

def scheduled_breaks(rec):
    policy = get_attendance_policy()
    if policy:
        lunch_start = time.fromisoformat(policy.lunch_window_start)
        lunch_end = time.fromisoformat(policy.lunch_window_end)
        lunch_start_hour = lunch_start.hour + lunch_start.minute / 60
        lunch_end_hour = lunch_end.hour + lunch_end.minute / 60
    else:
        lunch_start_hour, lunch_end_hour = LUNCH_WINDOW_START, LUNCH_WINDOW_END
    return [
        scheduled_break_status(rec, lunch_start_hour, lunch_end_hour, 'Lunch window'),
    ]

@app.template_filter('net_hours')
def template_net_hours(rec):
    return round(net_work_hours(rec), 2)

@app.template_filter('local_datetime')
def template_local_datetime(value):
    if not value:
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    policy = get_attendance_policy()
    timezone_name = policy.timezone_name if policy else DEFAULT_TIMEZONE
    return value.astimezone(ZoneInfo(timezone_name))

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def valid_image(file):
    if not file or not allowed_file(file.filename):
        return False
    try:
        from PIL import Image
        image = Image.open(file.stream)
        image.verify()
        file.stream.seek(0)
        return image.format.lower() in {'png', 'jpeg', 'gif'}
    except (ImportError, OSError):
        return False

def password_matches(stored_password, submitted_password):
    if stored_password.startswith(('pbkdf2:', 'scrypt:')):
        return check_password_hash(stored_password, submitted_password)
    return stored_password == submitted_password

def csv_cell(value):
    value = '' if value is None else str(value)
    if value.startswith(('=', '+', '-', '@')):
        return "'" + value
    return value

def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped_view(*args, **kwargs):
        if current_user.role != 'admin':
            flash('Access denied.')
            return redirect(url_for('dashboard'))
        return view(*args, **kwargs)
    return wrapped_view

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    supervisors = User.query.filter_by(role='supervisor', account_active=True).order_by(User.username).all()
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        role = request.form.get('role', 'student')
        supervisor_id = request.form.get('supervisor_id') or None
        email = validate_email_address(request.form.get('email'))
        raw_email = request.form.get('email', '').strip()
        if len(username) < 3 or len(username) > 150 or len(password) < 8:
            flash('Username must be 3-150 characters and password must be at least 8 characters.')
            return redirect(url_for('register'))
        if raw_email and not email:
            flash('Enter a valid email address.')
            return redirect(url_for('register'))
        if email and User.query.filter_by(email=email).first():
            flash('That email address is already in use.')
            return redirect(url_for('register'))
        if role not in {'student', 'supervisor', 'sip_center_supervisor'}:
            flash('Selected role is invalid.')
            return redirect(url_for('register'))
        if role != 'student':
            supervisor_id = None
        elif supervisor_id:
            supervisor = User.query.filter_by(id=supervisor_id, role='supervisor', account_active=True).first()
            if not supervisor:
                flash('Selected supervisor is invalid.')
                return redirect(url_for('register'))
        if User.query.filter_by(username=username).first():
            flash('Username already exists')
            return redirect(url_for('register'))
        new_user = User(
            username=username,
            password=generate_password_hash(password),
            role=role,
            supervisor_id=supervisor_id,
            email=email,
            email_notifications=bool(email and request.form.get('email_notifications')),
        )
        db.session.add(new_user)
        db.session.commit()
        flash('Registration successful! Please log in.')
        return redirect(url_for('login'))
    return render_template('register.html', supervisors=supervisors)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if not user:
            flash('No user found with that username.')
        elif not user.account_active:
            flash('This account is inactive. Contact an administrator for assistance.')
        elif not password_matches(user.password, password):
            flash('Incorrect password.')
        else:
            if not user.password.startswith(('pbkdf2:', 'scrypt:')):
                user.password = generate_password_hash(password)
                db.session.commit()
            login_user(user)
            return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/admin_dashboard')
@admin_required
def admin_dashboard():
    role_order = ['admin', 'supervisor', 'sip_center_supervisor', 'student']
    search = request.args.get('search', '').strip()[:150]
    selected_role = request.args.get('role', 'all')
    if selected_role not in role_order and selected_role != 'all':
        selected_role = 'all'
    query = User.query.order_by(User.role, User.username)
    if selected_role != 'all':
        query = query.filter(User.role == selected_role)
    if search:
        query = query.filter(or_(User.username.ilike(f'%{search}%'), User.email.ilike(f'%{search}%')))
    try:
        page_number = max(1, int(request.args.get('page', '1')))
    except ValueError:
        page_number = 1
    pagination = query.paginate(page=page_number, per_page=25, error_out=False)
    users = pagination.items
    supervisors = User.query.filter_by(role='supervisor', account_active=True).order_by(User.username).all()
    target_hours_by_user = {
        enrollment.student_id: enrollment.target_hours
        for enrollment in EnrollmentInfo.query.filter(
            EnrollmentInfo.student_id.in_([user.id for user in users])
        ).all()
    } if users else {}
    total, admins, supervisors_count, center_supervisors, students_count, unassigned, inactive = db.session.query(
        db.func.count(User.id),
        db.func.sum(case((User.role == 'admin', 1), else_=0)),
        db.func.sum(case((User.role == 'supervisor', 1), else_=0)),
        db.func.sum(case((User.role == 'sip_center_supervisor', 1), else_=0)),
        db.func.sum(case((User.role == 'student', 1), else_=0)),
        db.func.sum(case((and_(User.role == 'student', User.supervisor_id.is_(None)), 1), else_=0)),
        db.func.sum(case((User.account_active.is_(False), 1), else_=0)),
    ).one()
    stats = {
        'total': total,
        'admins': admins or 0,
        'supervisors': supervisors_count or 0,
        'sip_center_supervisors': center_supervisors or 0,
        'students': students_count or 0,
        'unassigned_students': unassigned or 0,
        'inactive': inactive or 0,
    }
    return render_template(
        'admin_dashboard.html',
        users=users,
        supervisors=supervisors,
        stats=stats,
        role_order=role_order,
        search=search,
        selected_role=selected_role,
        pagination=pagination,
        target_hours_by_user=target_hours_by_user,
    )

def admin_user_form_data():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    role = request.form.get('role', '').strip() or None
    supervisor_id = request.form.get('supervisor_id') or None
    email_raw = request.form.get('email', '').strip()
    email = validate_email_address(email_raw)
    target_hours_raw = request.form.get('target_hours', str(DEFAULT_TARGET_HOURS)).strip()
    if len(username) < 3 or len(username) > 150:
        return None, 'Username must be 3-150 characters.'
    if role and role not in {'admin', 'student', 'supervisor', 'sip_center_supervisor'}:
        return None, 'Selected role is invalid.'
    if role and role != 'student':
        supervisor_id = None
    elif supervisor_id:
        supervisor = User.query.filter_by(id=supervisor_id, role='supervisor', account_active=True).first()
        if not supervisor:
            return None, 'Selected supervisor is invalid.'
    if email_raw and not email:
        return None, 'Enter a valid email address.'
    try:
        target_hours = float(target_hours_raw)
    except ValueError:
        return None, 'Target hours must be a positive number.'
    if target_hours <= 0 or target_hours > 10000:
        return None, 'Target hours must be between 0 and 10000.'
    return {
        'username': username,
        'password': password,
        'role': role,
        'supervisor_id': supervisor_id,
        'email': email,
        'email_notifications': bool(email and request.form.get('email_notifications')),
        'target_hours': target_hours,
    }, None

@app.route('/admin/users/create', methods=['POST'])
@admin_required
def admin_create_user():
    data, error = admin_user_form_data()
    if error:
        flash(error)
        return redirect(url_for('admin_dashboard'))
    if len(data['password']) < 8:
        flash('Password must be at least 8 characters.')
        return redirect(url_for('admin_dashboard'))
    if User.query.filter_by(username=data['username']).first():
        flash('Username already exists.')
        return redirect(url_for('admin_dashboard'))
    if data['email'] and User.query.filter_by(email=data['email']).first():
        flash('That email address is already in use.')
        return redirect(url_for('admin_dashboard'))
    user = User(
        username=data['username'],
        password=generate_password_hash(data['password']),
        role=data['role'],
        supervisor_id=data['supervisor_id'],
        email=data['email'],
        email_notifications=data['email_notifications'],
    )
    db.session.add(user)
    if user.role == 'student' and user.supervisor_id:
        db.session.flush()
        db.session.add(EnrollmentInfo(
            student_id=user.id,
            supervisor_id=user.supervisor_id,
            target_hours=data['target_hours'],
        ))
    write_audit('user_created', f'Created {user.role} account.', user.username)
    db.session.commit()
    flash('User created.')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/users/<int:user_id>/update', methods=['POST'])
@admin_required
def admin_update_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash('User not found.')
        return redirect(url_for('admin_dashboard'))
    data, error = admin_user_form_data()
    if error:
        flash(error)
        return redirect(url_for('admin_dashboard'))
    if user.id == current_user.id and user.role == 'admin' and data['role'] not in (None, 'admin'):
        flash('You cannot demote your own admin account.')
        return redirect(url_for('admin_dashboard'))
    if data['role'] and data['role'] != user.role:
        flash('User roles are permanent and cannot be changed after creation.')
        return redirect(url_for('admin_dashboard'))
    duplicate = User.query.filter(User.username == data['username'], User.id != user.id).first()
    if duplicate:
        flash('Username already exists.')
        return redirect(url_for('admin_dashboard'))
    user.username = data['username']
    if data['email'] and User.query.filter(User.email == data['email'], User.id != user.id).first():
        flash('That email address is already in use.')
        return redirect(url_for('admin_dashboard'))
    user.email = data['email']
    user.email_notifications = data['email_notifications']
    user.supervisor_id = data['supervisor_id'] if user.role == 'student' else None
    if user.role == 'student':
        enrollment = EnrollmentInfo.query.filter_by(student_id=user.id).first()
        if not enrollment:
            enrollment = EnrollmentInfo(student_id=user.id)
            db.session.add(enrollment)
        enrollment.supervisor_id = user.supervisor_id
        enrollment.target_hours = data['target_hours']
    if data['password']:
        if len(data['password']) < 8:
            flash('Password must be at least 8 characters.')
            return redirect(url_for('admin_dashboard'))
        user.password = generate_password_hash(data['password'])
    write_audit('user_updated', 'Updated account details, assignment, or password.', user.username)
    db.session.commit()
    flash('User updated.')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash('User not found.')
        return redirect(url_for('admin_dashboard'))
    if user.id == current_user.id:
        flash('You cannot delete your own admin account.')
        return redirect(url_for('admin_dashboard'))
    target_username = user.username
    write_audit('user_deleted', 'Permanently deleted account and associated records.', target_username)
    user_attendance_ids = select(Attendance.id).where(Attendance.user_id == user.id)
    AttendanceRequest.query.filter(
        (AttendanceRequest.student_id == user.id)
        | (AttendanceRequest.reviewed_by == user.id)
        | (AttendanceRequest.attendance_id.in_(user_attendance_ids))
    ).delete(synchronize_session=False)
    Notification.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    Attendance.query.filter(Attendance.approved_by == user.id).update(
        {Attendance.approved_by: None},
        synchronize_session=False,
    )
    AttendanceRequest.query.filter_by(reviewed_by=user.id).update(
        {AttendanceRequest.reviewed_by: None},
        synchronize_session=False,
    )
    Attendance.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    EnrollmentInfo.query.filter((EnrollmentInfo.student_id == user.id) | (EnrollmentInfo.supervisor_id == user.id)).delete(synchronize_session=False)
    MonitoredStudent.query.filter_by(student_id=user.id).delete(synchronize_session=False)
    Message.query.filter((Message.sender_id == user.id) | (Message.receiver_id == user.id)).delete(synchronize_session=False)
    User.query.filter_by(supervisor_id=user.id).update({User.supervisor_id: None}, synchronize_session=False)
    db.session.delete(user)
    db.session.commit()
    flash('User deleted.')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/users/<int:user_id>/activate', methods=['POST'])
@admin_required
def admin_toggle_user_active(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash('User not found.')
        return redirect(url_for('admin_dashboard'))
    if user.id == current_user.id and user.account_active:
        flash('You cannot deactivate your own account.')
        return redirect(url_for('admin_dashboard'))
    user.account_active = not user.account_active
    state = 'activated' if user.account_active else 'deactivated'
    write_audit(f'user_{state}', f'Account {state}.', user.username)
    db.session.commit()
    flash(f'Account {state}.')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/users/export.csv')
@admin_required
def admin_export_users():
    supervisor = aliased(User)
    users = db.session.query(User, supervisor.username).outerjoin(
        supervisor,
        User.supervisor_id == supervisor.id,
    ).order_by(User.role, User.username).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Username', 'Email', 'Email Notifications', 'Role', 'Supervisor', 'Active'])
    for user, supervisor_username in users:
        writer.writerow([
            csv_cell(user.username),
            csv_cell(user.email),
            'yes' if user.email_notifications else 'no',
            csv_cell(user.role),
            csv_cell(supervisor_username or ''),
            'yes' if user.account_active else 'no',
        ])
    write_audit('user_roster_exported', f'Exported roster containing {len(users)} accounts.')
    db.session.commit()
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment;filename=user-roster.csv'},
    )

@app.route('/admin/users/import', methods=['POST'])
@admin_required
def admin_import_users():
    upload = request.files.get('file')
    if not upload or not upload.filename.lower().endswith('.csv'):
        flash('Choose a CSV file to import.')
        return redirect(url_for('admin_dashboard'))
    try:
        content = upload.stream.read(4 * 1024 * 1024 + 1)
        if len(content) > 4 * 1024 * 1024:
            flash('The CSV file is larger than 4 MB.')
            return redirect(url_for('admin_dashboard'))
        decoded = content.decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(decoded))
        required = {'username', 'password', 'role', 'email', 'supervisor_username'}
        headers = {header.strip().lower() for header in (reader.fieldnames or [])}
        if not required.issubset(headers):
            flash('CSV must contain username,password,role,email,supervisor_username columns.')
            return redirect(url_for('admin_dashboard'))
        parsed_rows = []
        errors = []
        seen_usernames = set()
        seen_emails = set()
        for line_number, raw_row in enumerate(reader, start=2):
            if line_number > 502:
                errors.append('Import is limited to 500 users per file.')
                break
            row = {(key or '').strip().lower(): (value or '').strip() for key, value in raw_row.items()}
            username = row.get('username', '')
            password = row.get('password', '')
            role = row.get('role', '').lower()
            email_raw = row.get('email', '')
            email = validate_email_address(email_raw)
            supervisor_name = row.get('supervisor_username', '')
            if len(username) < 3 or len(username) > 150 or len(password) < 8:
                errors.append(f'Line {line_number}: username or password length is invalid.')
                continue
            if role not in {'student', 'supervisor', 'sip_center_supervisor', 'admin'}:
                errors.append(f'Line {line_number}: role is invalid.')
                continue
            if email_raw and not email:
                errors.append(f'Line {line_number}: email address is invalid.')
                continue
            if role == 'student' and supervisor_name:
                supervisor = User.query.filter_by(
                    username=supervisor_name,
                    role='supervisor',
                    account_active=True,
                ).first()
                if not supervisor:
                    errors.append(f'Line {line_number}: supervisor was not found.')
                    continue
                supervisor_id = supervisor.id
            else:
                supervisor_id = None
            username_key = username.casefold()
            email_key = email.casefold() if email else None
            if username_key in seen_usernames or User.query.filter(db.func.lower(User.username) == username_key).first():
                errors.append(f'Line {line_number}: username is duplicated.')
                continue
            if email_key and (email_key in seen_emails or User.query.filter(db.func.lower(User.email) == email_key).first()):
                errors.append(f'Line {line_number}: email address is duplicated.')
                continue
            seen_usernames.add(username_key)
            if email_key:
                seen_emails.add(email_key)
            parsed_rows.append({
                'username': username,
                'password': password,
                'role': role,
                'email': email,
                'supervisor_id': supervisor_id,
            })
        if errors:
            flash('Import cancelled: ' + ' '.join(errors[:8]))
            return redirect(url_for('admin_dashboard'))
        if not parsed_rows:
            flash('The CSV contains no users to import.')
            return redirect(url_for('admin_dashboard'))
    except (UnicodeDecodeError, csv.Error) as exc:
        flash(f'Unable to read CSV file: {exc}')
        return redirect(url_for('admin_dashboard'))

    try:
        for row in parsed_rows:
            user = User(
                username=row['username'],
                password=generate_password_hash(row['password']),
                role=row['role'],
                email=row['email'],
                email_notifications=row.get('email_notifications', '').lower() in {'1', 'true', 'yes', 'on'},
                supervisor_id=row['supervisor_id'],
            )
            db.session.add(user)
            db.session.flush()
            if user.role == 'student' and user.supervisor_id:
                db.session.add(EnrollmentInfo(
                    student_id=user.id,
                    supervisor_id=user.supervisor_id,
                    target_hours=DEFAULT_TARGET_HOURS,
                ))
        write_audit('users_imported', f'Imported {len(parsed_rows)} accounts from CSV.')
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash('Import was cancelled because an account conflicts with existing data.')
        return redirect(url_for('admin_dashboard'))
    flash(f'Imported {len(parsed_rows)} users.')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/policy', methods=['GET', 'POST'])
@admin_required
def admin_policy():
    policy = ensure_default_policy()
    if request.method == 'POST':
        action = request.form.get('action', 'save_policy')
        if action == 'add_holiday':
            try:
                holiday_date = date.fromisoformat(request.form.get('holiday_date', ''))
                name = request.form.get('holiday_name', '').strip()
                if not name or len(name) > 120:
                    raise ValueError('Enter a holiday name of 1 to 120 characters.')
                if Holiday.query.filter_by(holiday_date=holiday_date).first():
                    raise ValueError('A holiday already exists on that date.')
            except ValueError as exc:
                flash(str(exc), 'warning')
            else:
                db.session.add(Holiday(holiday_date=holiday_date, name=name))
                write_audit('holiday_added', f'Added holiday {name} on {holiday_date}.')
                db.session.commit()
                flash('Holiday added.', 'success')
            return redirect(url_for('admin_policy'))
        try:
            timezone_name = request.form.get('timezone_name', '').strip()
            ZoneInfo(timezone_name)
            values = {
                'work_start': request.form.get('work_start', ''),
                'work_end': request.form.get('work_end', ''),
                'lunch_window_start': request.form.get('lunch_window_start', ''),
                'lunch_window_end': request.form.get('lunch_window_end', ''),
                'reminder_time': request.form.get('reminder_time', ''),
            }
            parsed_times = {key: time.fromisoformat(value) for key, value in values.items()}
            if parsed_times['work_start'] >= parsed_times['work_end']:
                raise ValueError('Work start must be earlier than work end.')
            if parsed_times['lunch_window_start'] >= parsed_times['lunch_window_end']:
                raise ValueError('The lunch window must end after it starts.')
            grace_minutes = int(request.form.get('grace_minutes', ''))
            lunch_max_minutes = int(request.form.get('lunch_max_minutes', ''))
            if not 0 <= grace_minutes <= 240:
                raise ValueError('Grace minutes must be between 0 and 240.')
            if not 1 <= lunch_max_minutes <= 240:
                raise ValueError('Maximum lunch must be between 1 and 240 minutes.')
            weekdays = sorted({
                int(day) for day in request.form.getlist('workdays')
                if day.isdigit() and 0 <= int(day) <= 6
            })
            if not weekdays:
                raise ValueError('Select at least one workday.')
        except (ValueError, ZoneInfoNotFoundError) as exc:
            flash(f'Invalid attendance policy: {exc}', 'warning')
            return redirect(url_for('admin_policy'))
        policy.timezone_name = timezone_name
        for key, value in values.items():
            setattr(policy, key, parsed_times[key].strftime('%H:%M'))
        policy.grace_minutes = grace_minutes
        policy.lunch_max_minutes = lunch_max_minutes
        policy.workdays = ','.join(str(day) for day in weekdays)
        policy.reminders_enabled = bool(request.form.get('reminders_enabled'))
        write_audit('attendance_policy_updated', 'Updated attendance schedule, break rules, or reminders.')
        db.session.commit()
        flash('Attendance policy saved.', 'success')
        return redirect(url_for('admin_policy'))
    holidays = Holiday.query.order_by(Holiday.holiday_date.desc()).all()
    return render_template(
        'admin_policy.html',
        policy=policy,
        holidays=holidays,
        workday_numbers={int(day) for day in policy.workdays.split(',') if day.isdigit()},
    )

@app.route('/admin/holidays/<int:holiday_id>/delete', methods=['POST'])
@admin_required
def admin_delete_holiday(holiday_id):
    holiday = db.session.get(Holiday, holiday_id)
    if not holiday:
        flash('Holiday not found.')
        return redirect(url_for('admin_policy'))
    write_audit('holiday_deleted', f'Removed holiday {holiday.name} on {holiday.holiday_date}.')
    db.session.delete(holiday)
    db.session.commit()
    flash('Holiday removed.')
    return redirect(url_for('admin_policy'))

@app.route('/admin/audit')
@admin_required
def admin_audit():
    page = request.args.get('page', 1, type=int)
    pagination = AuditLog.query.order_by(AuditLog.created_at.desc()).paginate(
        page=max(1, page), per_page=50, error_out=False,
    )
    return render_template('admin_audit.html', pagination=pagination)

def message_partner_for_current_user(partner_id):
    partner = db.session.get(User, partner_id)
    if not partner or not partner.account_active:
        return None
    if current_user.role == 'student':
        if current_user.supervisor_id == partner.id and partner.role == 'supervisor':
            return partner
    elif current_user.role == 'supervisor':
        if partner.role == 'student' and partner.supervisor_id == current_user.id:
            return partner
    return None

@app.route('/messages', methods=['GET', 'POST'])
@login_required
def messages():
    if current_user.role == 'student':
        partners = [db.session.get(User, current_user.supervisor_id)] if current_user.supervisor_id else []
        partners = [partner for partner in partners if partner and partner.account_active]
    elif current_user.role == 'supervisor':
        partners = User.query.filter_by(
            role='student',
            supervisor_id=current_user.id,
            account_active=True,
        ).order_by(User.username).all()
    else:
        flash('Messaging is available to students and their supervisors.')
        return redirect(url_for('dashboard'))
    selected_id = request.args.get('partner', type=int)
    if request.method == 'POST':
        selected_id = request.form.get('partner_id', type=int)
        partner = message_partner_for_current_user(selected_id) if selected_id else None
        content = request.form.get('content', '').strip()
        if not partner:
            flash('Choose a student or supervisor in your assigned group.', 'warning')
            return redirect(url_for('messages'))
        if not content or len(content) > 2000:
            flash('Message must contain 1 to 2000 characters.', 'warning')
            return redirect(url_for('messages', partner=partner.id))
        sent_at = local_now()
        message = Message(
            sender_id=current_user.id,
            receiver_id=partner.id,
            content=content,
            timestamp=sent_at,
        )
        db.session.add(message)
        queue_notification(
            partner,
            f'New message from {current_user.username}',
            content[:500],
            endpoint='messages',
            endpoint_values={'partner': current_user.id},
        )
        write_audit('message_sent', 'Sent a message.', partner.username)
        db.session.commit()
        flash('Message sent.', 'success')
        return redirect(url_for('messages', partner=partner.id))
    selected_partner = message_partner_for_current_user(selected_id) if selected_id else None
    if selected_id and not selected_partner:
        flash('That conversation is not available to your account.', 'warning')
        return redirect(url_for('messages'))
    conversation = []
    conversation_pagination = None
    if selected_partner:
        conversation_query = Message.query.filter(or_(
            (Message.sender_id == current_user.id) & (Message.receiver_id == selected_partner.id),
            (Message.sender_id == selected_partner.id) & (Message.receiver_id == current_user.id),
        ))
        page_number = max(1, request.args.get('page', 1, type=int) or 1)
        conversation_pagination = conversation_query.order_by(
            Message.timestamp.desc(),
            Message.id.desc(),
        ).paginate(page=page_number, per_page=50, error_out=False)
        conversation = list(reversed(conversation_pagination.items))
        unread = [
            item for item in conversation
            if item.receiver_id == current_user.id and item.read_at is None
        ]
        if unread:
            read_time = local_now()
            for item in unread:
                item.read_at = read_time
            db.session.commit()
    return render_template(
        'messages.html',
        partners=partners,
        selected_partner=selected_partner,
        conversation=conversation,
        conversation_pagination=conversation_pagination,
    )

@app.route('/notifications')
@login_required
def notifications():
    page = request.args.get('page', 1, type=int)
    pagination = Notification.query.filter_by(user_id=current_user.id).order_by(
        Notification.created_at.desc()
    ).paginate(page=max(1, page), per_page=30, error_out=False)
    return render_template('notifications.html', pagination=pagination)

@app.route('/notifications/<int:notification_id>/read', methods=['POST'])
@login_required
def mark_notification_read(notification_id):
    notification = Notification.query.filter_by(
        id=notification_id,
        user_id=current_user.id,
    ).first()
    if not notification:
        flash('Notification not found.', 'warning')
        return redirect(url_for('notifications'))
    notification.read_at = local_now()
    db.session.commit()
    safe_url = notification.url if notification.url.startswith('/') and not notification.url.startswith('//') else url_for('notifications')
    return redirect(safe_url)

@app.route('/attendance/requests', methods=['GET', 'POST'])
@login_required
def attendance_requests():
    if current_user.role == 'student':
        if request.method == 'POST':
            request_type = request.form.get('request_type', '').strip().lower()
            reason = request.form.get('reason', '').strip()
            date_raw = request.form.get('requested_date', '').strip()
            attendance_id = request.form.get('attendance_id', type=int)
            requested_time_in_raw = request.form.get('requested_time_in', '').strip()
            requested_time_out_raw = request.form.get('requested_time_out', '').strip()
            requested_lunch_start_raw = request.form.get('requested_lunch_start', '').strip()
            requested_lunch_end_raw = request.form.get('requested_lunch_end', '').strip()
            if request_type not in {'correction', 'absence', 'lunch_correction'}:
                flash('Select a valid request type.', 'warning')
                return redirect(url_for('attendance_requests'))
            if not reason or len(reason) > 1000:
                flash('Explain the request in 1 to 1000 characters.', 'warning')
                return redirect(url_for('attendance_requests'))
            try:
                requested_date = date.fromisoformat(date_raw)
                if requested_date > local_today() + timedelta(days=365):
                    raise ValueError('Requests cannot be more than one year in the future.')
            except ValueError as exc:
                flash(f'Invalid request date: {exc}', 'warning')
                return redirect(url_for('attendance_requests'))
            if request_type in {'correction', 'lunch_correction'} and requested_date > local_today():
                flash('Attendance corrections must be for today or an earlier date.', 'warning')
                return redirect(url_for('attendance_requests'))
            attendance = None
            if attendance_id:
                attendance = Attendance.query.filter_by(
                    id=attendance_id,
                    user_id=current_user.id,
                ).first()
                if not attendance:
                    flash('The selected attendance record was not found.', 'warning')
                    return redirect(url_for('attendance_requests'))
            if request_type == 'absence':
                if attendance or Attendance.query.filter_by(
                    user_id=current_user.id,
                    work_date=requested_date,
                ).first():
                    flash('An attendance record already exists for that date.', 'warning')
                    return redirect(url_for('attendance_requests'))
                requested_time_in = requested_time_out = None
                requested_lunch_start = requested_lunch_end = None
            elif request_type == 'correction':
                try:
                    requested_time_in = datetime.fromisoformat(requested_time_in_raw)
                    requested_time_out = datetime.fromisoformat(requested_time_out_raw)
                except ValueError:
                    flash('Provide valid requested clock-in and clock-out times.', 'warning')
                    return redirect(url_for('attendance_requests'))
                if requested_time_in.tzinfo or requested_time_out.tzinfo:
                    flash('Provide local date and time values without a timezone offset.', 'warning')
                    return redirect(url_for('attendance_requests'))
                if requested_time_in.date() != requested_date or requested_time_out.date() != requested_date:
                    flash('Requested times must fall on the selected date.', 'warning')
                    return redirect(url_for('attendance_requests'))
                if requested_time_out <= requested_time_in or requested_time_out > local_now():
                    flash('Clock-out must be after clock-in and cannot be in the future.', 'warning')
                    return redirect(url_for('attendance_requests'))
                if attendance and attendance.work_date and attendance.work_date != requested_date:
                    flash('The selected record does not match the requested date.', 'warning')
                    return redirect(url_for('attendance_requests'))
                requested_lunch_start = requested_lunch_end = None
            else:
                if not attendance or not attendance.time_in:
                    flash('Select an existing attendance record with a clock-in for the missed lunch.', 'warning')
                    return redirect(url_for('attendance_requests'))
                record_date = attendance.work_date or attendance.time_in.date()
                if record_date != requested_date:
                    flash('The selected record does not match the requested date.', 'warning')
                    return redirect(url_for('attendance_requests'))
                try:
                    requested_lunch_start = datetime.fromisoformat(requested_lunch_start_raw)
                    requested_lunch_end = datetime.fromisoformat(requested_lunch_end_raw)
                except ValueError:
                    flash('Provide valid requested lunch-out and afternoon time-in values.', 'warning')
                    return redirect(url_for('attendance_requests'))
                if requested_lunch_start.tzinfo or requested_lunch_end.tzinfo:
                    flash('Provide local date and time values without a timezone offset.', 'warning')
                    return redirect(url_for('attendance_requests'))
                if (
                    requested_lunch_start.date() != requested_date
                    or requested_lunch_end.date() != requested_date
                ):
                    flash('Requested lunch times must fall on the selected date.', 'warning')
                    return redirect(url_for('attendance_requests'))
                policy = get_attendance_policy()
                window_start = time.fromisoformat(policy.lunch_window_start if policy else '12:00')
                window_end = time.fromisoformat(policy.lunch_window_end if policy else '13:00')
                if not window_start <= requested_lunch_start.time() < window_end:
                    flash('The requested lunch-out must fall within the configured lunch window.', 'warning')
                    return redirect(url_for('attendance_requests'))
                now = local_now()
                if (
                    requested_lunch_start < attendance.time_in
                    or requested_lunch_end <= requested_lunch_start
                    or requested_lunch_end > now
                    or (attendance.time_out and requested_lunch_end > attendance.time_out)
                ):
                    flash('Afternoon time-in must be after lunch-out and cannot be in the future.', 'warning')
                    return redirect(url_for('attendance_requests'))
                requested_time_in = requested_time_out = None
            pending = AttendanceRequest.query.filter_by(
                student_id=current_user.id,
                requested_date=requested_date,
                status='pending',
            ).first()
            if pending:
                flash('You already have a pending request for that date.', 'warning')
                return redirect(url_for('attendance_requests'))
            supervisor = db.session.get(User, current_user.supervisor_id) if current_user.supervisor_id else None
            if not supervisor or supervisor.role != 'supervisor':
                flash('An assigned supervisor is required before submitting an attendance request.', 'warning')
                return redirect(url_for('attendance_requests'))
            attendance_request = AttendanceRequest(
                student_id=current_user.id,
                attendance_id=attendance.id if attendance else None,
                request_type=request_type,
                requested_date=requested_date,
                requested_time_in=requested_time_in,
                requested_time_out=requested_time_out,
                requested_lunch_start=requested_lunch_start,
                requested_lunch_end=requested_lunch_end,
                reason=reason,
            )
            db.session.add(attendance_request)
            db.session.flush()
            queue_notification(
                supervisor,
                f'Attendance request from {current_user.username}',
                f'{request_type.title()} request for {requested_date}: {reason[:350]}',
                endpoint='attendance_requests',
            )
            if request_type == 'lunch_correction':
                audit_action = 'attendance_lunch_correction_submitted'
                audit_details = (
                    f'Submitted lunch correction for {requested_date}: '
                    f'lunch-out {requested_lunch_start:%H:%M}, '
                    f'afternoon time-in {requested_lunch_end:%H:%M}.'
                )
            else:
                audit_action = 'attendance_request_submitted'
                audit_details = f'Submitted a {request_type} request for {requested_date}.'
            write_audit(audit_action, audit_details, current_user.username)
            db.session.commit()
            flash('Request submitted for supervisor review.', 'success')
            return redirect(url_for('attendance_requests'))
        requests_page = AttendanceRequest.query.filter_by(
            student_id=current_user.id,
        ).order_by(AttendanceRequest.created_at.desc()).all()
        records = Attendance.query.filter(
            Attendance.user_id == current_user.id,
            Attendance.approval_status != 'rejected',
        ).order_by(Attendance.work_date.desc()).all()
        return render_template(
            'attendance_requests.html',
            requests=requests_page,
            students={item.student_id: db.session.get(User, item.student_id) for item in requests_page},
            records=records,
            is_supervisor=False,
            prefill_type=request.args.get('request_type', ''),
            prefill_date=request.args.get('requested_date', ''),
            prefill_attendance_id=request.args.get('attendance_id', ''),
        )
    if current_user.role != 'supervisor':
        flash('Attendance requests are available to students and their supervisors.')
        return redirect(url_for('dashboard'))
    requests_page = (
        AttendanceRequest.query.join(User, AttendanceRequest.student_id == User.id)
        .filter(User.supervisor_id == current_user.id)
        .order_by(AttendanceRequest.status.asc(), AttendanceRequest.created_at.desc())
        .all()
    )
    return render_template(
        'attendance_requests.html',
        requests=requests_page,
        students={item.student_id: db.session.get(User, item.student_id) for item in requests_page},
        records=[],
        is_supervisor=True,
    )

@app.route('/attendance/requests/<int:request_id>/review', methods=['POST'])
@login_required
def review_attendance_request(request_id):
    if current_user.role != 'supervisor':
        flash('Only the assigned supervisor can review attendance requests.')
        return redirect(url_for('dashboard'))
    attendance_request = (
        AttendanceRequest.query.join(User, AttendanceRequest.student_id == User.id)
        .filter(
            AttendanceRequest.id == request_id,
            User.supervisor_id == current_user.id,
        ).first()
    )
    if not attendance_request:
        flash('Request not found or access denied.', 'warning')
        return redirect(url_for('attendance_requests'))
    if attendance_request.status != 'pending':
        flash('This request has already been reviewed.', 'warning')
        return redirect(url_for('attendance_requests'))
    decision = request.form.get('decision', '').strip().lower()
    review_note = request.form.get('review_note', '').strip()
    if decision not in {'approve', 'reject'} or len(review_note) > 1000:
        flash('Choose approve or reject and keep the review note under 1000 characters.', 'warning')
        return redirect(url_for('attendance_requests'))
    student = db.session.get(User, attendance_request.student_id)
    if (
        decision == 'approve'
        and attendance_request.request_type == 'absence'
        and Attendance.query.filter_by(
            user_id=student.id,
            work_date=attendance_request.requested_date,
        ).first()
    ):
        attendance_request.status = 'rejected'
        attendance_request.reviewed_by = current_user.id
        attendance_request.reviewed_at = local_now()
        attendance_request.review_note = 'An attendance record was added for this date after the request was submitted.'
        queue_notification(
            student,
            'Absence request could not be approved',
            f'An attendance record already exists for {attendance_request.requested_date}, so the absence request was closed.',
            endpoint='attendance_requests',
        )
        write_audit(
            'attendance_absence_conflict',
            f'Closed absence request for {attendance_request.requested_date} after attendance was recorded.',
            student.username,
        )
        db.session.commit()
        flash('Request was closed because attendance has since been recorded for that date.', 'warning')
        return redirect(url_for('attendance_requests'))
    if decision == 'approve':
        attendance = db.session.get(Attendance, attendance_request.attendance_id) if attendance_request.attendance_id else None
        if attendance_request.request_type == 'absence':
            attendance = Attendance(
                user_id=student.id,
                work_date=attendance_request.requested_date,
                approval_status='approved',
                approved_by=current_user.id,
                approved_at=local_now(),
                correction_note=attendance_request.reason,
            )
            db.session.add(attendance)
        elif attendance_request.request_type == 'lunch_correction':
            if (
                not attendance
                or attendance.user_id != student.id
                or not attendance.time_in
                or (attendance.work_date or attendance.time_in.date()) != attendance_request.requested_date
            ):
                attendance_request.status = 'rejected'
                attendance_request.reviewed_by = current_user.id
                attendance_request.reviewed_at = local_now()
                attendance_request.review_note = 'The attendance record no longer exists or does not match this request.'
                queue_notification(
                    student,
                    'Lunch correction could not be approved',
                    f'The attendance record for {attendance_request.requested_date} no longer matches the request.',
                    endpoint='attendance_requests',
                )
                write_audit(
                    'attendance_lunch_correction_conflict',
                    f'Closed lunch correction request for {attendance_request.requested_date} because its attendance record was unavailable.',
                    student.username,
                )
                db.session.commit()
                flash('Request was closed because its attendance record no longer matches.', 'warning')
                return redirect(url_for('attendance_requests'))
            attendance.work_date = attendance_request.requested_date
            attendance.lunch_start = attendance_request.requested_lunch_start
            attendance.lunch_end = attendance_request.requested_lunch_end
            attendance.approval_status = 'approved'
            attendance.approved_by = current_user.id
            attendance.approved_at = local_now()
            attendance.correction_note = attendance_request.reason
            attendance_request.status = 'approved'
        else:
            if not attendance:
                attendance = Attendance(user_id=student.id)
                db.session.add(attendance)
            attendance.work_date = attendance_request.requested_date
            attendance.time_in = attendance_request.requested_time_in
            attendance.time_out = attendance_request.requested_time_out
            attendance.approval_status = 'approved'
            attendance.approved_by = current_user.id
            attendance.approved_at = local_now()
            attendance.correction_note = attendance_request.reason
        attendance_request.status = 'approved'
    else:
        attendance_request.status = 'rejected'
        attendance = db.session.get(Attendance, attendance_request.attendance_id) if attendance_request.attendance_id else None
    attendance_request.reviewed_by = current_user.id
    attendance_request.reviewed_at = local_now()
    attendance_request.review_note = review_note or None
    decision_text = 'approved' if decision == 'approve' else 'rejected'
    queue_notification(
        student,
        f'Attendance request {decision_text}',
        f'Your {attendance_request.request_type} request for {attendance_request.requested_date} was {decision_text}.'
        + (f' Supervisor note: {review_note}' if review_note else ''),
        endpoint='attendance_requests',
    )
    if attendance_request.request_type == 'lunch_correction':
        audit_action = f'attendance_lunch_correction_{decision_text}'
        audit_details = (
            f'{decision_text.title()} lunch correction for {attendance_request.requested_date}: '
            f'lunch-out {attendance_request.requested_lunch_start:%H:%M}, '
            f'afternoon time-in {attendance_request.requested_lunch_end:%H:%M}.'
        )
    else:
        audit_action = f'attendance_request_{decision_text}'
        audit_details = f'Reviewed {attendance_request.request_type} request for {attendance_request.requested_date}.'
    write_audit(audit_action, audit_details, student.username)
    db.session.commit()
    flash(f'Request {decision_text}.', 'success')
    return redirect(url_for('attendance_requests'))

@app.route('/profile_photos/<path:filename>')
@login_required
def profile_photo(filename):
    if current_user.photo != filename:
        return Response('Forbidden', status=403)
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    if current_user.role == 'admin':
        return redirect(url_for('admin_dashboard'))
    if current_user.role == 'sip_center_supervisor':
        return redirect(url_for('sip_center_supervisor_dashboard'))
    if current_user.role == 'supervisor':
        return redirect(url_for('supervisor_dashboard'))
    # Editable profile
    upload_message = None
    if request.method == 'POST':
        if 'username' in request.form:
            new_username = request.form['username']
            if new_username and new_username != current_user.username:
                if User.query.filter_by(username=new_username).first():
                    flash('Username already exists.')
                else:
                    current_user.username = new_username
                    write_audit('profile_username_updated', 'Updated username.', current_user.username)
                    db.session.commit()
                    flash('Username updated.')
        if 'email' in request.form:
            raw_email = request.form.get('email', '').strip()
            email = validate_email_address(raw_email)
            duplicate_email = User.query.filter(
                User.email == email,
                User.id != current_user.id,
            ).first() if email else None
            if raw_email and not email:
                flash('Enter a valid email address.', 'warning')
            elif duplicate_email:
                flash('That email address is already in use.', 'warning')
            else:
                current_user.email = email
                current_user.email_notifications = bool(
                    email and request.form.get('email_notifications')
                )
                write_audit('profile_email_updated', 'Updated email notification preferences.', current_user.username)
                db.session.commit()
                flash('Email preferences updated.', 'success')
        if 'password' in request.form and request.form['password']:
            if len(request.form['password']) < 8:
                flash('Password must be at least 8 characters.')
                return redirect(url_for('dashboard'))
            current_user.password = generate_password_hash(request.form['password'])
            write_audit('profile_password_updated', 'Updated account password.', current_user.username)
            db.session.commit()
            flash('Password updated.')
        if 'message' in request.form:
            msg = request.form['message'].strip()
            supervisor = message_partner_for_current_user(current_user.supervisor_id) if current_user.supervisor_id else None
            if msg and len(msg) <= 2000 and supervisor:
                new_msg = Message(sender_id=current_user.id, receiver_id=supervisor.id, content=msg, timestamp=local_now())
                db.session.add(new_msg)
                queue_notification(
                    supervisor,
                    f'New message from {current_user.username}',
                    msg[:500],
                    endpoint='messages',
                    endpoint_values={'partner': current_user.id},
                )
                write_audit('message_sent', 'Sent a message to assigned supervisor.', supervisor.username)
                db.session.commit()
                flash('Message sent to supervisor.')
            elif msg:
                flash('Message was not sent. Confirm your supervisor assignment and keep it under 2000 characters.', 'warning')
        if 'photo' in request.files:
            file = request.files['photo']
            if file and valid_image(file):
                extension = 'jpg' if file.mimetype == 'image/jpeg' else file.mimetype.rsplit('/', 1)[-1]
                filename = secure_filename(f"{current_user.id}_{secrets.token_hex(16)}.{extension}")
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                if current_user.photo:
                    old_path = os.path.join(app.config['UPLOAD_FOLDER'], current_user.photo)
                    if os.path.isfile(old_path):
                        os.remove(old_path)
                current_user.photo = filename
                db.session.commit()
                upload_message = 'Profile photo uploaded successfully.'
            elif file:
                upload_message = 'Invalid file type. Please upload an image.'
    if current_user.role == 'supervisor':
        return redirect(url_for('supervisor_dashboard'))
    today = local_today()
    attendance_query = Attendance.query.filter_by(user_id=current_user.id)
    attendance = attendance_query.order_by(Attendance.id.desc()).first()
    # Today limits and button states
    start_of_today = datetime.combine(today, datetime.min.time())
    start_of_tomorrow = start_of_today + timedelta(days=1)
    today_record = (
        Attendance.query
        .filter(
            Attendance.user_id == current_user.id,
            or_(
                Attendance.work_date == today,
                and_(
                    Attendance.work_date.is_(None),
                    Attendance.time_in >= start_of_today,
                    Attendance.time_in < start_of_tomorrow,
                ),
            ),
        )
        .order_by(Attendance.id.desc())
        .first()
    )
    can_time_in = today_record is None or (
        today_record.time_in is None and today_record.approval_status == 'approved'
    )
    can_time_out = bool(today_record and today_record.time_in and not today_record.time_out)
    today_time_in_str = today_record.time_in.strftime('%I:%M %p') if today_record and today_record.time_in else None
    today_time_out_str = today_record.time_out.strftime('%I:%M %p') if today_record and today_record.time_out else None
    total_hours = aggregate_attendance(attendance_query)['hours']
    enrollment = EnrollmentInfo.query.filter_by(student_id=current_user.id).first()
    target_hours = enrollment.target_hours if enrollment and enrollment.target_hours else DEFAULT_TARGET_HOURS
    remaining_hours = max(0.0, target_hours - total_hours)
    progress_percent = min(100, round(total_hours / target_hours * 100, 1)) if target_hours else 0
    supervisor = None
    if current_user.supervisor_id:
        supervisor = db.session.get(User, current_user.supervisor_id)
    # Weekly attendance (last 7 days)
    week_days = [(today - timedelta(days=i)) for i in range(6, -1, -1)]
    week_start = datetime.combine(week_days[0], time.min)
    week_records = Attendance.query.filter(
        Attendance.user_id == current_user.id,
        Attendance.approval_status == 'approved',
        Attendance.time_in >= week_start,
        Attendance.time_in < start_of_tomorrow,
    ).order_by(Attendance.id.desc()).all()
    week_attendance = {}
    for d in week_days:
        week_attendance[d] = next(
            (rec for rec in week_records if rec.time_in and rec.time_in.date() == d),
            None,
        )
    # Status indicator
    status = 'clocked_out'
    if attendance and attendance.time_in and not attendance.time_out:
        status = 'clocked_in'
    # Attendance streaks
    streak = 0
    for d in reversed(week_days):
        rec = week_attendance[d]
        if rec and rec.time_in and rec.time_out and rec.approval_status == 'approved':
            streak += 1
        else:
            break
    # Attendance chart data (hours per day for last 7 days)
    chart_labels = [d.strftime('%a') for d in week_days]
    chart_data = []
    for d in week_days:
        rec = week_attendance[d]
        if rec and rec.time_in and rec.time_out and rec.approval_status == 'approved':
            chart_data.append(round(net_work_hours(rec), 2))
        else:
            chart_data.append(0)
    # Notifications
    notifications = []
    if not attendance or (attendance and attendance.time_out):
        notifications.append('You are currently clocked out. Don\'t forget to clock in!')
    messages = Message.query.filter(or_(
        Message.sender_id == current_user.id,
        Message.receiver_id == current_user.id,
    )).order_by(Message.timestamp.desc()).limit(10).all()
    photo_url = url_for('profile_photo', filename=current_user.photo) if current_user.photo else f'https://ui-avatars.com/api/?name={current_user.username}&background=0D8ABC&color=fff&size=64'
    on_lunch_break = bool(today_record and today_record.lunch_start and not today_record.lunch_end)
    on_coffee_break = bool(today_record and today_record.coffee_start and not today_record.coffee_end)
    policy = get_attendance_policy()
    lunch_window_start = time.fromisoformat(policy.lunch_window_start if policy else '12:00')
    lunch_window_end = time.fromisoformat(policy.lunch_window_end if policy else '13:00')
    current_time = local_now().time()
    lunch_window_open = lunch_window_start <= current_time < lunch_window_end
    can_start_lunch = bool(
        can_time_out
        and lunch_window_open
        and not on_lunch_break
        and not on_coffee_break
        and today_record
        and not today_record.lunch_start
    )
    missed_lunch_punch = bool(
        today_record
        and today_record.time_in
        and not today_record.lunch_start
        and current_time >= lunch_window_end
    )
    if can_time_out:
        if on_lunch_break:
            notifications.insert(0, 'You are on lunch break — up to 1 hour allowed.')
        elif on_coffee_break:
            notifications.insert(0, 'You are on coffee break — up to 30 minutes allowed.')
        elif missed_lunch_punch:
            notifications.insert(0, 'Your lunch time-out was not recorded. Submit a lunch correction request with the actual times.')
    return render_template(
        'dashboard.html',
        attendance=attendance,
        records=week_records,
        total_hours=total_hours,
        target_hours=target_hours,
        remaining_hours=remaining_hours,
        progress_percent=progress_percent,
        supervisor=supervisor,
        week_attendance=week_attendance,
        status=status,
        streak=streak,
        chart_labels=chart_labels,
        chart_data=chart_data,
        notifications=notifications,
        messages=messages,
        photo_url=photo_url,
        upload_message=upload_message,
        can_time_in=can_time_in,
        can_time_out=can_time_out,
        today_time_in=today_time_in_str,
        today_time_out=today_time_out_str,
        today_record=today_record,
        on_lunch_break=on_lunch_break,
        on_coffee_break=on_coffee_break,
        can_start_lunch=can_start_lunch,
        lunch_window_open=lunch_window_open,
        lunch_window_start=lunch_window_start,
        lunch_window_end=lunch_window_end,
        missed_lunch_punch=missed_lunch_punch,
        scheduled_breaks=scheduled_breaks(today_record) if today_record else [],
    )
@app.route('/export_csv')
@login_required
def export_csv():
    query = Attendance.query.filter_by(user_id=current_user.id)
    try:
        query = apply_report_filters(
            query,
            request.args.get('start_date', ''),
            request.args.get('end_date', ''),
            request.args.get('status', ''),
        )
    except ValueError as exc:
        flash(f'Invalid report filter: {exc}', 'warning')
        return redirect(url_for('records'))
    records = query.order_by(Attendance.work_date.desc(), Attendance.id.desc()).all()
    def generate():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Work Date', 'Review Status', 'Time In', 'Time Out', 'Lunch Break', 'Coffee Break', 'Net Hours (h)'])
        for rec in records:
            time_in = rec.time_in.strftime('%Y-%m-%d %H:%M') if rec.time_in else ''
            time_out = rec.time_out.strftime('%Y-%m-%d %H:%M') if rec.time_out else ''
            lunch = format_break_range(rec.lunch_start, rec.lunch_end)
            coffee = format_break_range(rec.coffee_start, rec.coffee_end)
            net_h = f"{net_work_hours(rec):.2f}" if rec.time_in and rec.time_out else ''
            work_date = (rec.work_date or (rec.time_in.date() if rec.time_in else '')).isoformat() if rec.work_date or rec.time_in else ''
            writer.writerow([csv_cell(value) for value in [work_date, rec.approval_status, time_in, time_out, lunch, coffee, net_h]])
        return output.getvalue()
    return Response(generate(), mimetype='text/csv', headers={'Content-Disposition': 'attachment;filename=attendance.csv'})

def apply_report_filters(query, start_raw='', end_raw='', status_filter=''):
    start_raw = (start_raw or '').strip()
    end_raw = (end_raw or '').strip()
    status_filter = (status_filter or '').strip().lower()
    allowed = {'pending', 'approved', 'rejected', 'absent', 'incomplete', 'complete', 'late'}
    if status_filter and status_filter not in allowed:
        raise ValueError('Status filter is not supported.')
    try:
        start_date = date.fromisoformat(start_raw) if start_raw else None
        end_date = date.fromisoformat(end_raw) if end_raw else None
    except ValueError as exc:
        raise ValueError('Use valid YYYY-MM-DD dates.') from exc
    if start_date and end_date and start_date > end_date:
        raise ValueError('Start date must be on or before end date.')
    if start_date:
        query = query.filter(attendance_date_at_or_after(start_date))
    if end_date:
        query = query.filter(attendance_date_before(end_date + timedelta(days=1)))
    if status_filter in {'pending', 'approved', 'rejected'}:
        query = query.filter(Attendance.approval_status == status_filter)
    elif status_filter == 'absent':
        query = query.filter(Attendance.time_in.is_(None))
    elif status_filter == 'incomplete':
        query = query.filter(Attendance.time_in.is_not(None), Attendance.time_out.is_(None))
    elif status_filter == 'complete':
        query = query.filter(
            Attendance.approval_status == 'approved',
            Attendance.time_in.is_not(None),
            Attendance.time_out.is_not(None),
        )
    elif status_filter == 'late':
        policy = get_attendance_policy()
        shift_start = time.fromisoformat(policy.work_start if policy else '09:00')
        grace_minutes = policy.grace_minutes if policy else 15
        cutoff_minutes = shift_start.hour * 60 + shift_start.minute + grace_minutes
        query = query.filter(
            Attendance.approval_status == 'approved',
            Attendance.time_in.is_not(None),
            (db.func.extract('hour', Attendance.time_in) * 60 + db.func.extract('minute', Attendance.time_in)) > cutoff_minutes,
        )
    return query

# Supervisor dashboard route
@app.route('/supervisor_dashboard', methods=['GET', 'POST'])
@login_required
def supervisor_dashboard():
    if current_user.role != 'supervisor':
        flash('Access denied.')
        return redirect(url_for('dashboard'))
    # Filtering/search
    search = request.args.get('search', '').strip()[:150]
    student_query = User.query.filter_by(
        role='student',
        supervisor_id=current_user.id,
        account_active=True,
    ).order_by(User.username)
    if search:
        student_query = student_query.filter(User.username.ilike(f'%{search}%'))
    try:
        page_number = max(1, int(request.args.get('page', '1')))
    except ValueError:
        page_number = 1
    pagination = student_query.paginate(page=page_number, per_page=25, error_out=False)
    filtered_students = pagination.items
    total_student_count = User.query.filter_by(
        role='student',
        supervisor_id=current_user.id,
        account_active=True,
    ).count()
    complete_approved = and_(
        Attendance.approval_status == 'approved',
        Attendance.time_in.is_not(None),
        Attendance.time_out.is_not(None),
    )
    approved_hours = db.session.query(
        db.func.coalesce(
            db.func.sum(case((complete_approved, attendance_net_hours_expression()), else_=0)),
            0,
        )
    ).join(User, Attendance.user_id == User.id).filter(
        User.role == 'student',
        User.supervisor_id == current_user.id,
        User.account_active.is_(True),
    ).scalar()
    average_hours = round(approved_hours / total_student_count, 2) if total_student_count else 0
    student_data = []
    page_student_ids = [student.id for student in filtered_students]
    page_metrics = {}
    if page_student_ids:
        page_metrics = {
            student_id: (float(hours or 0), int(approved_count or 0), int(present_count or 0))
            for student_id, hours, approved_count, present_count in db.session.query(
                Attendance.user_id,
                db.func.coalesce(
                    db.func.sum(case((complete_approved, attendance_net_hours_expression()), else_=0)),
                    0,
                ),
                db.func.sum(case((Attendance.approval_status == 'approved', 1), else_=0)),
                db.func.sum(case((complete_approved, 1), else_=0)),
            ).filter(
                Attendance.user_id.in_(page_student_ids)
            ).group_by(Attendance.user_id).all()
        }
    enrollments_by_student = {
        enrollment.student_id: enrollment
        for enrollment in EnrollmentInfo.query.filter(
            EnrollmentInfo.student_id.in_(page_student_ids)
        ).all()
    } if page_student_ids else {}
    for s in filtered_students:
        total_hours, approved_count, present_days = page_metrics.get(s.id, (0.0, 0, 0))
        percent = int((present_days / approved_count) * 100) if approved_count else 0
        enrollment = enrollments_by_student.get(s.id)
        target_hours = enrollment.target_hours if enrollment and enrollment.target_hours else DEFAULT_TARGET_HOURS
        student_data.append({
            'id': s.id,
            'username': s.username,
            'total_hours': round(total_hours, 2),
            'percent': percent,
            'target_hours': target_hours,
            'remaining_hours': max(0, round(target_hours - total_hours, 2)),
            'progress_percent': min(100, round(total_hours / target_hours * 100, 1)) if target_hours else 0,
        })
    # Approve/edit time records
    if request.method == 'POST':
        rec_id = request.form.get('record_id')
        approve = request.form.get('approve')
        edit_time_in = request.form.get('edit_time_in')
        edit_time_out = request.form.get('edit_time_out')
        if rec_id:
            try:
                rec_id = int(rec_id)
            except (TypeError, ValueError):
                flash('Invalid attendance record.')
                return redirect(url_for('supervisor_dashboard'))
            rec = (
                Attendance.query
                .join(User, Attendance.user_id == User.id)
                .filter(Attendance.id == rec_id, User.supervisor_id == current_user.id)
                .first()
            )
            if not rec:
                flash('Attendance record not found or access denied.')
                return redirect(url_for('supervisor_dashboard'))
            if approve:
                rec.approval_status = 'approved'
                rec.approved_by = current_user.id
                rec.approved_at = local_now()
                student = db.session.get(User, rec.user_id)
                write_audit('attendance_approved', f'Approved attendance record {rec_id}.', student.username if student else None)
                if student:
                    queue_notification(
                        student,
                        'Attendance approved',
                        f'Your attendance for {rec.work_date or (rec.time_in.date() if rec.time_in else "this day")} was approved.',
                        endpoint='records',
                    )
                flash(f'Record {rec_id} approved.')
            try:
                if edit_time_in:
                    rec.time_in = datetime.strptime(edit_time_in, '%Y-%m-%d %H:%M')
                if edit_time_out:
                    rec.time_out = datetime.strptime(edit_time_out, '%Y-%m-%d %H:%M')
            except ValueError:
                flash('Invalid attendance date or time.')
                return redirect(url_for('supervisor_dashboard'))
            if rec.time_in and rec.time_out and rec.time_out < rec.time_in:
                flash('Time out must be after time in.')
                return redirect(url_for('supervisor_dashboard'))
            if edit_time_in or edit_time_out:
                if rec.time_in:
                    rec.work_date = rec.time_in.date()
                rec.approval_status = 'approved'
                rec.approved_by = current_user.id
                rec.approved_at = local_now()
                student = db.session.get(User, rec.user_id)
                write_audit('attendance_edited', f'Edited attendance record {rec_id}.', student.username if student else None)
            db.session.commit()
            flash('Record updated.')
        return redirect(url_for('supervisor_dashboard'))
    pending_attendance = (
        db.session.query(Attendance, User)
        .join(User, Attendance.user_id == User.id)
        .filter(User.supervisor_id == current_user.id, Attendance.approval_status == 'pending')
        .order_by(Attendance.time_in.asc())
        .limit(100)
        .all()
    )
    return render_template(
        'supervisor_dashboard.html',
        student_data=student_data,
        search=search,
        pending_attendance=pending_attendance,
        pagination=pagination,
        total_student_count=total_student_count,
        average_hours=average_hours,
    )

@app.route('/supervisor/attendance/<int:attendance_id>/review', methods=['POST'])
@login_required
def review_attendance(attendance_id):
    if current_user.role != 'supervisor':
        flash('Only supervisors can review attendance.')
        return redirect(url_for('dashboard'))
    attendance, student = (
        db.session.query(Attendance, User)
        .join(User, Attendance.user_id == User.id)
        .filter(Attendance.id == attendance_id, User.supervisor_id == current_user.id)
        .first_or_404()
    )
    if attendance.approval_status != 'pending':
        flash('This attendance record has already been reviewed.', 'warning')
        return redirect(url_for('supervisor_dashboard'))
    if not attendance.time_in or not attendance.time_out:
        flash('Only completed attendance records can be reviewed.', 'warning')
        return redirect(url_for('supervisor_dashboard'))
    decision = request.form.get('decision', '').strip().lower()
    if decision not in {'approve', 'reject'}:
        flash('Choose approve or reject.', 'warning')
        return redirect(url_for('supervisor_dashboard'))
    attendance.approval_status = 'approved' if decision == 'approve' else 'rejected'
    attendance.approved_by = current_user.id
    attendance.approved_at = local_now()
    write_audit(
        f'attendance_{attendance.approval_status}',
        f'{attendance.approval_status.title()} attendance record {attendance.id}.',
        student.username,
    )
    queue_notification(
        student,
        f'Attendance {attendance.approval_status}',
        f'Your attendance for {attendance.work_date or attendance.time_in.date()} was {attendance.approval_status}.',
        endpoint='records',
    )
    db.session.commit()
    flash(f'Attendance {attendance.approval_status}.', 'success')
    return redirect(url_for('supervisor_dashboard'))
# Export all records to CSV (supervisor)
@app.route('/export_all_csv')
@login_required
def export_all_csv():
    if current_user.role != 'supervisor':
        flash('Access denied.')
        return redirect(url_for('dashboard'))
    query = (
        db.session.query(Attendance, User)
        .join(User, Attendance.user_id == User.id)
        .filter(User.supervisor_id == current_user.id)
    )
    try:
        query = apply_report_filters(
            query,
            request.args.get('start_date', ''),
            request.args.get('end_date', ''),
            request.args.get('status', ''),
        )
    except ValueError as exc:
        flash(f'Invalid report filter: {exc}', 'warning')
        return redirect(url_for('supervisor_dashboard'))
    all_records = query.order_by(Attendance.work_date.desc(), Attendance.id.desc()).all()
    def generate():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Student Username', 'Work Date', 'Review Status', 'Time In', 'Time Out', 'Lunch Break', 'Coffee Break', 'Net Hours (h)'])
        for attendance, user in all_records:
            time_in = attendance.time_in.strftime('%Y-%m-%d %H:%M') if attendance.time_in else ''
            time_out = attendance.time_out.strftime('%Y-%m-%d %H:%M') if attendance.time_out else ''
            lunch = format_break_range(attendance.lunch_start, attendance.lunch_end)
            coffee = format_break_range(attendance.coffee_start, attendance.coffee_end)
            net_h = f"{net_work_hours(attendance):.2f}" if attendance.time_in and attendance.time_out else ''
            work_date = (attendance.work_date or (attendance.time_in.date() if attendance.time_in else '')).isoformat() if attendance.work_date or attendance.time_in else ''
            writer.writerow([csv_cell(value) for value in [user.username, work_date, attendance.approval_status, time_in, time_out, lunch, coffee, net_h]])
        return output.getvalue()
    return Response(generate(), mimetype='text/csv', headers={'Content-Disposition': 'attachment;filename=all_attendance.csv'})
# Export all records to PDF (supervisor)
@app.route('/export_all_pdf')
@login_required
def export_all_pdf():
    if current_user.role != 'supervisor':
        flash('Access denied.')
        return redirect(url_for('dashboard'))
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
        import io
    except ImportError:
        flash('PDF export requires reportlab. Please install it.')
        return redirect(url_for('supervisor_dashboard'))
    all_records = (
        db.session.query(Attendance, User)
        .join(User, Attendance.user_id == User.id)
        .filter(User.supervisor_id == current_user.id)
        .order_by(Attendance.id.desc())
        .all()
    )
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    p.drawString(30, 750, 'All Attendance Records')
    y = 730
    p.drawString(30, y, 'Student Username | Time In | Time Out')
    y -= 20
    for attendance, user in all_records:
        line = f'{user.username} | '
        line += attendance.time_in.strftime('%Y-%m-%d %H:%M') if attendance.time_in else 'N/A'
        line += ' | '
        line += attendance.time_out.strftime('%Y-%m-%d %H:%M') if attendance.time_out else 'N/A'
        p.drawString(30, y, line)
        y -= 15
        if y < 50:
            p.showPage()
            y = 750
    p.save()
    buffer.seek(0)
    return Response(buffer, mimetype='application/pdf', headers={'Content-Disposition': 'attachment;filename=all_attendance.pdf'})

@app.route('/students_list')
@login_required
def students_list():
    search = request.args.get('q', '').strip()[:100]
    if len(search) < 2:
        return jsonify([])
    query = User.query.filter_by(role='student', account_active=True)
    if current_user.role == 'supervisor':
        query = query.filter(User.supervisor_id.is_(None))
    elif current_user.role == 'sip_center_supervisor':
        query = query.filter(~select(MonitoredStudent.id).where(
            MonitoredStudent.student_id == User.id,
        ).exists())
    else:
        return Response('Forbidden', status=403)
    students = query.filter(User.username.ilike(f'%{search}%')).order_by(
        User.username
    ).limit(30).all()
    return jsonify([{'username': student.username} for student in students])

@app.route('/time_in', methods=['POST'])
@login_required
def time_in():
    if current_user.role != 'student':
        flash('Only students can record attendance.')
        return redirect(url_for('dashboard'))
    # Enforce one time-in per day
    now = local_now()
    today = local_today()
    start_of_today = datetime.combine(today, datetime.min.time())
    start_of_tomorrow = start_of_today + timedelta(days=1)
    existing_today = Attendance.query.filter(
        Attendance.user_id == current_user.id,
        or_(
            Attendance.work_date == today,
            and_(Attendance.work_date.is_(None), Attendance.time_in >= start_of_today, Attendance.time_in < start_of_tomorrow),
        ),
    ).first()
    if existing_today and not (
        existing_today.time_in is None
        and existing_today.approval_status == 'approved'
    ):
        flash('You have already timed in today.')
        return redirect(url_for('dashboard'))
    if existing_today:
        attendance = existing_today
        attendance.time_in = now
        attendance.approval_status = 'pending'
        attendance.approved_by = None
        attendance.approved_at = None
        attendance.correction_note = None
    else:
        attendance = Attendance(user_id=current_user.id, time_in=now, work_date=today, approval_status='pending')
        db.session.add(attendance)
    db.session.commit()
    flash('Timed in successfully.')
    return redirect(url_for('dashboard'))

@app.route('/time_out', methods=['POST'])
@login_required
def time_out():
    if current_user.role != 'student':
        flash('Only students can record attendance.')
        return redirect(url_for('dashboard'))
    # Allow time-out only for today's open record
    today = local_today()
    start_of_today = datetime.combine(today, datetime.min.time())
    start_of_tomorrow = start_of_today + timedelta(days=1)
    attendance = (
        Attendance.query
        .filter(
            Attendance.user_id == current_user.id,
            Attendance.time_out.is_(None),
            or_(
                Attendance.work_date == today,
                and_(Attendance.work_date.is_(None), Attendance.time_in >= start_of_today, Attendance.time_in < start_of_tomorrow),
            ),
        )
        .order_by(Attendance.id.desc())
        .first()
    )
    if attendance and attendance.time_in and not attendance.time_out:
        if attendance.lunch_start and not attendance.lunch_end:
            flash('Record your afternoon time-in to end lunch before your final time-out.', 'warning')
            return redirect(url_for('dashboard'))
        end = local_now()
        finalize_open_breaks(attendance, end)
        attendance.time_out = end
        supervisor = db.session.get(User, current_user.supervisor_id) if current_user.supervisor_id else None
        if supervisor:
            queue_notification(
                supervisor,
                f'Attendance ready to review: {current_user.username}',
                f'{current_user.username} completed attendance on {today}.',
                endpoint='supervisor_dashboard',
            )
        db.session.commit()
        flash('Timed out successfully.')
        policy = get_attendance_policy()
        lunch_window_end = time.fromisoformat(policy.lunch_window_end if policy else '13:00')
        if not attendance.lunch_start and end.time() >= lunch_window_end:
            flash('Your lunch time-out is missing. Submit a lunch correction request with the actual times.', 'warning')
    else:
        flash('No open time-in to time out today.')
    return redirect(url_for('dashboard'))

def _today_open_attendance_for_user(user_id):
    today = local_today()
    start_of_today = datetime.combine(today, datetime.min.time())
    start_of_tomorrow = start_of_today + timedelta(days=1)
    return (
        Attendance.query
        .filter(
            Attendance.user_id == user_id,
            Attendance.time_out.is_(None),
            or_(
                Attendance.work_date == today,
                and_(Attendance.work_date.is_(None), Attendance.time_in >= start_of_today, Attendance.time_in < start_of_tomorrow),
            ),
        )
        .order_by(Attendance.id.desc())
        .first()
    )

@app.route('/break_start', methods=['POST'])
@login_required
def break_start():
    if current_user.role != 'student':
        flash('Breaks are only available on the student dashboard.')
        return redirect(url_for('dashboard'))
    break_type = (request.form.get('break_type') or '').strip().lower()
    if break_type != 'lunch':
        flash('Only lunch punches are enabled.')
        return redirect(url_for('dashboard'))
    rec = _today_open_attendance_for_user(current_user.id)
    if not rec or not rec.time_in or rec.time_out:
        flash('You must be timed in to start a break.')
        return redirect(url_for('dashboard'))
    on_lunch = rec.lunch_start and not rec.lunch_end
    on_coffee = rec.coffee_start and not rec.coffee_end
    if on_lunch or on_coffee:
        flash('End your current break before starting another.')
        return redirect(url_for('dashboard'))
    if break_type == 'lunch':
        if rec.lunch_end:
            flash('Lunch break already taken today.')
            return redirect(url_for('dashboard'))
        now = local_now()
        policy = get_attendance_policy()
        window_start = time.fromisoformat(policy.lunch_window_start if policy else '12:00')
        window_end = time.fromisoformat(policy.lunch_window_end if policy else '13:00')
        if not window_start <= now.time() < window_end:
            flash(
                f'Lunch time-out is available only between {window_start.strftime("%I:%M %p")} and {window_end.strftime("%I:%M %p")}.',
                'warning',
            )
            return redirect(url_for('dashboard'))
        rec.lunch_start = now
        lunch_limit, _ = policy_break_limits(policy)
        allowed_minutes = int(lunch_limit.total_seconds() // 60)
        flash(f'Lunch time-out recorded. Allowed lunch: {allowed_minutes} minute(s).')
    else:
        if rec.coffee_end:
            flash('Coffee break already taken today.')
            return redirect(url_for('dashboard'))
        rec.coffee_start = local_now()
        flash('Coffee break started. Allowed time: 30 minutes.')
    db.session.commit()
    return redirect(url_for('dashboard'))

@app.route('/break_end', methods=['POST'])
@login_required
def break_end():
    if current_user.role != 'student':
        flash('Breaks are only available on the student dashboard.')
        return redirect(url_for('dashboard'))
    break_type = (request.form.get('break_type') or '').strip().lower()
    if break_type != 'lunch':
        flash('Only lunch punches are enabled.')
        return redirect(url_for('dashboard'))
    rec = _today_open_attendance_for_user(current_user.id)
    if not rec or not rec.time_in or rec.time_out:
        flash('No active attendance found.')
        return redirect(url_for('dashboard'))
    end = local_now()
    lunch_limit, coffee_limit = policy_break_limits()
    if break_type == 'lunch':
        if not rec.lunch_start or rec.lunch_end:
            flash('No active lunch break to end.')
            return redirect(url_for('dashboard'))
        rec.lunch_end = end
        if not flash_if_overbreak('Lunch break', end - rec.lunch_start, lunch_limit):
            flash('Lunch break ended.')
    else:
        if not rec.coffee_start or rec.coffee_end:
            flash('No active coffee break to end.')
            return redirect(url_for('dashboard'))
        rec.coffee_end = end
        if not flash_if_overbreak('Coffee break', end - rec.coffee_start, coffee_limit):
            flash('Coffee break ended.')
    db.session.commit()
    return redirect(url_for('dashboard'))

@app.route('/attendance/<int:record_id>/delete', methods=['POST'])
@login_required
def delete_attendance(record_id):
    if current_user.role != 'supervisor':
        flash('Access denied.')
        return redirect(url_for('dashboard'))
    record = (
        Attendance.query
        .join(User, Attendance.user_id == User.id)
        .filter(Attendance.id == record_id, User.supervisor_id == current_user.id)
        .first()
    )
    if not record:
        flash('Attendance record not found or access denied.')
        return redirect(url_for('supervisor_dashboard'))
    student = db.session.get(User, record.user_id)
    student_username = student.username if student else None
    write_audit('attendance_deleted', f'Deleted attendance record {record_id}.', student_username)
    AttendanceRequest.query.filter_by(attendance_id=record.id).delete(synchronize_session=False)
    db.session.delete(record)
    db.session.commit()
    flash('Attendance record deleted.')
    return redirect(url_for('records', student=student_username))

@app.route('/records')
@login_required
def records():
    student_username = request.args.get('student')
    if current_user.role == 'supervisor' and student_username:
        student = User.query.filter_by(username=student_username, role='student', supervisor_id=current_user.id).first()
        if not student:
            flash('Student not found.')
            return redirect(url_for('supervisor_dashboard'))
        target_user = student
    else:
        target_user = current_user

    start_raw = request.args.get('start_date', '').strip()
    end_raw = request.args.get('end_date', '').strip()
    status_filter = request.args.get('status', '').strip().lower()
    try:
        query = apply_report_filters(
            Attendance.query.filter_by(user_id=target_user.id),
            start_raw,
            end_raw,
            status_filter,
        )
    except ValueError as exc:
        flash(f'Invalid report filter: {exc}', 'warning')
        return redirect(url_for('records', student=student_username) if student_username else url_for('records'))
    try:
        page_number = max(1, int(request.args.get('page', '1')))
    except ValueError:
        page_number = 1
    today = local_today()
    metrics = aggregate_attendance(query, today=today)
    pagination = query.order_by(Attendance.work_date.desc(), Attendance.id.desc()).paginate(
        page=page_number,
        per_page=30,
        error_out=False,
    )
    records = pagination.items

    absent_days = metrics['absent']
    late_days = metrics['late']
    total_hours = metrics['hours']
    attendance_percent = int((metrics['present'] / metrics['count']) * 100) if metrics['count'] else 0
    record_details = {
        rec.id: {
            'status': attendance_status(rec),
            'events': attendance_events(rec),
            'scheduled_breaks': scheduled_breaks(rec),
        }
        for rec in records
    }

    return render_template(
        'records.html',
        records=records,
        pagination=pagination,
        student=target_user,
        total_hours=round(total_hours, 2),
        attendance_percent=attendance_percent,
        absent_days=absent_days,
        late_days=late_days,
        today=today,
        start_date=start_raw,
        end_date=end_raw,
        status_filter=status_filter,
        record_details=record_details,
    )

@app.route('/enroll_student', methods=['POST'])
@login_required
def enroll_student():
    if current_user.role != 'supervisor':
        # AJAX vs redirect
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'message': 'Access denied.'}), 403
        flash('Access denied.')
        return redirect(url_for('dashboard'))
    student_username = (request.form.get('student_id') or '').strip()
    course_section = request.form.get('course_section') or None
    start_date_raw = request.form.get('start_date') or None
    notes = request.form.get('notes') or None
    try:
        target_hours = float(request.form.get('target_hours', str(DEFAULT_TARGET_HOURS)))
        if not 0 < target_hours <= 10000:
            raise ValueError
    except ValueError:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'message': 'Target hours must be between 1 and 10000.'}), 400
        flash('Target hours must be between 1 and 10000.')
        return redirect(url_for('supervisor_dashboard'))

    student = User.query.filter_by(username=student_username, role='student').first()
    if not student:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'message': 'Student not found.'}), 404
        flash('Student not found.')
    elif student.supervisor_id and student.supervisor_id != current_user.id:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'message': 'Student is assigned to another supervisor.'}), 409
        flash('Student is assigned to another supervisor.')
    else:
        student.supervisor_id = current_user.id
        enrollment = EnrollmentInfo.query.filter_by(student_id=student.id).first()
        if not enrollment:
            enrollment = EnrollmentInfo(student_id=student.id)
            db.session.add(enrollment)

        enrollment.supervisor_id = current_user.id
        enrollment.course_section = course_section
        enrollment.target_hours = target_hours
        if start_date_raw:
            try:
                enrollment.start_date = datetime.strptime(start_date_raw, '%Y-%m-%d').date()
            except ValueError:
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({'success': False, 'message': 'Invalid start date.'}), 400
                flash('Invalid start date.')
                return redirect(url_for('supervisor_dashboard'))
        else:
            enrollment.start_date = None
        enrollment.notes = notes

        write_audit('student_assigned', 'Assigned student and updated internship details.', student.username)
        db.session.commit()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'message': f'Student {student.username} enrolled successfully.'})
        flash(f'Student {student.username} enrolled successfully.')
    return redirect(url_for('supervisor_dashboard'))

@app.route('/add_student', methods=['POST'])
@login_required
def add_student():
    if current_user.role != 'sip_center_supervisor':
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'message': 'Access denied.'}), 403
        flash('Access denied.')
        return redirect(url_for('dashboard'))
    student_username = (request.form.get('student_username') or '').strip()
    course_section = request.form.get('course_section') or None
    start_date_raw = request.form.get('start_date') or None
    notes = request.form.get('notes') or None
    try:
        target_hours = float(request.form.get('target_hours', str(DEFAULT_TARGET_HOURS)))
        if not 0 < target_hours <= 10000:
            raise ValueError
    except ValueError:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'message': 'Target hours must be between 1 and 10000.'}), 400
        flash('Target hours must be between 1 and 10000.')
        return redirect(url_for('sip_center_supervisor_dashboard'))

    student = User.query.filter_by(username=student_username, role='student').first()
    if not student:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'message': 'Student not found.'}), 404
        flash('Student not found.')
        return redirect(url_for('sip_center_supervisor_dashboard'))
    if MonitoredStudent.query.filter_by(student_id=student.id).first():
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'message': 'Student already monitored.'}), 200
        flash('Student already monitored.')
        return redirect(url_for('sip_center_supervisor_dashboard'))
    db.session.add(MonitoredStudent(student_id=student.id))

    enrollment = EnrollmentInfo.query.filter_by(student_id=student.id).first()
    if not enrollment:
        enrollment = EnrollmentInfo(student_id=student.id)
        db.session.add(enrollment)
    enrollment.supervisor_id = student.supervisor_id
    enrollment.course_section = course_section
    enrollment.target_hours = target_hours
    if start_date_raw:
        try:
            enrollment.start_date = datetime.strptime(start_date_raw, '%Y-%m-%d').date()
        except ValueError:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'message': 'Invalid start date.'}), 400
            flash('Invalid start date.')
            return redirect(url_for('sip_center_supervisor_dashboard'))
    else:
        enrollment.start_date = None
    enrollment.notes = notes

    write_audit('student_monitored', 'Added student to center monitoring.', student.username)
    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'message': f'Student {student.username} enrolled for monitoring.'})
    flash('Student enrolled for monitoring.')
    return redirect(url_for('sip_center_supervisor_dashboard'))

@app.route('/monitored_student/<int:student_id>/delete', methods=['POST'])
@login_required
def delete_monitored_student(student_id):
    if current_user.role != 'sip_center_supervisor':
        flash('Access denied.')
        return redirect(url_for('dashboard'))
    monitored = MonitoredStudent.query.filter_by(student_id=student_id).first()
    if not monitored:
        flash('Student is not being monitored.')
        return redirect(url_for('sip_center_supervisor_dashboard'))
    db.session.delete(monitored)
    db.session.commit()
    flash('Student removed from monitoring.')
    return redirect(url_for('sip_center_supervisor_dashboard'))

@app.route('/sip_center_supervisor_dashboard')
@login_required
def sip_center_supervisor_dashboard():
    if current_user.role != 'sip_center_supervisor':
        flash('Access denied.')
        return redirect(url_for('dashboard'))
    search = request.args.get('search', '').strip()[:150]
    total_supervisors = User.query.filter_by(role='supervisor', account_active=True).count()
    monitored_query = (
        User.query.join(MonitoredStudent, MonitoredStudent.student_id == User.id)
        .filter(User.role == 'student', User.account_active.is_(True))
    )
    if search:
        matching_supervisors = db.session.query(User.id).filter(
            User.role == 'supervisor',
            User.username.ilike(f'%{search}%'),
        )
        monitored_query = monitored_query.filter(or_(
            User.username.ilike(f'%{search}%'),
            User.supervisor_id.in_(matching_supervisors),
        ))
    try:
        page_number = max(1, int(request.args.get('page', '1')))
    except ValueError:
        page_number = 1
    pagination = monitored_query.order_by(User.username).paginate(
        page=page_number,
        per_page=25,
        error_out=False,
    )
    monitored_students = pagination.items
    total_students = MonitoredStudent.query.join(
        User, MonitoredStudent.student_id == User.id,
    ).filter(User.role == 'student', User.account_active.is_(True)).count()
    complete_approved = and_(
        Attendance.approval_status == 'approved',
        Attendance.time_in.is_not(None),
        Attendance.time_out.is_not(None),
    )
    all_monitored = select(MonitoredStudent.student_id)
    total_hours_sum = db.session.query(
        db.func.coalesce(
            db.func.sum(case((complete_approved, attendance_net_hours_expression()), else_=0)),
            0,
        )
    ).filter(
        Attendance.user_id.in_(all_monitored)
    ).scalar()
    page_student_ids = [student.id for student in monitored_students]
    page_metrics = {}
    if page_student_ids:
        page_metrics = {
            student_id: (float(hours or 0), int(approved_count or 0), int(present_count or 0))
            for student_id, hours, approved_count, present_count in db.session.query(
                Attendance.user_id,
                db.func.coalesce(
                    db.func.sum(case((complete_approved, attendance_net_hours_expression()), else_=0)),
                    0,
                ),
                db.func.count(Attendance.id),
                db.func.sum(case((complete_approved, 1), else_=0)),
            ).filter(
                Attendance.user_id.in_(page_student_ids)
            ).group_by(Attendance.user_id).all()
        }
    page_supervisor_ids = {student.supervisor_id for student in monitored_students if student.supervisor_id}
    supervisors_by_id = {
        supervisor.id: supervisor.username
        for supervisor in User.query.filter(User.id.in_(page_supervisor_ids)).all()
    } if page_supervisor_ids else {}
    page_enrollments = {
        enrollment.student_id: enrollment.target_hours
        for enrollment in EnrollmentInfo.query.filter(
            EnrollmentInfo.student_id.in_(page_student_ids)
        ).all()
    } if page_student_ids else {}
    student_rows = []
    for student in monitored_students:
        total_hours, total_records, present_records = page_metrics.get(student.id, (0.0, 0, 0))
        target_hours = page_enrollments.get(student.id) or DEFAULT_TARGET_HOURS
        student_rows.append({
            'id': student.id,
            'username': student.username,
            'supervisor': supervisors_by_id.get(student.supervisor_id),
            'total_hours': round(total_hours, 2),
            'percent': int((present_records / total_records) * 100) if total_records else 0,
            'target_hours': target_hours,
            'progress_percent': min(100, round(total_hours / target_hours * 100, 1)) if target_hours else 0,
        })
    avg_hours = round(total_hours_sum / total_students, 2) if total_students > 0 else 0
    return render_template(
        'sip_center_supervisor_dashboard.html',
        student_rows=student_rows,
        total_students=total_students,
        total_supervisors=total_supervisors,
        avg_hours=avg_hours,
        pagination=pagination,
        search=search,
    )
 
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        ensure_break_columns()
        ensure_feature_columns()
        ensure_default_policy()
    app.run(
        host=os.environ.get('FLASK_HOST', '127.0.0.1'),
        port=int(os.environ.get('FLASK_PORT', '5000')),
        debug=os.environ.get('FLASK_DEBUG', '').lower() == 'true',
    )
