# Novax Tech Hub: Powering Beyond Limits

Flask website with a password-protected **Docs** library, a **Legal** library, an encrypted **Password Manager**, WhatsApp sharing and an admin panel. Runs on **SQLite** (default, zero setup) or **PostgreSQL**.

## Run it locally
```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py                   # opens on http://127.0.0.1:5000
```

## Features
- One continuous scrolling page: About, Skills, Services, Stack, Projects, Docs, Partnership, Contact. The menu highlights the section in view.
- Light and dark mode (follows the device setting, with a toggle in the menu that remembers the choice).
- Responsive from small phones to wide desktops.

## Database: SQLite or PostgreSQL
- **SQLite (default):** nothing to set up. Data lives in `instance/novax.db`.
- **PostgreSQL:** set one environment variable and restart. The tables are created automatically.
  ```bash
  export DATABASE_URL="postgresql://user:password@host:5432/dbname"   # postgres:// also works
  pip install -r requirements.txt                                      # includes psycopg2-binary
  python app.py
  ```
- **Moving existing SQLite data to PostgreSQL:** `python migrate_to_postgres.py --yes` (keeps ids, so shared links like `/docs/3` still work). Uploaded files stay in `instance/uploads` and `instance/media`.
- `python reset_admin.py` works with both databases.
- On PythonAnywhere, add `DATABASE_URL` in the Web tab's environment variables.

## Admin
- Sign in at `/admin/login` (link in the footer).
- First-run login: **manucho / Manucho990**. Change it under *Account* when you can.
- The admin login is not read from environment variables or a `.env` file. It is created from the code the first time the site starts (`app.py`, function `init_db`). Change it any time under *Account*.

### What the admin can do
- **Tabs** (`/admin/tabs`): show or hide any tab with a switch, rename menu labels, reorder with the arrows, and **add your own tabs** (title, formatted content, optional image and button). A new tab appears in the menu and as a section on the home page. Built-in tabs can be hidden but not deleted.
- **Projects** (`/admin/projects`): add a project with title, **domain name**, status, short description, longer **information** (opens under *Details*), **contributors** and a **screenshot**. Edit, reorder, hide or delete any project.
- **Upload documents** (PDF, DOC, DOCX, up to 25 MB) with a title, description and a password per document.
- **Password Manager** (`/admin/passwords`): every document password you set is saved here, encrypted (Fernet/AES), and linked to its document. Add your own logins too (name, username, password, note), show/copy them, edit or remove them. It asks for your admin password to open and locks itself after 10 idle minutes or on sign-out. **Back up `instance/vault.key`** (or set `VAULT_KEY`) with your database: without it saved passwords cannot be read. Documents whose passwords were set before this feature show under *Not stored yet*: set a new password to store it.
- **Share on WhatsApp**: buttons on each document (public and admin), each saved password, each project, the footer (share the site), and after setting a password (*Copy message*, *WhatsApp*, *Email*).
- **Enable / disable** any document with one switch. Disabled documents vanish from the public list and are blocked instantly.
- **Edit / change password**: changing a password signs out everyone who had unlocked it.
- **Tab items** (Tabs → *Items* on a custom tab): add members, team photos or cards. Each item has an optional image, name, role and description, all independent of each other.
- **Legal** (`/admin/legal`): upload company terms, policies and contracts as **PDF**. They appear as links in the **footer**, grouped by type. For **terms, policies and other** the admin chooses *Password protected* or *Open (no password)*; **contracts must always have a password** (8+ characters). Passwords can be added, changed or removed later with *Edit* (not removable from contracts). Extra protection: 4 wrong attempts pause access for 15 minutes, the document re-locks 20 minutes after it is opened, it is shown inline only (no download button), never cached, and kept out of search engines.
- **Stack** (`/admin/stack`): edit the Stack section's heading and text, add/rename/reorder/delete groups, and add/edit/reorder/remove the tools inside each group.
- **Projects**: the form starts by asking whether the project is hosted yet. If it is fully developed but has no hosting or domain, choose *Not yet*; the card then says "Developed, awaiting hosting" instead of showing a domain.
- **Site images**: upload your logo, the home-page photo and your profile photo (PNG, JPG, WEBP, up to 5 MB). The home page ships with a default photo (`static/img/hero.jpg`).

Notes: hiding the **Docs** tab removes the library from the site and menu, but direct links you've shared (`/docs/<id>`) still work with the password; disable the document itself to block it.

Content formatting for tabs and project details: blank line = new paragraph, `## ` subheading, `- ` bullets, `**bold**`, `[text](https://link)`.

## Deploy on PythonAnywhere
1. Upload this folder and create a virtualenv: `pip install -r requirements.txt`.
2. Web tab: add a Flask app, point the WSGI file to `wsgi.py` (`from app import app as application`).
3. Set environment variables: `SECRET_KEY` (any long random string), `COOKIE_SECURE=1`, optionally `DATABASE_URL` (PostgreSQL) and `VAULT_KEY` (a Fernet key; otherwise `instance/vault.key` is created). The admin login is not taken from environment variables.
4. Reload. The database and uploads live in `instance/` (back this folder up).

Netlify only hosts static files, so this Flask version needs a Python host such as PythonAnywhere or Render.

## Edit the text
Services, skills, projects and contact details are in `content.py`. Colours are at the top of `static/css/style.css`.

## Can't sign in to /admin?
1. **Changed the password under Account?** Then use the new one. The default `manucho` / `Manucho990` only applies until you change it.
2. **Use the `https://` address.** With `COOKIE_SECURE=1` the browser only keeps the login cookie over HTTPS. Enable *Force HTTPS* on the Web tab.
3. **Locked out?** Five wrong attempts lock sign-in for ten minutes. Click *Reload* on the Web tab to clear it.
4. **Still stuck?** In a Bash console: `cd ~/manucho-tech && workon novax-env && python reset_admin.py` (or `python reset_admin.py myname MyPassword123`), then Reload.
