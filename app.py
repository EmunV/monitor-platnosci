#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor Platnosci - aplikacja webowa
Uruchomienie lokalne: python app.py
Wdrozenie: Railway.app / Render.com
"""

import os, json, subprocess, sys, threading, time
from pathlib import Path
from functools import wraps
from flask import (Flask, render_template, request, session,
                   redirect, url_for, flash, send_from_directory,
                   jsonify)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "zmien-to-na-losowy-ciag-znakow")

BASE_DIR   = Path(__file__).parent
BANK_DIR   = BASE_DIR / "bank"
MONITOR_HTML = BASE_DIR / "monitor_platnosci.html"
TEMPLATE_HTML = BASE_DIR / "monitor_template.html"
CONFIG_PATH  = BASE_DIR / "config.json"

BANK_DIR.mkdir(exist_ok=True)

# ── UŻYTKOWNICY (z env lub pliku users.json) ──────────────────────────────────
def load_users():
    """Wczytaj użytkowników z users.json lub zmiennych środowiskowych."""
    users_path = BASE_DIR / "users.json"
    if users_path.exists():
        with open(users_path, encoding="utf-8") as f:
            return json.load(f)
    # Fallback: jeden użytkownik z env
    users = {}
    # Admin
    u1 = os.environ.get("ADMIN_USER", "jacek")
    p1 = os.environ.get("ADMIN_PASS", "zmien123")
    if u1: users[u1] = p1
    # User 2
    u2 = os.environ.get("USER2_NAME", "")
    p2 = os.environ.get("USER2_PASS", "")
    if u2 and p2: users[u2] = p2
    # User 3
    u3 = os.environ.get("USER3_NAME", "")
    p3 = os.environ.get("USER3_PASS", "")
    if u3 and p3: users[u3] = p3
    return users

def check_login(username, password):
    users = {k: v for k, v in load_users().items() if k and v}
    return users.get(username) == password

# ── DEKORATOR LOGOWANIA ───────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated

# ── WIDOKI ────────────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if check_login(username, password):
            session["logged_in"] = True
            session["username"] = username
            next_url = request.args.get("next") or url_for("monitor")
            return redirect(next_url)
        flash("Nieprawidlowy login lub haslo.")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def monitor():
    """Glowna strona - wyswietla monitor platnosci."""
    if MONITOR_HTML.exists():
        return MONITOR_HTML.read_text(encoding="utf-8")
    return render_template("no_data.html")

@app.route("/panel")
@login_required
def panel():
    """Panel administratora - aktualizacja danych."""
    bank_files = sorted(BANK_DIR.glob("*.csv"))
    monitor_exists = MONITOR_HTML.exists()
    monitor_size = round(MONITOR_HTML.stat().st_size / 1024) if monitor_exists else 0
    monitor_mtime = ""
    if monitor_exists:
        import datetime
        mt = MONITOR_HTML.stat().st_mtime
        monitor_mtime = datetime.datetime.fromtimestamp(mt).strftime("%d.%m.%Y %H:%M")
    return render_template("panel.html",
        bank_files=[f.name for f in bank_files],
        monitor_exists=monitor_exists,
        monitor_size=monitor_size,
        monitor_mtime=monitor_mtime,
        config_exists=CONFIG_PATH.exists())

@app.route("/upload-bank", methods=["POST"])
@login_required
def upload_bank():
    """Wgrywanie plikow CSV z mBanku."""
    files = request.files.getlist("csvfiles")
    saved = []
    for f in files:
        if f.filename and f.filename.lower().endswith(".csv"):
            name = secure_filename(f.filename)
            f.save(BANK_DIR / name)
            saved.append(name)
    if saved:
        flash(f"Wgrano {len(saved)} plik(ow): {', '.join(saved)}")
    else:
        flash("Brak plikow CSV do wgrania.")
    return redirect(url_for("panel"))

@app.route("/delete-bank/<filename>", methods=["POST"])
@login_required
def delete_bank(filename):
    """Usuwanie pliku CSV z serwera."""
    path = BANK_DIR / secure_filename(filename)
    if path.exists():
        path.unlink()
        flash(f"Usunieto: {filename}")
    return redirect(url_for("panel"))

_update_state = {"running": False, "output": "", "success": None, "started": 0}

def _run_update():
    script = BASE_DIR / "update_monitor.py"
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(BASE_DIR),
            capture_output=True, text=True,
            timeout=480, encoding="utf-8", errors="replace"
        )
        out = result.stdout + ("\n" + result.stderr if result.stderr else "")
        _update_state["output"] = out[-4000:]
        _update_state["success"] = result.returncode == 0 and MONITOR_HTML.exists()
    except subprocess.TimeoutExpired:
        _update_state["output"] = "TIMEOUT - skrypt dziala za dlugo (>8 min)"
        _update_state["success"] = False
    except Exception as e:
        _update_state["output"] = str(e)
        _update_state["success"] = False
    finally:
        _update_state["running"] = False

@app.route("/update", methods=["POST"])
@login_required
def update():
    """Uruchamia skrypt aktualizacji danych w tle."""
    global _update_state
    script = BASE_DIR / "update_monitor.py"
    if not script.exists():
        return jsonify({"status": "error", "message": "Brak pliku update_monitor.py"}), 400
    if not CONFIG_PATH.exists():
        return jsonify({"status": "error", "message": "Brak pliku config.json"}), 400
    if _update_state["running"]:
        return jsonify({"status": "running", "message": "Aktualizacja juz trwa..."})
    _update_state = {"running": True, "output": "Uruchamianie...", "success": None, "started": time.time()}
    t = threading.Thread(target=_run_update, daemon=True)
    t.start()
    return jsonify({"status": "started"})

@app.route("/update-status")
@login_required
def update_status():
    """Zwraca aktualny stan aktualizacji."""
    elapsed = round(time.time() - _update_state.get("started", 0))
    return jsonify({
        "running": _update_state["running"],
        "output": _update_state["output"],
        "success": _update_state["success"],
        "elapsed": elapsed
    })

@app.route("/status")
@login_required
def status():
    """JSON z aktualnym stanem monitora."""
    import datetime
    bank_files = list(BANK_DIR.glob("*.csv"))
    return jsonify({
        "monitor_exists": MONITOR_HTML.exists(),
        "monitor_size_kb": round(MONITOR_HTML.stat().st_size/1024) if MONITOR_HTML.exists() else 0,
        "bank_files": len(bank_files),
        "config_ok": CONFIG_PATH.exists(),
        "server_time": datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    print(f"Monitor Platnosci server: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
