import os
import re
import tempfile
from datetime import datetime, timedelta

DATABASE_DIR = tempfile.TemporaryDirectory()
DATABASE_PATH = os.path.join(DATABASE_DIR.name, 'test.db').replace('\\', '/')
os.environ['DATABASE_URL'] = f'sqlite:///{DATABASE_PATH}'
os.environ['SECRET_KEY'] = 'test-secret'

from app import Attendance, User, app, db
from werkzeug.security import generate_password_hash


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
