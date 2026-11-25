# CrashPilot AI — Phase 1

Local macOS starter scaffold for browser-based Firebase Console access.
Phase 1 verifies persistent browser authentication and navigation to configured
Firebase Crashlytics project pages. It does **not** extract crash records yet.

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

## First login and browser verification

For Crashlytics, use a Google account with Viewer access to the Firebase project,
set its Firebase project ID, and enable the `crashlytics` source.

```bash
playwright install chromium
python -m app.main browser-check
```

A Chromium window opens. Sign in to Google manually and complete any MFA challenge.
Return to Terminal and press Enter. The tool navigates to each enabled project's
Crashlytics console URL, prints the current URL/title and a short visible-text preview,
and saves a screenshot under `data/snapshots/`.

Verify the URL contains the expected Firebase Project ID and that the browser visibly
shows the intended project. If Google requests reauthentication, complete it manually.

## Security

- `browser_profiles/` contains sensitive session data. Never commit or share it.
- Use a dedicated macOS user account if practical.
- Grant only read access to the Firebase projects.
- Do not automate CAPTCHA or bypass Google security checks.
- Do not use internal/private Google endpoints. Phase 1 uses normal browser navigation
  and visible page content only.
- Screenshots and page text may contain project names or operational data; protect them.

## Phase 1 acceptance checklist

- [ ] Chromium launches on macOS
- [ ] Manual Google login completes
- [ ] A second run reuses the browser session (if still valid)
- [ ] Each enabled project opens the expected Crashlytics page
- [ ] Project ID is verified in the resulting URL
- [ ] Screenshot and diagnostic output are saved
- [ ] No credentials or browser profile files are committed

## Troubleshooting

- `python3.12: command not found`: install Python 3.12 from python.org or via Homebrew.
- Browser missing: run `playwright install chromium`.
- Login repeats: Google session may have expired or require reauthentication.
- Wrong project: disable it and verify the project ID and Console URL manually.
- Access denied: ask a project owner to grant read-only Firebase access.

## Next phase

Only after Phase 1 works reliably, implement UI selectors for the visible Crashlytics
issue list and issue detail pages. Validate selectors against real pages before enabling
collection. Do not infer issue data from screenshots when structured visible DOM text
is available.

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
