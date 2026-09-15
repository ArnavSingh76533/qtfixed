# Question Ai — Groq Telegram Bot

A Telegram assistant for text, images, math, and current-context web answers using **Groq `openai/gpt-oss-120b`**, your original
**Question Ai system prompt**, live streaming, native Telegram formatting, saved
conversations, and persistent broadcast campaigns.

The ZIP includes the original JSON files and five user-data backups unchanged.
The GitHub publishing helper includes source code only, never your records or keys.

## Group, photo, math, inline, and web update

### Update your running bot

```bash
git pull
python -m pip install -r requirements.txt
```

Restart the bot after installing dependencies. Existing `.env`, JSON, and runtime
state are preserved by Git because they are ignored. Keep your original records
in the same DATA_DIR. The free quota is now **40**, shared by regular, image, web,
and inline answers. Existing usage counts are retained; this update does not reset
them. The Copy answer button is removed; Telegram's native code selection remains.

### Groups

- When added to a group, the bot saves its name/ID and membership, enables it, and
  sends a welcome message. Join updates are deduplicated. No separate allow step
  is required for new groups. Saved active groups are eligible for `-group` campaigns.
- Previously explicitly disabled groups stay disabled until `/allowgroup` or a
  genuine remove/re-add. Disabled requests now get an explanation instead of silence.
- `/start` in an already-existing group registers missing group data too.
- Try `/ask@queryaibot hello`. `/groupstatus` reports enablement and privacy settings.
- Telegram can withhold unaddressed commands/ordinary messages when Group Privacy
  is enabled. Make the bot a group admin, or disable Group Privacy via BotFather
  (`/setprivacy`; Telegram may require removing/re-adding the bot after a change).
  The bot code cannot change BotFather settings. Explicit `/ask@queryaibot` targets
  this bot even when Telegram would send plain `/ask` to another privacy-enabled bot.
- Members still need to register privately with `/start` and satisfy the existing
  channel requirement. The bot explains those conditions instead of ignoring them.
- Settings changes edit the existing settings panel, including in groups.

### Image → extracted text → AI

Send a photo or an image document. You can also reply to an image with `/ocr` or
`/ask explain this`. The bot downloads the image, extracts text, appends your caption
and an embedded solve/explain instruction, then uses the normal AI/web answer flow.
Quota is charged once, only after a completed answer is delivered.

The original `compscilib.com/image-to-text` endpoint is restored. Since that external
service may be unavailable, `OCR_MODE=auto` falls back to local Tesseract. Install:

```bash
sudo apt-get update
sudo apt-get install -y tesseract-ocr
```

Optional `.env` settings:

```dotenv
OCR_MODE=auto
OCR_URL=https://ai-service-prod.compscilib.com/image-to-text
WEB_ENABLED=true
GROUP_MENTIONS_ONLY=false
```

`OCR_MODE=local` keeps images on your server; `remote` uses the configured API.
Local Tesseract works best on clear printed text; handwriting and dense math can
be inaccurate. The solve prompt asks the AI to request clarification for unreadable
symbols. File size is limited to 10 MB, decoded images to 25 megapixels, and OCR
text to 20,000 characters. The old instruction forbidding LaTeX is intentionally
replaced by the newly requested math support.

### Math formatting

- Display equations in `\[ ... \]` or `$$ ... $$` are rendered into local PNGs.
- Inline equations become readable text, with fractions kept parenthesized.
- Code fences and inline code are preserved literally.
- `/settings` → Math switches between `image` and `unicode`.
- Mathtext handles a useful subset of LaTeX, not a full TeX document. Unsupported
  environments/macros fall back to readable text. At most eight display equations
  are rendered per answer; additional equations use text. No shell or external
  LaTeX compilation is performed.

### Inline answers

In BotFather, configure `@queryaibot`:

1. `/setinline` → enable and set a query placeholder.
2. `/setinlinefeedback` → **100%** for generation immediately after selection.

Then type `@queryaibot what is photosynthesis?` in any chat and select the result.
The inline result appears immediately; generation starts after selection, not on
every keystroke. If feedback is off, tap **Generate answer**. Inline controls check
ownership, use the same 40-question quota, and never include private conversation
history. Inline math uses readable text, because an inline text result cannot be
expanded into multiple photo messages. Long answers have Previous/Next buttons.
Inline sessions last 30 minutes in memory and expire on restart.

### Current-context web answers

Your supplied Felo request/stream workflow is integrated. `/web <question>` forces
web search; clear current-context questions route automatically, and Groq can also
invoke a `web_search` function for cases such as changing officeholders. The original
Question Ai prompt is retained with the new routing instruction appended.

The integration parses complete SSE events, reconnects with a fresh replay buffer
to avoid duplicate text, and has an overall timeout. Incomplete results are labeled.
It uses your supplied endpoint contract; Felo is an external service and can change
or reject unauthenticated requests. No access challenges are bypassed. Web errors
are visible and do not claim that current information was verified. Felo's answer
is relayed with whatever links it supplies; sources are never fabricated. Questions
routed to web are sent to Felo; this is also explained by `/privacy`.

### Visual controls

Status messages use regular ⚡ emoji. Retry/New/Stop and broadcast controls use
Telegram's primary/success/danger button styles. No Premium emoji configuration
is needed. The bot's stored premium question subscriptions remain supported.

### Welcome visual

`assets/welcome.png` is the generated Question Ai welcome banner used by `/start`
in private chat. If sending the photo fails, the bot falls back to text. Generation
uses the built-in image tool; prompt: a navy/cyan/violet Q orbital emblem with a
lightning motif, title “Question Ai”, and “Ask. Explore. Understand.” subtitle.
The status message is now **⚡ Working…**, with **⚡ Reading image…** for OCR and
**🌐 Searching the web…** for web work.

## 1. Quick start

Requires **Python 3.10+**. Stop the old bot process first: Telegram polling allows
only one active process for a bot token. The application also locks its data path.

```bash
cd qt
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
nano .env
```

Fill the two empty values in the included `.env`:

```dotenv
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
GROQ_API_KEY=YOUR_NEW_GROQ_API_KEY
GROQ_MODEL=openai/gpt-oss-120b
```

Then:

```bash
python main.py
```

`.env` is loaded automatically from beside `main.py`, regardless of your current
working directory. On Windows, activate with `.venv\Scripts\activate` instead.
For a fresh GitHub checkout, first copy `.env.example` to `.env`.

Do not paste `export` or Python code into `.env`. Shell environment variables take
precedence over `.env`. The key pasted into chat was intentionally **not embedded**
in this project: replace it with a rotated key. No `requests` or Groq SDK install
is needed; the integration uses async HTTPX directly. HTTPX SOCKS support is
included for hosts configured with a SOCKS proxy.

## 2. Preserve existing data when migrating

1. Stop your existing bot.
2. Keep a backup of its current directory.
3. Put the updated Python files, requirements, and `.env` alongside your existing
   JSON records. If using the supplied ZIP snapshot, all its original JSON and
   backup files are retained byte-for-byte.
4. If your running bot has accumulated newer users since you uploaded the ZIP,
   keep those newer files. Do not overwrite them with this older snapshot.
5. Install requirements and start the new bot.

The existing schemas remain supported:

| File | Purpose |
|---|---|
| `user_data.json` | Registration, quota, subscriptions; runtime adds quota-window/activity fields as needed |
| `group_data.json` | Group registration and enabled state |
| `promo_codes.json` | Existing promo codes and used state |
| `request_log.json` | Preserved legacy data; not rewritten by this application |
| `user_data.json.*.backup` | Original backups; runtime maintains the newest five hourly backups |

New runtime files are created separately:

| File | Purpose |
|---|---|
| `chats.sqlite3` | Recent history and settings, isolated by chat, topic, and user |
| `campaigns.sqlite3` | Campaign definitions, recipient delivery states, blocked-recipient list |
| `ads.json` | Saved ad text |
| `bot_settings.json` | Log-channel changes made through `/setlogchannel` |

Back up the entire data directory with the bot stopped, including SQLite files.
The automatic hourly backup covers **user_data.json only**. JSON files use atomic
replacement; unrecoverable corruption stops loading instead of erasing records.
User recovery tries readable timestamped backups without changing the source ZIP.

## 3. Chat features

- Groq streaming: native animated drafts in private chats, throttled message edits
  in groups or when drafts are unavailable. Streaming displays partial plain text;
  final replies receive full formatting and are saved as normal messages.
- Your original **Question Ai** identity and instructions are the first system
  message: short/general answers by default, longer clarification, a few emojis and code without explanations/comments unless asked. Photo support
  is restored. The old no-LaTeX rule is replaced by LaTeX support, as requested.
  A web-search instruction routes questions needing current context to Felo. User-selected style can override the default response length.
- Groq internal reasoning is not displayed. Only answer `delta.content` is read.
- Bold, italic, strikethrough, headings, links, spoilers, blockquotes, inline code,
  and code blocks with language labels use Telegram message entities.
- Code blocks support native Telegram selection/copying. The separate Copy answer
  button has been removed.
- Long answers are split into Unicode-safe messages, with code and other formatting
  preserved across boundaries. Tables are displayed as readable text rows.
- `/stop` cancels generation; one in-flight answer per user prevents quota races.
  Other users can ask questions concurrently, up to the configured cap.
- History survives restarts. Free users retain the latest 6 exchanges; premium
  users retain 35. Older complete exchanges roll out instead of abruptly clearing
  the conversation. Context also has a configurable character budget.
- `/retry` replaces the last saved exchange only after the replacement succeeds.
  It uses one successful question. Previously posted Telegram messages stay visible.
- Failed, cancelled, or incomplete requests do not enter saved history or consume
  bot quota. Groq may still bill provider usage for cancelled/failed requests.
- In enabled groups, ordinary delivered text is accepted by default; `/ask` always
  works when Telegram delivers the command. Forum topics get
  separate histories. Anonymous administrator posts are ignored.
- Commands remain responsive during generation and broadcasting. Startup registers
  the Telegram command menu. Inline controls verify the original user's identity.

### User commands

| Command | Action |
|---|---|
| `/start` | Register in private chat and show welcome |
| `/ask <question>` | Ask in private or an enabled group |
| `/new`, `/reset` | Stop the current answer and clear this chat/topic's history |
| `/stop` | Cancel your active answer |
| `/retry` | Regenerate the latest completed answer |
| `/settings` | Toggle streaming; cycle balanced/concise/detailed style and low/medium/high reasoning |
| `/model` | Show the configured Groq model |
| `/export` | Download recent saved history as Markdown |
| `/forget` | Clear this chat/topic's saved history and settings |
| `/balance` | Show remaining quota and subscription |
| `/claim <code>` | Redeem an existing premium code |
| `/privacy` | Explain Groq, local history, and current logging |
| `/help` | Show commands |

Quota remains **40 successfully delivered questions per 24-hour window** for free
users; premium users have unlimited questions. The window starts with the first
successful request. Subscription expiry is enforced before answering. `/forget`
does not erase registration, quota, other chats, Telegram messages, or admin logs.

## 4. Advanced broadcasts

Only `ADMIN_ID` can create/control campaigns. The bot never sends a campaign merely
because it was created: you receive a content preview and must press **Start**.
Use broadcasts only for your own intended recipients. Use these admin commands in
private chat so the preview and delivery report are not posted into a group.

### Examples

All registered reachable users:

```text
/broadcast -user -- **Announcement**
Here is the second line of the message.
```

Premium users active in the last seven days, sample up to 100, with a URL button:

```text
/broadcast -user -premium -active 7 -random 100 --button "Open|https://example.com" -- **Premium update**
```

Enabled groups, quiet delivery, one message every two seconds, optional pin:

```text
/broadcast -group -silent -delay 2 -pin -- Group announcement
```

Reply to an existing Telegram message, then send:

```text
/broadcast -user
```

This copies **that single message**, preserving supported text/entities or media.
It does not use any image AI API. Albums are not expanded automatically. Copying
requires access to the source message and obeys Telegram content restrictions.
Explicit text after `--` takes priority over a replied-to message.

### Campaign options

| Option | Meaning |
|---|---|
| `-user` | Registered users |
| `-group` | Currently enabled groups |
| `-premium` / `-free` | User subscription filter; choose one |
| `-active N` | Users with a recorded successful question within N days |
| `-random N` or legacy `rN` | Random sample without duplicate targets |
| `-delay SECONDS` | Delay after a recipient; 0.1–30, default 1 |
| `-silent` | Suppress delivery notification sound |
| `-pin` | Try to pin; missing pin permissions do not fail the delivery |
| `--button "Title|URL"` | Add URL button; repeat up to eight times |
| `--` | End options; all remaining text is the message, preserving line breaks |

User filters apply to users; group recipients are filtered by enabled state. Known
blocked/unreachable recipients are excluded from later campaigns. Their original
user records are never deleted. If access is later restored, this blocked list
requires admin maintenance; no automatic probing is performed.

### Monitor and control

```text
/campaigns
/campaign <id> status
/campaign <id> pause
/campaign <id> resume
/campaign <id> cancel
/campaign <id> retry_failed
/campaign <id> report
```

- `/campaigns` shows the latest 15 campaigns.
- Status reports sent, pending, failed, skipped, and uncertain counts.
- Pause/cancel stop before the next recipient; an already in-flight request may
  finish. Wait for that request before resuming or starting another campaign.
- Resume continues pending recipients. Completed deliveries are not resent.
- `retry_failed` puts explicit failures back into pending state; then resume.
- `report` exports per-recipient status as CSV.
- One campaign sends at a time. AI requests remain independent.
- Telegram `RetryAfter` is respected. Blocked chats are recorded separately.
- On restart, running campaigns become **paused**, requiring an explicit resume.
- A network timeout can mean a message was delivered without acknowledgment.
  Those recipients become **uncertain** and are never retried automatically.
  Exactly-once delivery cannot be guaranteed by Telegram's API.
- Campaign drafts and reports persist until you remove/maintain the database.
  Campaign scheduling and automatic restart/resume are deliberately not enabled.

## 5. Other admin controls

| Command | Action |
|---|---|
| `/stats` | User/premium/group totals and active AI request count |
| `/gencharlie037` | Generate a premium code; admin only |
| `/resetcount` | Reset all bot quotas |
| `/ads <Markdown text>` | Save an ad shown after `/new` or `/reset` |
| `/ads off` | Disable ads |
| `/setlogchannel <id\|@username\|off>` | Persist a log destination or disable logging |
| `/allowgroup`, `/disallowgroup` | Group administrators enable/disable group usage |

Existing enabled groups stay enabled. Newly added groups are saved and enabled automatically, with a welcome message.
Both service-message and `my_chat_member` join updates are handled. Removing the
bot marks the group as no longer reachable for broadcasts. `/allowgroup` no longer
assumes a basic Telegram Chat has an `invite_link` field.
Admin operations check the configured Telegram user ID, not the command name.

## 6. Configuration reference

| Variable | Default / meaning |
|---|---|
| `BOT_TOKEN` | Required bot token from BotFather |
| `GROQ_API_KEY` | Required Groq API key |
| `GROQ_MODEL` | `openai/gpt-oss-120b` |
| `MAX_OUTPUT_TOKENS` | 4096; maximum 16384; includes model output/reasoning budget |
| `REQUEST_TIMEOUT` | 180 seconds for generation; up to 600 |
| `MAX_CONCURRENT_REQUESTS` | 8 users; up to 100 |
| `MAX_CONTEXT_CHARS` | 40000; preserves complete recent exchanges |
| `DRAFT_STREAMING` | `true`; private-chat drafts with automatic fallback |
| `GROUP_MENTIONS_ONLY` | `false`; set `true` to restrict ordinary text to mentions/replies |
| `ADMIN_ID` | Original `629986639`; replace for another owner |
| `BOT_USERNAME` | Original `queryaibot`; registration links use this |
| `CHANNEL_ID` | Original `-1002081366095`; empty disables membership requirement |
| `CHANNEL_URL` | Original `https://t.me/BotCommunityHub` |
| `LOG_CHANNEL_ID` | Original `-1002224010991`; empty disables logging |
| `DATA_DIR` | Script directory; relative overrides resolve from the script directory |

The included `.env` leaves only the required secrets empty. It keeps the original
admin/channel defaults unless you uncomment/change them. If `/setlogchannel` has
been used, its saved setting takes precedence over the environment on startup.
Change it again through the command to update or disable it.

Question logging sends text questions/answers in batches of 50 to the configured
admin destination. Partial batches are in memory and can be lost on restart.
Registration notices are also sent there. No credentials or full user databases
are printed to the terminal. Tell your bot users if you keep question logging on.

## 7. Run continuously on a VPS

After the manual start succeeds, you can use systemd. Adjust paths and user:

```ini
[Unit]
Description=Question Ai Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/qt
ExecStart=/home/ubuntu/qt/.venv/bin/python /home/ubuntu/qt/main.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

Save as `/etc/systemd/system/qtfixed.service`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now qtfixed
sudo journalctl -u qtfixed -f
```

Do not start a second manual process while the service runs.

## 8. Tests and limitations

```bash
python -m unittest discover -s tests -v
```

Tests use temporary state and mocked HTTP/Telegram responses. They cover streamed
Groq responses, auth errors, rate limiting, truncated streams, Markdown entities,
Unicode splitting, cancellation, retry history, callback ownership, persistent
campaign recovery, and earlier promo/quota fixes. They never contact Telegram or
Groq and do not touch the packaged user records. The CI workflow tests Python 3.10
and 3.12 when the source is pushed to GitHub; local validation used Python 3.12.

Live provider access, Telegram permissions, client rendering, and account-specific
rate limits still require a run with your configured keys. Changing `GROQ_MODEL`
requires a model enabled for your Groq account. Reasoning controls are sent only
for GPT-OSS models. Felo web search and image OCR are included. Code execution, voice, and payments
are not included. Local token counting is a character budget, not an exact tokenizer.

User/quota JSON persistence and history/campaign SQLite are separate stores; they
are not one transaction. Promo consumption and user activation also remain separate
file writes. A crash at a write boundary may require admin repair. Do not edit live
records externally or run multiple instances against one data directory.

## 9. Publish source to a new GitHub repository

The included helper creates a **private** repository named `qtfixed`, using your
locally authenticated GitHub CLI. It copies an explicit allowlist into a new Git
repository, so old history, `.env`, JSON, backups, SQLite, and ZIP files cannot be
included by accident.

```bash
gh auth login
python scripts/publish_github.py
```

Git and GitHub CLI must be installed. The helper refuses to overwrite an existing
repository and never force-pushes. Your VPS data remains local; a fresh clone needs
your private data files and `.env` copied in before migration.

Source repository: [ArnavSingh76533/qtfixed](https://github.com/ArnavSingh76533/qtfixed).
The repository excludes your JSON records, backups, live `.env`, and runtime databases.
Use the full ZIP for the preserved data snapshot, or copy your newer private records
into a checkout before starting the bot.

## Official references

- [Groq text generation and SSE](https://console.groq.com/docs/text-chat)
- [Groq GPT-OSS reasoning controls](https://console.groq.com/docs/reasoning)
- [Telegram Bot API, drafts, entities and copy buttons](https://core.telegram.org/bots/api)
- [python-telegram-bot documentation](https://docs.python-telegram-bot.org/en/stable/)
