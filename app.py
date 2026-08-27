from flask import Flask, render_template, redirect, url_for, request, session, flash, Response, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from datetime import datetime, timedelta, date
import csv
import io
import os
import secrets
from collections import defaultdict
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy import inspect, text

LUNCH_BREAK_MAX = timedelta(hours=1)
COFFEE_BREAK_MAX = timedelta(minutes=30)
LUNCH_WINDOW_START = 12
LUNCH_WINDOW_END = 13
COFFEE_WINDOW_START = 15
COFFEE_WINDOW_END = 15.5

app = Flask(__name__)
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

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    receiver_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    content = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    time_in = db.Column(db.DateTime, index=True)
    time_out = db.Column(db.DateTime)
    lunch_start = db.Column(db.DateTime, nullable=True)
    lunch_end = db.Column(db.DateTime, nullable=True)
    coffee_start = db.Column(db.DateTime, nullable=True)
    coffee_end = db.Column(db.DateTime, nullable=True)

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

@app.context_processor
def inject_today():
    """Expose today's date in all templates (e.g. records status vs. clock-in date)."""
    return {'today': date.today(), 'csrf_token': csrf_token}

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
    """Add break columns to existing SQLite attendance table if missing."""
    if db.engine.dialect.name != 'sqlite':
        return
    insp = inspect(db.engine)
    if not insp.has_table('attendance'):
        return
    existing = {c['name'] for c in insp.get_columns('attendance')}
    alters = []
    for col, ddl in (
        ('lunch_start', 'DATETIME'),
        ('lunch_end', 'DATETIME'),
        ('coffee_start', 'DATETIME'),
        ('coffee_end', 'DATETIME'),
    ):
        if col not in existing:
            alters.append(f'ALTER TABLE attendance ADD COLUMN {col} {ddl}')
    if not alters:
        return
    with db.engine.begin() as conn:
        for stmt in alters:
            conn.execute(text(stmt))

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

def flash_if_overbreak(label, duration, allowed):
    """Flash a warning when break duration exceeds allowance. Returns True if overbreak."""
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
    if attendance.lunch_start and not attendance.lunch_end:
        attendance.lunch_end = end_time
        flash_if_overbreak('Lunch break', end_time - attendance.lunch_start, LUNCH_BREAK_MAX)
    if attendance.coffee_start and not attendance.coffee_end:
        attendance.coffee_end = end_time
        flash_if_overbreak('Coffee break', end_time - attendance.coffee_start, COFFEE_BREAK_MAX)

def format_break_range(start, end):
    if not start:
        return ''
    if end:
        return f"{start.strftime('%Y-%m-%d %H:%M')}–{end.strftime('%H:%M')}"
    return f"{start.strftime('%Y-%m-%d %H:%M')} (in progress)"

def format_duration(start, end=None):
    if not start:
        return ''
    elapsed = (end or datetime.now()) - start
    minutes = max(0, int(elapsed.total_seconds() // 60))
    return f'{minutes // 60}h {minutes % 60:02d}m'

def attendance_status(rec):
    if not rec.time_in:
        return 'No clock-in', 'neutral'
    now = rec.time_out or datetime.now()
    lunch_duration = (rec.lunch_end or now) - rec.lunch_start if rec.lunch_start else timedelta()
    coffee_duration = (rec.coffee_end or now) - rec.coffee_start if rec.coffee_start else timedelta()
    overbreak = lunch_duration > LUNCH_BREAK_MAX or coffee_duration > COFFEE_BREAK_MAX
    if overbreak:
        return 'Overbreak', 'warning'
    if not rec.time_out:
        return ('Incomplete' if rec.time_in.date() < date.today() else 'Active'), 'active'
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
    window_start = rec.time_in.replace(hour=int(start_hour), minute=0, second=0, microsecond=0)
    window_end = rec.time_in.replace(
        hour=int(end_hour),
        minute=30 if end_hour % 1 else 0,
        second=0,
        microsecond=0,
    )
    session_end = rec.time_out or datetime.now()
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
    return [
        scheduled_break_status(rec, LUNCH_WINDOW_START, LUNCH_WINDOW_END, 'Lunch window'),
        scheduled_break_status(rec, COFFEE_WINDOW_START, COFFEE_WINDOW_END, 'Coffee window'),
    ]

@app.template_filter('net_hours')
def template_net_hours(rec):
    return round(net_work_hours(rec), 2)

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

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    supervisors = User.query.filter_by(role='supervisor').all()
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        role = request.form.get('role', 'student')
        supervisor_id = request.form.get('supervisor_id') or None
        if len(username) < 3 or len(username) > 150 or len(password) < 8:
            flash('Username must be 3-150 characters and password must be at least 8 characters.')
            return redirect(url_for('register'))
        if role not in {'student', 'supervisor', 'sip_center_supervisor'}:
            flash('Selected role is invalid.')
            return redirect(url_for('register'))
        if role != 'student':
            supervisor_id = None
        elif supervisor_id:
            supervisor = User.query.filter_by(id=supervisor_id, role='supervisor').first()
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
        elif not password_matches(user.password, password):
            flash('Incorrect password.')
        else:
            if not user.password.startswith(('pbkdf2:', 'scrypt:')):
                user.password = generate_password_hash(password)
                db.session.commit()
            login_user(user)
            return redirect(url_for('dashboard'))
    return render_template('login.html')

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
                    db.session.commit()
                    flash('Username updated.')
        if 'password' in request.form and request.form['password']:
            if len(request.form['password']) < 8:
                flash('Password must be at least 8 characters.')
                return redirect(url_for('dashboard'))
            current_user.password = generate_password_hash(request.form['password'])
            db.session.commit()
            flash('Password updated.')
        if 'message' in request.form:
            # Save message to a simple Message table
            msg = request.form['message']
            if msg:
                new_msg = Message(sender_id=current_user.id, receiver_id=current_user.supervisor_id, content=msg)
                db.session.add(new_msg)
                db.session.commit()
                flash('Message sent to supervisor.')
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
    attendance = Attendance.query.filter_by(user_id=current_user.id).order_by(Attendance.id.desc()).first()
    records = Attendance.query.filter_by(user_id=current_user.id).order_by(Attendance.id.desc()).all()
    # Today limits and button states
    start_of_today = datetime.combine(date.today(), datetime.min.time())
    start_of_tomorrow = start_of_today + timedelta(days=1)
    today_record = (
        Attendance.query
        .filter(Attendance.user_id == current_user.id, Attendance.time_in >= start_of_today, Attendance.time_in < start_of_tomorrow)
        .order_by(Attendance.id.desc())
        .first()
    )
    can_time_in = today_record is None
    can_time_out = bool(today_record and today_record.time_in and not today_record.time_out)
    today_time_in_str = today_record.time_in.strftime('%I:%M %p') if today_record and today_record.time_in else None
    today_time_out_str = today_record.time_out.strftime('%I:%M %p') if today_record and today_record.time_out else None
    # Calculate total hours worked (gross minus completed breaks)
    total_hours = 0
    for rec in records:
        if rec.time_in and rec.time_out:
            total_hours += net_work_hours(rec)
    supervisor = None
    if current_user.supervisor_id:
        supervisor = db.session.get(User, current_user.supervisor_id)
    # Weekly attendance (last 7 days)
    today = date.today()
    week_days = [(today - timedelta(days=i)) for i in range(6, -1, -1)]
    week_attendance = {}
    for d in week_days:
        found = None
        for rec in records:
            if rec.time_in and rec.time_in.date() == d:
                found = rec
                break
        week_attendance[d] = found
    # Status indicator
    status = 'clocked_out'
    if attendance and attendance.time_in and not attendance.time_out:
        status = 'clocked_in'
    # Attendance streaks
    streak = 0
    for d in reversed(week_days):
        rec = week_attendance[d]
        if rec and rec.time_in and rec.time_out:
            streak += 1
        else:
            break
    # Attendance chart data (hours per day for last 7 days)
    chart_labels = [d.strftime('%a') for d in week_days]
    chart_data = []
    for d in week_days:
        rec = week_attendance[d]
        if rec and rec.time_in and rec.time_out:
            chart_data.append(round(net_work_hours(rec), 2))
        else:
            chart_data.append(0)
    # Notifications
    notifications = []
    if not attendance or (attendance and attendance.time_out):
        notifications.append('You are currently clocked out. Don\'t forget to clock in!')
    messages = Message.query.filter_by(sender_id=current_user.id).order_by(Message.timestamp.desc()).all()
    photo_url = url_for('profile_photo', filename=current_user.photo) if current_user.photo else f'https://ui-avatars.com/api/?name={current_user.username}&background=0D8ABC&color=fff&size=64'
    on_lunch_break = bool(today_record and today_record.lunch_start and not today_record.lunch_end)
    on_coffee_break = bool(today_record and today_record.coffee_start and not today_record.coffee_end)
    can_start_lunch = bool(can_time_out and not on_lunch_break and not on_coffee_break and today_record and not today_record.lunch_end)
    can_start_coffee = bool(can_time_out and not on_lunch_break and not on_coffee_break and today_record and not today_record.coffee_end)
    can_end_lunch = on_lunch_break
    can_end_coffee = on_coffee_break
    if can_time_out:
        if on_lunch_break:
            notifications.insert(0, 'You are on lunch break — up to 1 hour allowed.')
        elif on_coffee_break:
            notifications.insert(0, 'You are on coffee break — up to 30 minutes allowed.')
    return render_template(
        'dashboard.html',
        attendance=attendance,
        records=records,
        total_hours=total_hours,
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
        can_start_coffee=can_start_coffee,
        can_end_lunch=can_end_lunch,
        can_end_coffee=can_end_coffee,
        scheduled_breaks=scheduled_breaks(today_record) if today_record else [],
    )
@app.route('/export_csv')
@login_required
def export_csv():
    records = Attendance.query.filter_by(user_id=current_user.id).order_by(Attendance.id.desc()).all()
    def generate():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Time In', 'Time Out', 'Lunch Break', 'Coffee Break', 'Net Hours (h)'])
        for rec in records:
            time_in = rec.time_in.strftime('%Y-%m-%d %H:%M') if rec.time_in else ''
            time_out = rec.time_out.strftime('%Y-%m-%d %H:%M') if rec.time_out else ''
            lunch = format_break_range(rec.lunch_start, rec.lunch_end)
            coffee = format_break_range(rec.coffee_start, rec.coffee_end)
            net_h = f"{net_work_hours(rec):.2f}" if rec.time_in and rec.time_out else ''
            writer.writerow([csv_cell(value) for value in [time_in, time_out, lunch, coffee, net_h]])
        return output.getvalue()
    return Response(generate(), mimetype='text/csv', headers={'Content-Disposition': 'attachment;filename=attendance.csv'})

# Supervisor dashboard route
@app.route('/supervisor_dashboard', methods=['GET', 'POST'])
@login_required
def supervisor_dashboard():
    if current_user.role != 'supervisor':
        flash('Access denied.')
        return redirect(url_for('dashboard'))
    # Filtering/search
    search = request.args.get('search', '').strip()
    sort = request.args.get('sort', 'desc')
    supervisor_filter = request.args.get('supervisor', '')
    # Get all students grouped by supervisor
    supervisors = User.query.filter_by(role='supervisor').all()
    students = User.query.filter_by(role='student', supervisor_id=current_user.id).all()
    grouped_students = {}
    for sup in supervisors:
        grouped_students[sup.username] = [s for s in students if str(s.supervisor_id) == str(sup.id)]
    # Filter students by name or supervisor
    filtered_students = students
    if search:
        filtered_students = [s for s in students if search.lower() in s.username.lower()]
    if supervisor_filter and supervisor_filter.isdigit():
        filtered_students = [s for s in filtered_students if str(s.supervisor_id) == supervisor_filter]
    # Attendance records (with search/sort)
    all_records = db.session.query(Attendance, User).join(User, Attendance.user_id == User.id).filter(User.supervisor_id == current_user.id)
    if search:
        all_records = all_records.filter(User.username.ilike(f'%{search}%'))
    if supervisor_filter and supervisor_filter.isdigit():
        all_records = all_records.filter(User.supervisor_id == int(supervisor_filter))
    if sort == 'asc':
        all_records = all_records.order_by(Attendance.time_in.asc())
    else:
        all_records = all_records.order_by(Attendance.time_in.desc())
    all_records = all_records.all()
    # Prepare student_data for table and time records
    student_data = []
    time_records = []
    records_by_student = defaultdict(list)
    for record, student_id in (
        db.session.query(Attendance, Attendance.user_id)
        .filter(Attendance.user_id.in_([student.id for student in students]))
        .order_by(Attendance.id.desc())
        .all()
    ):
        records_by_student[student_id].append(record)
    for s in filtered_students:
        records = records_by_student[s.id]
        total_hours = 0
        present_days = 0
        for rec in records:
            if rec.time_in and rec.time_out:
                total_hours += net_work_hours(rec)
                present_days += 1
        percent = 0
        if records:
            percent = int((present_days / len(records)) * 100)
        student_data.append({
            'id': s.id,
            'username': s.username,
            'total_hours': round(total_hours, 2),
            'percent': percent
        })
        # Only show the latest record for each student
        latest_record = records[0] if records else None
        time_records.append({
            'username': s.username,
            'record': latest_record
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
            db.session.commit()
            flash('Record updated.')
        return redirect(url_for('supervisor_dashboard'))
    users = User.query.all()
    return render_template('supervisor_dashboard.html', student_data=student_data, time_records=time_records, search=search, users=users)
# Export all records to CSV (supervisor)
@app.route('/export_all_csv')
@login_required
def export_all_csv():
    if current_user.role != 'supervisor':
        flash('Access denied.')
        return redirect(url_for('dashboard'))
    all_records = (
        db.session.query(Attendance, User)
        .join(User, Attendance.user_id == User.id)
        .filter(User.supervisor_id == current_user.id)
        .order_by(Attendance.id.desc())
        .all()
    )
    def generate():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Student Username', 'Time In', 'Time Out', 'Lunch Break', 'Coffee Break', 'Net Hours (h)'])
        for attendance, user in all_records:
            time_in = attendance.time_in.strftime('%Y-%m-%d %H:%M') if attendance.time_in else ''
            time_out = attendance.time_out.strftime('%Y-%m-%d %H:%M') if attendance.time_out else ''
            lunch = format_break_range(attendance.lunch_start, attendance.lunch_end)
            coffee = format_break_range(attendance.coffee_start, attendance.coffee_end)
            net_h = f"{net_work_hours(attendance):.2f}" if attendance.time_in and attendance.time_out else ''
            writer.writerow([csv_cell(value) for value in [user.username, time_in, time_out, lunch, coffee, net_h]])
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
    # Supervisors: return only students not yet assigned to any supervisor
    if current_user.role == 'supervisor':
        students = User.query.filter_by(role='student', supervisor_id=None).all()
        return jsonify([{'username': s.username} for s in students])
    # SIP Center Supervisor: return all students
    if current_user.role == 'sip_center_supervisor':
        students = User.query.filter_by(role='student').all()
        return jsonify([{'username': s.username} for s in students])
    return Response('Forbidden', status=403)

@app.route('/time_in', methods=['POST'])
@login_required
def time_in():
    # Enforce one time-in per day
    now = datetime.now()
    start_of_today = datetime.combine(date.today(), datetime.min.time())
    start_of_tomorrow = start_of_today + timedelta(days=1)
    existing_today = Attendance.query.filter(
        Attendance.user_id == current_user.id,
        Attendance.time_in >= start_of_today,
        Attendance.time_in < start_of_tomorrow
    ).first()
    if existing_today:
        flash('You have already timed in today.')
        return redirect(url_for('dashboard'))
    attendance = Attendance(user_id=current_user.id, time_in=now)
    db.session.add(attendance)
    db.session.commit()
    flash('Timed in successfully.')
    return redirect(url_for('dashboard'))

@app.route('/time_out', methods=['POST'])
@login_required
def time_out():
    # Allow time-out only for today's open record
    start_of_today = datetime.combine(date.today(), datetime.min.time())
    start_of_tomorrow = start_of_today + timedelta(days=1)
    attendance = (
        Attendance.query
        .filter(Attendance.user_id == current_user.id, Attendance.time_in >= start_of_today, Attendance.time_in < start_of_tomorrow)
        .order_by(Attendance.id.desc())
        .first()
    )
    if attendance and attendance.time_in and not attendance.time_out:
        end = datetime.now()
        finalize_open_breaks(attendance, end)
        attendance.time_out = end
        db.session.commit()
        flash('Timed out successfully.')
    else:
        flash('No open time-in to time out today.')
    return redirect(url_for('dashboard'))

def _today_open_attendance_for_user(user_id):
    start_of_today = datetime.combine(date.today(), datetime.min.time())
    start_of_tomorrow = start_of_today + timedelta(days=1)
    return (
        Attendance.query
        .filter(
            Attendance.user_id == user_id,
            Attendance.time_in >= start_of_today,
            Attendance.time_in < start_of_tomorrow,
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
    if break_type not in ('lunch', 'coffee'):
        flash('Invalid break type.')
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
        rec.lunch_start = datetime.now()
        flash('Lunch break started. Allowed time: 1 hour.')
    else:
        if rec.coffee_end:
            flash('Coffee break already taken today.')
            return redirect(url_for('dashboard'))
        rec.coffee_start = datetime.now()
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
    if break_type not in ('lunch', 'coffee'):
        flash('Invalid break type.')
        return redirect(url_for('dashboard'))
    rec = _today_open_attendance_for_user(current_user.id)
    if not rec or not rec.time_in or rec.time_out:
        flash('No active attendance found.')
        return redirect(url_for('dashboard'))
    end = datetime.now()
    if break_type == 'lunch':
        if not rec.lunch_start or rec.lunch_end:
            flash('No active lunch break to end.')
            return redirect(url_for('dashboard'))
        rec.lunch_end = end
        if not flash_if_overbreak('Lunch break', end - rec.lunch_start, LUNCH_BREAK_MAX):
            flash('Lunch break ended.')
    else:
        if not rec.coffee_start or rec.coffee_end:
            flash('No active coffee break to end.')
            return redirect(url_for('dashboard'))
        rec.coffee_end = end
        if not flash_if_overbreak('Coffee break', end - rec.coffee_start, COFFEE_BREAK_MAX):
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

    records = Attendance.query.filter_by(user_id=target_user.id).order_by(Attendance.id.desc()).all()

    # Compute analytics to match table logic
    total_records = len(records)
    present_records = [rec for rec in records if rec.time_in and rec.time_out]
    today = date.today()

    def record_counts_as_absent(rec):
        if not rec.time_in:
            return True
        if rec.time_out:
            return False
        # Same calendar day as clock-in: still in progress, not absent
        if rec.time_in.date() >= today:
            return False
        # Past day with no clock-out counts as absent
        return True

    absent_days = sum(1 for rec in records if record_counts_as_absent(rec))
    late_days = len([rec for rec in present_records if rec.time_in.hour > 9])
    total_hours = sum(net_work_hours(rec) for rec in present_records)
    attendance_percent = int((len(present_records) / total_records) * 100) if total_records else 0
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
        student=target_user,
        total_hours=round(total_hours, 2),
        attendance_percent=attendance_percent,
        absent_days=absent_days,
        late_days=late_days,
        today=today,
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
    students = User.query.filter_by(role='student').all()
    monitored_ids = {m.student_id for m in MonitoredStudent.query.all()}
    monitored_students = [s for s in students if s.id in monitored_ids]
    available_students = [s for s in students if s.id not in monitored_ids]
    supervisors = User.query.filter_by(role='supervisor').all()
    total_students = len(monitored_students)
    total_supervisors = len(supervisors)
    # Calculate avg hours and attendance
    student_rows = []
    total_hours_sum = 0
    records_by_student = defaultdict(list)
    for record, student_id in (
        db.session.query(Attendance, Attendance.user_id)
        .filter(Attendance.user_id.in_([student.id for student in monitored_students]))
        .order_by(Attendance.id.desc())
        .all()
    ):
        records_by_student[student_id].append(record)
    for s in monitored_students:
        records = records_by_student[s.id]
        total_hours = sum(net_work_hours(rec) for rec in records if rec.time_in and rec.time_out)
        percent = int((len([rec for rec in records if rec.time_in and rec.time_out]) / len(records)) * 100) if records else 0
        supervisor_name = None
        if s.supervisor_id:
            sup = db.session.get(User, s.supervisor_id)
            supervisor_name = sup.username if sup else None
        student_rows.append({
            'id': s.id,
            'username': s.username,
            'supervisor': supervisor_name,
            'total_hours': round(total_hours, 2),
            'percent': percent
        })
        total_hours_sum += total_hours
    avg_hours = round(total_hours_sum / total_students, 2) if total_students > 0 else 0
    # Pass all students for enrollment
    return render_template(
        'sip_center_supervisor_dashboard.html',
        student_rows=student_rows,
        total_students=total_students,
        total_supervisors=total_supervisors,
        avg_hours=avg_hours,
        students=students,
        supervisors=supervisors,
        available_students=available_students
    )
 
if __name__ == "__main__":
    with app.app_context():
        ensure_break_columns()
        db.create_all()
    app.run(
        host=os.environ.get('FLASK_HOST', '127.0.0.1'),
        port=int(os.environ.get('FLASK_PORT', '5000')),
        debug=os.environ.get('FLASK_DEBUG', '').lower() == 'true',
    )
