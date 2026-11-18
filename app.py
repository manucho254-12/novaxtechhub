"""Novax Tech Hub: portfolio site with a password-protected document library.

Stack: Flask + SQLite (default) or PostgreSQL (set DATABASE_URL). Run `python app.py` for development.
"""
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import quote

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import content

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "instance"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "novax.db")
# PostgreSQL is used when DATABASE_URL is set, e.g. postgresql://user:pass@host:5432/novax.
# Leave it unset to keep using the SQLite file above.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):  # some hosts still hand out the old scheme
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
USE_PG = DATABASE_URL.startswith("postgresql://")
if USE_PG:
    try:
        import psycopg2
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("DATABASE_URL points to PostgreSQL but psycopg2 is missing. "
                         "Run: pip install psycopg2-binary") from exc
MEDIA_DIR = os.path.join(DATA_DIR, "media")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)


def _secret_key():
    """Use SECRET_KEY from the environment, else create and keep one on disk."""
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    path = os.path.join(DATA_DIR, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(secrets.token_hex(32))
    with open(path) as fh:
        return fh.read().strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_secret_key(),
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,  # 25 MB per upload
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
)

ALLOWED = {
    ".pdf": ("PDF", "application/pdf", (b"%PDF",)),
    ".docx": ("DOCX", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", (b"PK\x03\x04",)),
    ".doc": ("DOC", "application/msword", (b"\xd0\xcf\x11\xe0",)),
}

# --------------------------------------------------------------------------- DB
SCHEMA = """
CREATE TABLE IF NOT EXISTS admin (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    must_change INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS media (
    key TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tabs (
    id {PK},
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'custom',
    enabled INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL DEFAULT '',
    button_label TEXT NOT NULL DEFAULT '',
    button_url TEXT NOT NULL DEFAULT '',
    image TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id {PK},
    title TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'Live',
    description TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '',
    contributors TEXT NOT NULL DEFAULT '',
    image TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id {PK},
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    stored_name TEXT NOT NULL,
    original_name TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    password_hash TEXT NOT NULL,
    pw_version INTEGER NOT NULL DEFAULT 1,
    protected INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    views INTEGER NOT NULL DEFAULT 0,
    downloads INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


PK_SQL = "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
# Tables whose integer id comes from the database; PostgreSQL needs RETURNING id to report it.
ID_TABLES = {"tabs", "projects", "documents", "stack_groups", "stack_items", "tab_items", "vault_entries"}
_INSERT_TABLE = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.I)


class PgRow:
    """A PostgreSQL row that behaves like sqlite3.Row: row["name"], row[0], iteration, keys()."""
    __slots__ = ("_names", "_values")

    def __init__(self, names, values):
        self._names, self._values = names, values

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        try:
            return self._values[self._names.index(key)]
        except ValueError:
            raise KeyError(key) from None

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return list(self._names)


class PgCursor:
    def __init__(self, cur, returning_id):
        self._cur = cur
        self.lastrowid = None
        self._names = [d[0] for d in cur.description] if cur.description else []
        if returning_id:
            self.lastrowid = cur.fetchone()[0]
            self._names = []

    def _wrap(self, row):
        return None if row is None else PgRow(self._names, tuple(row))

    def fetchone(self):
        return self._wrap(self._cur.fetchone()) if self._names else None

    def fetchall(self):
        return [self._wrap(r) for r in self._cur.fetchall()] if self._names else []

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def rowcount(self):
        return self._cur.rowcount


class Database:
    """One small interface over SQLite and PostgreSQL, so the rest of the app is written once.

    SQL is written with ? placeholders. For PostgreSQL they are converted to %s here."""

    def __init__(self):
        if USE_PG:
            self.raw = psycopg2.connect(DATABASE_URL)
        else:
            self.raw = sqlite3.connect(DB_PATH, timeout=15)
            self.raw.row_factory = sqlite3.Row
            self.raw.execute("PRAGMA foreign_keys = ON")

    def execute(self, sql, params=None):
        if not USE_PG:
            return self.raw.execute(sql, params or ())
        returning = False
        m = _INSERT_TABLE.match(sql)
        if m and m.group(1).lower() in ID_TABLES and "returning" not in sql.lower():
            sql, returning = sql.rstrip().rstrip(";") + " RETURNING id", True
        cur = self.raw.cursor()
        if params is None:
            cur.execute(sql)  # no parameters: a literal % (LIKE '%x%') must stay as it is
        else:
            cur.execute(sql.replace("%", "%%").replace("?", "%s"), tuple(params))
        return PgCursor(cur, returning)

    def executescript(self, script):
        if not USE_PG:
            self.raw.executescript(script)
            return
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def has_column(self, table, column):
        if USE_PG:
            row = self.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                               "AND table_name = ? AND column_name = ?", (table, column)).fetchone()
            return row is not None
        return column in {r[1] for r in self.raw.execute(f"PRAGMA table_info({table})")}

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


DB_ERRORS = (sqlite3.Error, psycopg2.Error) if USE_PG else (sqlite3.Error,)


def connect():
    return Database()


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        if exc is not None:
            db.rollback()  # PostgreSQL keeps a failed transaction open until it is rolled back
        db.close()


def init_db():
    db = connect()
    if USE_PG:  # several workers can start at once; let one create the tables
        db.execute("SELECT pg_advisory_lock(724101)")
    try:
        _init_db(db)
        db.commit()
    finally:
        if USE_PG:
            db.rollback()
            db.execute("SELECT pg_advisory_unlock(724101)")
            db.commit()
        db.close()


def _init_db(db):
    db.executescript(SCHEMA.replace("{PK}", PK_SQL))
    db.executescript(SCHEMA_EXTRA.replace("{PK}", PK_SQL))
    _ensure_column(db, "documents", "category", "TEXT NOT NULL DEFAULT 'library'")
    _ensure_column(db, "documents", "legal_kind", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, "documents", "protected", "INTEGER NOT NULL DEFAULT 1")
    if _ensure_column(db, "projects", "hosting", "TEXT NOT NULL DEFAULT 'hosted'"):
        db.execute("UPDATE projects SET hosting = 'pending' WHERE domain = '' AND (lower(status) LIKE '%soon%' "
                   "OR lower(status) LIKE '%ready%' OR lower(status) LIKE '%develop%')")
    row = db.execute("SELECT 1 FROM admin WHERE id = 1").fetchone()
    if not row:
        # First-run login. It is set here in the code on purpose and is never read from
        # environment variables or a .env file. Change it later under Admin > Account.
        user, pw, must_change = "manucho", "Manucho990", 1
        db.execute(
            "INSERT INTO admin (id, username, password_hash, must_change) VALUES (1, ?, ?, ?)",
            (user, generate_password_hash(pw), must_change),
        )
    # built-in tabs (add any that are missing, keep admin's choices for existing ones)
    existing = {r[0] for r in db.execute("SELECT slug FROM tabs")}
    pos = (db.execute("SELECT COALESCE(MAX(position),0) FROM tabs").fetchone()[0])
    for slug, title in BUILTIN_TABS:
        if slug not in existing:
            pos += 10
            db.execute("INSERT INTO tabs (slug, title, kind, enabled, position, created_at) VALUES (?,?,?,?,?,?)",
                       (slug, title, "builtin", 1, pos, now_iso()))
    # starter projects, once
    if not db.execute("SELECT 1 FROM meta WHERE key = 'projects_seeded'").fetchone():
        if db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0:
            for n, p in enumerate(content.PROJECTS, start=1):
                domain = p.get("url") if (p.get("url") or "").startswith("http") else ""
                pending = (not domain) and "soon" in p["status"].lower()
                db.execute(
                    "INSERT INTO projects (title, domain, status, hosting, description, position, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (p["title"], domain, p["status"], "pending" if pending else "hosted", p["text"],
                     n * 10, now_iso()))
        db.execute("INSERT INTO meta (key, value) VALUES ('projects_seeded', '1')")
    # stack: editable from the admin, seeded once from content.py
    if not db.execute("SELECT 1 FROM meta WHERE key = 'stack_seeded'").fetchone():
        if db.execute("SELECT COUNT(*) FROM stack_groups").fetchone()[0] == 0:
            for gi, (gname, items) in enumerate(content.STACK, start=1):
                gid = db.execute("INSERT INTO stack_groups (name, position) VALUES (?,?)", (gname, gi * 10)).lastrowid
                for ii, (iname, idesc) in enumerate(items, start=1):
                    db.execute("INSERT INTO stack_items (group_id, name, description, position) VALUES (?,?,?,?)",
                               (gid, iname, idesc, ii * 10))
        for k, v in content.STACK_SETTINGS.items():
            db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
                       ("stack_" + k, v))
        db.execute("INSERT INTO meta (key, value) VALUES ('stack_seeded', '1')")
    _one_time_updates(db)


def _one_time_updates(db):
    """Content updates for sites that already have data. Each runs once and never overwrites later edits."""
    def once(key):
        if db.execute("SELECT 1 FROM meta WHERE key = ?", (key,)).fetchone():
            return False
        db.execute("INSERT INTO meta (key, value) VALUES (?, '1')", (key,))
        return True

    if once("pg_stack_v1"):  # PostgreSQL is now supported, so list it under Database
        grp = db.execute("SELECT id FROM stack_groups WHERE lower(name) = 'database' ORDER BY id").fetchone()
        if grp and not db.execute("SELECT 1 FROM stack_items WHERE group_id = ? AND lower(name) = 'postgresql'",
                                  (grp[0],)).fetchone():
            pos = db.execute("SELECT COALESCE(MAX(position),0) + 10 FROM stack_items WHERE group_id = ?",
                             (grp[0],)).fetchone()[0]
            db.execute("INSERT INTO stack_items (group_id, name, description, position) VALUES (?,?,?,?)",
                       (grp[0], "PostgreSQL", content.POSTGRES_BLURB, pos))
        db.execute("UPDATE stack_items SET description = ? WHERE lower(name) = 'sqlite' AND description = ?",
                   (content.SQLITE_BLURB, content.SQLITE_BLURB_OLD))
        db.execute("UPDATE projects SET description = ? WHERE title = 'Novax Tech Hub website' AND description = ?",
                   (content.SITE_PROJECT_BLURB, content.SITE_PROJECT_BLURB_OLD))


SCHEMA_EXTRA = """
CREATE TABLE IF NOT EXISTS stack_groups (
    id {PK},
    name TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stack_items (
    id {PK},
    group_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tab_items (
    id {PK},
    tab_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    subtitle TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    image TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vault_entries (
    id {PK},
    label TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    secret_enc TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    doc_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

LEGAL_KINDS = [("terms", "Company terms"), ("policies", "Policies"), ("contracts", "Contracts"),
               ("other", "Other legal")]
LEGAL_TTL = 20 * 60  # a legal document stays unlocked for 20 minutes per sign-in
# Terms, policies and other legal files may be left open or protected. These kinds must always have a password.
PASSWORD_REQUIRED_KINDS = {"contracts"}
LEGAL_MIN_PW = 8


def _ensure_column(db, table, column, ddl):
    """Add a column to an existing table. Returns True when it was added."""
    if db.has_column(table, column):
        return False
    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return True


BUILTIN_TABS = [("about", "About"), ("skills", "Skills"), ("services", "Services"), ("stack", "Stack"),
                ("projects", "Projects"), ("docs", "Docs"), ("partnership", "Partnership"), ("contact", "Contact")]
RESERVED_SLUGS = {s for s, _ in BUILTIN_TABS} | {"top", "home", "admin", "docs", "media", "static"}



# ------------------------------------------------------------------- helpers
_attempts = {}


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() or request.remote_addr or "?")


def locked_out(key, limit=6, window=600):
    now = time.time()
    recent = [t for t in _attempts.get(key, []) if now - t < window]
    _attempts[key] = recent
    return len(recent) >= limit


def record_failure(key):
    _attempts.setdefault(key, []).append(time.time())


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@app.template_filter("filesize")
def filesize(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


@app.template_filter("pretty_date")
def pretty_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime("%d %b %Y")
    except (TypeError, ValueError):
        return value or ""


def slugify(text):
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower() or "document"


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def protect_posts():
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
            abort(400, "Your session expired. Go back, refresh the page and try again.")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/docs/"):
        resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    return resp


@app.context_processor
def inject_globals():
    return {"site": content.SITE, "csrf_token": csrf_token,
            "is_admin": bool(session.get("admin")), "now_year": datetime.now(timezone.utc).year,
            "media": media_url, "nav_tabs": enabled_tabs, "legal_groups": legal_groups,
            "tab_items": lambda tab_id: get_db().execute(
                "SELECT * FROM tab_items WHERE tab_id = ? ORDER BY position, id", (tab_id,)).fetchall(),
            "tab_on": lambda slug: bool(get_db().execute(
                "SELECT 1 FROM tabs WHERE slug = ? AND enabled = 1", (slug,)).fetchone()),
            "any_locked_legal": lambda: bool(get_db().execute(
                "SELECT 1 FROM documents WHERE category = 'legal' AND enabled = 1 AND protected = 1").fetchone()),
            "doc_wa": lambda doc: wa_url(doc_share_text(doc)),
            "site_wa": lambda: wa_url(f"{content.SITE['name']}: {content.SITE['tagline']}\n"
                                      + url_for("index", _external=True)),
            "project_wa": lambda p: wa_url(f"{p['title']}: " + (p["description"] or "")[:140] + "\n"
                                           + (ext_url(p["domain"]) if p["domain"] else
                                              url_for("index", _external=True) + "#projects"))}


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def get_doc(doc_id, enabled_only=False):
    row = get_db().execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None or (enabled_only and not row["enabled"]):
        return None
    return row


def is_unlocked(doc):
    if not doc["enabled"]:
        return False
    if not doc["protected"]:  # legal documents such as terms and policies may be left open
        return True
    rec = session.get("unlocked", {}).get(str(doc["id"]))
    version, stamp = (rec[0], rec[1]) if isinstance(rec, list) else (rec, 0)
    if version != doc["pw_version"]:
        return False
    if doc["category"] == "legal":  # legal documents re-lock automatically
        return (time.time() - stamp) < LEGAL_TTL
    return True


def legal_groups():
    """Enabled legal documents grouped for the footer: [(label, [rows])]."""
    rows = get_db().execute(
        "SELECT id, title, legal_kind, protected FROM documents WHERE category = 'legal' AND enabled = 1 "
        "ORDER BY created_at, id").fetchall()
    groups = []
    for key, label in LEGAL_KINDS:
        items = [r for r in rows if r["legal_kind"] == key]
        if items:
            groups.append((label, items))
    return groups


def validate_upload(file):
    """Return (ext, error). Checks extension and file signature."""
    name = file.filename or ""
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED:
        return None, "Only PDF, DOC and DOCX files can be uploaded."
    head = file.stream.read(8)
    file.stream.seek(0)
    if not any(head.startswith(sig) for sig in ALLOWED[ext][2]):
        return None, f"That file does not look like a real {ALLOWED[ext][0]}. Check it and try again."
    return ext, None


IMAGE_TYPES = {
    ".png": (b"\x89PNG",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".webp": (b"RIFF",),
}
MAX_IMAGE = 5 * 1024 * 1024


def media_slots():
    """Every image the admin can upload: (key, label, hint)."""
    slots = [("logo", "Logo", "Shown in the menu, hero and browser tab. Transparent PNG works best."),
             ("profile", "Profile photo", "Shown in the About section. Square photos look best."),
             ("hero", "Home photo", "The large photo at the top of the home page. Landscape or square, at least 780 px wide.")]
    return slots


def media_url(key):
    """URL of an uploaded image for this key, or None."""
    try:
        row = get_db().execute("SELECT filename FROM media WHERE key = ?", (key,)).fetchone()
    except DB_ERRORS:
        get_db().rollback()
        return None
    return url_for("media_file", filename=row["filename"]) if row else None



# ----------------------------------------------------------- content helpers
from markupsafe import Markup, escape
from urllib.parse import urlparse

_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline(text):
    s = str(escape(text))
    s = _BOLD.sub(r"<strong>\1</strong>", s)
    return _LINK.sub(r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)


@app.template_filter("rich")
def rich(text):
    """Tiny, safe formatter: blank line = paragraph, '## ' heading, '- ' bullets, **bold**, [text](https://link)."""
    out = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [ln.rstrip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if all(ln.lstrip().startswith(("- ", "* ")) for ln in lines):
            out.append("<ul>" + "".join(f"<li>{_inline(ln.lstrip()[2:])}</li>" for ln in lines) + "</ul>")
            continue
        if lines[0].startswith("## "):
            out.append(f"<h3>{_inline(lines[0][3:])}</h3>")
            lines = lines[1:]
        if lines:
            out.append("<p>" + "<br>".join(_inline(ln) for ln in lines) + "</p>")
    return Markup("".join(out))


@app.template_filter("ext_url")
def ext_url(value):
    value = (value or "").strip()
    if not value or value.startswith(("/", "#", "mailto:")) or re.match(r"^https?://", value, re.I):
        return value
    return "https://" + value


@app.template_filter("host")
def host(value):
    value = (value or "").strip()
    parsed = urlparse(value if "//" in value else "//" + value)
    return (parsed.netloc or value).replace("www.", "", 1)


@app.template_filter("contrib_list")
def contrib_list(value):
    return [p.strip() for p in re.split(r"[,\n]", value or "") if p.strip()]


def clean_url(value, allow_mailto=False):
    """Return a safe URL string, '' for empty, or None when invalid."""
    value = (value or "").strip()
    if not value:
        return ""
    if re.search(r"\s", value):
        return None
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value):
        if re.match(r"^https?://", value, re.I) or (allow_mailto and value.lower().startswith("mailto:")):
            return value
        return None
    return value  # bare domain such as example.com


def validate_image(file):
    """Return (ext, error) for an uploaded PNG/JPG/WEBP."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    head = file.stream.read(12)
    file.stream.seek(0)
    ok = ext in IMAGE_TYPES and any(head.startswith(s) for s in IMAGE_TYPES[ext])
    if ext == ".webp":
        ok = ok and head[8:12] == b"WEBP"
    return (ext, None) if ok else (None, "Images must be PNG, JPG or WEBP files.")


def save_image(file, prefix):
    """Validate and store an image in the media folder. Returns (filename, error)."""
    ext, err = validate_image(file)
    if err:
        return None, err
    name = f"{prefix}-{secrets.token_hex(4)}{ext}"
    path = os.path.join(MEDIA_DIR, name)
    file.save(path)
    if os.path.getsize(path) > MAX_IMAGE:
        os.remove(path)
        return None, "Images must be 5 MB or smaller."
    return name, None


def remove_image(name):
    if name:
        try:
            os.remove(os.path.join(MEDIA_DIR, name))
        except FileNotFoundError:
            pass


def enabled_tabs():
    return get_db().execute("SELECT * FROM tabs WHERE enabled = 1 ORDER BY position, id").fetchall()


MOVABLE = {"tabs": None, "projects": None, "stack_groups": None,
           "stack_items": "group_id", "tab_items": "tab_id"}


def move_row(table, row_id, direction):
    """Swap a row's position with its neighbour (within its group, if it has one). table is internal."""
    assert table in MOVABLE
    db = get_db()
    scope = MOVABLE[table]
    if scope:
        owner = db.execute(f"SELECT {scope} FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if owner is None:
            return
        rows = [r["id"] for r in db.execute(
            f"SELECT id FROM {table} WHERE {scope} = ? ORDER BY position, id", (owner[0],))]
    else:
        rows = [r["id"] for r in db.execute(f"SELECT id FROM {table} ORDER BY position, id")]
    if row_id not in rows:
        return
    i = rows.index(row_id)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(rows):
        rows[i], rows[j] = rows[j], rows[i]
    for n, rid in enumerate(rows, start=1):
        db.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (n * 10, rid))
    db.commit()


def unique_slug(title):
    base = slugify(title)[:40] or "page"
    if base in RESERVED_SLUGS:
        base += "-page"
    slug, n = base, 2
    db = get_db()
    while db.execute("SELECT 1 FROM tabs WHERE slug = ?", (slug,)).fetchone() or slug in RESERVED_SLUGS:
        slug = f"{base}-{n}"
        n += 1
    return slug


def next_position(table, scope_col=None, scope_val=None):
    assert table in MOVABLE
    if scope_col:
        assert scope_col == MOVABLE[table]
        return get_db().execute(f"SELECT COALESCE(MAX(position),0) + 10 FROM {table} WHERE {scope_col} = ?",
                                (scope_val,)).fetchone()[0]
    return get_db().execute(f"SELECT COALESCE(MAX(position),0) + 10 FROM {table}").fetchone()[0]


def generate_password():
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))


# ------------------------------------------------------------ password vault
from cryptography.fernet import Fernet, InvalidToken

VAULT_TTL = 10 * 60  # the Password Manager re-locks 10 minutes after the admin password is entered
_fernet = None


def _vault_fernet():
    """Passwords in the Password Manager are encrypted (Fernet: AES-128-CBC + HMAC).
    The key comes from VAULT_KEY if set, else from instance/vault.key (created on first use)."""
    global _fernet
    if _fernet is None:
        key = os.environ.get("VAULT_KEY", "").strip().encode()
        if not key:
            path = os.path.join(DATA_DIR, "vault.key")
            if not os.path.exists(path):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(Fernet.generate_key())
            with open(path, "rb") as fh:
                key = fh.read().strip()
        _fernet = Fernet(key)
    return _fernet


def vault_encrypt(text):
    return _vault_fernet().encrypt(text.encode()).decode()


def vault_decrypt(token):
    """Return the plain text, or None when the key does not match (for example a lost vault.key)."""
    try:
        return _vault_fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def vault_store_doc(db, doc_id, title, password):
    """Keep an encrypted copy of a document's password so it can be looked up (and re-shared) later."""
    enc, now = vault_encrypt(password), now_iso()
    row = db.execute("SELECT id FROM vault_entries WHERE doc_id = ?", (doc_id,)).fetchone()
    if row:
        db.execute("UPDATE vault_entries SET label = ?, secret_enc = ?, updated_at = ? WHERE id = ?",
                   (title[:140], enc, now, row["id"]))
    else:
        db.execute("INSERT INTO vault_entries (label, account, secret_enc, note, doc_id, created_at, updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", (title[:140], "", enc, "", doc_id, now, now))


def vault_unlocked():
    return bool(session.get("admin")) and session.get("vault_until", 0) > time.time()


def wa_url(text, number=""):
    """WhatsApp share link. Without a number WhatsApp lets the sender choose the contact."""
    digits = re.sub(r"\D", "", number or "")
    return f"https://wa.me/{digits}?text=" + quote(text)


def doc_share_text(doc, password=None):
    link = url_for("doc_page", doc_id=doc["id"], _external=True)
    text = f"Hello, here is the document \"{doc['title']}\" from Novax Tech Hub.\nOpen: {link}"
    if password:
        text += f"\nPassword: {password}"
    elif doc["protected"]:
        text += "\nIt is password protected. I will send you the password separately."
    return text


init_db()


@app.route("/media/<path:filename>")
def media_file(filename):
    resp = send_from_directory(MEDIA_DIR, filename)
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


# ------------------------------------------------------------------ public
@app.route("/")
def index():
    db = get_db()
    docs_rows = db.execute(
        "SELECT * FROM documents WHERE enabled = 1 AND category = 'library' "
        "ORDER BY created_at DESC, id DESC").fetchall()
    stack_cfg = {k: (db.execute("SELECT value FROM meta WHERE key = ?", ("stack_" + k,)).fetchone() or [""])[0]
                 for k in ("heading", "intro", "note")}
    stack_groups = []
    for g in db.execute("SELECT * FROM stack_groups ORDER BY position, id").fetchall():
        items = db.execute("SELECT * FROM stack_items WHERE group_id = ? ORDER BY position, id", (g["id"],)).fetchall()
        if items:
            stack_groups.append({"id": g["id"], "name": g["name"], "items": items})
    projects = db.execute("SELECT * FROM projects WHERE enabled = 1 ORDER BY position, id").fetchall()
    live = sum(1 for p in projects if (p["status"] or "").strip().lower() == "live")
    return render_template("index.html", c=content, docs=docs_rows, projects=projects,
                           tabs=enabled_tabs(), live_count=live,
                           stack_cfg=stack_cfg, stack_groups=stack_groups)


@app.route("/docs")
def docs():
    if not get_db().execute("SELECT 1 FROM tabs WHERE slug = 'docs' AND enabled = 1").fetchone():
        return redirect(url_for("index"))
    rows = get_db().execute(
        "SELECT * FROM documents WHERE enabled = 1 AND category = 'library' "
        "ORDER BY created_at DESC, id DESC").fetchall()
    return render_template("docs.html", docs=rows, active="docs")


@app.route("/docs/<int:doc_id>")
def doc_page(doc_id):
    doc = get_doc(doc_id, enabled_only=True)
    if doc is None:
        return render_template("doc_unavailable.html", active="docs"), 404
    if is_unlocked(doc):
        get_db().execute("UPDATE documents SET views = views + 1 WHERE id = ?", (doc_id,))
        get_db().commit()
    return render_template("doc.html", doc=doc, unlocked=is_unlocked(doc), active="docs",
                           type_label=ALLOWED[doc["ext"]][0], is_legal=doc["category"] == "legal",
                           legal_minutes=LEGAL_TTL // 60, share_url=wa_url(doc_share_text(doc)))


def _back_after_fail(doc_id):
    """Return to where the unlock form was submitted from."""
    if request.form.get("from") == "home":
        return redirect(url_for("index", open=doc_id) + "#docs")
    return redirect(url_for("doc_page", doc_id=doc_id))


@app.route("/docs/<int:doc_id>/unlock", methods=["POST"])
def doc_unlock(doc_id):
    doc = get_doc(doc_id, enabled_only=True)
    if doc is None:
        abort(404)
    if not doc["protected"]:
        return redirect(url_for("doc_page", doc_id=doc_id))
    legal = doc["category"] == "legal"
    key = f"doc:{client_ip()}:{doc_id}"
    if locked_out(key, limit=4 if legal else 6, window=900 if legal else 600):
        flash(f"Too many incorrect attempts. Wait {'fifteen' if legal else 'ten'} minutes, "
              "or ask Novax Tech Hub for the right password.", "error")
        return _back_after_fail(doc_id)
    if doc["password_hash"] and check_password_hash(doc["password_hash"], request.form.get("password", "")):
        unlocked = session.get("unlocked", {})
        unlocked[str(doc_id)] = [doc["pw_version"], int(time.time())]
        session["unlocked"] = unlocked
        _attempts.pop(key, None)
        return redirect(url_for("doc_page", doc_id=doc_id))
    record_failure(key)
    flash("That password is not correct. Check it with whoever shared the document.", "error")
    return _back_after_fail(doc_id)


@app.route("/docs/<int:doc_id>/lock", methods=["POST"])
def doc_lock(doc_id):
    unlocked = session.get("unlocked", {})
    unlocked.pop(str(doc_id), None)
    session["unlocked"] = unlocked
    return redirect(url_for("docs"))


@app.route("/docs/<int:doc_id>/file")
def doc_file(doc_id):
    doc = get_doc(doc_id, enabled_only=True)
    if doc is None or not is_unlocked(doc):
        abort(403)
    legal = doc["category"] == "legal"
    download = (not legal) and (request.args.get("download") == "1" or doc["ext"] != ".pdf")
    if download:
        get_db().execute("UPDATE documents SET downloads = downloads + 1 WHERE id = ?", (doc_id,))
        get_db().commit()
    resp = send_from_directory(
        UPLOAD_DIR, doc["stored_name"], mimetype=ALLOWED[doc["ext"]][1],
        as_attachment=download, download_name=f"{slugify(doc['title'])}{doc['ext']}")
    resp.headers["Cache-Control"] = "no-store, private"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


# ------------------------------------------------------------------- admin
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin"):
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        key = f"admin:{client_ip()}"
        if locked_out(key, limit=5):
            flash("Too many failed sign-ins. Try again in ten minutes.", "error")
            return render_template("admin/login.html"), 429
        row = get_db().execute("SELECT * FROM admin WHERE id = 1").fetchone()
        ok = (request.form.get("username", "").strip() == row["username"]
              and check_password_hash(row["password_hash"], request.form.get("password", "")))
        if ok:
            _attempts.pop(key, None)
            csrf = session.get("_csrf")
            session.clear()
            session["_csrf"] = csrf or secrets.token_hex(16)
            session["admin"] = True
            session.permanent = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/admin") else url_for("admin_dashboard"))
        record_failure(key)
        flash("Username or password is incorrect.", "error")
    return render_template("admin/login.html")


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin", None)
    session.pop("vault_until", None)
    return redirect(url_for("index"))


def pop_reveal(category):
    """One-time panel after a password is set, with ready-made share links. The session only remembers
    which document it was; the password itself is read back from the encrypted Password Manager."""
    reveal = session.get("reveal")
    if not reveal or reveal.get("cat", "library") != category:
        return None, None, None
    session.pop("reveal", None)
    db = get_db()
    doc = get_doc(reveal["id"])
    entry = db.execute("SELECT secret_enc FROM vault_entries WHERE doc_id = ?", (reveal["id"],)).fetchone()
    password = vault_decrypt(entry["secret_enc"]) if entry else None
    if doc is None or password is None:
        return None, None, None
    text = doc_share_text(doc, password)
    return {"id": doc["id"], "title": doc["title"], "password": password}, text, wa_url(text)


def docs_admin_url(doc):
    return url_for("admin_legal") if doc["category"] == "legal" else url_for("admin_dashboard")


@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM documents WHERE category = 'library' ORDER BY created_at DESC, id DESC").fetchall()
    stats = db.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(enabled),0) AS live, "
        "COALESCE(SUM(downloads),0) AS downloads, COALESCE(SUM(views),0) AS views "
        "FROM documents WHERE category = 'library'").fetchone()
    admin = db.execute("SELECT * FROM admin WHERE id = 1").fetchone()
    reveal, share_text, share_url = pop_reveal("library")
    return render_template("admin/dashboard.html", docs=rows, stats=stats, admin=admin,
                           reveal=reveal, share_text=share_text, share_url=share_url, admin_active="docs",
                           types=ALLOWED, ext_label=lambda e: ALLOWED[e][0], slots=media_slots())


@app.route("/admin/docs/upload", methods=["POST"])
@admin_required
def admin_upload():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    password = request.form.get("password", "")
    file = request.files.get("file")
    if not title:
        flash("Give the document a title.", "error")
    elif len(password) < 4:
        flash("Set a password of at least 4 characters for this document.", "error")
    elif not file or not file.filename:
        flash("Choose a PDF, DOC or DOCX file to upload.", "error")
    else:
        ext, err = validate_upload(file)
        if err:
            flash(err, "error")
        else:
            stored = f"{uuid.uuid4().hex}{ext}"
            path = os.path.join(UPLOAD_DIR, stored)
            file.save(path)
            now = now_iso()
            cur = get_db().execute(
                "INSERT INTO documents (title, description, stored_name, original_name, ext, size, "
                "password_hash, enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (title[:140], description[:400], stored, os.path.basename(file.filename)[:200], ext,
                 os.path.getsize(path), generate_password_hash(password),
                 1 if request.form.get("enabled") else 0, now, now))
            vault_store_doc(get_db(), cur.lastrowid, title, password)
            get_db().commit()
            session["reveal"] = {"id": cur.lastrowid, "cat": "library"}
            flash(f"“{title}” uploaded.", "ok")
    return redirect(url_for("admin_dashboard") + "#library")


@app.route("/admin/docs/<int:doc_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle(doc_id):
    doc = get_doc(doc_id)
    if doc is None:
        abort(404)
    new_state = 0 if doc["enabled"] else 1
    get_db().execute("UPDATE documents SET enabled = ?, updated_at = ? WHERE id = ?",
                     (new_state, now_iso(), doc_id))
    get_db().commit()
    flash(f"“{doc['title']}” is now {'enabled' if new_state else 'disabled'}.", "ok")
    return redirect(docs_admin_url(doc) + "#library")


@app.route("/admin/docs/<int:doc_id>/edit", methods=["POST"])
@admin_required
def admin_edit(doc_id):
    doc = get_doc(doc_id)
    if doc is None:
        abort(404)
    title = request.form.get("title", "").strip() or doc["title"]
    description = request.form.get("description", "").strip()
    new_pw = request.form.get("password", "")
    legal = doc["category"] == "legal"
    back = docs_admin_url(doc) + "#library"
    db = get_db()
    kind = doc["legal_kind"]
    if legal and request.form.get("legal_kind", "") in dict(LEGAL_KINDS):
        kind = request.form["legal_kind"]
    remove_pw = legal and request.form.get("remove_password") == "1" and bool(doc["protected"])
    need = LEGAL_MIN_PW if legal else 4
    if new_pw and len(new_pw) < need:
        flash(f"A new password needs at least {need} characters.", "error")
        return redirect(back)
    if legal:
        if new_pw and remove_pw:
            flash("Either set a new password or remove the password, not both.", "error")
            return redirect(back)
        will_be_protected = bool(new_pw) or (bool(doc["protected"]) and not remove_pw)
        if kind in PASSWORD_REQUIRED_KINDS and not will_be_protected:
            flash("Contracts must have a password. Enter one to continue.", "error")
            return redirect(back)
    if legal:
        db.execute("UPDATE documents SET legal_kind = ? WHERE id = ?", (kind, doc_id))
    if new_pw:
        db.execute("UPDATE documents SET title=?, description=?, password_hash=?, protected=1, "
                   "pw_version=pw_version+1, updated_at=? WHERE id=?",
                   (title[:140], description[:400], generate_password_hash(new_pw), now_iso(), doc_id))
        vault_store_doc(db, doc_id, title, new_pw)
        session["reveal"] = {"id": doc_id, "cat": doc["category"]}
        flash("Password saved. Anyone who had access must enter the new password.", "ok")
    elif remove_pw:
        db.execute("UPDATE documents SET title=?, description=?, password_hash='', protected=0, "
                   "pw_version=pw_version+1, updated_at=? WHERE id=?",
                   (title[:140], description[:400], now_iso(), doc_id))
        db.execute("DELETE FROM vault_entries WHERE doc_id = ?", (doc_id,))
        flash("Password removed. This document is now open to everyone who has the link.", "ok")
    else:
        db.execute("UPDATE documents SET title=?, description=?, updated_at=? WHERE id=?",
                   (title[:140], description[:400], now_iso(), doc_id))
        if doc["protected"]:  # keep the Password Manager label in step with the title
            db.execute("UPDATE vault_entries SET label = ? WHERE doc_id = ?", (title[:140], doc_id))
        flash("Document details saved.", "ok")
    db.commit()
    return redirect(back)


@app.route("/admin/docs/<int:doc_id>/delete", methods=["POST"])
@admin_required
def admin_delete(doc_id):
    doc = get_doc(doc_id)
    if doc is None:
        abort(404)
    try:
        os.remove(os.path.join(UPLOAD_DIR, doc["stored_name"]))
    except FileNotFoundError:
        pass
    get_db().execute("DELETE FROM vault_entries WHERE doc_id = ?", (doc_id,))
    get_db().execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    get_db().commit()
    flash(f"“{doc['title']}” was deleted.", "ok")
    return redirect(docs_admin_url(doc) + "#library")


@app.route("/admin/media/<key>", methods=["POST"])
@admin_required
def admin_media_upload(key):
    valid = {k: label for k, label, _ in media_slots()}
    if key not in valid:
        abort(404)
    file = request.files.get("image")
    if not file or not file.filename:
        flash("Choose an image first (PNG, JPG or WEBP).", "error")
        return redirect(url_for("admin_dashboard") + "#images")
    ext = os.path.splitext(file.filename)[1].lower()
    head = file.stream.read(12)
    file.stream.seek(0)
    ok = ext in IMAGE_TYPES and any(head.startswith(s) for s in IMAGE_TYPES[ext])
    if ext == ".webp":
        ok = ok and head[8:12] == b"WEBP"
    if not ok:
        flash("That file is not a PNG, JPG or WEBP image.", "error")
        return redirect(url_for("admin_dashboard") + "#images")
    name = f"{key}-{secrets.token_hex(4)}{ext}"
    path = os.path.join(MEDIA_DIR, name)
    file.save(path)
    if os.path.getsize(path) > MAX_IMAGE:
        os.remove(path)
        flash("Images must be 5 MB or smaller.", "error")
        return redirect(url_for("admin_dashboard") + "#images")
    db = get_db()
    old = db.execute("SELECT filename FROM media WHERE key = ?", (key,)).fetchone()
    db.execute("INSERT INTO media (key, filename, updated_at) VALUES (?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET filename=excluded.filename, updated_at=excluded.updated_at",
               (key, name, now_iso()))
    db.commit()
    if old:
        try:
            os.remove(os.path.join(MEDIA_DIR, old["filename"]))
        except FileNotFoundError:
            pass
    flash(f"{valid[key]} updated.", "ok")
    return redirect(url_for("admin_dashboard") + "#images")


@app.route("/admin/media/<key>/remove", methods=["POST"])
@admin_required
def admin_media_remove(key):
    db = get_db()
    row = db.execute("SELECT filename FROM media WHERE key = ?", (key,)).fetchone()
    if row:
        try:
            os.remove(os.path.join(MEDIA_DIR, row["filename"]))
        except FileNotFoundError:
            pass
        db.execute("DELETE FROM media WHERE key = ?", (key,))
        db.commit()
        flash("Image removed.", "ok")
    return redirect(url_for("admin_dashboard") + "#images")


# ------------------------------------------------------------- admin: tabs
STATUS_CHOICES = ["Live", "Developed, awaiting hosting", "Ready to launch", "Launching soon", "In development",
                  "Completed"]


def _back(anchor):
    return redirect(url_for(anchor))


@app.route("/admin/tabs")
@admin_required
def admin_tabs():
    db = get_db()
    rows = db.execute("SELECT * FROM tabs ORDER BY position, id").fetchall()
    counts = {r["tab_id"]: r["n"] for r in db.execute(
        "SELECT tab_id, COUNT(*) AS n FROM tab_items GROUP BY tab_id").fetchall()}
    return render_template("admin/tabs.html", tabs=rows, counts=counts, admin_active="tabs")


@app.route("/admin/tabs/new", methods=["POST"])
@admin_required
def admin_tab_new():
    title = request.form.get("title", "").strip()[:40]
    body = request.form.get("body", "").strip()[:8000]
    label = request.form.get("button_label", "").strip()[:40]
    url = clean_url(request.form.get("button_url", ""), allow_mailto=True)
    if not title:
        flash("Give the new tab a title. It becomes the menu label.", "error")
    elif url is None:
        flash("The button link must start with http:// or https:// (or be a plain domain).", "error")
    else:
        image = ""
        file = request.files.get("image")
        if file and file.filename:
            image, err = save_image(file, "tab")
            if err:
                flash(err, "error")
                return _back("admin_tabs")
        slug = unique_slug(title)
        get_db().execute(
            "INSERT INTO tabs (slug, title, kind, enabled, position, body, button_label, button_url, image, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (slug, title, "custom", 1 if request.form.get("enabled") else 0, next_position("tabs"),
             body, label, url, image or "", now_iso()))
        get_db().commit()
        flash(f"Tab “{title}” added. Use “Items” on it to add members, cards or photos with descriptions.", "ok")
    return _back("admin_tabs")


@app.route("/admin/tabs/<int:tab_id>/toggle", methods=["POST"])
@admin_required
def admin_tab_toggle(tab_id):
    tab = get_db().execute("SELECT * FROM tabs WHERE id = ?", (tab_id,)).fetchone()
    if tab is None:
        abort(404)
    new_state = 0 if tab["enabled"] else 1
    get_db().execute("UPDATE tabs SET enabled = ? WHERE id = ?", (new_state, tab_id))
    get_db().commit()
    flash(f"“{tab['title']}” is now {'visible' if new_state else 'hidden'} for visitors.", "ok")
    return _back("admin_tabs")


@app.route("/admin/tabs/<int:tab_id>/move", methods=["POST"])
@admin_required
def admin_tab_move(tab_id):
    move_row("tabs", tab_id, "up" if request.form.get("dir") == "up" else "down")
    return _back("admin_tabs")


@app.route("/admin/tabs/<int:tab_id>/edit", methods=["POST"])
@admin_required
def admin_tab_edit(tab_id):
    db = get_db()
    tab = db.execute("SELECT * FROM tabs WHERE id = ?", (tab_id,)).fetchone()
    if tab is None:
        abort(404)
    title = request.form.get("title", "").strip()[:40] or tab["title"]
    if tab["kind"] == "builtin":
        db.execute("UPDATE tabs SET title = ? WHERE id = ?", (title, tab_id))
        db.commit()
        flash("Menu label updated.", "ok")
        return _back("admin_tabs")
    body = request.form.get("body", "").strip()[:8000]
    url = clean_url(request.form.get("button_url", ""), allow_mailto=True)
    if url is None:
        flash("The button link must start with http:// or https:// (or be a plain domain).", "error")
        return _back("admin_tabs")
    image = tab["image"]
    file = request.files.get("image")
    if file and file.filename:
        new_image, err = save_image(file, "tab")
        if err:
            flash(err, "error")
            return _back("admin_tabs")
        remove_image(image)
        image = new_image
    elif request.form.get("remove_image"):
        remove_image(image)
        image = ""
    db.execute("UPDATE tabs SET title=?, body=?, button_label=?, button_url=?, image=? WHERE id=?",
               (title, body, request.form.get("button_label", "").strip()[:40], url, image, tab_id))
    db.commit()
    flash("Tab saved.", "ok")
    return _back("admin_tabs")


@app.route("/admin/tabs/<int:tab_id>/delete", methods=["POST"])
@admin_required
def admin_tab_delete(tab_id):
    db = get_db()
    tab = db.execute("SELECT * FROM tabs WHERE id = ?", (tab_id,)).fetchone()
    if tab is None:
        abort(404)
    if tab["kind"] == "builtin":
        flash("Built-in tabs can't be deleted. Hide them instead.", "error")
        return _back("admin_tabs")
    remove_image(tab["image"])
    for item in db.execute("SELECT image FROM tab_items WHERE tab_id = ?", (tab_id,)).fetchall():
        remove_image(item["image"])
    db.execute("DELETE FROM tab_items WHERE tab_id = ?", (tab_id,))
    db.execute("DELETE FROM tabs WHERE id = ?", (tab_id,))
    db.commit()
    flash(f"Tab “{tab['title']}” deleted.", "ok")
    return _back("admin_tabs")


# --------------------------------------------------------- admin: projects
def _project_fields():
    f = request.form
    title = f.get("title", "").strip()[:100]
    hosting = "pending" if f.get("hosting") == "pending" else "hosted"
    domain = "" if hosting == "pending" else clean_url(f.get("domain", ""))
    status = f.get("status", "").strip()
    if hosting == "pending" and status.lower() in ("", "live"):
        status = "Developed, awaiting hosting"
    elif not status:
        status = "Live"
    return {
        "title": title,
        "hosting": hosting,
        "domain": domain,
        "status": status[:40],
        "description": f.get("description", "").strip()[:400],
        "details": f.get("details", "").strip()[:6000],
        "contributors": f.get("contributors", "").strip()[:300],
    }


@app.route("/admin/projects")
@admin_required
def admin_projects():
    rows = get_db().execute("SELECT * FROM projects ORDER BY position, id").fetchall()
    return render_template("admin/projects.html", projects=rows, statuses=STATUS_CHOICES,
                           admin_active="projects")


@app.route("/admin/projects/new", methods=["POST"])
@admin_required
def admin_project_new():
    fields = _project_fields()
    if not fields["title"]:
        flash("Give the project a title.", "error")
    elif fields["domain"] is None:
        flash("Enter the domain as example.com or a full https:// link.", "error")
    elif not fields["description"]:
        flash("Add a short description for the project card.", "error")
    else:
        image = ""
        file = request.files.get("image")
        if file and file.filename:
            image, err = save_image(file, "project")
            if err:
                flash(err, "error")
                return _back("admin_projects")
        get_db().execute(
            "INSERT INTO projects (title, domain, status, hosting, description, details, contributors, image, "
            "enabled, position, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fields["title"], fields["domain"], fields["status"], fields["hosting"], fields["description"],
             fields["details"], fields["contributors"], image or "", 1 if request.form.get("enabled") else 0,
             next_position("projects"), now_iso()))
        get_db().commit()
        flash(f"Project “{fields['title']}” added.", "ok")
    return _back("admin_projects")


def _get_project(project_id):
    row = get_db().execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/admin/projects/<int:project_id>/edit", methods=["POST"])
@admin_required
def admin_project_edit(project_id):
    project = _get_project(project_id)
    fields = _project_fields()
    if not fields["title"] or not fields["description"]:
        flash("A project needs a title and a short description.", "error")
        return _back("admin_projects")
    if fields["domain"] is None:
        flash("Enter the domain as example.com or a full https:// link.", "error")
        return _back("admin_projects")
    image = project["image"]
    file = request.files.get("image")
    if file and file.filename:
        new_image, err = save_image(file, "project")
        if err:
            flash(err, "error")
            return _back("admin_projects")
        remove_image(image)
        image = new_image
    elif request.form.get("remove_image"):
        remove_image(image)
        image = ""
    get_db().execute(
        "UPDATE projects SET title=?, domain=?, status=?, hosting=?, description=?, details=?, contributors=?, "
        "image=? WHERE id=?",
        (fields["title"], fields["domain"], fields["status"], fields["hosting"], fields["description"],
         fields["details"], fields["contributors"], image, project_id))
    get_db().commit()
    flash("Project saved.", "ok")
    return _back("admin_projects")


@app.route("/admin/projects/<int:project_id>/toggle", methods=["POST"])
@admin_required
def admin_project_toggle(project_id):
    project = _get_project(project_id)
    new_state = 0 if project["enabled"] else 1
    get_db().execute("UPDATE projects SET enabled = ? WHERE id = ?", (new_state, project_id))
    get_db().commit()
    flash(f"“{project['title']}” is now {'visible' if new_state else 'hidden'} for visitors.", "ok")
    return _back("admin_projects")


@app.route("/admin/projects/<int:project_id>/move", methods=["POST"])
@admin_required
def admin_project_move(project_id):
    _get_project(project_id)
    move_row("projects", project_id, "up" if request.form.get("dir") == "up" else "down")
    return _back("admin_projects")


@app.route("/admin/projects/<int:project_id>/delete", methods=["POST"])
@admin_required
def admin_project_delete(project_id):
    project = _get_project(project_id)
    remove_image(project["image"])
    get_db().execute("DELETE FROM projects WHERE id = ?", (project_id,))
    get_db().commit()
    flash(f"Project “{project['title']}” deleted.", "ok")
    return _back("admin_projects")


# --------------------------------------------------- admin: tab items (members, cards)
def _get_tab(tab_id, custom_only=True):
    tab = get_db().execute("SELECT * FROM tabs WHERE id = ?", (tab_id,)).fetchone()
    if tab is None or (custom_only and tab["kind"] != "custom"):
        abort(404)
    return tab


def _item_fields():
    f = request.form
    return {"title": f.get("title", "").strip()[:80], "subtitle": f.get("subtitle", "").strip()[:80],
            "description": f.get("description", "").strip()[:1200]}


@app.route("/admin/tabs/<int:tab_id>/items")
@admin_required
def admin_tab_items(tab_id):
    tab = _get_tab(tab_id)
    items = get_db().execute("SELECT * FROM tab_items WHERE tab_id = ? ORDER BY position, id", (tab_id,)).fetchall()
    return render_template("admin/tab_items.html", tab=tab, items=items, admin_active="tabs")


@app.route("/admin/tabs/<int:tab_id>/items/new", methods=["POST"])
@admin_required
def admin_item_new(tab_id):
    _get_tab(tab_id)
    fields = _item_fields()
    file = request.files.get("image")
    has_image = bool(file and file.filename)
    back = redirect(url_for("admin_tab_items", tab_id=tab_id))
    if not (fields["title"] or fields["description"] or has_image):
        flash("Add at least a name, a description or an image.", "error")
        return back
    image = ""
    if has_image:
        image, err = save_image(file, "item")
        if err:
            flash(err, "error")
            return back
    get_db().execute(
        "INSERT INTO tab_items (tab_id, title, subtitle, description, image, position, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (tab_id, fields["title"], fields["subtitle"], fields["description"], image,
         next_position("tab_items", "tab_id", tab_id), now_iso()))
    get_db().commit()
    flash("Item added.", "ok")
    return back


def _get_item(item_id):
    item = get_db().execute("SELECT * FROM tab_items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    return item


@app.route("/admin/items/<int:item_id>/edit", methods=["POST"])
@admin_required
def admin_item_edit(item_id):
    item = _get_item(item_id)
    fields = _item_fields()
    back = redirect(url_for("admin_tab_items", tab_id=item["tab_id"]))
    image = item["image"]
    file = request.files.get("image")
    if file and file.filename:
        new_image, err = save_image(file, "item")
        if err:
            flash(err, "error")
            return back
        remove_image(image)
        image = new_image
    elif request.form.get("remove_image"):
        remove_image(image)
        image = ""
    if not (fields["title"] or fields["description"] or image):
        flash("An item needs at least a name, a description or an image. Delete it instead if you don't want it.", "error")
        return back
    get_db().execute("UPDATE tab_items SET title=?, subtitle=?, description=?, image=? WHERE id=?",
                     (fields["title"], fields["subtitle"], fields["description"], image, item_id))
    get_db().commit()
    flash("Item saved.", "ok")
    return back


@app.route("/admin/items/<int:item_id>/move", methods=["POST"])
@admin_required
def admin_item_move(item_id):
    item = _get_item(item_id)
    move_row("tab_items", item_id, "up" if request.form.get("dir") == "up" else "down")
    return redirect(url_for("admin_tab_items", tab_id=item["tab_id"]))


@app.route("/admin/items/<int:item_id>/delete", methods=["POST"])
@admin_required
def admin_item_delete(item_id):
    item = _get_item(item_id)
    remove_image(item["image"])
    get_db().execute("DELETE FROM tab_items WHERE id = ?", (item_id,))
    get_db().commit()
    flash("Item deleted.", "ok")
    return redirect(url_for("admin_tab_items", tab_id=item["tab_id"]))


# ------------------------------------------------------------ admin: stack
@app.route("/admin/stack")
@admin_required
def admin_stack():
    db = get_db()
    cfg = {k: (db.execute("SELECT value FROM meta WHERE key = ?", ("stack_" + k,)).fetchone() or [""])[0]
           for k in ("heading", "intro", "note")}
    groups = []
    for g in db.execute("SELECT * FROM stack_groups ORDER BY position, id").fetchall():
        items = db.execute("SELECT * FROM stack_items WHERE group_id = ? ORDER BY position, id", (g["id"],)).fetchall()
        groups.append({"row": g, "items": items})
    return render_template("admin/stack.html", cfg=cfg, groups=groups, admin_active="stack")


def _stack_back():
    return redirect(url_for("admin_stack"))


@app.route("/admin/stack/settings", methods=["POST"])
@admin_required
def admin_stack_settings():
    db = get_db()
    values = {"heading": request.form.get("heading", "").strip()[:120] or content.STACK_SETTINGS["heading"],
              "intro": request.form.get("intro", "").strip()[:400],
              "note": request.form.get("note", "").strip()[:600]}
    for k, v in values.items():
        db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   ("stack_" + k, v))
    db.commit()
    flash("Stack section text saved.", "ok")
    return _stack_back()


@app.route("/admin/stack/groups/new", methods=["POST"])
@admin_required
def admin_stack_group_new():
    name = request.form.get("name", "").strip()[:40]
    if not name:
        flash("Give the group a name, for example Frontend or Mobile.", "error")
    else:
        get_db().execute("INSERT INTO stack_groups (name, position) VALUES (?, ?)", (name, next_position("stack_groups")))
        get_db().commit()
        flash(f"Group “{name}” added. Now add the tools inside it.", "ok")
    return _stack_back()


def _get_group(group_id):
    row = get_db().execute("SELECT * FROM stack_groups WHERE id = ?", (group_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/admin/stack/groups/<int:group_id>/edit", methods=["POST"])
@admin_required
def admin_stack_group_edit(group_id):
    group = _get_group(group_id)
    name = request.form.get("name", "").strip()[:40] or group["name"]
    get_db().execute("UPDATE stack_groups SET name = ? WHERE id = ?", (name, group_id))
    get_db().commit()
    flash("Group renamed.", "ok")
    return _stack_back()


@app.route("/admin/stack/groups/<int:group_id>/move", methods=["POST"])
@admin_required
def admin_stack_group_move(group_id):
    _get_group(group_id)
    move_row("stack_groups", group_id, "up" if request.form.get("dir") == "up" else "down")
    return _stack_back()


@app.route("/admin/stack/groups/<int:group_id>/delete", methods=["POST"])
@admin_required
def admin_stack_group_delete(group_id):
    group = _get_group(group_id)
    db = get_db()
    db.execute("DELETE FROM stack_items WHERE group_id = ?", (group_id,))
    db.execute("DELETE FROM stack_groups WHERE id = ?", (group_id,))
    db.commit()
    flash(f"Group “{group['name']}” and its tools were deleted.", "ok")
    return _stack_back()


@app.route("/admin/stack/groups/<int:group_id>/items/new", methods=["POST"])
@admin_required
def admin_stack_item_new(group_id):
    _get_group(group_id)
    name = request.form.get("name", "").strip()[:60]
    if not name:
        flash("Give the tool a name, for example React or Laravel.", "error")
    else:
        get_db().execute(
            "INSERT INTO stack_items (group_id, name, description, position) VALUES (?,?,?,?)",
            (group_id, name, request.form.get("description", "").strip()[:300],
             next_position("stack_items", "group_id", group_id)))
        get_db().commit()
        flash(f"“{name}” added.", "ok")
    return _stack_back()


def _get_stack_item(item_id):
    row = get_db().execute("SELECT * FROM stack_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/admin/stack/items/<int:item_id>/edit", methods=["POST"])
@admin_required
def admin_stack_item_edit(item_id):
    item = _get_stack_item(item_id)
    name = request.form.get("name", "").strip()[:60] or item["name"]
    get_db().execute("UPDATE stack_items SET name = ?, description = ? WHERE id = ?",
                     (name, request.form.get("description", "").strip()[:300], item_id))
    get_db().commit()
    flash("Tool saved.", "ok")
    return _stack_back()


@app.route("/admin/stack/items/<int:item_id>/move", methods=["POST"])
@admin_required
def admin_stack_item_move(item_id):
    _get_stack_item(item_id)
    move_row("stack_items", item_id, "up" if request.form.get("dir") == "up" else "down")
    return _stack_back()


@app.route("/admin/stack/items/<int:item_id>/delete", methods=["POST"])
@admin_required
def admin_stack_item_delete(item_id):
    item = _get_stack_item(item_id)
    get_db().execute("DELETE FROM stack_items WHERE id = ?", (item_id,))
    get_db().commit()
    flash(f"“{item['name']}” removed.", "ok")
    return _stack_back()


# ------------------------------------------------- admin: legal documents
@app.route("/admin/legal")
@admin_required
def admin_legal():
    rows = get_db().execute(
        "SELECT * FROM documents WHERE category = 'legal' ORDER BY created_at DESC, id DESC").fetchall()
    reveal, share_text, share_url = pop_reveal("legal")
    return render_template("admin/legal.html", docs=rows, kinds=LEGAL_KINDS, kind_labels=dict(LEGAL_KINDS),
                           reveal=reveal, share_text=share_text, share_url=share_url,
                           ttl=LEGAL_TTL // 60, admin_active="legal", required_kinds=sorted(PASSWORD_REQUIRED_KINDS),
                           min_pw=LEGAL_MIN_PW, share_link=lambda d: wa_url(doc_share_text(d)))


@app.route("/admin/legal/upload", methods=["POST"])
@admin_required
def admin_legal_upload():
    title = request.form.get("title", "").strip()
    kind = request.form.get("legal_kind", "")
    description = request.form.get("description", "").strip()
    password = request.form.get("password", "")
    protect = request.form.get("protect", "1") == "1"
    file = request.files.get("file")
    if not title:
        flash("Give the legal document a title.", "error")
    elif kind not in dict(LEGAL_KINDS):
        flash("Choose what kind of document this is.", "error")
    elif kind in PASSWORD_REQUIRED_KINDS and not protect:
        flash("Contracts must have a password. Choose password protection and enter one.", "error")
    elif protect and len(password) < LEGAL_MIN_PW:
        flash(f"Enter a password of at least {LEGAL_MIN_PW} characters, or choose “Open” for terms and policies.", "error")
    elif not file or not file.filename:
        flash("Choose the PDF to upload.", "error")
    else:
        ext, err = validate_upload(file)
        if err:
            flash(err, "error")
        elif ext != ".pdf":
            flash("Legal documents must be uploaded as PDF files.", "error")
        else:
            stored = f"{uuid.uuid4().hex}{ext}"
            path = os.path.join(UPLOAD_DIR, stored)
            file.save(path)
            now = now_iso()
            db = get_db()
            cur = db.execute(
                "INSERT INTO documents (title, description, stored_name, original_name, ext, size, password_hash, "
                "protected, enabled, created_at, updated_at, category, legal_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (title[:140], description[:400], stored, os.path.basename(file.filename)[:200], ext,
                 os.path.getsize(path), generate_password_hash(password) if protect else "",
                 1 if protect else 0, 1 if request.form.get("enabled") else 0, now, now, "legal", kind))
            if protect:
                vault_store_doc(db, cur.lastrowid, title, password)
            db.commit()
            session["reveal"] = {"id": cur.lastrowid, "cat": "legal"}
            flash(f"“{title}” uploaded to the legal library"
                  + (" with a password." if protect else " as an open document."), "ok")
    return redirect(url_for("admin_legal") + "#library")


# ----------------------------------------------------- admin: Password Manager
def vault_required(view):
    """Needs the admin to be signed in AND to have re-entered the admin password in the last 10 minutes."""
    @wraps(view)
    @admin_required
    def wrapper(*args, **kwargs):
        if not vault_unlocked():
            flash("The Password Manager is locked. Enter your admin password to open it.", "error")
            return redirect(url_for("admin_passwords"))
        session["vault_until"] = time.time() + VAULT_TTL  # sliding: re-locks after 10 idle minutes
        return view(*args, **kwargs)
    return wrapper


def _entry_share_text(entry, doc, password):
    if doc is not None:
        return doc_share_text(doc, password)
    lines = [f"{entry['label']} (Novax Tech Hub)"]
    if entry["account"]:
        lines.append(f"Account: {entry['account']}")
    lines.append(f"Password: {password}")
    return "\n".join(lines)


@app.route("/admin/passwords")
@admin_required
def admin_passwords():
    unlocked = vault_unlocked()
    entries, missing = [], []
    if unlocked:
        session["vault_until"] = time.time() + VAULT_TTL
        db = get_db()
        for r in db.execute("SELECT * FROM vault_entries ORDER BY lower(label), id").fetchall():
            doc = get_doc(r["doc_id"]) if r["doc_id"] else None
            pw = vault_decrypt(r["secret_enc"])
            entries.append({
                "id": r["id"], "label": r["label"], "account": r["account"], "note": r["note"],
                "updated_at": r["updated_at"], "password": pw, "doc": doc,
                "share_url": wa_url(_entry_share_text(r, doc, pw)) if pw is not None else "",
                "share_text": _entry_share_text(r, doc, pw) if pw is not None else ""})
        missing = db.execute(
            "SELECT id, title, category FROM documents WHERE protected = 1 AND id NOT IN "
            "(SELECT doc_id FROM vault_entries WHERE doc_id IS NOT NULL) ORDER BY title").fetchall()
    resp = app.make_response(render_template(
        "admin/passwords.html", unlocked=unlocked, entries=entries, missing=missing,
        admin_active="passwords", ttl=VAULT_TTL // 60, docs_admin_url=docs_admin_url))
    resp.headers["Cache-Control"] = "no-store, private"
    return resp


@app.route("/admin/passwords/unlock", methods=["POST"])
@admin_required
def admin_passwords_unlock():
    key = f"vault:{client_ip()}"
    if locked_out(key, limit=5, window=600):
        flash("Too many incorrect attempts. Try again in ten minutes.", "error")
        return redirect(url_for("admin_passwords"))
    row = get_db().execute("SELECT password_hash FROM admin WHERE id = 1").fetchone()
    if check_password_hash(row["password_hash"], request.form.get("password", "")):
        _attempts.pop(key, None)
        session["vault_until"] = time.time() + VAULT_TTL
    else:
        record_failure(key)
        flash("That is not your admin password.", "error")
    return redirect(url_for("admin_passwords"))


@app.route("/admin/passwords/lock", methods=["POST"])
@admin_required
def admin_passwords_lock():
    session.pop("vault_until", None)
    flash("Password Manager locked.", "ok")
    return redirect(url_for("admin_passwords"))


@app.route("/admin/passwords/new", methods=["POST"])
@vault_required
def admin_password_new():
    label = request.form.get("label", "").strip()
    account = request.form.get("account", "").strip()
    secret = request.form.get("secret", "")
    note = request.form.get("note", "").strip()
    if not label:
        flash("Give the entry a name, for example “Hosting account”.", "error")
    elif not secret:
        flash("Enter the password to store.", "error")
    elif len(secret) > 500:
        flash("That password is too long (500 characters at most).", "error")
    else:
        now = now_iso()
        get_db().execute(
            "INSERT INTO vault_entries (label, account, secret_enc, note, doc_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)", (label[:140], account[:200], vault_encrypt(secret), note[:500], None, now, now))
        get_db().commit()
        flash(f"“{label[:140]}” saved.", "ok")
    return redirect(url_for("admin_passwords"))


def _get_entry(entry_id):
    row = get_db().execute("SELECT * FROM vault_entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/admin/passwords/<int:entry_id>/edit", methods=["POST"])
@vault_required
def admin_password_edit(entry_id):
    row = _get_entry(entry_id)
    label = request.form.get("label", "").strip() or row["label"]
    account = request.form.get("account", "").strip()
    note = request.form.get("note", "").strip()
    secret = request.form.get("secret", "")
    db = get_db()
    if secret and row["doc_id"]:
        flash("A document's password is changed from the document's Edit button, so the two always match.", "error")
        return redirect(url_for("admin_passwords"))
    if len(secret) > 500:
        flash("That password is too long (500 characters at most).", "error")
        return redirect(url_for("admin_passwords"))
    if secret:
        db.execute("UPDATE vault_entries SET label=?, account=?, note=?, secret_enc=?, updated_at=? WHERE id=?",
                   (label[:140], account[:200], note[:500], vault_encrypt(secret), now_iso(), entry_id))
    else:
        db.execute("UPDATE vault_entries SET label=?, account=?, note=?, updated_at=? WHERE id=?",
                   (label[:140], account[:200], note[:500], now_iso(), entry_id))
    db.commit()
    flash("Entry saved.", "ok")
    return redirect(url_for("admin_passwords"))


@app.route("/admin/passwords/<int:entry_id>/delete", methods=["POST"])
@vault_required
def admin_password_delete(entry_id):
    row = _get_entry(entry_id)
    get_db().execute("DELETE FROM vault_entries WHERE id = ?", (entry_id,))
    get_db().commit()
    flash(f"“{row['label']}” removed from the Password Manager.", "ok")
    return redirect(url_for("admin_passwords"))


@app.route("/admin/account", methods=["POST"])
@admin_required
def admin_account():
    db = get_db()
    row = db.execute("SELECT * FROM admin WHERE id = 1").fetchone()
    current = request.form.get("current", "")
    username = request.form.get("username", "").strip() or row["username"]
    new_pw = request.form.get("new_password", "")
    if not check_password_hash(row["password_hash"], current):
        flash("Your current password is not correct.", "error")
    elif new_pw and len(new_pw) < 8:
        flash("Use at least 8 characters for the admin password.", "error")
    else:
        pw_hash = generate_password_hash(new_pw) if new_pw else row["password_hash"]
        db.execute("UPDATE admin SET username=?, password_hash=?, must_change=0 WHERE id=1",
                   (username, pw_hash))
        db.commit()
        flash("Admin account updated.", "ok")
    return redirect(url_for("admin_dashboard") + "#account")


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, message="That page does not exist."), 404


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", code=403, message="You need to unlock this document first."), 403


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", code=400, message=getattr(e, "description", "Bad request.")), 400


@app.errorhandler(413)
def too_large(_e):
    return render_template("error.html", code=413, message="That file is larger than the 25 MB limit."), 413


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
