"""Unit tests for the MCP server: tools, SQLite persistence (a temp DB per test) and API-key auth.

The module is imported with spec_from_file_location to avoid clashing with the name `main.py`
(src/backend also has a main.py on sys.path). The clock is frozen at 2026-10-04 12:00 (Ho Chi
Minh City) so every date below is deterministic.
"""
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from mcp.server.fastmcp.exceptions import ToolError

MCP_MAIN = Path(__file__).resolve().parent.parent / "src" / "mcp-server" / "main.py"
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
DAY = "2026-10-17"  # a future date, used by most tests


def _load_with_db(tmp_path, monkeypatch):
    """Re-import the mcp-server main module with a fresh temp MCP_DB_PATH (clean DB)."""
    monkeypatch.setenv("MCP_DB_PATH", str(tmp_path / "restaurant.db"))
    monkeypatch.delenv("MCP_API_KEYS", raising=False)
    sys.modules.pop("mcp_server_main", None)
    spec = importlib.util.spec_from_file_location("mcp_server_main", MCP_MAIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mcp_server_main"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    mod = _load_with_db(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "_now", lambda: NOW)
    return mod


def book(srv, guest="g1", name="Hung", date=DAY, time="19:00", party=4, **kw):
    return srv.create_booking(guest, name, date, time, party, **kw)


# ----------------------------- Booking lifecycle -----------------------------


def test_booking_crud_and_persistence(srv, tmp_path, monkeypatch):
    out = book(srv, notes="no spicy food")
    assert out["created"] is True
    bid = out["booking"]["id"]
    assert out["booking"]["status"] == "CONFIRMED"
    assert out["booking"]["notes"] == "no spicy food"
    assert out["loyalty_points_earned"] == 10 and out["loyalty_points_total"] == 10

    assert srv.get_loyalty("g1")["points"] == 10
    lst = srv.list_bookings("g1")
    assert [b["id"] for b in lst["bookings"]] == [bid]

    # the booked table is no longer free for that slot
    taken = out["booking"]["table"]
    assert taken not in [t["table"] for t in srv.check_availability(DAY, "19:00", 4)["available"]]

    # a restart (module re-import) keeps the booking
    srv2 = _load_with_db(tmp_path, monkeypatch)
    monkeypatch.setattr(srv2, "_now", lambda: NOW)
    assert [b["id"] for b in srv2.list_bookings("g1")["bookings"]] == [bid]

    # cancel -> the table is free again, the points are taken back
    can = srv2.cancel_booking("g1", bid)
    assert can["booking"]["status"] == "CANCELLED"
    assert can["loyalty_points_reversed"] == 10 and can["loyalty_points_total"] == 0
    assert taken in [t["table"] for t in srv2.check_availability(DAY, "19:00", 4)["available"]]
    assert srv2.list_bookings("g1")["bookings"] == []


def test_guest_data_is_isolated(srv):
    a = book(srv, guest="zalo-111", name="Hung")
    b = book(srv, guest="zalo-222", name="Hung", time="12:00", party=2)  # same display name
    assert [x["id"] for x in srv.list_bookings("zalo-111")["bookings"]] == [a["booking"]["id"]]
    assert [x["id"] for x in srv.list_bookings("zalo-222")["bookings"]] == [b["booking"]["id"]]
    assert srv.list_bookings("zalo-333") == {"bookings": [], "count": 0, "truncated": False}
    assert srv.get_loyalty("zalo-333") == {"points": 0, "history": []}
    # points follow the guest id, not the display name
    assert srv.get_loyalty("zalo-111")["points"] == 10
    assert srv.get_loyalty("hung")["points"] == 0


def test_cancel_checks_ownership(srv):
    bid = book(srv, guest="owner")["booking"]["id"]
    with pytest.raises(ToolError, match="no booking") as stranger:
        srv.cancel_booking("intruder", bid)
    with pytest.raises(ToolError, match="no booking") as missing:
        srv.cancel_booking("owner", "bk-00000000")
    # same wording for "not yours" and "does not exist": ids cannot be probed
    assert str(stranger.value).replace(bid, "X") == str(missing.value).replace("bk-00000000", "X")
    assert srv.list_bookings("owner")["bookings"][0]["status"] == "CONFIRMED"
    assert srv.get_loyalty("owner")["points"] == 10


def test_cancel_accepts_id_in_any_case_and_is_idempotent(srv):
    bid = book(srv)["booking"]["id"]
    first = srv.cancel_booking("g1", f" {bid.upper()} ")
    assert first["loyalty_points_reversed"] == 10
    again = srv.cancel_booking("g1", bid)
    assert again["booking"]["status"] == "CANCELLED"
    assert again["loyalty_points_reversed"] == 0 and again["loyalty_points_total"] == 0


def test_loyalty_cannot_be_farmed_or_set(srv):
    assert not hasattr(srv, "add_loyalty_points")
    assert "add_loyalty_points" not in srv.TOOL_NAMES
    for i in range(3):  # book + cancel, again and again
        bid = book(srv, guest="farm", date=f"2026-10-{20 + i}")["booking"]["id"]
        srv.cancel_booking("farm", bid)
        srv.cancel_booking("farm", bid)
    loyalty = srv.get_loyalty("farm")
    assert loyalty["points"] == 0
    assert sum(h["points"] for h in loyalty["history"]) == 0
    assert len(loyalty["history"]) == 6  # +10/-10 three times, the repeated cancels add nothing


def test_duplicate_booking_returns_existing(srv):
    first = book(srv)
    again = book(srv, party=2)  # same guest, date and time: nothing new is created
    assert again["created"] is False
    assert again["booking"] == first["booking"]
    assert len(srv.list_bookings("g1")["bookings"]) == 1
    assert srv.get_loyalty("g1")["points"] == 10
    # after cancelling, the same slot can be booked again
    srv.cancel_booking("g1", first["booking"]["id"])
    assert book(srv)["created"] is True


# ----------------------------- Validation -----------------------------


@pytest.mark.parametrize("date", ["tối nay", "tomorrow", "2026-1-5", "2026-02-30", "17/10/2026", "20261017", ""])
def test_date_must_be_iso(srv, date):
    with pytest.raises(ToolError, match="date"):
        srv.create_booking("g1", "Hung", date, "19:00", 2)
    with pytest.raises(ToolError, match="date"):
        srv.check_availability(date, "19:00", 2)


def test_past_date_and_time_rejected(srv):
    with pytest.raises(ToolError, match="past"):
        book(srv, date="2026-10-03")
    with pytest.raises(ToolError, match="past"):
        book(srv, date="2020-01-01")
    with pytest.raises(ToolError, match="past"):
        book(srv, date="2026-10-04", time="11:00")  # earlier today (clock is 12:00)
    assert book(srv, date="2026-10-04", time="19:00", party=2)["created"] is True  # later today is fine


@pytest.mark.parametrize("time", ["7pm", "19h", "9:30", "25:00", "19:75", "09:30", "21:30", "22:00", "00:30"])
def test_time_must_be_hhmm_within_hours(srv, time):
    with pytest.raises(ToolError, match="time|booking hours"):
        srv.create_booking("g1", "Hung", DAY, time, 2)


def test_opening_hours_boundaries(srv):
    assert book(srv, time="10:00", party=2)["created"] is True
    assert book(srv, time="21:00", party=2)["created"] is True
    with pytest.raises(ToolError, match="21:00"):
        book(srv, time="21:01")


@pytest.mark.parametrize("party", [0, -1, 13, 99])
def test_party_size_bounds(srv, party):
    with pytest.raises(ToolError, match="party_size"):
        book(srv, party=party)
    with pytest.raises(ToolError, match="party_size"):
        srv.check_availability(DAY, "19:00", party)


def test_party_size_max_is_largest_table(srv):
    assert srv.MAX_PARTY_SIZE == 12
    assert book(srv, party=12)["booking"]["table"] == "T12"


def test_required_fields(srv):
    with pytest.raises(ToolError, match="guest_id"):
        srv.create_booking("  ", "Hung", DAY, "19:00", 2)
    with pytest.raises(ToolError, match="guest_id"):
        srv.list_bookings("")
    with pytest.raises(ToolError, match="customer"):
        srv.create_booking("g1", "   ", DAY, "19:00", 2)
    with pytest.raises(ToolError, match="notes"):
        book(srv, notes="x" * 501)


def test_unknown_menu_category(srv):
    with pytest.raises(ToolError, match="unknown category"):
        srv.get_menu("nope")


# ----------------------------- Tables and slots -----------------------------


def test_explicit_table_must_exist_and_fit(srv):
    with pytest.raises(ToolError, match="does not exist"):
        book(srv, party=2, table="T99")
    with pytest.raises(ToolError, match="too small"):
        book(srv, party=4, table="T1")  # T1 seats 2
    ok = book(srv, party=2, table=" t3 ")  # normalised: case and spaces
    assert ok["booking"]["table"] == "T3"


def test_bookings_hold_the_table_for_two_hours(srv):
    book(srv, guest="a", party=4, table="T3", time="19:00")
    # 19:30 overlaps the 19:00 booking on T3, and so does 18:30, 20:59 and 17:01
    for t in ("19:30", "18:30", "20:59", "17:01"):
        assert "T3" not in [x["table"] for x in srv.check_availability(DAY, t, 4)["available"]], t
        with pytest.raises(ToolError, match="already booked"):
            book(srv, guest="b", party=4, table="T3", time=t)
    # exactly two hours apart is free
    for t in ("21:00", "17:00"):
        assert "T3" in [x["table"] for x in srv.check_availability(DAY, t, 4)["available"]], t
    assert book(srv, guest="b", party=4, table="T3", time="21:00")["booking"]["table"] == "T3"
    # another date is unaffected
    assert "T3" in [x["table"] for x in srv.check_availability("2026-10-18", "19:00", 4)["available"]]


def test_auto_assignment_skips_overlapping_tables(srv):
    first = book(srv, guest="a", party=2, time="19:00")["booking"]["table"]
    second = book(srv, guest="b", party=2, time="19:30")["booking"]["table"]
    assert first == "T1" and second == "T2"  # the 19:30 couple does not get T1's table
    third = book(srv, guest="c", party=2, time="19:45")["booking"]["table"]
    assert third == "T3"  # both 2-seaters are held, so the next smallest table is used


def test_full_slot_is_reported_with_a_hint(srv):
    for i in range(1, 13):
        book(srv, guest=f"g{i}", party=1, time="19:00")  # 12 bookings take all 12 tables
    assert srv.check_availability(DAY, "19:00", 1)["available"] == []
    with pytest.raises(ToolError, match="check_availability"):
        book(srv, guest="late", party=1, time="19:00")


def test_smallest_table_that_fits_in_numeric_order(srv):
    free = [t["table"] for t in srv.check_availability(DAY, "19:00", 1)["available"]]
    assert free == ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9", "T10", "T11", "T12"]
    # string order would put T10 before T9
    assert [t["table"] for t in srv.check_availability(DAY, "19:00", 7)["available"]] == ["T9", "T10", "T11", "T12"]
    assert [t["seats"] for t in srv.check_availability(DAY, "19:00", 5)["available"]] == [6, 6, 8, 8, 10, 12]
    assert book(srv, guest="a", party=7)["booking"]["table"] == "T9"
    assert book(srv, guest="b", party=7)["booking"]["table"] == "T10"
    assert book(srv, guest="c", party=3)["booking"]["table"] == "T3"  # smallest that fits 3 is a 4-seater


# ----------------------------- Listing -----------------------------


def test_list_bookings_is_upcoming_only(srv, monkeypatch):
    book(srv, date="2026-10-04", time="19:00", party=2)
    assert len(srv.list_bookings("g1")["bookings"]) == 1
    # the seating (19:00-21:00) is still running at 20:30 ...
    monkeypatch.setattr(srv, "_now", lambda: NOW.replace(hour=20, minute=30))
    assert len(srv.list_bookings("g1")["bookings"]) == 1
    # ... and is over at 21:30
    monkeypatch.setattr(srv, "_now", lambda: NOW.replace(hour=21, minute=30))
    assert srv.list_bookings("g1")["bookings"] == []


def test_list_bookings_is_capped_and_ordered(srv):
    start = NOW.date() + timedelta(days=1)
    for i in range(25):
        book(srv, party=2, date=(start + timedelta(days=24 - i)).isoformat())  # created in reverse order
    out = srv.list_bookings("g1")
    assert out["count"] == 20 and len(out["bookings"]) == 20
    assert out["truncated"] is True
    dates = [b["date"] for b in out["bookings"]]
    assert dates == sorted(dates) and dates[0] == start.isoformat()
    assert srv.list_bookings("nobody")["truncated"] is False


def test_get_loyalty_history_is_newest_first(srv):
    first = book(srv, date="2026-10-20")["booking"]["id"]
    book(srv, date="2026-10-21")
    srv.cancel_booking("g1", first)
    hist = srv.get_loyalty("g1")["history"]
    assert [h["points"] for h in hist] == [-10, 10, 10]
    assert first in hist[0]["reason"]


# ----------------------------- Static data -----------------------------


def test_restaurant_info(srv):
    info = srv.restaurant_info()
    assert info["name"] and info["address"] and info["phone"]
    assert info["opening_hours"] == {"open": "10:00", "close": "22:00", "last_seating": "21:00"}
    assert info["timezone"] == "Asia/Ho_Chi_Minh"
    assert info["seating_minutes"] == 120 and info["max_party_size"] == 12


def test_menu(srv):
    assert set(srv.get_menu()["menu"]) == {"khai-vi", "mon-chinh", "nuoc", "trang-mieng"}
    assert set(srv.get_menu("all")["menu"]) == {"khai-vi", "mon-chinh", "nuoc", "trang-mieng"}
    assert srv.get_menu("nuoc")["category"] == "nuoc"


def test_legacy_database_is_refused(tmp_path, monkeypatch):
    db = tmp_path / "restaurant.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE bookings (id TEXT PRIMARY KEY, customer TEXT NOT NULL)")
    with pytest.raises(RuntimeError, match="older version"):
        _load_with_db(tmp_path, monkeypatch)


# ----------------------------- MCP protocol -----------------------------

ACCEPT = {"Accept": "application/json, text/event-stream"}
LIST_BODY = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
KEY_A, KEY_B = "a" * 32, "b" * 32
EXPECTED_TOOLS = {"restaurant_info", "get_menu", "check_availability", "create_booking",
                  "list_bookings", "cancel_booking", "get_loyalty"}


def _call(client, name, arguments, headers=ACCEPT):
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    return client.post("/mcp", json=body, headers=headers).json()["result"]


def test_tools_over_mcp_protocol(srv, monkeypatch):
    """One test = one app lifespan (the MCP session manager cannot be restarted in a process)."""
    from starlette.testclient import TestClient

    monkeypatch.setattr(srv, "ALLOW_ANONYMOUS", True)
    with TestClient(srv.app) as c:
        tools = c.post("/mcp", json=LIST_BODY, headers=ACCEPT).json()["result"]["tools"]
        names = {t["name"] for t in tools}
        assert names == EXPECTED_TOOLS == set(srv.TOOL_NAMES)
        for t in tools:
            assert len(t["description"]) > 80, t["name"]  # every tool is documented
            assert t["annotations"] is not None
        by_name = {t["name"]: t for t in tools}
        for name in ("create_booking", "list_bookings", "cancel_booking", "get_loyalty"):
            assert "guest_id" in by_name[name]["inputSchema"]["required"], name
        for name in ("get_menu", "check_availability", "restaurant_info"):
            assert "guest_id" not in by_name[name]["inputSchema"].get("properties", {}), name

        ok = _call(c, "create_booking", {"guest_id": "g1", "customer": "Hung", "date": DAY,
                                         "time": "19:00", "party_size": 4})
        assert ok["isError"] is False
        assert ok["structuredContent"]["created"] is True
        assert json.loads(ok["content"][0]["text"]) == ok["structuredContent"]  # text copy for older clients

        # failures are tool errors (isError) whose text says how to fix the call
        bad_date = _call(c, "check_availability", {"date": "tomorrow", "time": "19:00", "party_size": 2})
        assert bad_date["isError"] is True and "YYYY-MM-DD" in bad_date["content"][0]["text"]
        no_guest = _call(c, "list_bookings", {"guest_id": " "})
        assert no_guest["isError"] is True and "guest_id" in no_guest["content"][0]["text"]
        stranger = _call(c, "cancel_booking", {"guest_id": "other", "booking_id": ok["structuredContent"]["booking"]["id"]})
        assert stranger["isError"] is True
        assert _call(c, "restaurant_info", {})["isError"] is False
        health = c.get("/health").json()
        assert health["tools"] == 7
        assert c.get("/").json()["tools"] == srv.TOOL_NAMES


# ----------------------------- Auth (fail-closed API key) -----------------------------


def test_load_api_keys_env(srv, monkeypatch):
    monkeypatch.setenv("MCP_API_KEYS", f" {KEY_A} , ,{KEY_B}")
    assert srv._load_api_keys() == [KEY_A, KEY_B]
    monkeypatch.delenv("MCP_API_KEYS")
    assert srv._load_api_keys() == []


@pytest.mark.parametrize("bad", ["change-me", "<openssl rand -hex 32>", "k" * 31, f"{KEY_A},short", "x" * 40 + ">"])
def test_placeholder_or_short_keys_are_refused(srv, monkeypatch, bad):
    monkeypatch.setenv("MCP_API_KEYS", bad)
    with pytest.raises(ValueError, match="MCP_API_KEYS"):
        srv._load_api_keys()


def test_server_refuses_to_start_with_a_placeholder_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DB_PATH", str(tmp_path / "restaurant.db"))
    monkeypatch.setenv("MCP_API_KEYS", "change-me")
    spec = importlib.util.spec_from_file_location("mcp_server_main_bad", MCP_MAIN)
    with pytest.raises(ValueError, match="openssl rand"):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


def test_extract_key_headers(srv):
    assert srv._extract_key([(b"x-api-key", b" abc ")]) == "abc"
    assert srv._extract_key([(b"authorization", b"Bearer xyz")]) == "xyz"
    assert srv._extract_key([(b"authorization", b"Basic xyz")]) == ""
    assert srv._extract_key([]) == ""
    assert srv._key_valid("") is False


def test_mcp_auth_fail_closed(srv, monkeypatch):
    """One test = one app lifespan (the MCP session manager cannot be restarted in a process)."""
    from starlette.testclient import TestClient

    with TestClient(srv.app) as c:
        # 1. No key configured -> 503, the server never opens itself
        monkeypatch.setattr(srv, "API_KEYS", [])
        monkeypatch.setattr(srv, "ALLOW_ANONYMOUS", False)
        assert c.post("/mcp", json=LIST_BODY, headers=ACCEPT).status_code == 503
        assert c.get("/health").json()["mcp_auth"].startswith("locked")

        # 2. ALLOW_ANONYMOUS (local development) -> open
        monkeypatch.setattr(srv, "ALLOW_ANONYMOUS", True)
        r = c.post("/mcp", json=LIST_BODY, headers=ACCEPT)
        assert r.status_code == 200
        assert len(r.json()["result"]["tools"]) == len(EXPECTED_TOOLS)

        # 3. Keys configured -> a key is always required, even with ALLOW_ANONYMOUS
        monkeypatch.setattr(srv, "API_KEYS", [KEY_A, KEY_B])
        r = c.post("/mcp", json=LIST_BODY, headers=ACCEPT)
        assert r.status_code == 401 and "www-authenticate" in r.headers
        assert c.post("/mcp", json=LIST_BODY, headers={**ACCEPT, "X-Api-Key": "wrong"}).status_code == 401
        assert c.post("/mcp", json=LIST_BODY,
                      headers={**ACCEPT, "Authorization": "Bearer wrong"}).status_code == 401

        # 4. Both header styles are accepted, and both rotation keys work
        for hdr in ({"X-Api-Key": KEY_A}, {"Authorization": f"Bearer {KEY_B}"}):
            r = c.post("/mcp", json=LIST_BODY, headers={**ACCEPT, **hdr})
            assert r.status_code == 200, hdr
            assert len(r.json()["result"]["tools"]) == len(EXPECTED_TOOLS)

        # 5. /health and / need no key and do not leak the key
        assert c.get("/health").status_code == 200
        assert c.get("/").status_code == 200
        health = c.get("/health").json()
        assert health["mcp_auth"] == "api-key (2 key)" and health["tools"] == len(EXPECTED_TOOLS)
        assert KEY_A not in json.dumps(health) and KEY_A not in c.get("/").text
