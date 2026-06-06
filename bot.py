"""
Filip's BASE — Leaderboard Bot
================================
A Telegram bot that captures member lifting stats, writes them to a Google
Sheet, and posts native leaderboards inside Telegram.

Design goals:
  * Minimal effort for the user — taps wherever possible, every lift skippable.
  * Captures the numeric Telegram ID automatically (works even for users with
    no @username), which is the whole reason this is a bot and not a form.
  * Two-tab sheet: a PUBLIC 'board' tab (no Telegram IDs) and a PRIVATE
    'mapping' tab (Telegram ID <-> IG, for retention DMs).

All secrets come from environment variables. Nothing sensitive is in this file.
  BOT_TOKEN            - from @BotFather
  SPREADSHEET_ID       - the long id in your Google Sheet URL
  GOOGLE_CREDENTIALS   - the full service-account JSON (as a single-line string)
  ADMIN_IDS            - comma-separated Telegram user IDs allowed to remove rows
                         (optional; leave unset if you don't need it)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
log = logging.getLogger("base-bot")

# --------------------------------------------------------------------------- #
#  Lift configuration — change here to add/remove/rename boards
# --------------------------------------------------------------------------- #
# key: internal id  |  label: shown to user  |  unit: kg or reps  |  col: sheet header
LIFTS = [
    {"key": "pullup_kg", "label": "Pull-up — added weight", "unit": "kg",
     "prompt": "Weighted pull-up — heaviest *added* weight in kg?\n(just the number, e.g. 60)",
     "header": "Pull-up 1RM (added kg)"},
    {"key": "pullup_reps", "label": "Pull-up — max reps", "unit": "reps",
     "prompt": "Pull-up — max reps at *bodyweight*?\n(just the number, e.g. 25)",
     "header": "Pull-up max reps (BW)"},
    {"key": "muscleup_kg", "label": "Muscle-up — added weight", "unit": "kg",
     "prompt": "Weighted muscle-up — heaviest *added* weight in kg?\n(just the number, e.g. 40)",
     "header": "Muscle-up 1RM (added kg)"},
    {"key": "dip_kg", "label": "Dip — added weight", "unit": "kg",
     "prompt": "Weighted dip — heaviest *added* weight in kg?\n(just the number, e.g. 50)",
     "header": "Dip 1RM (added kg)"},
    {"key": "squat_kg", "label": "Squat — added weight", "unit": "kg",
     "prompt": "Weighted squat — heaviest *added* weight in kg?\n(just the number, e.g. 80)",
     "header": "Squat 1RM (added kg)"},
]
LIFT_BY_KEY = {l["key"]: l for l in LIFTS}

MIN_ENTRIES_TO_SHOW = 3  # a board stays hidden until it has at least this many entries

# Public sheet columns, in order
BOARD_HEADERS = (
    ["Telegram ID", "Display name", "Bodyweight (kg)"]
    + [l["header"] for l in LIFTS]
    + ["Instagram", "Nationality", "Updated"]
)
# NOTE: 'Telegram ID' lives in column A of the board tab too, used ONLY as the
# internal row key so /update can find a member's row. It is never shown in any
# posted leaderboard. The separate 'mapping' tab is the retention surface.

MAPPING_HEADERS = ["Telegram ID", "Username", "Display name", "Instagram", "Updated"]

# --------------------------------------------------------------------------- #
#  Conversation states
# --------------------------------------------------------------------------- #
(
    NAME,
    BODYWEIGHT,
    INSTAGRAM,
    LIFT,          # generic lift-capture state; index tracked in user_data
    CONFIRM,
    UPDATE_PICK,   # choosing which field to edit
    UPDATE_VALUE,  # entering the new value
) = range(7)


# --------------------------------------------------------------------------- #
#  Google Sheets backend
# --------------------------------------------------------------------------- #
class Sheet:
    """Thin wrapper around the two worksheets. Caches the gspread client."""

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

    def __init__(self) -> None:
        creds_json = os.environ["GOOGLE_CREDENTIALS"]
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=self.SCOPES)
        self.gc = gspread.authorize(creds)
        self.ss = self.gc.open_by_key(os.environ["SPREADSHEET_ID"])
        self.board = self._ensure_ws("board", BOARD_HEADERS)
        self.mapping = self._ensure_ws("mapping", MAPPING_HEADERS)

    def _ensure_ws(self, title: str, headers: list[str]):
        try:
            ws = self.ss.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.ss.add_worksheet(title=title, rows=1000, cols=len(headers))
        # make sure the header row matches (write it if the sheet is empty)
        existing = ws.row_values(1)
        if existing != headers:
            ws.update([headers], "A1", value_input_option="RAW")
        return ws

    # -- board operations -------------------------------------------------- #
    def find_row(self, tg_id: int) -> int | None:
        """Return the 1-based row number for a telegram id, or None."""
        ids = self.board.col_values(1)  # column A = Telegram ID
        target = str(tg_id)
        for i, val in enumerate(ids[1:], start=2):  # skip header
            if val == target:
                return i
        return None

    def upsert(self, entry: dict) -> None:
        """Insert or update a member's board row and mapping row."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tg_id = entry["tg_id"]
        board_row = [
            str(tg_id),
            entry.get("name", ""),
            _num_or_blank(entry.get("bodyweight")),
        ]
        for l in LIFTS:
            board_row.append(_num_or_blank(entry.get(l["key"])))
        board_row += [
            entry.get("instagram", ""),
            entry.get("nationality", ""),
            now,
        ]
        row_num = self.find_row(tg_id)
        if row_num:
            self.board.update(
                [board_row], f"A{row_num}", value_input_option="USER_ENTERED"
            )
        else:
            self.board.append_row(board_row, value_input_option="USER_ENTERED")

        # mapping tab (private)
        map_ids = self.mapping.col_values(1)
        map_row = [
            str(tg_id),
            entry.get("username", ""),
            entry.get("name", ""),
            entry.get("instagram", ""),
            now,
        ]
        if str(tg_id) in map_ids:
            r = map_ids.index(str(tg_id)) + 1
            self.mapping.update([map_row], f"A{r}", value_input_option="RAW")
        else:
            self.mapping.append_row(map_row, value_input_option="RAW")

    def get_entry(self, tg_id: int) -> dict | None:
        row_num = self.find_row(tg_id)
        if not row_num:
            return None
        values = self.board.row_values(row_num)
        # pad to header length
        values += [""] * (len(BOARD_HEADERS) - len(values))
        record = dict(zip(BOARD_HEADERS, values))
        return record

    def delete_entry(self, tg_id: int) -> bool:
        row_num = self.find_row(tg_id)
        if not row_num:
            return False
        self.board.delete_rows(row_num)
        map_ids = self.mapping.col_values(1)
        if str(tg_id) in map_ids:
            self.mapping.delete_rows(map_ids.index(str(tg_id)) + 1)
        return True

    def all_rows(self) -> list[dict]:
        records = self.board.get_all_records(expected_headers=BOARD_HEADERS)
        return records

    def members_with_ig(self) -> list[tuple[str, str]]:
        """(display name, instagram) for everyone who shared an IG."""
        out = []
        for r in self.all_rows():
            ig = str(r.get("Instagram", "")).strip()
            if ig:
                out.append((str(r.get("Display name", "")).strip() or "—", ig))
        return out


def _num_or_blank(v):
    """Return a rounded number for the sheet, or '' if not set."""
    if v is None or v == "":
        return ""
    try:
        f = float(v)
        return int(f) if f == int(f) else round(f, 1)
    except (TypeError, ValueError):
        return ""


# single global sheet handle, created lazily so import doesn't require creds
_sheet: Sheet | None = None


def sheet() -> Sheet:
    global _sheet
    if _sheet is None:
        _sheet = Sheet()
    return _sheet


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def parse_number(text: str) -> float | None:
    """Lenient number parse: strips kg, +, spaces, commas-as-decimals."""
    if text is None:
        return None
    s = text.strip().lower()
    s = s.replace("kg", "").replace("+", "").replace(",", ".").strip()
    try:
        f = float(s)
        if f < 0 or f > 1000:  # sanity bounds
            return None
        return f
    except ValueError:
        return None


def admin_ids() -> set[int]:
    raw = os.environ.get("ADMIN_IDS", "")
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def entry_summary(d: dict) -> str:
    """Pretty multi-line summary of a draft entry (uses internal keys)."""
    lines = [f"🏷  *{_md(d.get('name','—'))}*"]
    if d.get("instagram"):
        lines[0] += f"   ·   📷 {_md(d['instagram'])}"
    bw = d.get("bodyweight")
    if bw:
        lines.append(f"⚖️  {_num_or_blank(bw)} kg")
    lift_bits = []
    for l in LIFTS:
        v = d.get(l["key"])
        if v not in (None, ""):
            suffix = "kg" if l["unit"] == "kg" else " reps"
            lift_bits.append(f"{l['label'].split(' — ')[0]} {_num_or_blank(v)}{suffix}")
    if lift_bits:
        lines.append("💪  " + "  ·  ".join(lift_bits))
    if d.get("nationality"):
        lines.append(f"🌍  {_md(d['nationality'])}")
    return "\n".join(lines)


def _md(text: str) -> str:
    """Escape the characters that break Markdown (v1)."""
    if text is None:
        return ""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


# --------------------------------------------------------------------------- #
#  /start  — onboarding conversation
# --------------------------------------------------------------------------- #
WELCOME = (
    "🦍 *Welcome to the BASE leaderboard.*\n\n"
    "Takes about 30 seconds. You can update your numbers any time with /update.\n\n"
    "Every lift is optional — just skip the ones you don't train."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    context.user_data.clear()
    context.user_data["tg_id"] = user.id
    context.user_data["username"] = user.username or ""

    await update.message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN)

    # Prefill display name from Telegram
    default_name = user.first_name or (user.username or "Athlete")
    context.user_data["_default_name"] = default_name

    kb = ReplyKeyboardMarkup(
        [[f"Use “{default_name}”"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "First — how should you show up on the board?\n"
        f"Tap to use *{_md(default_name)}*, or type a different name.",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return NAME


async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    default = context.user_data.get("_default_name", "")
    if text == f"Use “{default}”":
        text = default
    context.user_data["name"] = text[:40]
    await update.message.reply_text(
        "Your *Instagram* handle?",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.MARKDOWN,
    )
    return INSTAGRAM


async def got_bodyweight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    n = parse_number(update.message.text)
    if n is None:
        await update.message.reply_text("Just the number please — like 78")
        return BODYWEIGHT
    context.user_data["bodyweight"] = n
    context.user_data["_lift_idx"] = 0
    return await ask_lift(update, context)


async def got_instagram(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    handle = update.message.text.strip().lstrip("@")[:40]
    if not handle:
        await update.message.reply_text(
            "Instagram is required — please type your handle, e.g. @yourname"
        )
        return INSTAGRAM
    context.user_data["instagram"] = "@" + handle
    await update.message.reply_text(
        "Your *bodyweight* in kg? (just the number, e.g. 78)",
        parse_mode=ParseMode.MARKDOWN,
    )
    return BODYWEIGHT


async def ask_lift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    idx = context.user_data["_lift_idx"]
    if idx >= len(LIFTS):
        return await show_confirm(update, context)
    lift = LIFTS[idx]
    kb = ReplyKeyboardMarkup([["Skip"]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(
        lift["prompt"], reply_markup=kb, parse_mode=ParseMode.MARKDOWN
    )
    return LIFT


async def got_lift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    idx = context.user_data["_lift_idx"]
    lift = LIFTS[idx]
    text = update.message.text.strip()
    if text.lower() == "skip":
        pass
    else:
        n = parse_number(text)
        if n is None:
            await update.message.reply_text("Just the number please — or tap Skip")
            return LIFT
        # reps should be whole numbers
        context.user_data[lift["key"]] = int(n) if lift["unit"] == "reps" else n
    context.user_data["_lift_idx"] = idx + 1
    return await ask_lift(update, context)


async def show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    summary = entry_summary(context.user_data)
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Looks good", callback_data="confirm_save")],
            [InlineKeyboardButton("✏️ Start over", callback_data="confirm_restart")],
        ]
    )
    await update.message.reply_text(
        "Here's your entry:\n\n" + summary + "\n\nGood to go?",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return CONFIRM


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "confirm_restart":
        await query.edit_message_text("No problem — send /start to go again.")
        return ConversationHandler.END

    # save
    try:
        sheet().upsert(dict(context.user_data))
    except Exception as e:  # noqa: BLE001
        log.exception("save failed")
        await query.edit_message_text(
            "Something went wrong saving your entry. Please try /start again in a moment."
        )
        return ConversationHandler.END

    await query.edit_message_text(
        "You're on the board. 🦍\n\n"
        "Post a new PR any time with /update.\n"
        "See the rankings with /leaderboard.\n\n"
        "See you in the chat. 💪"
    )
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
#  /update  — edit one field
# --------------------------------------------------------------------------- #
async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg_id = update.effective_user.id
    entry = sheet().get_entry(tg_id)
    if not entry:
        await update.message.reply_text(
            "You're not on the board yet — send /start to add yourself first."
        )
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["tg_id"] = tg_id
    context.user_data["username"] = update.effective_user.username or ""
    # load existing values into draft (internal keys)
    context.user_data["name"] = entry.get("Display name", "")
    context.user_data["bodyweight"] = entry.get("Bodyweight (kg)", "")
    context.user_data["instagram"] = entry.get("Instagram", "")
    context.user_data["nationality"] = entry.get("Nationality", "")
    for l in LIFTS:
        context.user_data[l["key"]] = entry.get(l["header"], "")

    buttons = [
        [InlineKeyboardButton("⚖️ Bodyweight", callback_data="edit_bodyweight")],
    ]
    for l in LIFTS:
        buttons.append(
            [InlineKeyboardButton(f"💪 {l['label']}", callback_data=f"edit_{l['key']}")]
        )
    buttons.append([InlineKeyboardButton("📷 Instagram", callback_data="edit_instagram")])
    buttons.append([InlineKeyboardButton("🏷 Display name", callback_data="edit_name")])
    buttons.append([InlineKeyboardButton("🗑 Remove me from the board", callback_data="edit_delete")])

    await update.message.reply_text(
        "Your current entry:\n\n" + entry_summary(context.user_data) +
        "\n\nWhat do you want to change?",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN,
    )
    return UPDATE_PICK


async def update_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data.replace("edit_", "")

    if field == "delete":
        sheet().delete_entry(context.user_data["tg_id"])
        await query.edit_message_text("Done — you've been removed from the board.")
        return ConversationHandler.END

    context.user_data["_edit_field"] = field
    if field == "name":
        prompt = "Type your new display name:"
    elif field == "instagram":
        prompt = "Type your Instagram handle (or 'none' to clear it):"
    elif field == "bodyweight":
        prompt = "Type your bodyweight in kg:"
    else:
        lift = LIFT_BY_KEY[field]
        unit = "kg (added)" if lift["unit"] == "kg" else "reps"
        prompt = f"Type your new {lift['label'].split(' — ')[0]} — {unit}:"
    await query.edit_message_text(prompt)
    return UPDATE_VALUE


async def update_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    field = context.user_data["_edit_field"]
    text = update.message.text.strip()

    if field == "name":
        context.user_data["name"] = text[:40]
    elif field == "instagram":
        if text.lower() in ("none", "clear", "-"):
            context.user_data["instagram"] = ""
        else:
            context.user_data["instagram"] = "@" + text.lstrip("@")[:40]
    else:
        n = parse_number(text)
        if n is None:
            await update.message.reply_text("Just the number please.")
            return UPDATE_VALUE
        if field == "bodyweight":
            context.user_data["bodyweight"] = n
        else:
            lift = LIFT_BY_KEY[field]
            context.user_data[field] = int(n) if lift["unit"] == "reps" else n

    try:
        sheet().upsert(dict(context.user_data))
    except Exception:  # noqa: BLE001
        log.exception("update save failed")
        await update.message.reply_text("Couldn't save that — try /update again shortly.")
        return ConversationHandler.END

    await update.message.reply_text(
        "Updated. ✅\n\nSee where you land: /leaderboard"
    )
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
#  /leaderboard  — pick a lift, then render the board
# --------------------------------------------------------------------------- #
async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    buttons = [
        [InlineKeyboardButton(l["label"], callback_data=f"board_{l['key']}")]
        for l in LIFTS
    ]
    await update.message.reply_text(
        "Which board?", reply_markup=InlineKeyboardMarkup(buttons)
    )


def _ranked_for(lift: dict) -> list[dict]:
    """Read the board and return every entry with a numeric value for this lift,
    sorted high → low. Shared by the top-10 and full-board views."""
    ranked = []
    for r in sheet().all_rows():
        raw = r.get(lift["header"], "")
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        ranked.append({
            "id": str(r.get("Telegram ID", "")),
            "name": str(r.get("Display name", "")).strip() or "—",
            "bw": r.get("Bodyweight (kg)", ""),
            "val": val,
        })
    ranked.sort(key=lambda x: x["val"], reverse=True)
    return ranked


async def board_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    key = query.data.replace("board_", "")
    lift = LIFT_BY_KEY[key]
    viewer_id = str(update.effective_user.id)

    ranked = _ranked_for(lift)
    if len(ranked) < MIN_ENTRIES_TO_SHOW:
        await query.edit_message_text(
            f"*{lift['label']}*\n\nNot enough entries yet "
            f"({len(ranked)}/{MIN_ENTRIES_TO_SHOW}). Be one of the first — /start",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    text = render_board(lift, ranked, viewer_id)
    # Offer the complete ranking only when there's more than the top 10 to see.
    reply_markup = None
    if len(ranked) > 10:
        reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"📋 Full board ({len(ranked)})",
                                   callback_data=f"fullboard_{key}")]]
        )
    await query.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup
    )


def render_board(lift: dict, ranked: list[dict], viewer_id: str) -> str:
    """Monospace leaderboard. Top 10 + the viewer's own line if outside top 10."""
    unit = "" if lift["unit"] == "reps" else ""
    fmt = (lambda v: f"{int(v)}") if lift["unit"] == "reps" else (lambda v: f"{v:g}")

    title = f"🦍 BASE — {lift['label']}"
    header = f"{'#':>2}  {'ATHLETE':<14}{'BW':>5}  {'BEST':>6}"
    sep = "─" * len(header)
    lines = [f"```\n{title}\n\n{header}\n{sep}"]

    viewer_rank = None
    for i, e in enumerate(ranked, start=1):
        if e["id"] == viewer_id:
            viewer_rank = i

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, e in enumerate(ranked[:10], start=1):
        name = (e["name"][:13] + "…") if len(e["name"]) > 14 else e["name"]
        bw = f"{e['bw']}" if e["bw"] != "" else "—"
        you = "  ← you" if e["id"] == viewer_id else ""
        if i in medals:
            # emoji renders ~2 cols wide, so use one less space to keep alignment
            rank_cell = f"{medals[i]} "
        else:
            rank_cell = f"{i:>2} "
        lines.append(f"{rank_cell} {name:<14}{bw:>5}  {fmt(e['val']):>6}{you}")

    # viewer outside the top 10 — append their line + gap to next
    if viewer_rank and viewer_rank > 10:
        me = ranked[viewer_rank - 1]
        above = ranked[viewer_rank - 2]
        gap = above["val"] - me["val"]
        gap_txt = f"{gap:g}" if lift["unit"] == "kg" else f"{int(gap)}"
        lines.append("…")
        lines.append(
            f"{viewer_rank:>2}  {me['name'][:14]:<14}"
            f"{me['bw']:>5}  {fmt(me['val']):>6}  ← you"
        )
        unit_word = "kg" if lift["unit"] == "kg" else "reps"
        lines.append(f"\n{gap_txt} {unit_word} off #{viewer_rank - 1}.")

    lines.append("```")
    lines.append("\n_Update yours: /update_")
    return "\n".join(lines)


async def board_full(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Expand the top-10 message into the COMPLETE ranking, paged across
    messages so it never hits Telegram's 4096-char limit."""
    query = update.callback_query
    await query.answer()
    key = query.data.replace("fullboard_", "")
    lift = LIFT_BY_KEY[key]
    viewer_id = str(update.effective_user.id)

    ranked = _ranked_for(lift)
    if not ranked:
        await query.edit_message_text("No entries yet — /start to be the first.")
        return

    messages = render_full_board(lift, ranked, viewer_id)
    # Replace the top-10 message in place with the first page, then post the rest.
    await query.edit_message_text(messages[0], parse_mode=ParseMode.MARKDOWN)
    for msg in messages[1:]:
        await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


def render_full_board(lift: dict, ranked: list[dict], viewer_id: str) -> list[str]:
    """The complete ranking as a list of monospace messages, each a self-contained
    code block kept under Telegram's 4096-char cap. Header repeats on every page."""
    fmt = (lambda v: f"{int(v)}") if lift["unit"] == "reps" else (lambda v: f"{v:g}")
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    header = f"{'#':>2}  {'ATHLETE':<14}{'BW':>5}  {'BEST':>6}"
    sep = "─" * len(header)
    title = f"🦍 BASE — {lift['label']}  ·  {len(ranked)} athletes"

    row_lines = []
    for i, e in enumerate(ranked, start=1):
        name = (e["name"][:13] + "…") if len(e["name"]) > 14 else e["name"]
        bw = f"{e['bw']}" if e["bw"] != "" else "—"
        you = "  ← you" if e["id"] == viewer_id else ""
        rank_cell = f"{medals[i]} " if i in medals else f"{i:>2} "
        row_lines.append(f"{rank_cell} {name:<14}{bw:>5}  {fmt(e['val']):>6}{you}")

    LIMIT = 3500  # headroom under Telegram's 4096 hard cap
    messages: list[str] = []
    i = 0
    first = True
    while i < len(row_lines):
        body = (f"{title}\n\n" if first else "") + f"{header}\n{sep}"
        start = i
        while i < len(row_lines) and len(body) + 1 + len(row_lines[i]) <= LIMIT:
            body += "\n" + row_lines[i]
            i += 1
        if i == start:  # safety: guarantee progress even on a pathological row
            body += "\n" + row_lines[i]
            i += 1
        messages.append(f"```\n{body}\n```")
        first = False
    messages[-1] += "\n\n_Update yours: /update_"
    return messages


# --------------------------------------------------------------------------- #
#  /members  — the IG directory (since a text board can't be clickable)
# --------------------------------------------------------------------------- #
async def members_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pairs = sheet().members_with_ig()
    if not pairs:
        await update.message.reply_text("No one's shared an Instagram yet.")
        return
    pairs.sort(key=lambda p: p[0].lower())
    lines = ["*BASE — follow each other on IG* 📷\n"]
    for name, ig in pairs:
        handle = ig.lstrip("@")
        lines.append(f"• {_md(name)} — [@{handle}](https://instagram.com/{handle})")
    text = "\n".join(lines)
    # Telegram messages cap at 4096 chars; chunk if needed
    for chunk in _chunks(text, 3900):
        await update.message.reply_text(
            chunk, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
        )


def _chunks(text: str, size: int):
    lines = text.split("\n")
    buf = ""
    for ln in lines:
        if len(buf) + len(ln) + 1 > size:
            yield buf
            buf = ""
        buf += ln + "\n"
    if buf:
        yield buf


# --------------------------------------------------------------------------- #
#  misc
# --------------------------------------------------------------------------- #
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Cancelled.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🦍 *BASE leaderboard*\n\n"
        "/start — add yourself to the board\n"
        "/update — change a number or your details\n"
        "/leaderboard — see the rankings\n"
        "/members — follow other members on IG\n",
        parse_mode=ParseMode.MARKDOWN,
    )


def build_app() -> Application:
    token = os.environ["BOT_TOKEN"]
    app = Application.builder().token(token).build()

    onboarding = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
            BODYWEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_bodyweight)],
            INSTAGRAM: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_instagram)],
            LIFT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_lift)],
            CONFIRM: [CallbackQueryHandler(on_confirm, pattern="^confirm_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    updating = ConversationHandler(
        entry_points=[CommandHandler("update", update_cmd)],
        states={
            UPDATE_PICK: [CallbackQueryHandler(update_pick, pattern="^edit_")],
            UPDATE_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(onboarding)
    app.add_handler(updating)
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CallbackQueryHandler(board_show, pattern="^board_"))
    app.add_handler(CallbackQueryHandler(board_full, pattern="^fullboard_"))
    app.add_handler(CommandHandler("members", members_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("start_help", help_cmd))
    return app


def main() -> None:
    app = build_app()
    log.info("BASE leaderboard bot starting (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
