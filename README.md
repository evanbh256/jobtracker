# jobtracker

A small, fully local internship application tracker for Windows + Brave.

- **App**: one Python file (`app/jobtracker.py`, stdlib only, no installs) serving a
  kanban board at <http://localhost:8765>. It listens on `127.0.0.1` only.
- **Extension**: a Manifest V3 Brave/Chromium extension. On a job posting, open the
  popup and click **Log application**. Company, title, location, URL, job ID, posted
  date and the full job description are pulled from the page by fixed rules (no AI,
  no network besides `localhost`).

No accounts, no cloud, no telemetry. This repo holds code only; your data lives
outside it.

## Requirements

- Windows 10/11
- Python 3.9+ from <https://www.python.org/downloads/> (tick **"Add python.exe to PATH"**
  during install). Nothing else to install.
- Brave (any Chromium browser works the same way).

## Start the app

Double-click **`start-jobtracker.bat`**. A console window opens, the server starts,
and the board opens in your browser. Keep the window open while you use it; close
it (or Ctrl+C) to stop.

From a terminal instead:

```bat
py -3 app\jobtracker.py --open
```

Options: `--data <folder>` (data location), `--port <n>` (default 8765; if you change
it, also change the port in `extension/manifest.json` and `extension/popup.js`).

## Install the extension in Brave (load unpacked)

1. Go to `brave://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and select the `extension` folder of this repo.
4. Click the puzzle-piece icon in the toolbar and pin **Job Tracker Capture**.

After pulling new code, click the reload arrow on the extension card.

### Let the extension start and stop the app (one-time)

Double-click **`install-launcher.bat`**. It registers a small launcher
(`native/host.py`) with Brave for this extension only (a per-user registry entry, no
admin rights), then reload the extension.

From then on you never need the `.bat` launcher:

- Opening the popup starts the app in the background (no console window) if it isn't
  running. The dot at the top shows its state.
- **Open board** starts the app if needed and opens the board in a new tab.
- **Stop app** shuts the app down.

If you move the repo folder, run `install-launcher.bat` again. To remove the
registration: `py -3 native\install.py --uninstall`.
Background-run app output goes to `jobtracker-data\server.log`.

The extension ID is pinned to `ljffoebffpkpgcmehjgdjopdjoiabfhf` (via `key` in
`manifest.json`) so the launcher can allow exactly this extension.

## Leads (night-hunt agent CSV)

A second page, **Leads** (<http://localhost:8765/leads>, or the tab at the top), shows
jobs your night-hunt agent found. Leads never appear on the board by themselves.

1. After each agent run, copy its CSV over `leads\night-hunt-log.csv` inside this
   repo folder (the console prints the exact path; `--leads <folder>` changes it).
   The folder is git-ignored, so the CSV is never committed.
2. Open or refocus the Leads page. The file is re-read every time; no import step.

Highlights: **Draft ready** (green, a draft exists and no reply logged),
**No job link** (yellow, the agent saved a search link), **Duplicate** (purple, same
company and program logged twice), **In tracker** (blue, already on the board).

Edit Applied / Draft / Reply / Reply date / Notes / Job link on the page. Edits are
written into the CSV and also kept in `leads-edits.json`, so when you drop in the
agent's next CSV they are re-applied. If the agent changed that same cell itself,
the agent's value wins. **Add to board** creates an Applied card and sets
`application_filled=yes`.

## Using it

1. Open a job posting (Greenhouse, Lever, Workday, Handshake, LinkedIn, iCIMS, Ashby,
   SmartRecruiters, or any careers page).
2. Click the extension icon. It reads the page immediately and shows what it found.
   - **Yellow** field: guessed from a weak signal (page title, domain, URL). Check it.
   - **Red** field: nothing found. Fill it in or leave it blank.
   - Fix anything by typing in the field, then click **Log application**.
3. The card appears in **Applied** with today's date.

If the app isn't running, the capture is stored inside the extension and sent the
next time you open the popup while the app is running. The popup shows how many are
waiting. Captures are never dropped.

On the board: drag cards between columns, click a card to edit any field and read the
saved job description, **Delete** with a 10-second **Undo**.

### Possible reposts

When you capture something that matches an existing entry, the popup shows
**"Possible repost of: …"** with the reason, and you pick **Add anyway** or **Skip**.
Nothing is ever blocked. A match is any of:

- same job ID (and same company or same platform),
- same canonical URL,
- same company and a near-identical title, ignoring case, punctuation, company
  suffixes (Inc, LLC, Corp…), and season/year/remote noise
  ("Summer 2027", "(Remote)", "Internship" vs "Intern").

Entries you add anyway carry a **possible repost** badge, and their panel names the
original. If a queued (offline) capture turns out to be a possible repost, the popup
asks you the next time it opens.

### Auto-Ghosted

Any card still in **Applied** 90 days after its applied date moves to **Ghosted** on
its own (checked at startup and every time the board loads). Those cards carry an
**auto-ghosted** badge, and `changes.csv` logs the move with actor `auto`, so you can
tell them apart from moves you made. If you drag one back to Applied, it stays there.
Change the window with `GHOST_AFTER_DAYS` in `app/jobtracker.py`.

### Search

The search box (press `/`) filters cards by company, title, location, notes, and the
full job description text. Matching words are highlighted in the open JD.

## Where the data lives

Default: `%USERPROFILE%\jobtracker-data` (e.g. `C:\Users\you\jobtracker-data`).
Change it with `--data` or the `JOBTRACKER_DATA` environment variable.

```
jobtracker-data\
  applications.csv   one row per application (open in Excel; close it before editing in the app)
  changes.csv        every change: timestamp, app id, field, old, new, actor (user / capture / auto)
  jd\                one .txt per job description; applications.csv points here by relative path
  trash\             JDs of deleted applications (moved back on Undo)
```

The app starts from an empty tracker and never touches any other CSV. If the
data folder already has an `applications.csv` that isn't one of ours, it refuses to start.

If `applications.csv` is open in Excel, Windows locks it; edits will fail with a
message until you close Excel. Captures made from the extension during that time are
queued and retried.

## Removing the Offer column

Edit `STATUSES` near the top of `app/jobtracker.py` and remove `"Offer"`. The board
builds its columns from that list.

## Development

```bash
python -m unittest discover -s tests -v   # server API + CSV tests (stdlib)
npm install && npm test                   # extractor tests (dev-only: jsdom)
```

`tests/fixtures/*.html` are **hand-built approximations** of each platform's markup,
not real captures. When a real site extracts badly, save its page into that folder
and add a test.

## Security notes

- The server binds to `127.0.0.1` and rejects requests whose `Host` isn't
  `localhost:<port>`/`127.0.0.1:<port>` (blocks DNS rebinding) and whose `Origin` is a
  website (blocks random pages from writing to your tracker). The extension's
  `chrome-extension://` origin and the board itself are allowed.
- Extension permissions: `activeTab`, `scripting`, `nativeMessaging` (only to talk
  to the launcher, which accepts just "start" and "status"), and host access to
  `localhost:8765`/`127.0.0.1:8765` only. It can read a page only after you click its
  icon on that page.
- Captured text is only ever rendered as text, never as HTML.
