"""Reset the admin login. Run from the project folder in a console:

    python reset_admin.py                    -> username "manucho", password "Manucho990"
    python reset_admin.py myname MyPass123   -> your own username and password

Works with both SQLite and PostgreSQL (whichever the site is set up to use).
Afterwards click Reload on the PythonAnywhere Web tab (this also clears any sign-in lockout).
"""
import sys

from werkzeug.security import generate_password_hash

import app as site  # creates the database if it does not exist yet

username = sys.argv[1] if len(sys.argv) > 1 else "manucho"
password = sys.argv[2] if len(sys.argv) > 2 else "Manucho990"
if len(password) < 8:
    sys.exit("Use a password of at least 8 characters.")

must_change = 1 if len(sys.argv) < 3 else 0
db = site.connect()
try:
    if db.execute("SELECT 1 FROM admin WHERE id = 1").fetchone():
        db.execute("UPDATE admin SET username = ?, password_hash = ?, must_change = ? WHERE id = 1",
                   (username, generate_password_hash(password), must_change))
    else:
        db.execute("INSERT INTO admin (id, username, password_hash, must_change) VALUES (1, ?, ?, ?)",
                   (username, generate_password_hash(password), must_change))
    db.commit()
finally:
    db.close()
print(f"Done. Sign in at /admin/login with username: {username}  password: {password}")
print("(database: " + ("PostgreSQL via DATABASE_URL" if site.USE_PG else site.DB_PATH) + ")")
