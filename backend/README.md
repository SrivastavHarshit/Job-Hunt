# 🤖 AI Job Hunt Agent — Full Stack

A real backend agent that searches the internet for Python/ML/AI jobs every 2 hours
and emails them to you automatically — even when your browser is closed.

## Architecture

```
Frontend (HTML/JS)
      │ HTTP
      ▼
Backend (FastAPI) ◄── APScheduler (runs every 2h)
      │                    │
      │                    ▼
      │           Claude API + Web Search
      │                    │
      │                    ▼
      ├──────────► SQLite DB (jobs.db)
      │
      └──────────► Gmail SMTP → Your Inbox
```

## Quick Setup (5 minutes)

### 1. Clone / unzip this project

```
job_agent/
├── backend/
│   ├── main.py
│   └── requirements.txt
├── frontend/
│   └── index.html
└── README.md
```

### 2. Install Python dependencies

```bash
cd backend
pip install -r requirements.txt
```

### 3. Get your Anthropic API key

Sign up at https://console.anthropic.com → API Keys → Create Key
It starts with `sk-ant-api...`

### 4. Set up Gmail App Password (for sending emails)

Gmail requires an "App Password" — NOT your regular Gmail password.

1. Go to: https://myaccount.google.com/apppasswords
2. Select "Mail" + "Windows Computer" (or any device)
3. Click Generate → copy the 16-char password (e.g. `abcd efgh ijkl mnop`)
4. You must have 2-Factor Authentication enabled on your Google account

### 5. Run the backend

```bash
cd backend
python main.py
```

You should see:
```
INFO: Database initialized at data/jobs.db
INFO: Scheduler started
INFO: Uvicorn running on http://0.0.0.0:8000
```

### 6. Open the frontend

Open `frontend/index.html` in your browser, OR visit http://localhost:8000

### 7. Configure in the UI

Fill in:
- **Anthropic API Key**: your `sk-ant-...` key
- **Send alerts to**: your email where you want to receive jobs
- **Gmail SMTP user**: your Gmail address (e.g. yourname@gmail.com)
- **Gmail App Password**: the 16-char app password from step 4
- **Keywords**: Python, Machine Learning, AI, etc.
- **Locations**: Bangalore, Remote, etc. (or leave empty for anywhere)

Click **Save Config**, then **Start Agent**.

---

## What happens when the agent runs

1. Claude searches the web (LinkedIn, Naukri, Indeed, Glassdoor, AngelList, etc.)
2. New jobs are parsed and saved to SQLite (`data/jobs.db`)
3. Duplicate jobs are automatically skipped
4. A formatted HTML email is sent to your inbox via Gmail SMTP
5. The scheduler sleeps for 2 hours and repeats

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/config` | Load current config |
| POST | `/api/config` | Save config |
| POST | `/api/agent/start` | Start the scheduler |
| POST | `/api/agent/stop` | Stop the scheduler |
| POST | `/api/agent/run-now` | Trigger immediate search |
| GET | `/api/status` | Agent status + stats |
| GET | `/api/jobs` | All found jobs |
| GET | `/api/runs` | Run history |
| DELETE | `/api/jobs/clear` | Clear all jobs |

---

## Run as a background service (Linux/macOS)

To keep the agent running even after you close the terminal:

### Option A — systemd (Linux)

Create `/etc/systemd/system/job-agent.service`:

```ini
[Unit]
Description=AI Job Hunt Agent
After=network.target

[Service]
WorkingDirectory=/path/to/job_agent/backend
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable job-agent
sudo systemctl start job-agent
sudo systemctl status job-agent
```

### Option B — nohup (quick and simple)

```bash
cd backend
nohup python main.py > ../data/agent.log 2>&1 &
echo $! > ../data/agent.pid
```

Stop with: `kill $(cat data/agent.pid)`

### Option C — screen/tmux

```bash
screen -S job-agent
cd backend && python main.py
# Ctrl+A, D to detach
```

### Option D — Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt
COPY backend/ ./backend/
COPY frontend/ ./frontend/
WORKDIR /app/backend
CMD ["python", "main.py"]
```

```bash
docker build -t job-agent .
docker run -d -p 8000:8000 -v $(pwd)/data:/app/backend/data --name job-agent job-agent
```

---

## Troubleshooting

**"Could not reach backend"**
→ Make sure `python main.py` is running in the backend folder

**"Email not sending"**
→ Check Gmail App Password is correct (not your account password)
→ Make sure 2FA is enabled on your Google account

**"No jobs found"**
→ Check your Anthropic API key is valid and has credits
→ Try broader keywords or remove location filter

**Logs**
→ Backend logs: `data/agent.log`
→ Frontend logs: "Activity Log" tab in the UI

---

## Customization

Edit `main.py` to:
- Add more job sites to the Claude search prompt
- Add Telegram/WhatsApp notifications instead of email
- Export jobs to Google Sheets
- Filter by company size, salary range, etc.
# Job-Hunt
This will search job for you.
