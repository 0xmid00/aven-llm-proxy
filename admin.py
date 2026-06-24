"""
admin.py
--------
Web admin panel for the llm-proxy.

Routes (mounted on the same Flask app as proxy.py):
  GET  /admin               — login page (or dashboard if session is valid)
  POST /admin/login         — verify admin/admin, set session cookie
  POST /admin/logout        — clear session
  GET  /admin/api/users     — return all users as JSON (for the table)
  POST /admin/add_user      — create a new user (code auto-generated)
  POST /admin/edit_user     — edit any field on an existing user
  POST /admin/delete_user   — delete a user by code
  POST /admin/toggle_sub    — toggle is_subscribed 0/1 for a user

The user database is users.csv (read+written on every change).
"""

import csv
import os
import secrets
import string
import threading
import time
from datetime import datetime, date

from flask import Blueprint, request, session, jsonify, Response, redirect, url_for

# Thread lock so concurrent admin edits don't corrupt the CSV
_CSV_LOCK = threading.Lock()

USERS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.csv")
FIELDS = ["name", "username", "contact", "code", "is_subscribed",
          "tokens_used", "tokens_limit", "subscription_end"]

ADMIN_USER = "admin"
ADMIN_PASS = "admin"

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


# ---------- CSV helpers ----------

def _read_users():
    """Read users.csv → list of dicts (strings preserved as-is)."""
    if not os.path.exists(USERS_CSV):
        return []
    out = []
    with open(USERS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Coerce numeric fields for convenience
            try:
                row["is_subscribed"] = int(row.get("is_subscribed", "0") or 0)
            except (ValueError, TypeError):
                row["is_subscribed"] = 0
            try:
                row["tokens_used"] = int(row.get("tokens_used", "0") or 0)
            except (ValueError, TypeError):
                row["tokens_used"] = 0
            try:
                row["tokens_limit"] = int(row.get("tokens_limit", "0") or 0)
            except (ValueError, TypeError):
                row["tokens_limit"] = 0
            out.append(row)
    return out


def _write_users(users):
    """Write list of dicts back to users.csv (atomic-ish: write then rename)."""
    tmp = USERS_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for u in users:
            # Ensure all fields exist + normalize types
            row = {k: u.get(k, "") for k in FIELDS}
            row["is_subscribed"] = str(int(row.get("is_subscribed", 0) or 0))
            row["tokens_used"] = str(int(row.get("tokens_used", 0) or 0))
            row["tokens_limit"] = str(int(row.get("tokens_limit", 0) or 0))
            writer.writerow(row)
    os.replace(tmp, USERS_CSV)


def _gen_code():
    """Generate a cryptographically-secure license code.

    Format: AVEN-XXXXX-XXXXX-XXXXX-XXXXX  (4 groups of 5 chars = 20 random chars)
    Alphabet: A-Z + 0-9 (36 chars)
    Entropy: 36^20 ≈ 1.3 × 10^31 combinations — unbruteforcable.

    Uses `secrets` (CSPRNG) instead of `random` (Mersenne Twister is predictable).
    """
    chars = string.ascii_uppercase + string.digits  # 36 chars
    parts = ["".join(secrets.choice(chars) for _ in range(5)) for _ in range(4)]
    return "AVEN-" + "-".join(parts)


def _user_public(u):
    """Public-facing dict for API responses (no internal fields)."""
    return {
        "name": u.get("name", ""),
        "username": u.get("username", ""),
        "contact": u.get("contact", ""),
        "code": u.get("code", ""),
        "is_subscribed": int(u.get("is_subscribed", 0) or 0),
        "tokens_used": int(u.get("tokens_used", 0) or 0),
        "tokens_limit": int(u.get("tokens_limit", 0) or 0),
        "tokens_remaining": max(0, int(u.get("tokens_limit", 0) or 0) - int(u.get("tokens_used", 0) or 0)),
        "subscription_end": u.get("subscription_end", ""),
    }


# ---------- Auth gate ----------

def _is_logged_in():
    return session.get("admin_user") == ADMIN_USER


# ---------- Routes ----------

@admin_bp.route("/", methods=["GET"])
def admin_root():
    if not _is_logged_in():
        return _login_page()
    return _dashboard_page()


@admin_bp.route("/login", methods=["POST"])
def admin_login():
    user = (request.form.get("username") or "").strip()
    pwd = (request.form.get("password") or "").strip()
    if user == ADMIN_USER and pwd == ADMIN_PASS:
        session["admin_user"] = user
        return redirect("/admin/")
    return _login_page(error="بيانات الدخول غير صحيحة")


@admin_bp.route("/logout", methods=["POST", "GET"])
def admin_logout():
    session.pop("admin_user", None)
    return redirect("/admin/")


@admin_bp.route("/api/users", methods=["GET"])
def api_users():
    if not _is_logged_in():
        return jsonify({"error": "unauthorized"}), 401
    with _CSV_LOCK:
        users = _read_users()
    return jsonify({"users": [_user_public(u) for u in users]})


@admin_bp.route("/add_user", methods=["POST"])
def add_user():
    if not _is_logged_in():
        return jsonify({"error": "unauthorized"}), 401
    name = (request.form.get("name") or request.json.get("name") or "").strip()
    username = (request.form.get("username") or (request.json or {}).get("username") or "").strip()
    contact = (request.form.get("contact") or (request.json or {}).get("contact") or "").strip()
    tokens_limit = (request.form.get("tokens_limit") or (request.json or {}).get("tokens_limit") or "100000").strip()
    subscription_end = (request.form.get("subscription_end") or (request.json or {}).get("subscription_end") or "").strip()

    if not name or not username:
        return jsonify({"error": "name and username are required"}), 400

    code = _gen_code()
    new_user = {
        "name": name,
        "username": username,
        "contact": contact,
        "code": code,
        "is_subscribed": 1,
        "tokens_used": 0,
        "tokens_limit": int(tokens_limit) if tokens_limit.isdigit() else 100000,
        "subscription_end": subscription_end,
    }
    with _CSV_LOCK:
        users = _read_users()
        # Ensure code is unique
        while any(u.get("code") == code for u in users):
            code = _gen_code()
            new_user["code"] = code
        users.append(new_user)
        _write_users(users)
    return jsonify({"ok": True, "user": _user_public(new_user)})


@admin_bp.route("/edit_user", methods=["POST"])
def edit_user():
    if not _is_logged_in():
        return jsonify({"error": "unauthorized"}), 401
    data = request.json or request.form
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "code is required"}), 400
    with _CSV_LOCK:
        users = _read_users()
        target = None
        for u in users:
            if u.get("code") == code:
                target = u
                break
        if not target:
            return jsonify({"error": "user not found"}), 404
        # Patch any provided field
        for k in FIELDS:
            if k == "code":
                continue  # don't allow editing the code (it's the key)
            if k in data and data[k] is not None and data[k] != "":
                if k in ("is_subscribed", "tokens_used", "tokens_limit"):
                    try:
                        target[k] = int(data[k])
                    except (ValueError, TypeError):
                        pass
                else:
                    target[k] = str(data[k]).strip()
        _write_users(users)
    return jsonify({"ok": True, "user": _user_public(target)})


@admin_bp.route("/delete_user", methods=["POST"])
def delete_user():
    if not _is_logged_in():
        return jsonify({"error": "unauthorized"}), 401
    data = request.json or request.form
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "code is required"}), 400
    with _CSV_LOCK:
        users = _read_users()
        new_users = [u for u in users if u.get("code") != code]
        if len(new_users) == len(users):
            return jsonify({"error": "user not found"}), 404
        _write_users(new_users)
    return jsonify({"ok": True})


@admin_bp.route("/toggle_sub", methods=["POST"])
def toggle_sub():
    if not _is_logged_in():
        return jsonify({"error": "unauthorized"}), 401
    data = request.json or request.form
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "code is required"}), 400
    with _CSV_LOCK:
        users = _read_users()
        target = None
        for u in users:
            if u.get("code") == code:
                target = u
                break
        if not target:
            return jsonify({"error": "user not found"}), 404
        target["is_subscribed"] = 0 if int(target.get("is_subscribed", 0) or 0) else 1
        _write_users(users)
    return jsonify({"ok": True, "is_subscribed": target["is_subscribed"]})


# ---------- HTML pages (inline so the project is self-contained) ----------

def _login_page(error=None):
    err_html = f'<div class="err">{error}</div>' if error else ''
    return Response(f"""<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<title>Aven Admin — Login</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: #0F172A; color: #E2EBF0; margin: 0; padding: 40px 20px; }}
  .card {{ max-width: 360px; margin: 60px auto; background: #1a2235; border: 1px solid #1e2d45; border-radius: 14px; padding: 28px; box-shadow: 0 10px 40px rgba(0,0,0,0.4); }}
  h1 {{ font-size: 22px; margin: 0 0 6px; }}
  .sub {{ font-size: 12px; color: #64748B; margin-bottom: 22px; }}
  label {{ display: block; font-size: 11px; color: #64748B; margin: 10px 0 4px; text-transform: uppercase; letter-spacing: 0.04em; }}
  input {{ width: 100%; box-sizing: border-box; background: #0F172A; border: 1px solid #1e2d45; border-radius: 8px; color: #E2EBF0; padding: 10px 12px; font-size: 14px; font-family: inherit; }}
  input:focus {{ outline: none; border-color: #7C3AED; }}
  button {{ width: 100%; margin-top: 16px; padding: 11px; border: none; border-radius: 8px; background: linear-gradient(135deg, #7C3AED, #3BB2F6); color: #fff; font-size: 14px; font-weight: 700; cursor: pointer; font-family: inherit; }}
  button:hover {{ opacity: 0.92; }}
  .err {{ background: rgba(239,68,68,0.1); border: 1px solid #ef4444; color: #fca5a5; padding: 8px 12px; border-radius: 6px; font-size: 12px; margin-bottom: 12px; }}
</style></head><body>
<div class="card">
  <h1>🔐 Aven Admin</h1>
  <div class="sub">لوحة إدارة المستخدمين — دخول المسؤول</div>
  {err_html}
  <form method="post" action="/admin/login">
    <label>اسم المستخدم</label>
    <input name="username" autocomplete="off" autofocus>
    <label>كلمة المرور</label>
    <input name="password" type="password">
    <button type="submit">دخول</button>
  </form>
</div>
</body></html>""", mimetype="text/html; charset=utf-8")


def _dashboard_page():
    return Response(f"""<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="UTF-8">
<title>Aven Admin — Dashboard</title>
<style>
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: #0F172A; color: #E2EBF0; margin: 0; padding: 20px; }}
  .hdr {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }}
  h1 {{ font-size: 18px; margin: 0; }}
  .actions-top {{ display: flex; gap: 8px; }}
  .btn {{ background: #1a2235; border: 1px solid #1e2d45; color: #E2EBF0; padding: 7px 14px; border-radius: 8px; font-size: 12px; font-weight: 600; cursor: pointer; font-family: inherit; text-decoration: none; }}
  .btn:hover {{ background: #243049; }}
  .btn-primary {{ background: linear-gradient(135deg, #7C3AED, #3BB2F6); border: none; color: #fff; }}
  .btn-danger {{ color: #ef4444; border-color: #ef4444; }}
  .btn-danger:hover {{ background: rgba(239,68,68,0.15); }}
  table {{ width: 100%; border-collapse: collapse; background: #1a2235; border-radius: 10px; overflow: hidden; }}
  th, td {{ padding: 10px 12px; text-align: right; border-bottom: 1px solid #1e2d45; font-size: 12px; }}
  th {{ background: #0F172A; color: #64748B; font-weight: 600; text-transform: uppercase; font-size: 10px; letter-spacing: 0.04em; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover {{ background: #243049; }}
  td input, td select {{ width: 100%; box-sizing: border-box; background: #0F172A; border: 1px solid #1e2d45; border-radius: 4px; color: #E2EBF0; padding: 4px 6px; font-size: 11px; font-family: inherit; }}
  .badge-on {{ background: rgba(34,211,238,0.15); color: #22D3EE; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 700; }}
  .badge-off {{ background: rgba(239,68,68,0.15); color: #ef4444; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 700; }}
  .modal-bg {{ position: fixed; inset: 0; background: rgba(0,0,0,0.7); display: none; align-items: center; justify-content: center; z-index: 100; }}
  .modal-bg.show {{ display: flex; }}
  .modal {{ background: #1a2235; border: 1px solid #1e2d45; border-radius: 14px; padding: 24px; max-width: 420px; width: 90%; }}
  .modal h2 {{ margin: 0 0 14px; font-size: 16px; }}
  .modal label {{ display: block; font-size: 11px; color: #64748B; margin: 8px 0 4px; }}
  .modal input {{ width: 100%; box-sizing: border-box; background: #0F172A; border: 1px solid #1e2d45; border-radius: 6px; color: #E2EBF0; padding: 8px 10px; font-size: 13px; font-family: inherit; }}
  .toast {{ position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #1a2235; border: 1px solid #22D3EE; color: #22D3EE; padding: 10px 18px; border-radius: 8px; font-size: 12px; font-weight: 600; opacity: 0; transition: opacity 0.2s; pointer-events: none; }}
  .toast.show {{ opacity: 1; }}
</style></head><body>
<div class="hdr">
  <h1>🔐 Aven Admin — User Management</h1>
  <div class="actions-top">
    <button class="btn btn-primary" onclick="openModal()">+ إضافة مستخدم</button>
    <a class="btn" href="/admin/logout">خروج</a>
  </div>
</div>
<table>
  <thead>
    <tr>
      <th>الاسم</th><th>المستخدم</th><th>التواصل</th><th>الكود</th>
      <th>مشترك</th><th>توكنز مستخدمة</th><th>الحد</th><th>الانتهاء</th><th>إجراءات</th>
    </tr>
  </thead>
  <tbody id="usersBody">
    <tr><td colspan="9" style="text-align:center;color:#64748B;padding:30px">جارٍ التحميل...</td></tr>
  </tbody>
</table>

<div class="modal-bg" id="addModal">
  <div class="modal">
    <h2>إضافة مستخدم جديد</h2>
    <label>الاسم</label>
    <input id="newName" placeholder="Ahmed">
    <label>اسم المستخدم</label>
    <input id="newUsername" placeholder="ahmed99">
    <label>التواصل</label>
    <input id="newContact" placeholder="+213555123456">
    <label>حد التوكنز</label>
    <input id="newLimit" type="number" value="100000">
    <label>تاريخ الانتهاء (YYYY-MM-DD)</label>
    <input id="newEnd" placeholder="2026-12-31">
    <div style="display:flex;gap:8px;margin-top:16px">
      <button class="btn" onclick="closeModal()" style="flex:1">إلغاء</button>
      <button class="btn btn-primary" onclick="addUser()" style="flex:1">إضافة</button>
    </div>
    <div style="margin-top:10px;font-size:11px;color:#64748B">سيتم توليد الكود تلقائياً بصيغة AVEN-XXXXX-XXXXX-XXXXX-XXXXX (20 حرف عشوائي آمن)</div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
function toast(msg) {{
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1800);
}}
async function loadUsers() {{
  const r = await fetch('/admin/api/users');
  const data = await r.json();
  const body = document.getElementById('usersBody');
  body.innerHTML = '';
  if (!data.users || !data.users.length) {{
    body.innerHTML = '<tr><td colspan="9" style="text-align:center;color:#64748B;padding:30px">لا يوجد مستخدمون</td></tr>';
    return;
  }}
  data.users.forEach(u => {{
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><input value="${{u.name}}" onchange="editField('${{u.code}}','name',this.value)"></td>
      <td><input value="${{u.username}}" onchange="editField('${{u.code}}','username',this.value)"></td>
      <td><input value="${{u.contact}}" onchange="editField('${{u.code}}','contact',this.value)"></td>
      <td style="font-family:monospace;font-size:11px">${{u.code}}</td>
      <td><span class="${{u.is_subscribed ? 'badge-on' : 'badge-off'}}" style="cursor:pointer" onclick="toggleSub('${{u.code}}')">${{u.is_subscribed ? 'مشترك' : 'موقوف'}}</span></td>
      <td><input type="number" value="${{u.tokens_used}}" onchange="editField('${{u.code}}','tokens_used',this.value)" style="width:80px"></td>
      <td><input type="number" value="${{u.tokens_limit}}" onchange="editField('${{u.code}}','tokens_limit',this.value)" style="width:80px"></td>
      <td><input value="${{u.subscription_end}}" onchange="editField('${{u.code}}','subscription_end',this.value)" style="width:110px"></td>
      <td>
        <button class="btn btn-danger" onclick="deleteUser('${{u.code}}','${{u.name}}')" style="padding:3px 8px;font-size:10px">حذف</button>
      </td>
    `;
    body.appendChild(tr);
  }});
}}
async function editField(code, field, value) {{
  const body = {{ code, [field]: value }};
  const r = await fetch('/admin/edit_user', {{ method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify(body) }});
  if (r.ok) toast('تم الحفظ');
  else toast('فشل الحفظ');
}}
async function toggleSub(code) {{
  const r = await fetch('/admin/toggle_sub', {{ method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify({{code}}) }});
  if (r.ok) {{ toast('تم التبديل'); loadUsers(); }}
}}
async function deleteUser(code, name) {{
  if (!confirm('حذف المستخدم "' + name + '"؟')) return;
  const r = await fetch('/admin/delete_user', {{ method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify({{code}}) }});
  if (r.ok) {{ toast('تم الحذف'); loadUsers(); }}
}}
function openModal() {{ document.getElementById('addModal').classList.add('show'); }}
function closeModal() {{ document.getElementById('addModal').classList.remove('show'); }}
async function addUser() {{
  const body = {{
    name: document.getElementById('newName').value.trim(),
    username: document.getElementById('newUsername').value.trim(),
    contact: document.getElementById('newContact').value.trim(),
    tokens_limit: document.getElementById('newLimit').value.trim(),
    subscription_end: document.getElementById('newEnd').value.trim(),
  }};
  if (!body.name || !body.username) {{ toast('الاسم واسم المستخدم مطلوبان'); return; }}
  const r = await fetch('/admin/add_user', {{ method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify(body) }});
  if (r.ok) {{ toast('تمت الإضافة'); closeModal(); loadUsers(); }}
  else toast('فشلت الإضافة');
}}
loadUsers();
</script>
</body></html>""", mimetype="text/html; charset=utf-8")
