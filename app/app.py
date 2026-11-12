"""林道巡护倒木采集系统（Flask + SQLite）

- /            采集页（巡护员）：离线可用，待传记录存浏览器 IndexedDB
- /records     清障队查看页：只展示已确认入库的记录
- /api/records GET 列表 / POST 补传（uuid 幂等，同一笔只入库一次）
- /photos/...  已入库的原照
- /sw.js       Service Worker（按 APP_VERSION 渲染，版本与页面同源）

换版/故障演示开关（均通过环境变量，不改业务代码）：
- APP_VERSION          当前部署版本号，注入页面与 SW 缓存名
- SHELL_FAIL_PATHS     逗号分隔的路径，命中即返回 503（例：/static/app.js），
                       用来模拟“下载途中服务换版 / 文件没取齐”
- SHELL_FAIL_RATE      外壳请求随机 503 的概率（0~1，默认 0）
"""
import os
import random
import sqlite3
import uuid as uuidlib
from datetime import datetime, timezone

from flask import (Flask, Response, g, jsonify, render_template, request,
                   send_from_directory)
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
DB_PATH = os.path.join(DATA_DIR, "records.db")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# 当前部署版本：页面徽标与 SW 缓存名共用，换版时由部署环境覆盖
APP_VERSION = os.environ.get("APP_VERSION", "1").strip() or "1"
# 演示用故障注入：命中这些外壳路径直接 503
SHELL_FAIL_PATHS = {
    p.strip() for p in os.environ.get("SHELL_FAIL_PATHS", "").split(",") if p.strip()
}
try:
    SHELL_FAIL_RATE = min(1.0, max(0.0, float(os.environ.get("SHELL_FAIL_RATE", "0"))))
except ValueError:
    SHELL_FAIL_RATE = 0.0

# 受故障注入影响的“外壳”路径（与 SW 的 SHELL 清单一致）
SHELL_PATHS = {
    "/",
    "/static/app.js",
    "/static/style.css",
    "/static/sample-photo.png",
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # 原照最大 30MB

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid        TEXT NOT NULL UNIQUE,   -- 浏览器生成的幂等键
    lat         REAL NOT NULL,
    lng         REAL NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    photo       TEXT NOT NULL,          -- photos 目录下的文件名
    created_at  TEXT NOT NULL,          -- 现场采集时间（浏览器上报）
    uploaded_at TEXT NOT NULL           -- 入库时间（服务器）
);
"""


def init_db():
    os.makedirs(PHOTO_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.after_request
def cache_headers(resp):
    # HTML 页面不做 HTTP 缓存，在线刷新可探测新版本；离线可用性由 SW 负责
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    elif request.path.startswith("/static/"):
        # 静态资源：允许浏览器缓存但必须每次回源校验，配合版本化 SW 缓存避免混版
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.before_request
def inject_shell_failure():
    """模拟换版途中的 503：仅作用于外壳资源，不影响补传 API 与已入库照片。"""
    path = request.path
    if request.method != "GET" or path not in SHELL_PATHS:
        return None
    if path in SHELL_FAIL_PATHS or (
        SHELL_FAIL_RATE > 0 and random.random() < SHELL_FAIL_RATE
    ):
        return Response(
            "Service Unavailable (simulated deploy failure on %s)" % path,
            status=503,
            mimetype="text/plain",
        )
    return None


# ---------------- 页面 ----------------

@app.route("/")
def index():
    return render_template("index.html", app_version=APP_VERSION)


@app.route("/sw.js")
def service_worker():
    # SW 模板渲染：APP_VERSION 决定缓存名；SW 本身不注入故障，
    # 保证浏览器始终能完成“探测新版本”这一步，失败只发生在外壳下载阶段。
    resp = Response(
        render_template("sw.js", app_version=APP_VERSION),
        mimetype="application/javascript",
    )
    # 每次都回源校验，确保浏览器能及时发现新版 SW
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/version")
def version_info():
    """演示辅助：查看当前服务版本与故障开关（不进 SW 缓存清单）。"""
    return jsonify({
        "app_version": APP_VERSION,
        "shell_fail_paths": sorted(SHELL_FAIL_PATHS),
        "shell_fail_rate": SHELL_FAIL_RATE,
    })


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.route("/records")
def records_page():
    rows = get_db().execute(
        "SELECT * FROM records ORDER BY id DESC"
    ).fetchall()
    return render_template("records.html", records=rows, app_version=APP_VERSION)


# ---------------- API ----------------

@app.route("/api/records", methods=["GET"])
def api_list():
    rows = get_db().execute(
        "SELECT id, uuid, lat, lng, note, photo, created_at, uploaded_at "
        "FROM records ORDER BY id DESC"
    ).fetchall()
    return jsonify([
        {
            "id": r["id"],
            "uuid": r["uuid"],
            "lat": r["lat"],
            "lng": r["lng"],
            "note": r["note"],
            "photo_url": "/photos/" + r["photo"],
            "created_at": r["created_at"],
            "uploaded_at": r["uploaded_at"],
        }
        for r in rows
    ])


@app.route("/api/records", methods=["POST"])
def api_create():
    # ---- 校验 ----
    client_uuid = (request.form.get("uuid") or "").strip()
    try:
        client_uuid = str(uuidlib.UUID(client_uuid))  # 规范化
    except (ValueError, AttributeError):
        return jsonify({"ok": False, "error": "uuid 缺失或格式非法"}), 400

    try:
        lat = float(request.form.get("lat", ""))
        lng = float(request.form.get("lng", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "经纬度缺失或非法"}), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"ok": False, "error": "经纬度超出范围"}), 400

    note = (request.form.get("note") or "").strip()[:500]
    created_at = (request.form.get("created_at") or "").strip() or \
        datetime.now(timezone.utc).isoformat()

    photo = request.files.get("photo")
    if photo is None or not photo.filename:
        return jsonify({"ok": False, "error": "缺少现场照片"}), 400

    db = get_db()

    # ---- 幂等：已入库则直接确认，不重复写入 ----
    existing = db.execute(
        "SELECT id FROM records WHERE uuid = ?", (client_uuid,)
    ).fetchone()
    if existing:
        return jsonify({"ok": True, "duplicate": True, "id": existing["id"]})

    # ---- 保存原照（文件名用 uuid，天然去重）----
    ext = os.path.splitext(secure_filename(photo.filename))[1].lower()
    if ext not in ALLOWED_EXT:
        ext = ".jpg"
    fname = client_uuid + ext
    photo.save(os.path.join(PHOTO_DIR, fname))

    # ---- 入库；唯一约束兜底并发/重试竞争 ----
    try:
        cur = db.execute(
            "INSERT INTO records (uuid, lat, lng, note, photo, created_at, uploaded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (client_uuid, lat, lng, note, fname, created_at,
             datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        try:
            os.remove(os.path.join(PHOTO_DIR, fname))
        except OSError:
            pass
        row = db.execute(
            "SELECT id FROM records WHERE uuid = ?", (client_uuid,)
        ).fetchone()
        return jsonify({"ok": True, "duplicate": True,
                        "id": row["id"] if row else None})

    return jsonify({"ok": True, "duplicate": False, "id": cur.lastrowid})


@app.route("/photos/<path:name>")
def photo_file(name):
    return send_from_directory(PHOTO_DIR, name)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
