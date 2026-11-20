#!/usr/bin/env python3
"""Hynix Loader update/catalog API. Persistence: PostgreSQL (Neon)."""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
HOST = os.environ.get("HYNIX_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", os.environ.get("HYNIX_PORT", "8787")))
ADMIN_TOKEN = os.environ.get("HYNIX_ADMIN_TOKEN", "")
MAX_BODY = 2 * 1024 * 1024
DB_LOCK = threading.RLock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("Не задан DATABASE_URL — укажи строку подключения PostgreSQL (Neon).")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=15)


@contextmanager
def db():
    con = connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return fallback


def init_db() -> None:
    with DB_LOCK, db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS mods (
            id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS game_versions (
            id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS releases (
            id SERIAL PRIMARY KEY,
            version TEXT NOT NULL,
            platform TEXT NOT NULL,
            arch TEXT NOT NULL,
            download_url TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            mandatory INTEGER NOT NULL DEFAULT 0,
            published INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(version, platform, arch)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS user_stats (
            hwid_hash TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            expires_at TEXT,
            version TEXT NOT NULL DEFAULT '',
            client_version TEXT NOT NULL DEFAULT '',
            launches INTEGER NOT NULL DEFAULT 0,
            total_play_seconds INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS license_keys (
            key TEXT PRIMARY KEY,
            plan TEXT NOT NULL,
            duration_days INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'available',
            hwid TEXT,
            username TEXT,
            used_at TEXT,
            expires_at TEXT,
            created_at TEXT NOT NULL
        )""")
        # Колонки для управления пользователями (миграция старых БД)
        rows = con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='user_stats'"
        ).fetchall()
        user_cols = {r["column_name"] for r in rows}
        for col, ddl in (("banned", "ALTER TABLE user_stats ADD COLUMN banned INTEGER NOT NULL DEFAULT 0"),
                         ("plan_locked", "ALTER TABLE user_stats ADD COLUMN plan_locked INTEGER NOT NULL DEFAULT 0")):
            if col not in user_cols:
                con.execute(ddl)
        has_mods = con.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
        if not has_mods:
            seeds = load_json(ROOT / "catalog" / "mods.json", [])
            now = utc_now()
            for item in seeds if isinstance(seeds, list) else []:
                if isinstance(item, dict) and item.get("id"):
                    con.execute("INSERT INTO mods VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                                (str(item["id"]), json.dumps(item, ensure_ascii=False), now, now))
        has_versions = con.execute("SELECT COUNT(*) AS c FROM game_versions").fetchone()["c"]
        if not has_versions:
            seeds = load_json(ROOT / "updates.json", {}).get("versions", [])
            now = utc_now()
            for item in seeds if isinstance(seeds, list) else []:
                if isinstance(item, dict) and item.get("id"):
                    con.execute("INSERT INTO game_versions VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                                (str(item["id"]), json.dumps(item, ensure_ascii=False), now, now))


def clean_mod(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Ожидается объект мода")
    mod_id = str(data.get("id", "")).strip().lower()
    project_id = str(data.get("projectId", mod_id)).strip()
    name = str(data.get("name", "")).strip()
    category = str(data.get("category", "Другое")).strip()[:48]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", mod_id):
        raise ValueError("ID мода: 2–64 символа a-z, цифры, _ или -")
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,100}", project_id):
        raise ValueError("Некорректный Modrinth project ID/slug")
    if not name or len(name) > 80:
        raise ValueError("Название обязательно и не длиннее 80 символов")
    return {
        "id": mod_id,
        "projectId": project_id,
        "name": name,
        "description": str(data.get("description", ""))[:600],
        "category": category or "Другое",
        "loader": "fabric",
        "gameVersions": data.get("gameVersions", []),
        "enabled": bool(data.get("enabled", True)),
        "downloadUrl": f"https://modrinth.com/mod/{project_id}",
    }


def clean_game_version(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Ожидается объект версии")
    version_id = str(data.get("id", "")).strip()
    mc = str(data.get("mcVersion", "")).strip()
    fabric = str(data.get("fabricVersion", "")).strip()
    java = int(data.get("javaVersion", 21))
    plan = str(data.get("requiredPlan", "free")).lower()
    mod_url = str(data.get("modUrl", "")).strip()
    if not re.fullmatch(r"[a-zA-Z0-9._-]{2,80}", version_id):
        raise ValueError("Некорректный ID версии")
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", mc):
        raise ValueError("Некорректная версия Minecraft")
    if not re.fullmatch(r"[A-Za-z0-9.+_-]{1,40}", fabric):
        raise ValueError("Некорректная версия Fabric")
    if java not in (17, 21):
        raise ValueError("Поддерживаются Java 17 и 21")
    if plan not in ("free", "premium", "beta"):
        raise ValueError("Тариф должен быть free, premium или beta")
    if not mod_url.startswith("https://") or "YOUR_MOD_URL_HERE" in mod_url:
        raise ValueError("Для клиента нужна рабочая HTTPS-ссылка на JAR")
    mod_sha1 = str(data.get("modSha1", "")).lower().strip()
    mod_size = int(data.get("modSize", 0) or 0)
    if not re.fullmatch(r"[a-f0-9]{40}", mod_sha1):
        raise ValueError("Для нового клиента обязательно укажите SHA-1 из 40 hex-символов")
    if mod_size <= 0 or mod_size > 2_000_000_000:
        raise ValueError("Для нового клиента укажите размер JAR в байтах")
    return {
        "id": version_id,
        "name": str(data.get("name", f"Hynix {mc}"))[:80],
        "mcVersion": mc,
        "fabricVersion": fabric,
        "javaVersion": java,
        "folder": str(data.get("folder", f"Hynix-{mc}"))[:80],
        "description": str(data.get("description", ""))[:500],
        "status": "online" if data.get("status", "online") == "online" else "offline",
        "modUrl": mod_url,
        "modSha1": mod_sha1,
        "modSize": mod_size,
        "requiredMods": [str(x)[:64] for x in data.get("requiredMods", []) if isinstance(x, str)][:30],
        "requiredPlan": plan,
    }


def version_tuple(value: str):
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$", value or "")
    return tuple(int(x) for x in match.groups()) if match else (0, 0, 0)


class Handler(BaseHTTPRequestHandler):
    server_version = "HynixAPI/1.0"

    def log_message(self, fmt, *args):
        print("[API] " + (fmt % args))

    def send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            raise ValueError("Пустое тело запроса или превышен лимит 2 МБ")
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Тело запроса должно быть JSON-объектом")
        return data

    def require_admin(self) -> bool:
        if len(ADMIN_TOKEN) < 32:
            self.send_json(503, {"ok": False, "error": "Задайте HYNIX_ADMIN_TOKEN длиной не менее 32 символов"})
            return False
        provided = self.headers.get("Authorization", "")
        if not provided.startswith("Bearer ") or not hmac.compare_digest(provided[7:], ADMIN_TOKEN):
            self.send_json(401, {"ok": False, "error": "Неверный токен администратора"})
            return False
        return True

    def handle_stats_ping(self, data: dict) -> None:
        hwid_hash = str(data.get("hwidHash", "")).strip().lower()
        if not re.fullmatch(r"[a-f0-9]{64}", hwid_hash):
            raise ValueError("Некорректный hwidHash")
        username = str(data.get("username", "")).strip()[:32] or "unknown"
        plan = str(data.get("plan", "free")).strip().lower()
        if plan not in ("free", "premium", "beta"):
            plan = "free"
        expires_raw = data.get("expiresAt")
        expires_at = str(expires_raw).strip()[:40] if expires_raw else None
        version = str(data.get("version", "")).strip()[:40]
        client_version = str(data.get("clientVersion", "")).strip()[:20]
        event = str(data.get("event", "heartbeat")).strip().lower()
        if event not in ("launch", "heartbeat"):
            event = "heartbeat"
        now = utc_now()
        launches_delta = 1 if event == "launch" else 0
        with DB_LOCK, db() as con:
            row = con.execute("SELECT last_seen, launches, total_play_seconds, banned, plan_locked, plan, expires_at FROM user_stats WHERE hwid_hash=%s", (hwid_hash,)).fetchone()
            play_delta = 0
            if row and event == "heartbeat":
                try:
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(row["last_seen"])).total_seconds()
                    play_delta = int(max(0, min(120, elapsed)))
                except Exception:
                    play_delta = 0
            launches = (row["launches"] if row else 0) + launches_delta
            total_play = (row["total_play_seconds"] if row else 0) + play_delta
            if row:
                banned = row["banned"]
                # Забаненный или с ручным планом: не даём heartbeat'у перезаписать тариф/подписку
                if row["plan_locked"]:
                    plan, exp = row["plan"], row["expires_at"]
                elif banned:
                    plan, exp = "free", None
                else:
                    plan, exp = plan, expires_at
                con.execute("""UPDATE user_stats SET username=%s, plan=%s, expires_at=%s, version=%s,
                               client_version=%s, launches=%s, total_play_seconds=%s, last_seen=%s
                               WHERE hwid_hash=%s""",
                            (username, plan, exp, version, client_version, launches, total_play, now, hwid_hash))
            else:
                con.execute("""INSERT INTO user_stats(hwid_hash, username, plan, expires_at, version,
                               client_version, launches, total_play_seconds, first_seen, last_seen)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (hwid_hash, username, plan, expires_at, version, client_version,
                             launches, total_play, now, now))
        banned_flag = bool(row and row["banned"])
        self.send_json(200, {"ok": True, "banned": banned_flag})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if path in ("/health", "/healthz"):
                return self.send_json(200, {"ok": True, "service": "hynix-api", "time": utc_now()})
            if path == "/api/catalog/mods":
                with DB_LOCK, db() as con:
                    rows = con.execute("SELECT data FROM mods").fetchall()
                items = [json.loads(r["data"]) for r in rows]
                items = [item for item in items if item.get("enabled", True)]
                items.sort(key=lambda item: str(item.get("name", "")).casefold())
                return self.send_json(200, {"mods": items})
            if path == "/api/catalog/versions":
                with DB_LOCK, db() as con:
                    rows = con.execute("SELECT data FROM game_versions ORDER BY created_at").fetchall()
                items = [json.loads(r["data"]) for r in rows]
                items = [item for item in items if item.get("status", "online") == "online" and str(item.get("modUrl", "")).startswith("https://") and "YOUR_MOD_URL_HERE" not in str(item.get("modUrl", ""))]
                return self.send_json(200, {"versions": items})
            if path == "/api/updates/latest":
                platform = query.get("platform", ["win32"])[0]
                arch = query.get("arch", ["x64"])[0]
                current = query.get("current", ["0.0.0"])[0]
                with DB_LOCK, db() as con:
                    rows = con.execute("SELECT * FROM releases WHERE platform=%s AND arch=%s AND published=1", (platform, arch)).fetchall()
                candidates = [r for r in rows if version_tuple(r["version"]) > version_tuple(current)]
                if not candidates:
                    return self.send_json(200, {"available": False, "current": current})
                release = max(candidates, key=lambda r: version_tuple(r["version"]))
                return self.send_json(200, {
                    "available": True, "version": release["version"], "platform": release["platform"],
                    "arch": release["arch"], "downloadUrl": release["download_url"],
                    "sha256": release["sha256"], "notes": release["notes"],
                    "mandatory": bool(release["mandatory"]), "publishedAt": release["created_at"],
                })
            if path == "/api/admin/stats":
                if not self.require_admin(): return
                with DB_LOCK, db() as con:
                    counts = {table: con.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
                              for table in ("mods", "game_versions", "releases")}
                    counts["publishedReleases"] = con.execute("SELECT COUNT(*) AS c FROM releases WHERE published=1").fetchone()["c"]
                return self.send_json(200, {"ok": True, **counts})
            if path == "/api/admin/mods":
                if not self.require_admin(): return
                with DB_LOCK, db() as con:
                    rows = con.execute("SELECT data FROM mods ORDER BY created_at DESC").fetchall()
                return self.send_json(200, {"mods": [json.loads(r["data"]) for r in rows]})
            if path == "/api/admin/versions":
                if not self.require_admin(): return
                with DB_LOCK, db() as con:
                    rows = con.execute("SELECT data FROM game_versions ORDER BY created_at DESC").fetchall()
                # The public catalog is filtered, but administrators must be able
                # to see and repair offline or draft entries too.
                return self.send_json(200, {"versions": [json.loads(r["data"]) for r in rows]})
            if path == "/api/stats/online":
                now = datetime.now(timezone.utc)
                online = 0
                with DB_LOCK, db() as con:
                    rows = con.execute("SELECT last_seen FROM user_stats").fetchall()
                for r in rows:
                    try:
                        if (now - datetime.fromisoformat(r["last_seen"])).total_seconds() < 300:
                            online += 1
                    except Exception:
                        pass
                return self.send_json(200, {"online": online})
            if path == "/api/admin/keys":
                if not self.require_admin(): return
                with DB_LOCK, db() as con:
                    rows = con.execute("SELECT * FROM license_keys ORDER BY created_at DESC").fetchall()
                return self.send_json(200, {"keys": [dict(r) for r in rows]})
            if path == "/api/admin/users":
                if not self.require_admin(): return
                with DB_LOCK, db() as con:
                    rows = con.execute("SELECT * FROM user_stats ORDER BY last_seen DESC").fetchall()
                return self.send_json(200, {"users": [dict(r) for r in rows]})
            if path == "/api/admin/releases":
                if not self.require_admin(): return
                with DB_LOCK, db() as con:
                    rows = con.execute("SELECT * FROM releases ORDER BY created_at DESC").fetchall()
                return self.send_json(200, {"releases": [dict(r) for r in rows]})
            return self.send_json(404, {"ok": False, "error": "Маршрут не найден"})
        except Exception as exc:
            return self.send_json(500, {"ok": False, "error": str(exc)})

    def handle_key_redeem(self, data: dict) -> None:
        key = str(data.get("key", "")).strip()
        hwid = str(data.get("hwid", "")).strip()[:128]
        username = str(data.get("username", "")).strip()[:32]
        if not key or len(key) > 100 or not hwid:
            raise ValueError("Укажи ключ и HWID")
        now = utc_now()
        with DB_LOCK, db() as con:
            row = con.execute("SELECT * FROM license_keys WHERE key=%s", (key,)).fetchone()
            if not row:
                return self.send_json(404, {"ok": False, "error": "Ключ не найден"})
            if row["status"] == "banned":
                return self.send_json(403, {"ok": False, "error": "Ключ заблокирован"})
            if row["status"] == "used":
                if row["hwid"] and row["hwid"] != hwid:
                    return self.send_json(403, {"ok": False, "error": "Ключ уже активирован на другом устройстве"})
                return self.send_json(200, {"ok": True, "key": key, "plan": row["plan"], "expiresAt": row["expires_at"]})
            expires = (datetime.now(timezone.utc) + timedelta(days=int(row["duration_days"]))).isoformat()
            con.execute("""UPDATE license_keys SET status='used', hwid=%s, username=%s, used_at=%s, expires_at=%s
                           WHERE key=%s""",
                        (hwid, username or None, now, expires, key))
        return self.send_json(200, {"ok": True, "key": key, "plan": row["plan"], "expiresAt": expires})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/stats/ping":
            try:
                data = self.read_json()
                self.handle_stats_ping(data)
            except (ValueError, json.JSONDecodeError) as exc:
                return self.send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                return self.send_json(500, {"ok": False, "error": str(exc)})
            return
        if path == "/api/keys/redeem":
            try:
                data = self.read_json()
                self.handle_key_redeem(data)
            except (ValueError, json.JSONDecodeError) as exc:
                return self.send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                return self.send_json(500, {"ok": False, "error": str(exc)})
            return
        if not self.require_admin(): return
        try:
            data = self.read_json()
            now = utc_now()
            if path == "/api/admin/mods":
                item = clean_mod(data)
                with DB_LOCK, db() as con:
                    con.execute("INSERT INTO mods VALUES (%s, %s, %s, %s) ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                                (item["id"], json.dumps(item, ensure_ascii=False), now, now))
                return self.send_json(200, {"ok": True, "mod": item})
            if path == "/api/admin/versions":
                item = clean_game_version(data)
                with DB_LOCK, db() as con:
                    con.execute("INSERT INTO game_versions VALUES (%s, %s, %s, %s) ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                                (item["id"], json.dumps(item, ensure_ascii=False), now, now))
                return self.send_json(200, {"ok": True, "version": item})
            if path == "/api/admin/releases":
                version = str(data.get("version", "")).strip()
                platform = str(data.get("platform", "win32"))
                arch = str(data.get("arch", "x64"))
                url = str(data.get("downloadUrl", "")).strip()
                sha256 = str(data.get("sha256", "")).lower().strip()
                if not re.fullmatch(r"\d+\.\d+\.\d+", version): raise ValueError("Версия должна иметь формат X.Y.Z")
                if platform != "win32" or arch != "x64": raise ValueError("Пока поддерживается только Windows x64")
                if not url.startswith("https://"): raise ValueError("Ссылка на установщик должна использовать HTTPS")
                if not re.fullmatch(r"[a-f0-9]{64}", sha256): raise ValueError("Нужен SHA-256 из 64 hex-символов")
                with DB_LOCK, db() as con:
                    con.execute("""INSERT INTO releases(version,platform,arch,download_url,sha256,notes,mandatory,published,created_at)
                                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                  ON CONFLICT(version,platform,arch) DO UPDATE SET
                                    download_url=excluded.download_url, sha256=excluded.sha256,
                                    notes=excluded.notes, mandatory=excluded.mandatory,
                                    published=excluded.published, created_at=excluded.created_at""",
                                (version, platform, arch, url, sha256, str(data.get("notes", ""))[:4000],
                                 1 if data.get("mandatory") else 0, 1 if data.get("published", True) else 0, now))
                return self.send_json(200, {"ok": True, "version": version, "saved": True})
            if path == "/api/admin/users/ban":
                username = str(data.get("username", "")).strip()
                if not username:
                    raise ValueError("Укажи username")
                with DB_LOCK, db() as con:
                    cur = con.execute("UPDATE user_stats SET banned=1, plan='free', expires_at=NULL WHERE LOWER(username)=LOWER(%s)", (username,))
                return self.send_json(200, {"ok": True, "updated": cur.rowcount})
            if path == "/api/admin/users/unban":
                username = str(data.get("username", "")).strip()
                if not username:
                    raise ValueError("Укажи username")
                with DB_LOCK, db() as con:
                    cur = con.execute("UPDATE user_stats SET banned=0 WHERE LOWER(username)=LOWER(%s)", (username,))
                return self.send_json(200, {"ok": True, "updated": cur.rowcount})
            if path == "/api/admin/users/setplan":
                username = str(data.get("username", "")).strip()
                plan = str(data.get("plan", "")).strip().lower()
                if not username:
                    raise ValueError("Укажи username")
                if plan not in ("free", "premium", "beta"):
                    raise ValueError("План: free, premium или beta")
                with DB_LOCK, db() as con:
                    cur = con.execute("UPDATE user_stats SET plan=%s, plan_locked=1 WHERE LOWER(username)=LOWER(%s)", (plan, username))
                return self.send_json(200, {"ok": True, "updated": cur.rowcount})
            if path == "/api/admin/users/extend":
                username = str(data.get("username", "")).strip()
                try:
                    days = int(data.get("days", 0))
                except (TypeError, ValueError):
                    days = 0
                if not username:
                    raise ValueError("Укажи username")
                if not 1 <= days <= 3650:
                    raise ValueError("Дни: от 1 до 3650")
                with DB_LOCK, db() as con:
                    rows = con.execute("SELECT hwid_hash, expires_at FROM user_stats WHERE LOWER(username)=LOWER(%s)", (username,)).fetchall()
                    updated = 0
                    for r in rows:
                        base = datetime.now(timezone.utc)
                        try:
                            current = datetime.fromisoformat(str(r["expires_at"]).replace("Z", "+00:00")) if r["expires_at"] else base
                            if current < base:
                                current = base
                        except Exception:
                            current = base
                        new_exp = (current + timedelta(days=days)).isoformat()
                        con.execute("UPDATE user_stats SET expires_at=%s, plan_locked=1 WHERE hwid_hash=%s", (new_exp, r["hwid_hash"]))
                        updated += 1
                return self.send_json(200, {"ok": True, "updated": updated})
            if path == "/api/admin/keys":
                plan = str(data.get("plan", "")).strip().lower()
                if plan not in ("premium", "beta"):
                    raise ValueError("План ключа: premium или beta")
                try:
                    duration_days = int(data.get("durationDays", 0))
                except (TypeError, ValueError):
                    duration_days = 0
                if not 1 <= duration_days <= 3650:
                    raise ValueError("Срок ключа: от 1 до 3650 дней")
                key = str(data.get("key", "")).strip() or f"HYNIX-{secrets.token_hex(4).upper()}-{secrets.token_hex(8).upper()}"
                if not re.fullmatch(r"[A-Za-z0-9-]{8,100}", key):
                    raise ValueError("Некорректный формат ключа")
                with DB_LOCK, db() as con:
                    con.execute("""INSERT INTO license_keys(key, plan, duration_days, status, created_at)
                                   VALUES(%s, %s, %s, 'available', %s)
                                   ON CONFLICT(key) DO UPDATE SET plan=excluded.plan,
                                     duration_days=excluded.duration_days, status='available'""",
                                (key, plan, duration_days, now))
                return self.send_json(200, {"ok": True, "key": key, "plan": plan, "durationDays": duration_days})
            return self.send_json(404, {"ok": False, "error": "Маршрут не найден"})
        except psycopg.errors.UniqueViolation:
            return self.send_json(409, {"ok": False, "error": "Такая версия уже опубликована для этой платформы"})
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            return self.send_json(500, {"ok": False, "error": str(exc)})

    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip("/")
        if not self.require_admin(): return
        parts = path.split("/")
        try:
            with DB_LOCK, db() as con:
                if len(parts) == 5 and parts[3] == "mods":
                    cur = con.execute("DELETE FROM mods WHERE id=%s", (parts[4],))
                elif len(parts) == 5 and parts[3] == "versions":
                    cur = con.execute("DELETE FROM game_versions WHERE id=%s", (parts[4],))
                elif len(parts) == 5 and parts[3] == "releases":
                    cur = con.execute("DELETE FROM releases WHERE id=%s", (parts[4],))
                elif len(parts) == 5 and parts[3] == "keys":
                    cur = con.execute("UPDATE license_keys SET status='banned' WHERE key=%s", (parts[4],))
                else:
                    return self.send_json(404, {"ok": False, "error": "Маршрут не найден"})
            return self.send_json(200, {"ok": True, "deleted": cur.rowcount > 0})
        except Exception as exc:
            return self.send_json(500, {"ok": False, "error": str(exc)})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()


def run_embedded_bot() -> None:
    """Telegram-бот в отдельном потоке — чтобы всё жило в одном web-сервисе (Render free не даёт workers)."""
    import telegram_bot

    token = os.environ.get("BOT_TOKEN") or os.environ.get("HYNIX_TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("ADMIN_CHAT_ID") or os.environ.get("HYNIX_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[BOT] BOT_TOKEN/ADMIN_CHAT_ID не заданы — бот отключён")
        return
    # Бот ходит в API по localhost — внешний URL сервиса неизвестен заранее
    if not os.environ.get("HYNIX_API_URL"):
        os.environ["HYNIX_API_URL"] = f"http://127.0.0.1:{PORT}"
    bot = telegram_bot.HynixTelegramBot(token, chat_id)
    print(f"[BOT] Telegram bot started, admin chat: {chat_id}")
    try:
        bot.send_message(chat_id, "🤖 <b>Hynix Bot запущен</b>\n\n/api-сервер и бот работают в одном сервисе.")
    except Exception as exc:
        print(f"[BOT] welcome message failed: {exc}")
    offset = 0
    while True:
        try:
            updates = bot.get_updates(offset=offset, timeout=30)
            if updates.get("ok"):
                for update in updates.get("result", []):
                    offset = update.get("update_id", 0) + 1
                    if "message" in update:
                        bot.process_message(update["message"])
                    elif "callback_query" in update:
                        bot.handle_callback(update["callback_query"])
        except Exception as exc:
            print(f"[BOT] polling error: {exc}")
            time.sleep(5)
        time.sleep(1)


def main():
    if not DATABASE_URL:
        print("[FATAL] Не задан DATABASE_URL. Добавь строку подключения PostgreSQL (Neon) в переменные окружения.")
        sys.exit(1)
    init_db()
    if not ADMIN_TOKEN or len(ADMIN_TOKEN) < 32:
        print("[WARN] HYNIX_ADMIN_TOKEN не задан или короче 32 символов. Админ-эндпоинты не будут доступны.")
    if (os.environ.get("BOT_TOKEN") or os.environ.get("HYNIX_TELEGRAM_BOT_TOKEN")) and \
       (os.environ.get("ADMIN_CHAT_ID") or os.environ.get("HYNIX_TELEGRAM_CHAT_ID")):
        sys.path.insert(0, str(ROOT))
        threading.Thread(target=run_embedded_bot, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Hynix API listening on {HOST}:{PORT}; database: PostgreSQL")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
