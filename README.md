# ♞ Chess Puzzle LINE Bot

A LINE bot that posts a chess puzzle to your group every day. The puzzle comes as a card with:

- the **board picture**
- a **💡 Show solution** button (the bot replies with the answer)
- **Opening / Middlegame / Endgame** buttons that fetch a new puzzle of that type
- a link to solve it interactively on Lichess

Everything is free: puzzles come from the public [Lichess API](https://lichess.org/api#tag/Puzzles),
the board is drawn locally with Pillow, and **all button taps are answered with LINE *reply*
messages, which don't count toward your monthly quota**. Only the daily post uses the quota
(1 message × number of group members).

```
            08:00 daily ──► push card ──► LINE group
                                             │  tap "Endgame" / "Show solution"
LINE servers ──► POST /callback ──► this app ─┴─► reply (free)
LINE app     ──► GET  /board/<id>.png  (board picture)
```

---

## 1. Set up in VS Code

Open this folder in VS Code, then in the terminal run once:

```bash
bash setup.sh
```

It creates `.env` (from `.env.example`, with a random `ADMIN_KEY`), copies the run/debug configs
into `.vscode/`, makes a `.venv` and installs the packages, then runs the tests.
Select the `.venv` interpreter if VS Code asks.

Check that it works **without LINE**:

```bash
pytest -q                          # 10 offline tests
python scripts/preview.py endgame  # fetches a real puzzle, saves preview_<id>.png, prints the solution
```

(These are also in VS Code's **Run and Debug** panel.)

## 2. Create the LINE bot (about 5 minutes)

1. Create a LINE Official Account at [manager.line.biz](https://manager.line.biz) (free plan), then
   **Settings → Messaging API → Enable Messaging API** and pick/create a provider.
   (Since Sept 2024, Messaging API channels are switched on from the Official Account like this.)
2. Copy the **Channel ID** and **Channel secret** into `LINE_CHANNEL_ID` and `LINE_CHANNEL_SECRET` in `.env`
   (Developers Console → *Basic settings* tab, or Official Account Manager → *Settings → Messaging API*).
   That's enough: the bot issues its own 15-minute access token and renews it automatically.
3. *(Optional)* If you prefer a long-lived token: *Messaging API* tab → bottom →
   **Channel access token (long-lived)** → *Issue* → `LINE_CHANNEL_ACCESS_TOKEN`. It's used instead when set.
4. In LINE Official Account Manager → **Settings → Response settings**:
   - **Allow bot to join group chats** → *Enabled*  ← required
   - **Auto-reply messages** → *Disabled*, **Greeting messages** → *Disabled* (optional, stops duplicate replies)
   - **Webhooks** → *Enabled*

## 3. Give your laptop a public HTTPS URL

LINE must reach your laptop for the webhook and the board images. Use either tool:

```bash
# Option A: Cloudflare Tunnel (free, no account, no warning page)
cloudflared tunnel --url http://localhost:8000

# Option B: ngrok (free account)
ngrok http 8000
```

Copy the `https://….trycloudflare.com` or `https://….ngrok-free.app` URL into `BASE_URL` in `.env`.

## 4. Run it

Press **F5 → "Run bot (uvicorn)"**, or:

```bash
uvicorn app.main:app --reload --port 8000
```

In the LINE console → Messaging API tab → **Webhook URL** = `https://<your-tunnel>/callback`
→ **Update** → **Verify** (it should say *Success*) → turn on **Use webhook**.

## 5. Add it to your group

1. Scan the bot's QR code (Messaging API tab) with your phone and add it as a friend.
2. Open your group → **Invite** → choose the bot.
3. The bot says hi and shows the menu. The group ID is saved automatically in `data/groups.json`.

Try these in the group:

| Type / tap | What happens |
|---|---|
| tap **Opening / Middlegame / Endgame** | new puzzle of that type |
| tap **💡 Show solution** | bot replies with the answer, move by move |
| `/chess` | shows the menu card |
| `/chess daily` | today's Lichess daily puzzle |
| `/chess opening` · `/chess middlegame` · `/chess endgame` | same as the buttons |
| `/หมากรุก ท้ายเกม` (also `เปิดเกม`, `กลางเกม`) | Thai versions |

Messages without the `/chess` (or `/หมากรุก`) prefix are ignored, so normal chat never triggers the bot.
Change the prefixes with `COMMAND_PREFIXES` in `.env`.

**Group ID:** you don't need to look it up. The bot saves it when it's invited (or when anyone
types in the group). To see it: the terminal logs `Joined group C…`, or open `data/groups.json`,
or call `GET /admin/groups` in `requests.http`.

To test the daily post without waiting until 08:00 (this **uses quota**), use `requests.http` in VS Code
(REST Client extension) or run:

```bash
curl -X POST http://localhost:8000/admin/push-daily -H "X-Admin-Key: <your ADMIN_KEY>"
```

---

## Customize

| What | Where |
|---|---|
| Puzzle types / button labels | `DEFAULT_CATEGORIES` in `app/config.py`. Any [Lichess theme](https://lichess.org/training/themes) works, e.g. `"mateIn2": "Mate in 2"`, `"fork": "Fork"` |
| Difficulty | `PUZZLE_DIFFICULTY` in `.env`: `easiest`, `easier`, `normal`, `harder` (default), `hardest`. Affects the type buttons; the Lichess daily puzzle is fixed |
| Post time | `DAILY_PUSH_TIME=19:30` in `.env` |
| Card design | `app/cards.py`. Paste the JSON from `scripts/preview.py` into the [Flex Simulator](https://developers.line.biz/flex-simulator/) to edit it visually |
| Board colours | `app/board.py` |

**Note on spoilers:** *Show solution* posts the answer in the group, so everyone sees it.
If you'd rather each person check privately, players can use **Solve on Lichess ↗** instead.

## Message quota (free plan)

- Daily post = **1 × number of members** each day. A 10-person group uses about 300 per month.
  Check your monthly limit in LINE Official Account Manager → *Insight / Messages*.
- Button taps, text commands, and the join greeting are **replies, which are free and unlimited**.
- If you go over the limit, the daily push fails (the bot logs an error), but replies keep working.

## Run it 24/7 for free (later)

Your laptop has to stay on while the bot runs. When you're ready to host it:

- **Render / Koyeb free web service**: start command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`,
  and copy your `.env` values into the dashboard. Free instances sleep when idle, so:
  - set `DAILY_PUSH_ENABLED=false` and trigger the daily post from a free cron
    ([cron-job.org](https://cron-job.org) or a GitHub Actions schedule) with
    `POST https://<app>/admin/push-daily` + `X-Admin-Key` header. The request also wakes the server.
  - their disk is wiped on restart, so put your group ID(s) in `GROUP_IDS`
    (get them from `GET /admin/groups` while testing locally).
- The first button tap after a long idle can be slow or missed while the server wakes up; tap again.
  (A free uptime pinger hitting `GET /` every 10 minutes keeps it awake.)

## Project layout

```
app/
  main.py      FastAPI: /callback webhook, /board/<id>.png, /admin/*, daily scheduler
  bot.py       what to do for each LINE event (join, text, button postbacks)
  cards.py     Flex Message card layouts
  lichess.py   Lichess API client + puzzle position/solution logic
  board.py     PNG board renderer (Pillow only, no Cairo)
  store.py     remembers group IDs (data/groups.json)
  config.py    settings from .env
scripts/preview.py   try a puzzle without LINE
tests/               offline tests (Lichess and LINE are faked)
```

Board font: DejaVu Sans (free licence, see `app/assets/DejaVu-LICENSE.txt`).
