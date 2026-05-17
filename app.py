import os
import sqlite3
import time
import math
import threading
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

APP_NAME = "Torn Stock Watcher"
DB_PATH = os.environ.get("DB_PATH", "stock_watcher.db")
TORN_API_BASE = os.environ.get("TORN_API_BASE", "https://api.torn.com")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "20"))

# Assumption: Torn API v1 has stocks under torn/?selections=stocks.
# If Torn changes the endpoint/selection, set TORN_STOCKS_PATH in Render env.
TORN_STOCKS_PATH = os.environ.get("TORN_STOCKS_PATH", "/torn/?selections=stocks&key={key}")

# For automatic backend snapshots.
# Put a LIMITED Torn API key in Render env as TORN_API_KEY.
TORN_API_KEY = os.environ.get("TORN_API_KEY", "").strip()
AUTO_SNAPSHOT_ENABLED = os.environ.get("AUTO_SNAPSHOT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
AUTO_SNAPSHOT_SECONDS = int(os.environ.get("AUTO_SNAPSHOT_SECONDS", "900"))  # 15 minutes default
AUTO_SNAPSHOT_SAVE_UNCHANGED = os.environ.get("AUTO_SNAPSHOT_SAVE_UNCHANGED", "false").lower() in ("1", "true", "yes", "on")

AUTO_STATE = {
    "started": False,
    "last_check_ts": None,
    "last_saved_ts": None,
    "last_changed": False,
    "last_message": "Auto watcher not started yet.",
    "last_error": None
}

app = Flask(__name__)
CORS(app)


def now_ts() -> int:
    return int(time.time())


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            stock_id TEXT NOT NULL,
            acronym TEXT NOT NULL,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            raw_json TEXT
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_stock_ts ON snapshots(stock_id, ts)")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            stock_id TEXT NOT NULL,
            acronym TEXT NOT NULL,
            buy_price REAL NOT NULL,
            target_price REAL NOT NULL,
            stop_loss_price REAL NOT NULL,
            score REAL NOT NULL,
            risk TEXT NOT NULL,
            horizon_hours INTEGER NOT NULL DEFAULT 24,
            checked INTEGER NOT NULL DEFAULT 0,
            outcome TEXT,
            max_price REAL,
            min_price REAL,
            final_price REAL,
            profit_pct REAL
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_predictions_ts ON predictions(ts)")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS learned_weights (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """)
        # Default weights. These change slowly as the app learns from saved outcomes.
        defaults = {
            "w_1h": 2.0,
            "w_6h": 1.25,
            "w_24h": 0.75,
            "w_recovery": 0.50,
            "w_volatility": -0.35
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO learned_weights (key, value) VALUES (?, ?)", (k, v))


def fetch_torn_stocks(api_key: str):
    path = TORN_STOCKS_PATH.format(key=api_key)
    url = TORN_API_BASE.rstrip("/") + path
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(data["error"])

    # Torn stock shape can vary by API/version. This parser accepts common forms.
    stocks = data.get("stocks") if isinstance(data, dict) else None
    if stocks is None and isinstance(data, dict):
        stocks = data

    parsed = []
    if isinstance(stocks, dict):
        iterable = stocks.items()
    elif isinstance(stocks, list):
        iterable = enumerate(stocks)
    else:
        raise RuntimeError("Could not find stocks in Torn API response.")

    for sid, item in iterable:
        if not isinstance(item, dict):
            continue

        acronym = str(
            item.get("acronym")
            or item.get("ticker")
            or item.get("symbol")
            or item.get("name")
            or sid
        ).upper()

        name = str(item.get("name") or acronym)
        price = item.get("current_price", item.get("price", item.get("market_price")))

        try:
            price = float(price)
        except Exception:
            continue

        parsed.append({
            "stock_id": str(sid),
            "acronym": acronym,
            "name": name,
            "price": price,
            "raw": item
        })

    if not parsed:
        raise RuntimeError("No valid stock prices were parsed from Torn API.")

    return parsed


def save_snapshot(stocks):
    ts = now_ts()
    with db() as conn:
        for s in stocks:
            conn.execute("""
                INSERT INTO snapshots (ts, stock_id, acronym, name, price, raw_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (ts, s["stock_id"], s["acronym"], s["name"], s["price"], json.dumps(s["raw"])))
    return ts


def latest_price_map():
    """
    Returns the latest saved price per stock_id.
    """
    init_db()
    with db() as conn:
        rows = conn.execute("""
            SELECT s1.stock_id, s1.price
            FROM snapshots s1
            INNER JOIN (
                SELECT stock_id, MAX(ts) AS max_ts
                FROM snapshots
                GROUP BY stock_id
            ) s2 ON s1.stock_id = s2.stock_id AND s1.ts = s2.max_ts
        """).fetchall()
    return {str(r["stock_id"]): float(r["price"]) for r in rows}


def stocks_changed(new_stocks):
    """
    True when there are no prior snapshots, a new stock appears, or any stock price changed.
    """
    latest = latest_price_map()
    if not latest:
        return True

    for s in new_stocks:
        sid = str(s["stock_id"])
        price = float(s["price"])
        old = latest.get(sid)
        if old is None:
            return True
        if abs(old - price) > 0.000001:
            return True

    return False


def auto_snapshot_once():
    """
    Fetches Torn stocks and saves only if a price changed,
    unless AUTO_SNAPSHOT_SAVE_UNCHANGED=true.
    """
    init_db()
    AUTO_STATE["last_check_ts"] = now_ts()

    if not TORN_API_KEY:
        AUTO_STATE["last_error"] = "Missing Render env TORN_API_KEY."
        AUTO_STATE["last_message"] = "Auto snapshot skipped because TORN_API_KEY is not set."
        return {"ok": False, "saved": 0, "changed": False, "error": AUTO_STATE["last_error"]}

    stocks = fetch_torn_stocks(TORN_API_KEY)
    changed = stocks_changed(stocks)

    if changed or AUTO_SNAPSHOT_SAVE_UNCHANGED:
        ts = save_snapshot(stocks)
        AUTO_STATE["last_saved_ts"] = ts
        AUTO_STATE["last_changed"] = changed
        AUTO_STATE["last_error"] = None
        AUTO_STATE["last_message"] = f"Saved {len(stocks)} stock prices. Changed={changed}."
        return {"ok": True, "saved": len(stocks), "changed": changed, "ts": ts}

    AUTO_STATE["last_changed"] = False
    AUTO_STATE["last_error"] = None
    AUTO_STATE["last_message"] = "Checked stocks. No price changes, so no snapshot saved."
    return {"ok": True, "saved": 0, "changed": False, "ts": None}


def auto_snapshot_loop():
    AUTO_STATE["started"] = True
    AUTO_STATE["last_message"] = "Auto watcher running."
    while True:
        try:
            if AUTO_SNAPSHOT_ENABLED:
                auto_snapshot_once()
        except Exception as e:
            AUTO_STATE["last_error"] = str(e)
            AUTO_STATE["last_message"] = f"Auto snapshot error: {e}"
        time.sleep(max(60, AUTO_SNAPSHOT_SECONDS))


def start_auto_watcher():
    if AUTO_STATE["started"]:
        return
    t = threading.Thread(target=auto_snapshot_loop, daemon=True)
    t.start()


def pct_change(old, new):
    if not old or old <= 0:
        return 0.0
    return ((new - old) / old) * 100.0


def get_weights():
    init_db()
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM learned_weights").fetchall()
    weights = {r["key"]: float(r["value"]) for r in rows}
    return {
        "w_1h": weights.get("w_1h", 2.0),
        "w_6h": weights.get("w_6h", 1.25),
        "w_24h": weights.get("w_24h", 0.75),
        "w_recovery": weights.get("w_recovery", 0.50),
        "w_volatility": weights.get("w_volatility", -0.35),
    }


def set_weights(weights):
    with db() as conn:
        for k, v in weights.items():
            conn.execute("INSERT OR REPLACE INTO learned_weights (key, value) VALUES (?, ?)", (k, float(v)))


def clamp(n, lo, hi):
    return max(lo, min(hi, n))


def get_window_price(rows, cutoff_ts):
    older = [r for r in rows if r["ts"] <= cutoff_ts]
    if older:
        return older[-1]["price"]
    return rows[0]["price"] if rows else None


def stock_analysis(hours=24):
    since = now_ts() - (hours * 3600)
    with db() as conn:
        rows = conn.execute("""
            SELECT * FROM snapshots
            WHERE ts >= ?
            ORDER BY stock_id ASC, ts ASC
        """, (since,)).fetchall()

    by_stock = {}
    for r in rows:
        by_stock.setdefault(r["stock_id"], []).append(r)

    results = []
    current_ts = now_ts()

    for stock_id, rs in by_stock.items():
        if len(rs) < 2:
            continue

        latest = rs[-1]
        current = float(latest["price"])

        p1h = get_window_price(rs, current_ts - 3600)
        p6h = get_window_price(rs, current_ts - 6 * 3600)
        p24h = rs[0]["price"]

        prices = [float(x["price"]) for x in rs]
        low = min(prices)
        high = max(prices)

        change_1h = pct_change(p1h, current)
        change_6h = pct_change(p6h, current)
        change_24h = pct_change(p24h, current)

        volatility = pct_change(low, high)
        dip_recovery = pct_change(low, current)

        # Self-learning MVP scoring:
        # The starting weights are sensible defaults. /api/learn adjusts them slowly
        # based on whether previous picks hit their target, hit stop-loss, or went flat.
        weights = get_weights()
        score = (
            change_1h * weights["w_1h"]
            + change_6h * weights["w_6h"]
            + change_24h * weights["w_24h"]
            + dip_recovery * weights["w_recovery"]
            + volatility * weights["w_volatility"]
        )

        if volatility < 2:
            risk = "Low"
        elif volatility < 5:
            risk = "Medium"
        else:
            risk = "High"

        # targets are cautious estimates, not guarantees
        target_pct = max(0.75, min(4.5, (score / 12.0) + 1.25))
        stop_loss_pct = max(0.75, min(3.0, volatility / 3.0))

        target_price = current * (1 + target_pct / 100)
        stop_loss_price = current * (1 - stop_loss_pct / 100)

        results.append({
            "stock_id": stock_id,
            "acronym": latest["acronym"],
            "name": latest["name"],
            "current_price": round(current, 4),
            "low_24h": round(low, 4),
            "high_24h": round(high, 4),
            "change_1h": round(change_1h, 3),
            "change_6h": round(change_6h, 3),
            "change_24h": round(change_24h, 3),
            "volatility": round(volatility, 3),
            "score": round(score, 3),
            "risk": risk,
            "target_price": round(target_price, 4),
            "target_pct": round(target_pct, 3),
            "stop_loss_price": round(stop_loss_price, 4),
            "stop_loss_pct": round(stop_loss_pct, 3),
            "sample_count": len(rs),
            "last_seen": latest["ts"]
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


@app.route("/")
def home():
    return jsonify({
        "app": APP_NAME,
        "ok": True,
        "endpoints": [
            "/api/health",
            "/api/snapshot",
            "/api/stocks",
            "/api/pick",
            "/api/simulate",
            "/api/record_prediction",
            "/api/check_predictions",
            "/api/learn",
            "/api/learning_status",
            "/api/auto_status",
            "/api/auto_snapshot_once",
            "/static/torn-stock-watcher.user.js"
        ]
    })


@app.route("/api/health")
def health():
    init_db()
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"]
        latest = conn.execute("SELECT MAX(ts) t FROM snapshots").fetchone()["t"]
    return jsonify({"ok": True, "snapshots": count, "latest_snapshot": latest})


@app.route("/api/snapshot", methods=["POST"])
def snapshot():
    init_db()
    data = request.get_json(force=True, silent=True) or {}
    api_key = data.get("api_key", "").strip()
    if not api_key:
        return jsonify({"ok": False, "error": "Missing api_key"}), 400

    try:
        stocks = fetch_torn_stocks(api_key)
        ts = save_snapshot(stocks)
        return jsonify({"ok": True, "saved": len(stocks), "ts": ts})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/stocks")
def stocks():
    init_db()
    return jsonify({"ok": True, "items": stock_analysis()})


@app.route("/api/pick")
def pick():
    init_db()
    items = stock_analysis()
    best = items[0] if items else None
    return jsonify({
        "ok": True,
        "pick": best,
        "message": "Prediction is a scoring estimate, not guaranteed profit."
    })


@app.route("/api/simulate")
def simulate():
    init_db()
    amount = float(request.args.get("amount", "0") or 0)
    stock = request.args.get("stock", "").upper().strip()
    items = stock_analysis()

    if stock:
        candidates = [x for x in items if x["acronym"].upper() == stock or x["stock_id"] == stock]
    else:
        candidates = items[:1]

    if amount <= 0:
        return jsonify({"ok": False, "error": "Amount must be above 0."}), 400

    if not candidates:
        return jsonify({"ok": False, "error": "No stock analysis available yet. Record more snapshots first."}), 404

    x = candidates[0]
    current = x["current_price"]
    target = x["target_price"]
    stop = x["stop_loss_price"]

    shares = math.floor(amount / current) if current > 0 else 0
    spent = shares * current
    gross_target = shares * target
    gross_stop = shares * stop

    return jsonify({
        "ok": True,
        "stock": x,
        "amount_input": round(amount, 2),
        "shares": shares,
        "estimated_spent": round(spent, 2),
        "predicted_total": round(gross_target, 2),
        "predicted_profit": round(gross_target - spent, 2),
        "predicted_roi_pct": round(pct_change(spent, gross_target), 3),
        "possible_stop_loss_total": round(gross_stop, 2),
        "possible_loss": round(gross_stop - spent, 2),
        "warning": "This is only correct if the prediction target is reached."
    })


@app.route("/api/record_prediction", methods=["POST"])
def record_prediction():
    """
    Saves the current best pick as a prediction to check later.
    This is useful for daily learning/backtesting.
    """
    init_db()
    items = stock_analysis()
    if not items:
        return jsonify({"ok": False, "error": "No analysis available yet."}), 404

    x = items[0]
    with db() as conn:
        conn.execute("""
            INSERT INTO predictions
            (ts, stock_id, acronym, buy_price, target_price, stop_loss_price, score, risk, horizon_hours)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now_ts(),
            x["stock_id"],
            x["acronym"],
            x["current_price"],
            x["target_price"],
            x["stop_loss_price"],
            x["score"],
            x["risk"],
            24
        ))

    return jsonify({"ok": True, "recorded": x})


@app.route("/api/check_predictions", methods=["POST", "GET"])
def check_predictions():
    """
    Checks old predictions against recorded price movement.
    Outcome types:
    - target_hit
    - stop_loss_hit
    - profit
    - loss
    - flat
    """
    init_db()
    cutoff = now_ts() - 24 * 3600

    with db() as conn:
        preds = conn.execute("""
            SELECT * FROM predictions
            WHERE checked = 0 AND ts <= ?
            ORDER BY ts ASC
            LIMIT 100
        """, (cutoff,)).fetchall()

        checked = 0
        for p in preds:
            rows = conn.execute("""
                SELECT price, ts FROM snapshots
                WHERE stock_id = ? AND ts >= ? AND ts <= ?
                ORDER BY ts ASC
            """, (p["stock_id"], p["ts"], p["ts"] + p["horizon_hours"] * 3600)).fetchall()

            if not rows:
                continue

            prices = [float(r["price"]) for r in rows]
            max_price = max(prices)
            min_price = min(prices)
            final_price = prices[-1]
            buy_price = float(p["buy_price"])
            target = float(p["target_price"])
            stop = float(p["stop_loss_price"])

            profit_pct = pct_change(buy_price, final_price)

            if max_price >= target:
                outcome = "target_hit"
                profit_pct = pct_change(buy_price, target)
            elif min_price <= stop:
                outcome = "stop_loss_hit"
                profit_pct = pct_change(buy_price, stop)
            elif profit_pct > 0.25:
                outcome = "profit"
            elif profit_pct < -0.25:
                outcome = "loss"
            else:
                outcome = "flat"

            conn.execute("""
                UPDATE predictions
                SET checked = 1, outcome = ?, max_price = ?, min_price = ?, final_price = ?, profit_pct = ?
                WHERE id = ?
            """, (outcome, max_price, min_price, final_price, profit_pct, p["id"]))
            checked += 1

    return jsonify({"ok": True, "checked": checked})


@app.route("/api/learn", methods=["POST", "GET"])
def learn():
    """
    Slowly adjusts scoring weights from checked prediction outcomes.

    This is intentionally conservative:
    - It does not overfit after one lucky day.
    - It rewards signals that were present in winning predictions.
    - It slightly reduces risk appetite if stop-loss hits often.
    """
    init_db()
    with db() as conn:
        preds = conn.execute("""
            SELECT * FROM predictions
            WHERE checked = 1
            ORDER BY ts DESC
            LIMIT 200
        """).fetchall()

    if len(preds) < 5:
        return jsonify({
            "ok": True,
            "learned": False,
            "message": "Need at least 5 checked predictions before learning adjusts weights.",
            "checked_predictions": len(preds),
            "weights": get_weights()
        })

    wins = [p for p in preds if p["outcome"] in ("target_hit", "profit")]
    losses = [p for p in preds if p["outcome"] in ("stop_loss_hit", "loss")]
    flats = [p for p in preds if p["outcome"] == "flat"]

    win_rate = len(wins) / len(preds)
    loss_rate = len(losses) / len(preds)

    weights = get_weights()

    # Conservative learning logic:
    # If win rate is good, trust momentum slightly more.
    # If loss rate is high, penalize volatility more and reduce short-term chase.
    if win_rate >= 0.55:
        weights["w_1h"] += 0.03
        weights["w_6h"] += 0.02
        weights["w_24h"] += 0.01
        weights["w_recovery"] += 0.01

    if loss_rate >= 0.35:
        weights["w_volatility"] -= 0.04
        weights["w_1h"] -= 0.02

    if len(flats) / len(preds) >= 0.40:
        # Too many flat calls means the score needs to demand stronger movement.
        weights["w_6h"] += 0.02
        weights["w_recovery"] -= 0.01

    # Keep weights sane.
    weights["w_1h"] = clamp(weights["w_1h"], 0.50, 4.00)
    weights["w_6h"] = clamp(weights["w_6h"], 0.25, 3.00)
    weights["w_24h"] = clamp(weights["w_24h"], 0.10, 2.00)
    weights["w_recovery"] = clamp(weights["w_recovery"], 0.05, 1.50)
    weights["w_volatility"] = clamp(weights["w_volatility"], -2.00, -0.05)

    set_weights(weights)

    return jsonify({
        "ok": True,
        "learned": True,
        "checked_predictions": len(preds),
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "win_rate": round(win_rate * 100, 2),
        "loss_rate": round(loss_rate * 100, 2),
        "weights": weights
    })


@app.route("/api/learning_status")
def learning_status():
    init_db()
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM predictions").fetchone()["c"]
        checked = conn.execute("SELECT COUNT(*) c FROM predictions WHERE checked = 1").fetchone()["c"]
        recent = conn.execute("""
            SELECT outcome, COUNT(*) c
            FROM predictions
            WHERE checked = 1
            GROUP BY outcome
        """).fetchall()

    return jsonify({
        "ok": True,
        "total_predictions": total,
        "checked_predictions": checked,
        "outcomes": {r["outcome"]: r["c"] for r in recent},
        "weights": get_weights()
    })


@app.route("/static/<path:filename>")
def serve_static_file(filename):
    return send_from_directory("static", filename)


@app.route("/api/auto_status")
def auto_status():
    init_db()
    return jsonify({
        "ok": True,
        "auto_enabled": AUTO_SNAPSHOT_ENABLED,
        "interval_seconds": AUTO_SNAPSHOT_SECONDS,
        "save_unchanged": AUTO_SNAPSHOT_SAVE_UNCHANGED,
        "has_render_api_key": bool(TORN_API_KEY),
        "state": AUTO_STATE
    })


@app.route("/api/auto_snapshot_once", methods=["POST", "GET"])
def auto_snapshot_once_route():
    try:
        result = auto_snapshot_once()
        status = 200 if result.get("ok") else 400
        return jsonify(result), status
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


init_db()
start_auto_watcher()

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
