"""TradingView webhook ingestion — TradingView webhook ingestion: receive, validate, dedupe, persist, and queue signals.

No Alpaca orders are placed from this module. It owns transport/authentication,
freshness, dedupe, latest-state persistence, and an append-only actionable queue.
The trading bot consumes that queue separately for paper ENTRY/EXIT execution.

Stdlib-only (no Flask/FastAPI) so it runs without any new package install.

SECURITY: this server speaks plain HTTP and has no TLS of its own. The secret
travels in the alert message body (TradingView alerts can't set custom HTTP
headers), so it MUST be treated as a credential: only expose this endpoint
through an HTTPS tunnel/reverse proxy (ngrok, Cloudflare Tunnel, nginx+certbot,
etc.) that terminates TLS in front of it, and never put the secret in
screenshots, logs, committed Pine code, or persisted webhook state. The secret
is popped from the payload before logging/storage — see do_POST.

Run standalone:
    python3 -m tradingview.webhook_server

Env vars:
    TRADINGVIEW_WEBHOOK_SECRET      shared secret, sent by TradingView as header
                                     X-Webhook-Secret. Required — server refuses
                                     to start without it.
    TRADINGVIEW_WEBHOOK_PORT        default 8787
    TRADINGVIEW_WEBHOOK_HOST        default 0.0.0.0
    TRADINGVIEW_ALLOWED_STRATEGY_MODE  default "INTRADAY" — any other
                                     strategy_mode (e.g. "SWING") is rejected so a
                                     5m webhook can never touch a multi-day position.
    TRADINGVIEW_FRESHNESS_BUFFER_SECONDS  default 90 — extra allowance on top of
                                     one bar's duration before an ENTRY/EXIT signal
                                     is considered stale.
    TRADINGVIEW_STATE_DIR           default tradingview/state (relative to repo root)
"""

from __future__ import annotations

import hmac
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

WEBHOOK_SECRET = os.getenv("TRADINGVIEW_WEBHOOK_SECRET", "")
WEBHOOK_PORT = int(os.getenv("TRADINGVIEW_WEBHOOK_PORT", "8787"))
WEBHOOK_HOST = os.getenv("TRADINGVIEW_WEBHOOK_HOST", "0.0.0.0")
ALLOWED_STRATEGY_MODE = os.getenv("TRADINGVIEW_ALLOWED_STRATEGY_MODE", "INTRADAY").strip().upper()
FRESHNESS_BUFFER_SECONDS = int(os.getenv("TRADINGVIEW_FRESHNESS_BUFFER_SECONDS", "90"))
STATE_DIR = Path(os.getenv("TRADINGVIEW_STATE_DIR", str(Path(__file__).parent / "state")))

SIGNAL_STATE_FILE = STATE_DIR / "latest_signals.json"
SEEN_IDS_FILE = STATE_DIR / "seen_signal_ids.json"
ACTIONABLE_QUEUE_FILE = STATE_DIR / "actionable_signals.jsonl"
SEEN_IDS_MAX_AGE_HOURS = 48  # bound file growth; dedupe only needs to cover a signal's realistic retry window

VALID_EVENTS = {"FORMING", "WATCH", "READY", "ENTRY", "EXIT_WATCH", "EXIT"}
VALID_SIDES = {"CALL", "PUT"}
ACTIONABLE_EVENTS = {"ENTRY", "EXIT"}  # only these get a freshness check; READY/WATCH/FORMING are informational

_lock = threading.Lock()


def _now_utc():
    return datetime.now(timezone.utc)


def _load_json(path, default):
    if not path.exists():
        return default
    try:
        with path.open("r") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2, default=str)
    tmp.replace(path)


def _parse_bar_time(raw):
    raw = str(raw or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _timeframe_minutes(raw):
    """TradingView sends timeframe as a string like "5", "15", "60", "D"."""
    raw = str(raw or "").strip().upper()
    if raw in ("D", "1D"):
        return 24 * 60
    try:
        return max(1, int(raw))
    except ValueError:
        return 5  # conservative default so an unparsable timeframe still gets a freshness check


def log(msg):
    print(f"[{_now_utc().isoformat(timespec='seconds')}] [tradingview] {msg}", flush=True)


def _check_auth(secret_header, secret_body):
    """Accept either the X-Webhook-Secret header or a "secret" field in the JSON
    body. TradingView's native alert webhook delivery cannot set custom HTTP
    headers — only the message body is configurable — so body-based auth is
    the path real TradingView alerts will actually use. The header check is
    kept for any other client (e.g. a relay) that can set headers."""
    if not WEBHOOK_SECRET:
        return False, "server has no TRADINGVIEW_WEBHOOK_SECRET configured — refusing all requests"
    if hmac.compare_digest(secret_header or "", WEBHOOK_SECRET):
        return True, ""
    if hmac.compare_digest(secret_body or "", WEBHOOK_SECRET):
        return True, ""
    return False, "invalid or missing secret (checked X-Webhook-Secret header and body 'secret' field)"


def _validate_schema(payload):
    required = [
        "schema_version", "strategy", "strategy_mode", "event", "side", "symbol",
        "timeframe", "bar_time", "underlying_price", "setup_type", "reason_code",
        "reason", "signal_id",
    ]
    missing = [f for f in required if f not in payload]
    if missing:
        return False, f"missing required field(s): {', '.join(missing)}"

    if payload.get("schema_version") != 1:
        return False, f"unsupported schema_version {payload.get('schema_version')!r} (expected 1)"

    event = str(payload.get("event", "")).strip().upper()
    if event not in VALID_EVENTS:
        return False, f"invalid event {event!r} (expected one of {sorted(VALID_EVENTS)})"

    side = str(payload.get("side", "")).strip().upper()
    if side not in VALID_SIDES:
        return False, f"invalid side {side!r} (expected CALL or PUT)"

    if not str(payload.get("symbol", "")).strip():
        return False, "symbol must be non-empty"

    try:
        price = float(payload.get("underlying_price"))
        if price <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return False, "underlying_price must be a positive number"

    try:
        _parse_bar_time(payload.get("bar_time"))
    except Exception:
        return False, f"bar_time is not valid ISO8601: {payload.get('bar_time')!r}"

    if not str(payload.get("signal_id", "")).strip():
        return False, "signal_id must be non-empty"

    return True, ""


def _safe_parse_iso(raw, default):
    try:
        return _parse_bar_time(raw)
    except Exception:
        return default


def _is_duplicate(signal_id):
    with _lock:
        seen = _load_json(SEEN_IDS_FILE, {})
        if signal_id in seen:
            return True
        cutoff = _now_utc() - timedelta(hours=SEEN_IDS_MAX_AGE_HOURS)
        seen = {
            sid: ts for sid, ts in seen.items()
            if _safe_parse_iso(ts, default=_now_utc()) >= cutoff
        }
        seen[signal_id] = _now_utc().isoformat()
        _save_json(SEEN_IDS_FILE, seen)
        return False


def _freshness_ok(event, bar_time, timeframe_minutes):
    if event not in ACTIONABLE_EVENTS:
        return True, "informational event — no freshness gate"
    age_seconds = (_now_utc() - bar_time).total_seconds()
    tolerance = (timeframe_minutes * 60) + max(0, FRESHNESS_BUFFER_SECONDS)
    if age_seconds > tolerance:
        return False, f"bar_time is {age_seconds:.0f}s old > tolerance {tolerance:.0f}s ({timeframe_minutes}m bar + {FRESHNESS_BUFFER_SECONDS}s buffer)"
    if age_seconds < -tolerance:
        return False, f"bar_time is {abs(age_seconds):.0f}s in the future > tolerance {tolerance:.0f}s"
    return True, f"age {age_seconds:.0f}s within tolerance {tolerance:.0f}s"


def _store_latest_signal(payload):
    symbol = str(payload["symbol"]).strip().upper()
    side = str(payload["side"]).strip().upper()
    key = f"{symbol}:{side}"
    with _lock:
        state = _load_json(SIGNAL_STATE_FILE, {})
        state[key] = {**payload, "symbol": symbol, "side": side, "received_at": _now_utc().isoformat()}
        _save_json(SIGNAL_STATE_FILE, state)


def _append_actionable_signal(payload):
    """Append execution-relevant events so a later state update cannot overwrite them.

    The bot performs its own consumed-signal idempotency check. This queue is append-only
    JSONL so ENTRY cannot be lost if EXIT_WATCH/EXIT arrives before the next bot scan.
    """
    event = str(payload.get("event", "") or "").upper()
    if event not in {"ENTRY", "EXIT_WATCH", "EXIT"}:
        return
    record = {**payload, "received_at": _now_utc().isoformat()}
    with _lock:
        ACTIONABLE_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with ACTIONABLE_QUEUE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str, separators=(",", ":")) + "\n")


def _process_signal(payload):
    """Shared validate/dedupe/freshness/store pipeline. Returns (http_status, body_dict)."""
    schema_ok, schema_reason = _validate_schema(payload)
    if not schema_ok:
        log(f"REJECTED (schema): {schema_reason} | payload={payload}")
        return 400, {"status": "rejected", "reason": schema_reason}

    event = str(payload["event"]).strip().upper()
    side = str(payload["side"]).strip().upper()
    symbol = str(payload["symbol"]).strip().upper()
    signal_id = str(payload["signal_id"]).strip()
    strategy_mode = str(payload.get("strategy_mode", "")).strip().upper()

    # Keep SWING completely isolated from the intraday webhook path — a stale
    # 5m EXIT_CALL must never be able to touch a multi-day SWING position.
    if strategy_mode != ALLOWED_STRATEGY_MODE:
        log(f"REJECTED (strategy_mode): {signal_id} strategy_mode={strategy_mode!r} != {ALLOWED_STRATEGY_MODE!r}")
        return 200, {"status": "rejected", "reason": f"strategy_mode {strategy_mode!r} not accepted by this endpoint"}

    if _is_duplicate(signal_id):
        log(f"DUPLICATE: {signal_id} — ignored")
        return 200, {"status": "duplicate", "signal_id": signal_id}

    try:
        bar_time = _parse_bar_time(payload["bar_time"])
    except Exception as e:
        log(f"REJECTED (bar_time parse): {signal_id} error={e}")
        return 400, {"status": "rejected", "reason": f"bar_time parse error: {e}"}

    tf_minutes = _timeframe_minutes(payload.get("timeframe"))
    fresh_ok, fresh_reason = _freshness_ok(event, bar_time, tf_minutes)
    if not fresh_ok:
        log(f"STALE: {signal_id} {symbol} {side} {event} — {fresh_reason}")
        return 200, {"status": "stale", "signal_id": signal_id, "reason": fresh_reason}

    _store_latest_signal(payload)
    _append_actionable_signal(payload)

    log(
        f"ACCEPTED: {signal_id} {symbol} {side} {event} "
        f"setup={payload.get('setup_type')} reason={payload.get('reason')!r} "
        f"price={payload.get('underlying_price')} ({fresh_reason}) — queued for bot consumption "
        f"(this server places no orders itself)."
    )
    return 200, {"status": "accepted", "signal_id": signal_id, "event": event, "note": "queued for bot consumption; this server places no orders itself"}


class _Handler(BaseHTTPRequestHandler):
    server_version = "TradingViewWebhook/1"

    def log_message(self, fmt, *args):
        pass  # replaced by our own log() calls; keep stdout clean of the default access log

    def _send_json(self, status, body):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"status": "ok", "time": _now_utc().isoformat()})
            return
        if path == "/webhook/tradingview/state":
            with _lock:
                state = _load_json(SIGNAL_STATE_FILE, {})
            self._send_json(200, state)
            return
        self._send_json(404, {"status": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/webhook/tradingview":
            self._send_json(404, {"status": "not_found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(length) if length > 0 else b""
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else None
        except Exception as e:
            log(f"REJECTED (body parse): {e}")
            self._send_json(400, {"status": "rejected", "reason": f"invalid JSON body: {e}"})
            return

        if not isinstance(payload, dict):
            log("REJECTED (schema): request body is not a JSON object")
            self._send_json(400, {"status": "rejected", "reason": "body must be a JSON object"})
            return

        # Pop the secret before any logging/storage — it must never be persisted or echoed.
        secret_body = payload.pop("secret", None)
        auth_ok, auth_reason = _check_auth(self.headers.get("X-Webhook-Secret"), secret_body)
        if not auth_ok:
            log(f"REJECTED (auth): {auth_reason}")
            self._send_json(401, {"status": "rejected", "reason": auth_reason})
            return

        status, body = _process_signal(payload)
        self._send_json(status, body)


def main():
    if not WEBHOOK_SECRET:
        raise SystemExit("TRADINGVIEW_WEBHOOK_SECRET is not set — refusing to start webhook server.")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log(f"Starting on {WEBHOOK_HOST}:{WEBHOOK_PORT} (allowed strategy_mode={ALLOWED_STRATEGY_MODE!r}; transport only — no orders in webhook process).")
    server = ThreadingHTTPServer((WEBHOOK_HOST, WEBHOOK_PORT), _Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
