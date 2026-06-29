# ExpiredDomains.net scraper

This logs in to ExpiredDomains.net, scrapes the expired-domains table, filters domains by the configured TLD list, deduplicates them, and repeats every 45 minutes by default.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
```

Edit `.env` and set:

```dotenv
EXPIREDDOMAINS_LOGIN=your-username
EXPIREDDOMAINS_PASSWORD=your-password
```

ExpiredDomains.net labels the login field as `Username`. If email login returns “unknown,” put the account username in `EXPIREDDOMAINS_LOGIN`.

## Run

Run the Django GUI:

```powershell
.\venv\Scripts\python.exe manage.py runserver 127.0.0.1:8001 --noreload
```

Open:

```text
http://127.0.0.1:8001/
```

Use the dashboard buttons to start or stop the scraper.

Scrape one time:

```powershell
python main.py --once
```

Run forever, every 45 minutes:

```powershell
python main.py
```

On Windows, you can use the venv runner scripts:

```powershell
.\run_once.ps1
.\run_forever.ps1
```

## AdsPower

To run through AdsPower, keep AdsPower open and set:

```dotenv
ADSPOWER_ENABLED=true
ADSPOWER_API_URL=http://127.0.0.1:51441
ADSPOWER_API_KEY=your-api-key
ADSPOWER_USER_ID=your-profile-user-id
```

The profile user id is the value returned by the AdsPower API, for example `k1dvakoe`, not the UI row number.

## MongoDB

Set MongoDB values in `.env`:

```dotenv
MONGO_URI=mongodb://127.0.0.1:27017
MONGO_DB=expired_domains_db
MONGO_COLLECTION=expired_domains
```

The scraper creates indexes automatically.

Check/create the database and collection:

```powershell
python main.py --check-db
```

Output files:

- `result.json`: all saved matching rows
- `domains.jsonl`: append-only matching rows, one JSON object per line
- `seen_domains.json`: dedupe state
- `.browser-state/`: persistent browser cookies/session

## Optional signup helper

Set these in `.env`:

```dotenv
EXPIREDDOMAINS_REGISTER_USERNAME=your-username
EXPIREDDOMAINS_REGISTER_EMAIL=your-email
EXPIREDDOMAINS_REGISTER_PASSWORD=your-password
```

Then run:

```powershell
python main.py --signup
```

If ExpiredDomains.net sends an activation email, activate the account before running the scraper.

Request another activation email:

```powershell
python main.py --request-activation
```

Request a password reset email:

```powershell
python main.py --request-password-reset
```

Save a browser login session manually:

```powershell
python main.py --login-visible
```

After the browser opens, log in on ExpiredDomains.net, return to the terminal, and press Enter. The session is stored in `.browser-state/`.

## VPS systemd service

On Linux, put the project in `/opt/expired-domains-scraper`, install dependencies, then create:

```ini
[Unit]
Description=ExpiredDomains.net scraper
After=network-online.target

[Service]
WorkingDirectory=/opt/expired-domains-scraper
ExecStart=/opt/expired-domains-scraper/.venv/bin/python /opt/expired-domains-scraper/main.py
Restart=always
RestartSec=20
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Save it as `/etc/systemd/system/expired-domains-scraper.service`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now expired-domains-scraper
sudo journalctl -u expired-domains-scraper -f
```
"# automation-ml" 
