"""Tests de la lógica pura del dashboard/Home (sin BD)."""
import uuid
from datetime import datetime, timedelta, timezone

from app.services.dashboard import (
    build_hourly_series,
    classify_attention,
    minutes_between,
    preview_text,
)

NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


# ---------- minutes_between ----------

def test_minutes_between_basic():
    assert minutes_between(NOW - timedelta(minutes=30), NOW) == 30


def test_minutes_between_none_is_zero():
    assert minutes_between(None, NOW) == 0


def test_minutes_between_future_clamped_to_zero():
    # earlier posterior a later -> no negativos
    assert minutes_between(NOW + timedelta(minutes=10), NOW) == 0


# ---------- build_hourly_series ----------

def test_build_hourly_series_fills_24_buckets():
    series = build_hourly_series({9: 3}, {9: 14})
    assert len(series) == 24
    assert all(p["hour"] == i for i, p in enumerate(series))


def test_build_hourly_series_today_and_avg():
    series = build_hourly_series({9: 3, 14: 5}, {9: 14, 23: 7})
    by_hour = {p["hour"]: p for p in series}
    assert by_hour[9] == {"hour": 9, "today": 3, "avg_7d": 2.0}   # 14/7
    assert by_hour[14] == {"hour": 14, "today": 5, "avg_7d": 0.0}
    assert by_hour[23] == {"hour": 23, "today": 0, "avg_7d": 1.0}  # 7/7
    assert by_hour[0] == {"hour": 0, "today": 0, "avg_7d": 0.0}


# ---------- preview_text ----------

def test_preview_text_short_passthrough():
    assert preview_text("hola mundo", None) == "hola mundo"


def test_preview_text_collapses_whitespace():
    assert preview_text("  multiple\n  spaces ", None) == "multiple spaces"


def test_preview_text_truncates_with_ellipsis():
    out = preview_text("a" * 100, None, max_len=80)
    assert out.endswith("…")
    assert len(out) == 80


def test_preview_text_media_fallback():
    assert preview_text(None, "image") == "[image]"
    assert preview_text("", "audio") == "[audio]"


def test_preview_text_empty():
    assert preview_text(None, None) == ""


# ---------- classify_attention ----------

def _row(**over):
    base = {
        "conversation_id": uuid.uuid4(),
        "canal": "whatsapp",
        "status": "bot",
        "last_message_at": NOW - timedelta(minutes=5),
        "derivada_a_humano_at": None,
        "asignada_a": None,
        "contact_name": "Ana",
        "contact_phone": "+34600000000",
        "last_rol": "user",
        "last_contenido": "Hola",
        "last_media_type": None,
    }
    base.update(over)
    return base


def test_classify_handoff_goes_to_handoffs():
    rows = [_row(status="humano", derivada_a_humano_at=NOW - timedelta(minutes=30))]
    out = classify_attention(rows, now=NOW, stale_minutes=10)
    assert len(out["handoffs"]) == 1
    assert len(out["waiting"]) == 0
    assert out["handoffs"][0]["waiting_minutes"] == 30


def test_classify_waiting_when_user_silent_past_threshold():
    rows = [_row(status="bot", last_rol="user", last_message_at=NOW - timedelta(minutes=15))]
    out = classify_attention(rows, now=NOW, stale_minutes=10)
    assert len(out["waiting"]) == 1
    assert out["waiting"][0]["waiting_minutes"] == 15


def test_classify_recent_user_message_excluded():
    rows = [_row(status="bot", last_rol="user", last_message_at=NOW - timedelta(minutes=5))]
    out = classify_attention(rows, now=NOW, stale_minutes=10)
    assert out["waiting"] == []


def test_classify_bot_answered_excluded():
    rows = [_row(status="bot", last_rol="assistant", last_message_at=NOW - timedelta(minutes=60))]
    out = classify_attention(rows, now=NOW, stale_minutes=10)
    assert out["waiting"] == []
    assert out["handoffs"] == []


def test_classify_handoffs_sorted_by_wait_desc():
    rows = [
        _row(status="humano", derivada_a_humano_at=NOW - timedelta(minutes=30)),
        _row(status="humano", derivada_a_humano_at=NOW - timedelta(minutes=120)),
    ]
    out = classify_attention(rows, now=NOW, stale_minutes=10)
    waits = [h["waiting_minutes"] for h in out["handoffs"]]
    assert waits == [120, 30]


def test_classify_serializes_ids_and_dates():
    cid = uuid.uuid4()
    op = uuid.uuid4()
    rows = [
        _row(
            conversation_id=cid,
            status="humano",
            asignada_a=op,
            derivada_a_humano_at=NOW - timedelta(minutes=5),
            last_message_at=NOW - timedelta(minutes=5),
        )
    ]
    out = classify_attention(rows, now=NOW, stale_minutes=10)
    item = out["handoffs"][0]
    assert item["conversation_id"] == str(cid)
    assert item["assigned_to"] == str(op)
    assert item["last_message_at"] == (NOW - timedelta(minutes=5)).isoformat()
    assert item["preview"] == "Hola"
