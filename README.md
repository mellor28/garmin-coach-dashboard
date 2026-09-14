# Garmin Running Coach

A small local toolkit that pulls your running data from Garmin Connect so
Claude can give you real coaching feedback on demand.

## How it works

1. **One-time login** (`login_setup.py`) -- you run this yourself, directly
   in a Terminal window, so your Garmin password is never typed into a
   chat with Claude. It logs into Garmin Connect and caches a session in
   `garmin_tokens/`.
2. **Fetching data** (`fetch_activities.py`) -- Claude runs this for you
   whenever you ask ("pull my latest runs", "evaluate this week's
   training"). It only needs the cached session, not your password.
3. **Coaching** -- once data lands in `data/latest_export.json` and
   `data/summary.csv`, ask Claude things like "how did today's run go?"
   or "evaluate my training this month" and Claude reads the fetched
   data and writes an actual evaluation: pacing, heart-rate zones,
   trends over time, recovery signals, and suggestions.

## Setup (do this once)

Open Terminal.app on your Mac and run:

```bash
cd ~/Documents/garmin-coach
python3 -m venv venv
source venv/bin/activate
pip3 install -r requirements.txt
python3 login_setup.py
```

Enter your Garmin Connect email and password when prompted (and an MFA
code if you have two-factor login enabled). You should see "Login
succeeded." You generally won't need to repeat this for months -- Garmin
sessions are long-lived.

## Using it day to day

### Automatic dashboard refresh (no AI required)

For a one-click refresh in Finder, double-click:

`Refresh Garmin Dashboard.command`

macOS will open Terminal, run the complete refresh, and open the updated
dashboard in your browser. If macOS blocks it the first time, Control-click
the file, choose **Open**, then confirm **Open**.

After the one-time login, one command connects with your cached Garmin
session, fetches new data, merges it into your local history, rebuilds the
dashboard, validates it, and opens it:

```bash
cd ~/Documents/garmin-coach
./venv/bin/python refresh_dashboard.py
```

Normal refreshes request at least the latest 45 days and merge them with the
history already on disk. If the Mac has not refreshed for longer, the window
automatically expands to cover all time since the last successful sync plus a
seven-day overlap. This keeps refreshes efficient without missing runs after a
long shutdown. The first refresh downloads up to ten years of history.

The dashboard also refreshes Garmin sleep, body battery, resting heart rate,
HRV, training readiness, stress, respiration, and SpO2 when those readings are
supported by the connected watch.

Useful options:

```bash
# Re-download all history
./venv/bin/python refresh_dashboard.py --full

# Refresh without opening the browser
./venv/bin/python refresh_dashboard.py --no-open
```

This workflow runs entirely on your Mac and does not call Claude or consume
AI tokens. If the cached Garmin session expires, run `login_setup.py` again.

### Encrypted iPhone website

The `docs/` directory is the GitHub Pages version of Run Atlas. Its application
shell is public, but Garmin data is stored only in
`dashboard-data.enc.json`, encrypted locally with AES-256-GCM.

Run `Set Up Encrypted Dashboard.command` once and choose a strong dashboard
password. The password is saved in macOS Keychain and is never committed or
uploaded. After GitHub Pages is configured, the normal
`Refresh Garmin Dashboard.command` also encrypts and publishes each update.

On iPhone, open the GitHub Pages address, enter the same password, then choose
**Share → Add to Home Screen**. Safari may offer to save the password in
iCloud Keychain. Decryption happens only inside the browser.

### Fetch data manually

Just ask Claude, for example:

- "Pull my Garmin runs from the last 2 weeks and evaluate my training"
- "How was today's run?"
- "Am I overtraining this month?"

Claude runs `fetch_activities.py` on your behalf and reads the results --
no manual steps needed after the one-time setup above.

You can also run it yourself any time:

```bash
source venv/bin/activate
python3 fetch_activities.py --days 14
```

## Local dashboard

`dashboard.html` is a self-contained, offline dashboard. Just double-click it
to open in your browser; no internet or server needed. It is a goal-free
training snapshot with:

- **Distance-first overview** -- switch between this week, the rolling last
  30 days, and the current continuous training block. Total kilometres are the
  primary number, supported by time, cadence, pace, heart rate, elevation, and
  training load.
- **Training rhythm** -- weekly distance and the selected period's descriptive
  heart-rate zone distribution.
- **Latest run** -- pace and cadence context plus the watch's current running
  economy metrics such as vertical ratio, ground contact time, efficiency,
  stride length, and power.
- **Recovery window** -- the latest available sleep, body battery, resting
  heart rate, HRV, readiness, stress, respiration, and SpO2 readings.
- **Form and history** -- cadence, aerobic-efficiency and lap-level trends, plus
  a period-filtered run log.

Garmin history often reaches back years. The dashboard identifies the current
continuous block using a 60-day gap and limits form trends to that block so old
athletic periods do not distort the current picture.

To regenerate it after a fresh fetch:

```bash
python3 build_dashboard.py data/latest_export.json dashboard.html
```

(or point it at `data/coaching_export_slim.json` / any other export file you have).

For a dashboard covering your full history rather than a recent window, fetch
with a large `--days` value first, e.g.:

```bash
python3 fetch_activities.py --days 3650 --wellness-days 30
```

## Files

- `login_setup.py` -- one-time interactive login (run by you)
- `fetch_activities.py` -- pulls runs + wellness data (run by you when you want fresh data)
- `refresh_dashboard.py` -- fetches, merges, rebuilds, validates, and opens the dashboard without AI
- `Refresh Garmin Dashboard.command` -- double-clickable macOS refresh launcher
- `publish_dashboard.py` -- encrypts dashboard data and publishes the Pages build
- `Set Up Encrypted Dashboard.command` -- one-time local password and Keychain setup
- `docs/` -- public GitHub Pages shell plus encrypted dashboard data
- `build_dashboard.py` + `dashboard_template.html` -- generate `dashboard.html` from a fetched export
- `dashboard.html` -- the local dashboard (generated; open anytime, doesn't need regenerating unless you want fresh data in it)
- `garmin_tokens/` -- your cached Garmin session -- treat like a password, don't share it
- `data/` -- exported activity data (JSON + CSV)

## Security notes

- Your Garmin password is only ever typed into your own Terminal, never into chat.
- `garmin_tokens/` is equivalent to being logged in -- it's excluded from git via `.gitignore`; don't share that folder.
- This uses the unofficial `garminconnect` / `garth` Python libraries (the
  same ones many hobbyist Garmin projects use), which talk to the same
  endpoints as the Garmin Connect app/site. Garmin doesn't officially
  support this, and could change things that require an update here.

## If something breaks

Garmin occasionally tweaks field names or endpoints. If `fetch_activities.py`
runs but some numbers look missing or off, tell Claude what you're seeing
(or paste the warning/error text) and it can adjust the script.
