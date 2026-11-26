# CrashPilot AI

Local macOS dashboard and automation for Firebase Crashlytics and repository diagnostics.

## Requirements

- macOS
- Python 3.12+

## Setup

```bash
cd ~/Projects/crashpilot-ai
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
CRASHPILOT_SYNC_INTERVAL_MIN=0 ./run.sh serve
```

Open `http://127.0.0.1:8787`. On a fresh checkout, CrashPilot creates an empty local
database in `data/`. Add your first project with **Add Project** in the sidebar.
Crash records, run reports, and browser sessions are stored locally and excluded
from Git.

## Configure projects

To configure projects by hand, copy `config/projects.example.yaml` to
`config/projects.yaml` on a fresh checkout, then edit the repository path and enable
the project and its input sources. Git ignores the local configuration. Keep project
keys unique and keep credentials out of the YAML file.

## Connect Firebase Crashlytics

Set the Firebase project ID and enable the `crashlytics` source in
`config/projects.yaml`. CrashPilot resolves apps from any configured Crashlytics
Console URLs. With no URLs, it discovers apps for the project's platform
(Android and iOS for Flutter). To restrict collection to specific apps, set
`firebase_app_ids` to a list of Firebase App IDs from Firebase Project Settings
or from `firebase apps:list --project PROJECT_ID`.

For local use, the default Crashlytics backend is **Firebase CLI** (`CRASHPILOT_CRASHLYTICS_BACKEND=firebase_cli`), which uses the official Firebase CLI MCP tools (`core,crashlytics`) without requiring Playwright or `gcloud`. Install Firebase CLI (`npm install -g firebase-tools@latest`) and sign in with a Google account that has read access to Crashlytics:

```bash
./run.sh login
./run.sh sync --project YOUR_PROJECT_KEY --limit 5
```

Alternatively, you can switch `CRASHPILOT_CRASHLYTICS_BACKEND` to:
- `api`: Call the public Crashlytics REST API directly using Google Application Default Credentials (`gcloud auth application-default login` or environment ADC).
- `browser`: Keep legacy Playwright browser collection (`python -m playwright install chromium`, then `./run.sh login`). Browser profiles and screenshots are stored locally, and the browser module remains intact for other platforms or web consoles.

The scheduled cycle and manual sync import Firebase issue IDs, counts from Crashlytics' default seven-day `topIssues` report, and one recent event for each of the top issues selected by `--limit` per app. Collection writes to SQLite and makes no AI calls. Use `--skip-crashlytics-sync` on a cycle to use the existing local data.

### Local dashboard and Live execution

Run `./run.sh` on macOS and open the loopback address printed in the terminal.
The dashboard is single-user and local-only. LAN binding and forwarded requests
are rejected. Keep it off reverse proxies and shared network endpoints.

Dry runs execute checks against a temporary copy. Live runs require an executed
check before publishing changes. Auto-merge additionally requires an independent
verification command or runtime check. A requested GitHub auto-merge remains
pending until GitHub confirms the merge. A busy cycle exits with status 75;
an input source failure exits with status 3 and preserves that source’s last good queue.

### Live agent permissions on macOS

Each executor receives a temporary home, a private Git index and write access to its task worktree. The verifier reads the complete patch at `CRASHPILOT_DIFF_PATH` and has read-only access to the worktree. Checks and agents run without the desktop session's credentials. An agent profile can explicitly name required environment variables in `credential_env`. A network-backed CLI can use an operator-managed HTTP(S) proxy via `loopback_proxy_port`; the profile can connect only to that local port. Configure the proxy to allow the required provider. Authentication stored in the user's normal home is outside the agent's scope.

Project execution settings accept `protected_branches` (shell-style branch patterns). Protected base branches permit PR creation and require manual merge. The default protects `main`, `master` and `production`. Preview uses the current working files as its baseline, so the verification diff shows changes produced during the preview.
