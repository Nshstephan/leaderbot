# BASE Leaderboard Bot — Setup Guide

This gets the bot live. No server experience needed. Three stages:

1. **Telegram** — create the bot, get a token (~3 min)
2. **Google** — make the sheet + a service account so the bot can write to it (~10 min)
3. **Deploy** — push the code to a free host that runs it 24/7 (~10 min)

You only do this once. Take it slow; each step is copy-paste.

> **The golden rule:** the bot token and the Google JSON are passwords. Never
> paste them into a chat, never commit them to GitHub. They go *only* into the
> host's "environment variables" box (Stage 3).

---

## STAGE 1 — Create the Telegram bot

1. In Telegram, search for **@BotFather** (the official one, has a blue check).
2. Send `/newbot`.
3. Give it a name (e.g. `Filip's BASE Leaderboard`) and a username ending in
   `bot` (e.g. `filip_base_leaderboard_bot`).
4. BotFather replies with a line like:
   `Use this token to access the HTTP API: 123456789:AAH...`
   **That long string is your `BOT_TOKEN`.** Copy it somewhere safe for now.
5. Optional but nice — still in BotFather, send `/setcommands`, pick your bot,
   and paste:
   ```
   start - Add yourself to the leaderboard
   update - Update your numbers
   leaderboard - See the rankings
   members - Follow other members on IG
   help - What this bot does
   ```
   This makes the commands show up in a menu in the chat.

---

## STAGE 2 — Google Sheet + service account

The bot writes to a normal Google Sheet. To let a program write to it, Google
needs a "service account" — basically a robot Google user with its own login
file. Sounds scary, it's just clicking through a wizard.

### 2a. Make the sheet

1. Create a new Google Sheet. Name it anything (e.g. `BASE Leaderboard`).
2. Look at the URL:
   `https://docs.google.com/spreadsheets/d/`**`1AbC...xyz`**`/edit`
   The bold part is your **`SPREADSHEET_ID`**. Copy it.
3. That's it — leave the sheet empty. The bot creates the `board` and `mapping`
   tabs and their headers automatically on first run.

### 2b. Create the service account

1. Go to **https://console.cloud.google.com/** and sign in.
2. Top bar → project dropdown → **New Project** → name it `base-leaderboard`
   → Create. Wait a few seconds, then make sure it's the selected project.
3. In the search bar at the top, type **"Google Sheets API"**, open it, click
   **Enable**.
4. Search the top bar for **"Service Accounts"** (under IAM & Admin) and open it.
5. **+ Create Service Account** → name it `base-bot` → Create and Continue →
   skip the optional role steps → **Done**.
6. You're back on the list. Click the service account you just made
   (an email like `base-bot@base-leaderboard.iam.gserviceaccount.com`).
   **Copy that email — you need it in step 2c.**
7. Open the **Keys** tab → **Add Key** → **Create new key** → **JSON** → Create.
   A `.json` file downloads. **This is your `GOOGLE_CREDENTIALS`.** Keep it safe.

### 2c. Share the sheet with the robot

The robot can't see your sheet until you share it, exactly like sharing with a
person:

1. Open your Google Sheet → **Share**.
2. Paste the service-account **email** from step 2b.6.
3. Give it **Editor** access. Untick "notify". Share.

Done. The robot can now read and write that one sheet and nothing else.

---

## STAGE 3 — Deploy to a free host (Railway)

The bot is a small program that must run constantly to answer messages. Railway
runs it for free at your scale, with no server to manage. (Render and Fly.io
work too; Railway is the least fiddly.)

### 3a. Put the code on GitHub

The host pulls the code from GitHub.

1. Make a free account at **https://github.com**.
2. Create a **new repository** (the green "New" button), name it
   `base-leaderboard`, set it to **Private**, Create.
3. Upload the three files — `bot.py`, `requirements.txt`, and `Procfile` —
   using GitHub's **"uploading an existing file"** link on the empty repo page.
   (Drag them in, then "Commit changes".)
   > Do **NOT** upload your `.json` key or any `.env` file. Code only.

### 3b. Deploy on Railway

1. Make a free account at **https://railway.app** (sign in with GitHub — easiest).
2. **New Project** → **Deploy from GitHub repo** → pick `base-leaderboard`.
   Authorize Railway to see the repo if it asks.
3. Railway starts building. It'll fail the first time because the secrets aren't
   set yet — that's expected. Set them now:
4. Open your project → the service → **Variables** tab → add these four
   (click "New Variable" / "Raw Editor"):

   | Name | Value |
   |---|---|
   | `BOT_TOKEN` | the token from Stage 1 |
   | `SPREADSHEET_ID` | the id from step 2a |
   | `GOOGLE_CREDENTIALS` | the **entire contents** of the `.json` file, pasted as-is |
   | `ADMIN_IDS` | leave blank (or your Telegram numeric id) |

   For `GOOGLE_CREDENTIALS`: open the `.json` in a text editor, select all, copy,
   paste the whole thing into the value box. It's long and full of braces — that's
   correct. Paste it exactly.
5. Railway redeploys automatically. Watch the **Deploy Logs** — you want to see:
   `BASE leaderboard bot starting (polling)…`
   If you see that, **it's live.**

### 3c. Test it

1. Open Telegram, find your bot, send `/start`.
2. Walk through the flow. Confirm.
3. Open your Google Sheet — your row should be in the `board` tab, and your
   Telegram id + IG in the `mapping` tab.
4. Send `/leaderboard` (you'll need 3 entries before a board shows — add a
   couple of test ones, or lower `MIN_ENTRIES_TO_SHOW` in `bot.py` temporarily).

That's the whole thing. Once it's running, you don't touch it again.

---

## Day-to-day

- **Members add themselves:** they open the bot and `/start`. Put the bot link
  in Filip's welcome DM and pin it in the chat.
- **Members log a PR:** `/update`, tap the field, type the number.
- **Show the board:** anyone sends `/leaderboard`. You can also paste a board
  into the chat yourself and pin it.
- **Reach quiet members:** the `mapping` tab has Telegram id + IG for everyone.
  (Remember the win-back pacing rules — ~15 DMs/day from a personal account.)

## Changing things later

- **Add/remove a lift or rename a board:** edit the `LIFTS` list at the top of
  `bot.py`, commit to GitHub — Railway redeploys itself.
- **Change how many entries before a board appears:** `MIN_ENTRIES_TO_SHOW`.
- **Make the board prettier (image instead of text):** that's a later upgrade —
  the bot can render a PNG and post it as a photo. Ask when you want it.

## If something breaks

- Bot silent? Check Railway **Deploy Logs** for a red error line.
- "GOOGLE_CREDENTIALS" / auth errors → the JSON was pasted incompletely, or you
  forgot to share the sheet with the robot email (step 2c).
- Can't open the sheet → `SPREADSHEET_ID` wrong, or sheet not shared.
- Token errors → `BOT_TOKEN` mistyped.
