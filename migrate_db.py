"""Initialize the database and apply the small SQLite compatibility migration."""
from werkzeug.security import generate_password_hash

from app import User, app, db, ensure_break_columns

with app.app_context():
    ensure_break_columns()
    db.create_all()
    for user in User.query.all():
        if not user.password.startswith(('pbkdf2:', 'scrypt:')):
            user.password = generate_password_hash(user.password)
    db.session.commit()
    print('Database migration completed.')
