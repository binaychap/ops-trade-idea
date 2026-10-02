# Deploy Ops Trade Idea on Oracle Cloud

The names and paths below are for new `ops-trade-idea` deployments. Existing
`ops-paper-trade` installations keep their current directory, database path and
service names until migrated. Stop and disable the old units before enabling
replacement units, retain the existing SQLite database and `.env`, and rebuild
`.venv` with `uv sync` if the checkout directory moves (scripts contain absolute
paths). Editing this runbook does not rename installed services or move data.

This guide deploys one Oracle Linux 9 VM on Oracle Cloud with persistent SQLite storage, a private FastAPI
dashboard, and optional trading workers. It does not deploy anything by itself.
Provider details checked September 15, 2026; verify the console's current
Always Free allowances before creating resources.

## 1. Create an Oracle account

Sign up at https://signup.cloud.oracle.com/.

Oracle's standard trial provides up to $300 in credits for 30 days. Unused
credits expire. Eligible Always Free resources can continue afterward within
account limits without a paid upgrade. Paid trial resources can be reclaimed
and permanently deleted unless you upgrade; do not depend on a grace period
for continued operation. Keep total ARM allocations within the Always Free
allowance before the trial ends.

Oracle may require a supported payment card for identity verification and
place a temporary authorization hold. Always Free has no uptime SLA.

References: [Free Tier FAQ](https://www.oracle.com/cloud/free/faq/),
[Always Free limits](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).

## 2. Create the compute instance

Use **Compute → Instances → Create instance** in your home region.

| Setting                                   | Suggested choice                                |
| ----------------------------------------- | ----------------------------------------------- |
| Name                                      | `ops-trade-idea`                                |
| Image                                     | Oracle Linux 9                                  |
| Shape                                     | Always Free-eligible `VM.Standard.A1.Flex`      |
| CPU / memory                              | Start with 1 OCPU and 4 GB RAM                  |
| Capacity                                  | On-demand                                       |
| Availability domain                       | Any available domain in your home region        |
| Capacity reservation / dedicated host     | None                                            |
| Compute cluster / cluster placement group | Leave unset                                     |
| Boot disk                                 | Default approximately 46.6–50 GB                |
| Custom disk performance                   | Leave disabled; standard Balanced is sufficient |
| In-transit encryption                     | Enable when supported                           |
| Customer-managed encryption key           | Leave disabled; use Oracle-managed keys         |
| Additional block volumes                  | None initially                                  |

Oracle currently documents an ARM allowance equivalent to 2 OCPUs and 12 GB RAM,
plus 200 GB combined boot/block storage. These are total allowances, not per VM.
Select an eligible image and check your account's remaining allowance. The
proposed VM sizing is an estimate; ARM dependencies must be tested.

`AD-1` means Availability Domain 1, not a paid tier. In a name such as
`<prefix>:US-ASHBURN-AD-1`, the prefix identifies your tenancy's mapping and
Ashburn identifies the region. If capacity is unavailable, try another
availability domain where the shape is supported, or wait. Do not delete a
working VM merely to retry provisioning.

Use on-demand rather than preemptible capacity: preemptible instances can be
terminated when Oracle needs the capacity. On-demand can still be Always Free.
Security capability labels on an image do not prove that the VM is free.

References: [Regions and availability domains](https://docs.oracle.com/en-us/iaas/Content/General/Concepts/regions.htm),
[Compute capacity](https://docs.oracle.com/en-us/iaas/Content/Compute/Concepts/computeoverview.htm).

## 3. Configure networking

Choose an existing suitable VCN or create one. For direct SSH from your Mac:

- Create a **public subnet**, for example `paper-trade-public`.
- If requested, use a non-overlapping subnet CIDR such as `10.0.0.0/24` within
  your VCN's CIDR.
- Enable automatic public IPv4 assignment.
- Ensure an enabled Internet Gateway exists and the subnet route table has
  `0.0.0.0/0` pointing to it.
- Allow inbound TCP **destination port 22** from your current public IPv4
  address with `/32`; leave the source-port range unrestricted.
- Permit the outbound connectivity needed for package downloads, DNS, and
  HTTPS requests to Optionomics and Webull.
- Do not open dashboard port 8000 to the internet.

### Instance has only a private IP

1. Open **Compute → Instances → your instance**.
2. Open **Networking → Attached VNICs**, then the primary VNIC.
3. Open **IP administration**.
4. Beside the primary private IP, choose **⋮ → Edit**.
5. Set **Public IP type → Ephemeral public IP**, then **Update**.

This requires a public subnet. If the option is disabled, inspect the subnet
type and permissions; do not terminate the instance. A private subnet needs a
separate access approach, such as OCI Bastion, or a planned network change.
Assigning a public IP alone does not create the gateway route or SSH rule.

[Oracle public-IP instructions](https://docs.oracle.com/en-us/iaas/Content/Network/Tasks/assigning-ephemeral-public-existing-private-ip.htm).

## 4. Save the SSH key and connect

When creating the VM, either upload your existing public key or generate a key
pair and download the private key. Never commit or share the private key.
Wait for the VM to show **Running**, then copy its public IP.

On your **Mac**, replace the example filename and IP:

```bash
chmod 600 ~/Downloads/ssh-key.key
ssh -i ~/Downloads/ssh-key.key opc@YOUR_PUBLIC_IP
```

Oracle Linux uses `opc` as the default SSH username. These instructions use
Oracle Linux 9, not Ubuntu. Oracle Cloud is the hosting provider; the VM image
determines the operating system.
Verify the host fingerprint through a trusted console channel when possible
before accepting the first connection.

| Error                         | Check                                                                           |
| ----------------------------- | ------------------------------------------------------------------------------- |
| Permission denied (publickey) | Matching private key and correct OS username                                    |
| Connection timed out          | Public IP, subnet, Internet Gateway route, SSH security rule and guest firewall |
| Unprotected private key file  | Apply `chmod 600` to the private key                                            |
| No public IP                  | Follow the preceding public-IP assignment steps                                 |

## 5. Install the application on the VM

### Install tool with timeout

```ps -eo pid,stat,etime,cmd | grep '[d]nf'
sudo timeout 60 dnf -v --setopt=timeout=5 --setopt=retries=0 makecache

sudo dnf --disablerepo='*' \
  --enablerepo=ol9_baseos_latest \
  --enablerepo=ol9_appstream \
  --setopt=timeout=5 --setopt=retries=0 \
  install -y git curl ca-certificates

```

Run on the **Oracle Linux 9 VM**:

```bash
cat /etc/os-release
sudo dnf install -y git curl ca-certificates
```

The OS output should identify Oracle Linux 9. Use `dnf`, not Ubuntu's `apt`.
The guide installs Python through uv, so it does not replace the system Python.

Install uv using its official installer. Download and inspect it first:

```bash
curl -fL --connect-timeout 5 --max-time 60 --retry 2 https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
less /tmp/uv-install.sh
sh /tmp/uv-install.sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

[Official uv installation instructions](https://docs.astral.sh/uv/getting-started/installation/).

Clone using your repository's actual URL. Do not embed access tokens in the URL.
For a private repository, configure a read-only deploy key or appropriate Git
credentials first.

```bash
git clone YOUR_REPOSITORY_URL ~/ops-trade-idea
cd ~/ops-trade-idea
uv python install 3.12
uv sync --locked --python 3.12
```

If installation fails on ARM, resolve that before enabling any trading process.
The project requires Python 3.12 or newer.

## 6. Configure secrets and persistent state

Create `.env` in the repository on the VM using a terminal editor. Supply your
own credentials privately. Start with these operating settings:

```dotenv
DRY_RUN=true
OPTIONOMICS_POLL_ENABLED=false
NEXT_DAY_EXIT_ENABLED=false
DATABASE_PATH=/var/lib/ops-trade-idea/bot.sqlite3
BULLISH_STOCK_ACCOUNT_NUMBER=YOUR_SANDBOX_CASH_ACCOUNT_NUMBER
TOP_BULLISH_ACCOUNT_NUMBER=YOUR_SANDBOX_MARGIN_ACCOUNT_NUMBER
OPTIONS_MARGIN_ACCOUNT_NUMBER=YOUR_SANDBOX_MARGIN_ACCOUNT_NUMBER
BULLISH_PROFIT_PERCENT=10
BULLISH_STOP_LOSS_PERCENT=5
BEARISH_PROFIT_PERCENT=20
BEARISH_STOP_LOSS_PERCENT=10
IRON_CONDOR_PROFIT_PERCENT=10
IRON_CONDOR_STOP_LOSS_PERCENT=5
```

Also copy your required `OPTIONOMICS_EMAIL`, `OPTIONOMICS_API_KEY`,
`OPTIONOMICS_API_URL`, `WEBULL_APP_KEY`, and `WEBULL_APP_SECRET` settings.
Use the README for other supported settings. Do not commit `.env`.

```bash
chmod 600 .env
sudo install -d -m 700 -o "$(id -un)" -g "$(id -gn)" /var/lib/ops-trade-idea
```

The shared broker currently hardcodes the Webull sandbox endpoint. The dedicated
bullish runner selects `TOP_BULLISH_ACCOUNT_NUMBER`; bearish PUT and neutral
iron-condor submissions select `OPTIONS_MARGIN_ACCOUNT_NUMBER`. Set both to the
same intended account number when all three should use one margin account.
Both routes use the same exact, unique account-number lookup and submit with
the returned API account ID. Missing configuration or an unmatched/ambiguous
account stops submission, without falling back to the first account.

The main service's bullish stock path requires `BULLISH_STOCK_ACCOUNT_NUMBER`
and resolves it by the same exact-match lookup, without first-account fallback.
The separate option runner's CALL path still selects the first returned account;
verify that destination separately if you enable it. Account type, permissions and sandbox availability
have not been verified by the local configuration comparison. Dry runs do not
validate account access. See [strategy account selection](README.md#strategy-account-selection).

The percentage values above are defaults; `10` means 10%. Restart affected
services after changing account or percentage settings. Existing orders are
unchanged, and account changes do not reset ledger reservations or deduplication.

### Transfer existing SQLite history

Preserve history to retain duplicate-order protection. For final cutover, stop
local trading processes before creating the backup and leave them stopped while
cloud trading is enabled. Do not simply copy a database that is being written.

On your **Mac**, from the repository (replace the source path if configured
elsewhere):

```bash
uv run python - <<'PY'
import sqlite3
from pathlib import Path
source = Path('bot.sqlite3').resolve()
target = Path('/tmp/ops-trade-idea-migration.sqlite3')
if target.exists():
    raise SystemExit('Choose a new backup path; destination already exists')
with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
    with sqlite3.connect(target) as dst:
        src.backup(dst)
PY
scp -i ~/Downloads/ssh-key.key /tmp/ops-trade-idea-migration.sqlite3 opc@YOUR_PUBLIC_IP:~/migration.sqlite3
```

On the **VM**, before starting services:

```bash
test ! -e /var/lib/ops-trade-idea/bot.sqlite3 && install -m 600 ~/migration.sqlite3 /var/lib/ops-trade-idea/bot.sqlite3
```

If the destination exists, inspect and back it up before deciding whether to
replace it. Do not merge or overwrite trading ledgers blindly.

## 7. Verify the private dashboard first

On the VM:

```bash
cd ~/ops-trade-idea
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --lifespan off
```

`--lifespan off` prevents startup trading workers during this initial check.
In another terminal on your Mac:

```bash
ssh -i ~/Downloads/ssh-key.key -N -L 8001:127.0.0.1:8000 opc@YOUR_PUBLIC_IP
```

Open:

- Dashboard: http://127.0.0.1:8001/
- Full records: http://127.0.0.1:8001/records
- Health: http://127.0.0.1:8001/health

These pages have no login. Keep the SSH tunnel approach: `/records` includes
raw stored data. The health endpoint alone does not establish successful scans.
Stop the manual VM server with Ctrl+C before starting its managed replacement.

## 8. Configure automatic startup with systemd

Use one FastAPI worker and no development reload. The templates below use Oracle Linux’s default user `opc` and assume the clone
is at `/home/opc/ops-trade-idea`.

Create `/etc/systemd/system/ops-trade-idea.service`:

```ini
[Unit]
Description=Ops Trade Idea API
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=opc
WorkingDirectory=/home/opc/ops-trade-idea
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/opc/ops-trade-idea/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=on-failure
RestartSec=15
UMask=0077

[Install]
WantedBy=multi-user.target
```

With the disabled-worker settings from step 6, this starts only the API/dashboard.
The app reads `.env` from its working directory.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ops-trade-idea
sudo systemctl status ops-trade-idea
sudo systemctl stop ops-trade-idea
sudo systemctl start ops-trade-idea
sudo journalctl -u ops-trade-idea -n 100 --no-pager
```

For the optional bullish runner, create
`/etc/systemd/system/ops-trade-idea-bullish.service`:

```ini
[Unit]
Description=Ops Trade Idea bullish scans
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=opc
WorkingDirectory=/home/opc/ops-trade-idea
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/opc/ops-trade-idea/.venv/bin/python app/main-top-bullish.py
Restart=on-failure
RestartSec=15
UMask=0077

[Install]
WantedBy=multi-user.target
```

Do not start the bullish service until the validation and cutover steps below.
Its internal scheduler already repeats every five minutes; no cron is needed.

## 9. Validate and enable only intended strategies

Before submissions:

1. Run tests with polling disabled and dry-run enabled. Resolve failures before
   unattended operation. At documentation time, the bullish market-hours tests
   remain while the corresponding checks are absent from the current source.
2. Restore or verify required market-hours protection. `main.py` currently checks
   weekday 09:30–16:00 New York time, not holidays or early closes.
3. Verify read-only account lookup and fresh quotes from the cloud VM.
4. Reconcile any uncertain orders with Webull before retrying.
5. Run a bullish preview against a separate database:

   ```bash
   DRY_RUN=true DATABASE_PATH=/tmp/bullish-preview.sqlite3 uv run python app/main-top-bullish.py --once
   ```

   This makes real feed/quote requests but does not submit orders. Dry-run rows
   reserve symbols, which is why the operational database must not be used.

6. Confirm the dashboard, logs, service restart and backup restoration work.
7. Stop all local trading processes, transfer final database history, and enable
   cloud trading only when ready.

For actual sandbox submissions, set `DRY_RUN=false`. To enable the main strategy,
set `OPTIONOMICS_POLL_ENABLED=true` and restart the API service. Keep it false
if you only want bullish trading. `DRY_RUN` is shared by both processes.

To activate the bullish service after these checks:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ops-trade-idea-bullish
sudo journalctl -u ops-trade-idea-bullish -f
```

Keep `NEXT_DAY_EXIT_ENABLED=false` until its broker behavior is validated. The
bullish runner does not create next-day exit jobs; enabling that worker does not
add scheduled exits to bullish orders. Bullish symbol deduplication is permanent
and applies even when changing accounts. The two strategies do not share a
complete cross-strategy duplicate/position guard.

## 10. Backups, monitoring, and updates

- Automate a daily SQLite backup using the backup API shown above with a unique
  timestamped destination. Keep copies outside the VM, with retention limits.
- Test restoring a backup into a separate file, never over the running ledger.
- Monitor disk space, service status, last completed scan and unresolved orders.
- Keep logs rotated; never publish credentials or raw broker logs.
- Before updating code, stop the relevant services, back up state, pull the
  intended revision, run `uv sync --locked`, validate, and restart.
- Restart services after changing `.env`; do not start a second manual runner.

Live cash-exit HTTP 429 responses are recognized by status or the
`TOO_MANY_REQUESTS` code. The worker persists a queue-wide cooldown and stops
that reconciliation pass. If logs instead show each `Scheduled stock exit ...
deferred` independently, the service is running an older checkout; update the
repo and restart every service that runs the cash-exit worker (API and bullish
runner, if enabled). After restart, expect one `Webull rate limited exits;
queue deferred` warning per 429 event rather than a per-job deferral.

Useful commands:

```bash
sudo systemctl status ops-trade-idea ops-trade-idea-bullish
sudo journalctl -u ops-trade-idea-bullish -n 100 --no-pager
df -h /var/lib/ops-trade-idea
sudo systemctl stop ops-trade-idea-bullish
sudo systemctl start ops-trade-idea-bullish
```

## 11. Understand free-tier interruptions

Oracle can classify a VM as idle over seven days when CPU utilization's 95th
percentile and network utilization are below 20%; A1 also considers memory below
20%. A small task every five minutes does not guarantee avoidance of reclamation.

If the VM is unavailable, polling and application-managed exits stop. Broker
orders already submitted are not automatically cancelled by a server outage.
`systemd` handles process failures and boots, not recovery of reclaimed compute.
Maintain backups and an external availability check. Do not assume uninterrupted
execution just because the resource is called Always Free.

## Last 3 days log and create file

```
sudo journalctl -u ops-trade-idea --since "3 days ago" --no-pager > ops-trade-idea-last-3-days.log
```
