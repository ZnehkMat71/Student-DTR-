"""Initialize the database and apply the small SQLite compatibility migration."""
import os

from werkzeug.security import generate_password_hash

from app import User, app, db, ensure_break_columns, ensure_feature_columns, ensure_default_policy


def ensure_admin_user():
    default_username = 'admin'
    default_password = 'admin'
    admin_username = os.environ.get('ADMIN_USERNAME', '').strip() or default_username
    admin_password = os.environ.get('ADMIN_PASSWORD', '') or default_password

    if len(admin_username) < 3 or len(admin_username) > 150:
        raise ValueError('ADMIN_USERNAME must be 3-150 characters.')
    if len(admin_password) < 8 and (admin_username, admin_password) != (default_username, default_password):
        raise ValueError('ADMIN_PASSWORD must be at least 8 characters.')

    admin = User.query.filter_by(username=admin_username).first()
    if admin:
        admin.role = 'admin'
        admin.supervisor_id = None
        admin.password = generate_password_hash(admin_password)
    else:
        db.session.add(User(
            username=admin_username,
            password=generate_password_hash(admin_password),
            role='admin',
        ))
    print(f'Admin account ensured: {admin_username}')
    return admin_username, admin_password


with app.app_context():
    db.create_all()
    ensure_break_columns()
    ensure_feature_columns()
    ensure_default_policy()
    for user in User.query.all():
        if not user.password.startswith(('pbkdf2:', 'scrypt:')):
            user.password = generate_password_hash(user.password)
    ensure_admin_user()
    db.session.commit()
    print('Database migration completed.')
