"""Restaurant MCP Server: the restaurant's business API, exposed as MCP tools.

Deployment model: this server does NOT run on AgentBase. Run it inside the customer VPC
(vServer with docker compose, or VKS) and register it as a connector `restaurant` on a
Private MCP Gateway (Outbound Auth = API Key, header `X-Api-Key`). The agent runtime reaches
it only through the gateway: Agent -> MCP Gateway -> Policy Group -> connector -> this server.

Auth (fail-closed): /mcp requires an API key.
  - MCP_API_KEYS="key1,key2"  (several keys allow rotation without downtime). Every key must be
    at least 32 characters and must not contain `<` or `>` (template placeholders): the server
    refuses to start otherwise.
  - Header: `X-Api-Key: <key>` or `Authorization: Bearer <key>`
  - No key configured -> /mcp returns 503 (it never opens itself). Set ALLOW_ANONYMOUS=true
    only for local development.
  - A wrong or missing key -> 401. /health and / are always open (health probes).
  The gateway attaches the key when it forwards a call (the secret lives in Access Control),
  so the agent never sees it.

Guest identity: every tool that reads or writes guest data takes a `guest_id`, an opaque string
that identifies the guest (the Zalo user id). The agent platform supplies it, never the guest and
never the model, and all guest data (bookings, loyalty points) is keyed by it. `customer` is only
the display name printed on a booking.

State is stored in **SQLite** (bookings + loyalty), so a restart does not lose data. The DB
path comes from env `MCP_DB_PATH` (default `data/restaurant.db` next to this file, which is
`/app/data/restaurant.db` in the container). Mount a persistent volume on `/app/data`.
Environment: PORT (default 8080), MCP_DB_PATH, MCP_API_KEYS, ALLOW_ANONYMOUS.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date as Date
from datetime import datetime, timedelta
from datetime import time as Time
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.responses import JSONResponse
from starlette.routing import Route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("restaurant-mcp")

# ------------------------- Auth configuration -------------------------

MIN_API_KEY_LENGTH = 32


def _load_api_keys() -> list[str]:
    """Read MCP_API_KEYS and refuse placeholders and short keys (fail closed at startup)."""
    keys = [k.strip() for k in os.environ.get("MCP_API_KEYS", "").split(",") if k.strip()]
    for key in keys:
        if "<" in key or ">" in key or len(key) < MIN_API_KEY_LENGTH:
            raise ValueError(
                f"MCP_API_KEYS contains a placeholder or a key shorter than {MIN_API_KEY_LENGTH} "
                "characters; generate a real one with `openssl rand -hex 32`"
            )
    return keys


API_KEYS = _load_api_keys()
ALLOW_ANONYMOUS = os.environ.get("ALLOW_ANONYMOUS", "").strip().lower() in ("1", "true", "yes")

mcp = FastMCP("restaurant", stateless_http=True, json_response=True, host="0.0.0.0")

# ------------------------- Restaurant data -------------------------

TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Sample details: replace them with your restaurant's real data.
RESTAURANT_NAME = "Quán Ngon 123"
RESTAURANT_ADDRESS = "123 Nguyen Hue, District 1, Ho Chi Minh City"
RESTAURANT_PHONE = "+84 28 0000 0000"

OPENING_TIME = Time(10, 0)
CLOSING_TIME = Time(22, 0)
LAST_SEATING = Time(21, 0)
# A booking holds its table for this long: two bookings on the same table must start at least
# this many minutes apart.
SEATING_MINUTES = 120
POINTS_PER_BOOKING = 10
MAX_LIST = 20
MAX_NAME_LENGTH = 80
MAX_NOTES_LENGTH = 500
MAX_GUEST_ID_LENGTH = 128

# Dish names are the restaurant's Vietnamese menu names (proper nouns); notes are in English.
MENU = {
    "khai-vi": [
        {"id": "A1", "name": "Gỏi cuốn tôm thịt", "price": 45000, "note": "fresh shrimp and pork spring rolls; vegetarian dipping sauce available"},
        {"id": "A2", "name": "Chả giò rế", "price": 55000, "note": "crispy lattice spring rolls"},
        {"id": "A3", "name": "Nộm xoài khô bò", "price": 50000, "note": "spicy dried-beef mango salad"},
    ],
    "mon-chinh": [
        {"id": "M1", "name": "Bò kho bánh mì", "price": 89000, "note": "beef stew with bread"},
        {"id": "M2", "name": "Cá kho tộ", "price": 120000, "note": "caramelised fish in clay pot"},
        {"id": "M3", "name": "Cơm cháy cá sặc", "price": 95000, "note": "crispy rice with snakeskin gourami fish"},
        {"id": "M4", "name": "Lẩu gà lá chanh", "price": 350000, "note": "lime-leaf chicken hotpot, serves 4"},
        {"id": "M5", "name": "Cơm chay thập cẩm", "price": 70000, "note": "vegetarian mixed rice"},
    ],
    "nuoc": [
        {"id": "N1", "name": "Cà phê sữa đá", "price": 30000, "note": "iced milk coffee"},
        {"id": "N2", "name": "Trà tắc", "price": 25000, "note": "iced kumquat tea"},
        {"id": "N3", "name": "Soda chanh", "price": 35000, "note": "lime soda"},
    ],
    "trang-mieng": [
        {"id": "D1", "name": "Chè ba màu", "price": 35000, "note": "three-colour sweet dessert"},
        {"id": "D2", "name": "Bánh flan", "price": 30000, "note": "caramel flan"},
    ],
}

# Tables T1..T12 with different capacities
TABLES = {f"T{i}": seats for i, seats in enumerate([2, 2, 4, 4, 4, 4, 6, 6, 8, 8, 10, 12], start=1)}
MAX_PARTY_SIZE = max(TABLES.values())

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
TIME_RE = re.compile(r"\d{2}:\d{2}")

# ------------------------- SQLite persistence -------------------------


def _resolve_db_path() -> str:
    env_path = os.environ.get("MCP_DB_PATH", "").strip()
    if env_path:
        return env_path
    default = Path(__file__).resolve().parent / "data" / "restaurant.db"
    try:
        default.parent.mkdir(parents=True, exist_ok=True)
        probe = default.parent / ".probe"
        probe.touch()
        probe.unlink()
        return str(default)
    except OSError:
        # Read-only filesystem fallback. In production set MCP_DB_PATH to a path on the
        # persistent volume so data is never silently written to ephemeral storage.
        log.warning("default data dir %s is not writable; falling back to /tmp (data is NOT persistent)",
                    default.parent)
        return "/tmp/restaurant.db"


DB_PATH = _resolve_db_path()


@contextmanager
def _db(write: bool = False) -> Iterator[sqlite3.Connection]:
    """Open a connection, always close it, and make writes atomic.

    A write opens `BEGIN IMMEDIATE`, so the availability check and the insert of a booking form
    one serialised unit; an exception (for example a ToolError) rolls the whole unit back.
    """
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _init_db() -> None:
    with _db() as conn:
        columns = {r["name"] for r in conn.execute("SELECT name FROM pragma_table_info('bookings')")}
        if columns and "guest_id" not in columns:
            raise RuntimeError(
                f"{DB_PATH} was created by an older version of this sample (bookings are keyed by "
                "guest name, not guest_id). Back it up and delete it to start with the new schema."
            )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id         TEXT PRIMARY KEY,
                guest_id   TEXT NOT NULL,
                customer   TEXT NOT NULL,
                date       TEXT NOT NULL,
                time       TEXT NOT NULL,
                party_size INTEGER NOT NULL,
                table_id   TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'CONFIRMED',
                notes      TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings(date, status);
            CREATE INDEX IF NOT EXISTS idx_bookings_guest ON bookings(guest_id, status);
            CREATE TABLE IF NOT EXISTS loyalty (
                guest_id TEXT PRIMARY KEY,
                points   INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS loyalty_history (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                guest_id TEXT NOT NULL,
                ts       TEXT NOT NULL,
                delta    INTEGER NOT NULL,
                reason   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_loyalty_history_guest ON loyalty_history(guest_id, id);
            """
        )


_init_db()


def _now() -> datetime:
    """Current time in the restaurant's timezone (a seam for tests)."""
    return datetime.now(TZ)


def _table_number(table: str) -> int:
    return int(table[1:])


def _row_to_booking(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "customer": r["customer"],
        "date": r["date"],
        "time": r["time"],
        "party_size": r["party_size"],
        "table": r["table_id"],
        "status": r["status"],
        "notes": r["notes"],
        "created_at": r["created_at"],
    }


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def _free_tables(conn: sqlite3.Connection, date: str, hhmm: str, party_size: int) -> list[str]:
    """Tables that seat the party and are not held by another booking around that time.

    Sorted smallest first, then by table number (numeric: T2 before T10).
    """
    start = _minutes(hhmm)
    rows = conn.execute(
        "SELECT table_id, time FROM bookings WHERE date=? AND status='CONFIRMED'", (date,)
    ).fetchall()
    busy = {r["table_id"] for r in rows if abs(_minutes(r["time"]) - start) < SEATING_MINUTES}
    fits = [t for t, seats in TABLES.items() if seats >= party_size and t not in busy]
    return sorted(fits, key=lambda t: (TABLES[t], _table_number(t)))


def _loyalty_add(conn: sqlite3.Connection, guest_id: str, delta: int, reason: str) -> int:
    """Apply a points change and return the new balance (server-side only, never a tool)."""
    conn.execute(
        "INSERT INTO loyalty(guest_id, points) VALUES(?, ?) "
        "ON CONFLICT(guest_id) DO UPDATE SET points = points + excluded.points",
        (guest_id, delta),
    )
    conn.execute(
        "INSERT INTO loyalty_history(guest_id, ts, delta, reason) VALUES(?, ?, ?, ?)",
        (guest_id, _now().isoformat(timespec="seconds"), delta, reason),
    )
    return conn.execute("SELECT points FROM loyalty WHERE guest_id=?", (guest_id,)).fetchone()["points"]


# ------------------------- Input validation -------------------------


def _guest(guest_id: str) -> str:
    gid = (guest_id or "").strip()
    if not gid or len(gid) > MAX_GUEST_ID_LENGTH:
        raise ToolError(
            f"guest_id is required (1-{MAX_GUEST_ID_LENGTH} characters). It identifies the guest and is "
            "supplied by the agent platform: do not ask the guest for it and do not invent one."
        )
    return gid


def _party(party_size: int) -> int:
    if not 1 <= party_size <= MAX_PARTY_SIZE:
        raise ToolError(
            f"party_size must be between 1 and {MAX_PARTY_SIZE} (the largest table). For bigger "
            f"groups the guest should call the restaurant on {RESTAURANT_PHONE}."
        )
    return party_size


def _parse_slot(date_str: str, time_str: str) -> datetime:
    """Validate a booking date and time and return the slot start (restaurant timezone)."""
    now = _now()
    if not DATE_RE.fullmatch(date_str or ""):
        raise ToolError(
            f"date must be ISO YYYY-MM-DD, for example '{now.date().isoformat()}', got {date_str!r}. "
            "Convert relative dates such as 'tomorrow' or 'Saturday' to an ISO date first."
        )
    try:
        day = Date.fromisoformat(date_str)
    except ValueError:
        raise ToolError(f"date {date_str!r} is not a real calendar date; use ISO YYYY-MM-DD.") from None
    if not TIME_RE.fullmatch(time_str or ""):
        raise ToolError(f"time must be 24-hour HH:MM, for example '19:30', got {time_str!r}.")
    hour, minute = int(time_str[:2]), int(time_str[3:])
    if hour > 23 or minute > 59:
        raise ToolError(f"time {time_str!r} is not a real time of day; use 24-hour HH:MM, for example '19:30'.")
    start = Time(hour, minute)
    if not OPENING_TIME <= start <= LAST_SEATING:
        raise ToolError(
            f"{time_str} is outside the booking hours. The restaurant is open "
            f"{OPENING_TIME:%H:%M}-{CLOSING_TIME:%H:%M} and the last seating is {LAST_SEATING:%H:%M}; "
            "ask the guest for another time."
        )
    slot = datetime.combine(day, start, tzinfo=TZ)
    if slot <= now:
        raise ToolError(
            f"{date_str} {time_str} is in the past (it is now {now:%Y-%m-%d %H:%M} in Ho Chi Minh City). "
            "Ask the guest for a future date and time."
        )
    return slot


def _clean_text(value: str, field: str, max_length: int, required: bool = False) -> str:
    text = " ".join((value or "").split())
    if required and not text:
        raise ToolError(f"{field} is required.")
    if len(text) > max_length:
        raise ToolError(f"{field} is too long ({len(text)} characters, maximum {max_length}); shorten it.")
    return text


# ------------------------- MCP Tools -------------------------

TOOL_NAMES: list[str] = []

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)

# Argument formats are described here and enforced in the tool bodies, so a bad value comes back
# as a ToolError that says how to fix it (a schema `pattern` would only give a pydantic message).
GuestId = Annotated[str, Field(
    description="Opaque guest identifier supplied by the agent platform (the Zalo user id). "
                "Never ask the guest for it and never invent one.")]
DateArg = Annotated[str, Field(
    description="Date as ISO YYYY-MM-DD in Ho Chi Minh City time, for example '2026-10-17'.")]
TimeArg = Annotated[str, Field(
    description="Start time as 24-hour HH:MM, for example '19:30'. Bookings run 10:00-21:00.")]
PartySize = Annotated[int, Field(
    description=f"Number of guests, 1 to {MAX_PARTY_SIZE}, for example 4.")]


def _tool(annotations: ToolAnnotations):
    """Register a function as an MCP tool and remember its name (used by /health and /)."""
    def register(fn):
        TOOL_NAMES.append(fn.__name__)
        return mcp.tool(annotations=annotations)(fn)
    return register


@_tool(_READ_ONLY)
def restaurant_info() -> dict[str, Any]:
    """Return the restaurant's name, address, phone number and opening hours.

    Use it for questions such as "what time do you close?", "where are you?" or "what is your
    phone number?". Takes no arguments.

    Returns:
        name, address, phone, timezone, opening_hours (open, close, last_seating as HH:MM),
        seating_minutes (how long a booking holds a table) and max_party_size.
    """
    return {
        "name": RESTAURANT_NAME,
        "address": RESTAURANT_ADDRESS,
        "phone": RESTAURANT_PHONE,
        "timezone": str(TZ),
        "opening_hours": {
            "open": f"{OPENING_TIME:%H:%M}",
            "close": f"{CLOSING_TIME:%H:%M}",
            "last_seating": f"{LAST_SEATING:%H:%M}",
        },
        "seating_minutes": SEATING_MINUTES,
        "max_party_size": MAX_PARTY_SIZE,
    }


@_tool(_READ_ONLY)
def get_menu(
    category: Annotated[Literal["all", "khai-vi", "mon-chinh", "nuoc", "trang-mieng"], Field(
        description="Menu section: khai-vi (starters), mon-chinh (mains), nuoc (drinks), "
                    "trang-mieng (desserts) or all (default).")] = "all",
) -> dict[str, Any]:
    """Show the menu, with prices in VND.

    Args:
        category: 'khai-vi', 'mon-chinh', 'nuoc', 'trang-mieng', or 'all' (default) for everything.

    Returns:
        With a category: {"category": ..., "items": [{"id", "name", "price", "note"}]}.
        With 'all': {"menu": {category: [items]}}.

    Errors:
        Unknown category: the message lists the valid ones.
    """
    if category == "all":
        return {"menu": MENU}
    items = MENU.get(category)
    if items is None:
        raise ToolError(f"unknown category {category!r}; use one of: all, {', '.join(MENU)}.")
    return {"category": category, "items": items}


@_tool(_READ_ONLY)
def check_availability(date: DateArg, time: TimeArg, party_size: PartySize) -> dict[str, Any]:
    """List the tables that are free for a date, time and party size.

    A booking holds its table for 2 hours, so a table booked at 19:00 is not free at 19:30.

    Args:
        date: ISO date YYYY-MM-DD, for example '2026-10-17'. Today or later.
        time: 24-hour HH:MM, for example '19:30', between 10:00 and 21:00 (last seating).
        party_size: number of guests, 1 to 12, for example 4.

    Returns:
        {"date", "time", "party_size", "available": [{"table", "seats"}]} with the smallest
        suitable tables first. An empty list means the slot is full: suggest another time.

    Errors:
        Invalid or past date, time outside opening hours, party size out of range.
    """
    slot = _parse_slot(date, time)
    _party(party_size)
    with _db() as conn:
        free = _free_tables(conn, slot.date().isoformat(), time, party_size)
    return {
        "date": date, "time": time, "party_size": party_size,
        "available": [{"table": t, "seats": TABLES[t]} for t in free],
    }


@_tool(_WRITE)
def create_booking(
    guest_id: GuestId,
    customer: Annotated[str, Field(
        description="Guest's name as it should appear on the booking, for example 'Hung'.")],
    date: DateArg,
    time: TimeArg,
    party_size: PartySize,
    table: Annotated[str, Field(
        description="Optional table id such as 'T3'. Leave empty to get the smallest free table that fits.")] = "",
    notes: Annotated[str, Field(
        description="Optional free text: allergies, favourite dishes, special occasion.")] = "",
) -> dict[str, Any]:
    """Book a table for a guest and award loyalty points.

    Confirm the details with the guest before calling it. The booking holds the table for
    2 hours and earns the guest 10 loyalty points (reversed if the booking is cancelled).
    Calling it again for the same guest, date and time does not create a second booking: it
    returns the existing one with "created": false.

    Args:
        guest_id: opaque guest identifier supplied by the agent platform (Zalo user id).
        customer: guest's name for the booking, for example 'Hung'.
        date: ISO date YYYY-MM-DD, today or later, for example '2026-10-17'.
        time: 24-hour HH:MM between 10:00 and 21:00, for example '19:30'.
        party_size: number of guests, 1 to 12, for example 4.
        table: optional table id, for example 'T3'. It must seat the party and be free.
        notes: optional allergies, favourite dishes or occasion, up to 500 characters.

    Returns:
        {"created": true, "booking": {...}, "loyalty_points_earned": 10, "loyalty_points_total": n}
        or, for a repeated call, {"created": false, "booking": {...}} (the existing booking).
        A booking has id, customer, date, time, party_size, table, status, notes, created_at.

    Errors:
        Invalid or past date, time outside opening hours, party size out of range, unknown table,
        table too small or already taken, no table free (call check_availability for other times).
    """
    guest = _guest(guest_id)
    name = _clean_text(customer, "customer", MAX_NAME_LENGTH, required=True)
    note_text = _clean_text(notes, "notes", MAX_NOTES_LENGTH)
    slot = _parse_slot(date, time)
    _party(party_size)
    wanted = (table or "").strip().upper()
    if wanted and wanted not in TABLES:
        raise ToolError(f"table {wanted!r} does not exist; tables are {', '.join(TABLES)}. "
                        "Omit `table` to let the server choose.")
    if wanted and TABLES[wanted] < party_size:
        raise ToolError(f"table {wanted} seats {TABLES[wanted]}, too small for {party_size} guests. "
                        "Pick a bigger table or omit `table` to let the server choose.")

    day = slot.date().isoformat()
    with _db(write=True) as conn:
        existing = conn.execute(
            "SELECT * FROM bookings WHERE guest_id=? AND date=? AND time=? AND status='CONFIRMED'",
            (guest, day, time),
        ).fetchone()
        if existing:
            return {"created": False, "booking": _row_to_booking(existing)}

        free = _free_tables(conn, day, time, party_size)
        if wanted and wanted not in free:
            raise ToolError(
                f"table {wanted} is already booked around {time} on {day} (a booking holds a table for "
                f"{SEATING_MINUTES // 60} hours). "
                + (f"Free tables that fit: {', '.join(free)}." if free else "No table is free for this slot.")
            )
        if not free:
            raise ToolError(
                f"no table for {party_size} guests is free at {time} on {day}. "
                "Call check_availability to look at other times."
            )
        chosen = wanted or free[0]
        booking_id = f"bk-{uuid.uuid4().hex[:8]}"
        created_at = _now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO bookings(id, guest_id, customer, date, time, party_size, table_id, status, notes, created_at) "
            "VALUES(?,?,?,?,?,?,?,'CONFIRMED',?,?)",
            (booking_id, guest, name, day, time, party_size, chosen, note_text, created_at),
        )
        total = _loyalty_add(conn, guest, POINTS_PER_BOOKING, f"Booking {booking_id} confirmed")
    return {
        "created": True,
        "booking": {
            "id": booking_id, "customer": name, "date": day, "time": time, "party_size": party_size,
            "table": chosen, "status": "CONFIRMED", "notes": note_text, "created_at": created_at,
        },
        "loyalty_points_earned": POINTS_PER_BOOKING,
        "loyalty_points_total": total,
    }


@_tool(_READ_ONLY)
def list_bookings(guest_id: GuestId) -> dict[str, Any]:
    """List one guest's upcoming confirmed bookings (those that have not finished yet).

    Only the guest identified by guest_id is ever returned. Cancelled and past bookings are not
    listed, and at most 20 are returned.

    Args:
        guest_id: opaque guest identifier supplied by the agent platform (Zalo user id).

    Returns:
        {"bookings": [...], "count": n, "truncated": bool} ordered by date and time. Each booking
        has id, customer, date, time, party_size, table, status, notes, created_at. `truncated`
        is true when more than 20 upcoming bookings exist and the rest are left out.

    Errors:
        Missing guest_id.
    """
    guest = _guest(guest_id)
    cutoff = (_now().replace(tzinfo=None) - timedelta(minutes=SEATING_MINUTES)).strftime("%Y-%m-%d %H:%M")
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE guest_id=? AND status='CONFIRMED' AND date || ' ' || time > ? "
            "ORDER BY date, time LIMIT ?",
            (guest, cutoff, MAX_LIST + 1),
        ).fetchall()
    bookings = [_row_to_booking(r) for r in rows[:MAX_LIST]]
    return {"bookings": bookings, "count": len(bookings), "truncated": len(rows) > MAX_LIST}


@_tool(_DESTRUCTIVE)
def cancel_booking(
    guest_id: GuestId,
    booking_id: Annotated[str, Field(
        description="Booking id as returned by create_booking or list_bookings, for example 'bk-1a2b3c4d'.")],
) -> dict[str, Any]:
    """Cancel one of the guest's bookings and reverse the loyalty points it earned.

    Confirm with the guest before calling it. A guest can only cancel their own bookings.
    Cancelling an already cancelled booking changes nothing.

    Args:
        guest_id: opaque guest identifier supplied by the agent platform (Zalo user id).
        booking_id: booking id such as 'bk-1a2b3c4d' (from create_booking or list_bookings).

    Returns:
        {"booking": {...}, "loyalty_points_reversed": 10, "loyalty_points_total": n}; the booking has
        status CANCELLED. For a booking that was already cancelled, loyalty_points_reversed is 0.

    Errors:
        Missing guest_id, or no booking with that id for this guest (the same message whether the
        id does not exist or belongs to someone else).
    """
    guest = _guest(guest_id)
    booking_id = (booking_id or "").strip().lower()
    with _db(write=True) as conn:
        row = conn.execute("SELECT * FROM bookings WHERE id=? AND guest_id=?", (booking_id, guest)).fetchone()
        if row is None:
            raise ToolError(
                f"no booking {booking_id!r} found for this guest. Call list_bookings to see the guest's bookings."
            )
        booking = _row_to_booking(row)
        if row["status"] == "CANCELLED":
            reversed_points = 0
            balance = conn.execute("SELECT points FROM loyalty WHERE guest_id=?", (guest,)).fetchone()
            total = balance["points"] if balance else 0
        else:
            conn.execute("UPDATE bookings SET status='CANCELLED' WHERE id=?", (booking_id,))
            booking["status"] = "CANCELLED"
            reversed_points = POINTS_PER_BOOKING
            total = _loyalty_add(conn, guest, -POINTS_PER_BOOKING, f"Booking {booking_id} cancelled")
    return {"booking": booking, "loyalty_points_reversed": reversed_points, "loyalty_points_total": total}


@_tool(_READ_ONLY)
def get_loyalty(guest_id: GuestId) -> dict[str, Any]:
    """Show a guest's loyalty points and their recent history.

    Points are awarded automatically when a booking is created (10 points) and taken back when it
    is cancelled; they cannot be set by any tool.

    Args:
        guest_id: opaque guest identifier supplied by the agent platform (Zalo user id).

    Returns:
        {"points": n, "history": [{"time", "points", "reason"}]} with the 20 latest changes, newest
        first. A guest with no activity has 0 points and an empty history.

    Errors:
        Missing guest_id.
    """
    guest = _guest(guest_id)
    with _db() as conn:
        row = conn.execute("SELECT points FROM loyalty WHERE guest_id=?", (guest,)).fetchone()
        hist = conn.execute(
            "SELECT ts, delta, reason FROM loyalty_history WHERE guest_id=? ORDER BY id DESC LIMIT 20",
            (guest,),
        ).fetchall()
    return {
        "points": row["points"] if row else 0,
        "history": [{"time": h["ts"], "points": h["delta"], "reason": h["reason"]} for h in hist],
    }


# ------------------------- HTTP app -------------------------


def _auth_mode() -> str:
    if API_KEYS:
        return f"api-key ({len(API_KEYS)} key)"
    return "anonymous (ALLOW_ANONYMOUS, local use only)" if ALLOW_ANONYMOUS else "locked (MCP_API_KEYS not set)"


async def health(request):
    return JSONResponse(
        {
            "status": "ok",
            "server": "restaurant-mcp",
            "tools": len(TOOL_NAMES),
            "db": os.path.basename(DB_PATH),
            "db_ok": os.path.exists(DB_PATH),
            "mcp_auth": _auth_mode(),
        }
    )


async def root(request):
    return JSONResponse(
        {
            "server": "restaurant-mcp",
            "mcp_endpoint": "/mcp",
            "auth": "X-Api-Key: <key>  or  Authorization: Bearer <key>",
            "persistence": f"sqlite ({DB_PATH})",
            "tools": TOOL_NAMES,
        }
    )


# streamable_http_app() returns a Starlette app whose lifespan starts the session manager.
# MCP streamable HTTP is served at /mcp by default. Extra routes are appended to this same
# app (do NOT mount it into another app: Mount does not run the sub-app's lifespan).
asgi_app = mcp.streamable_http_app()
asgi_app.router.routes.append(Route("/health", health, methods=["GET"]))
asgi_app.router.routes.append(Route("/", root, methods=["GET"]))


def _extract_key(headers) -> str:
    h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in headers or []}
    if h.get("x-api-key"):
        return h["x-api-key"].strip()
    auth = h.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return ""


def _key_valid(supplied: str) -> bool:
    if not supplied:
        return False
    ok = False
    for good in API_KEYS:  # check every key so timing does not reveal which one matched
        ok |= secrets.compare_digest(supplied.encode(), good.encode())
    return ok


class RequireApiKeyMiddleware:
    """Fail-closed ASGI middleware for /mcp.

    - MCP_API_KEYS set -> a valid key is mandatory (401 when missing or wrong).
    - No key and ALLOW_ANONYMOUS -> open (local development only).
    - No key and no ALLOW_ANONYMOUS -> 503; the server never opens itself.
    - /health and / are always open (health probes must return 200).
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    async def _reply(send, status: int, message: str, extra=()):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), *extra]})
        await send({"type": "http.response.body",
                    "body": json.dumps({"error": message}, ensure_ascii=False).encode()})

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") == "http" and (path.rstrip("/") == "/mcp" or path.startswith("/mcp/")):
            if not API_KEYS:
                if not ALLOW_ANONYMOUS:
                    return await self._reply(send, 503, "MCP_API_KEYS is not configured (fail-closed)")
            elif not _key_valid(_extract_key(scope.get("headers"))):
                client = (scope.get("client") or ("?",))[0]
                log.warning("401 on /mcp from %s: missing or invalid API key", client)
                return await self._reply(send, 401, "missing or invalid API key (X-Api-Key / Bearer)",
                                         [(b"www-authenticate", b'Bearer realm="restaurant-mcp"')])
        await self.app(scope, receive, send)


app = RequireApiKeyMiddleware(asgi_app)


if __name__ == "__main__":
    import uvicorn

    log.info("restaurant-mcp | %d tools | auth: %s | db: %s", len(TOOL_NAMES), _auth_mode(), DB_PATH)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
