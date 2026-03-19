import json
import os
import smtplib
import sqlite3
import logging
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from contextlib import asynccontextmanager
from typing import Optional

import anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ─── Ensure data directory exists ──────────────────────────────────────────────
os.makedirs("data", exist_ok=True)

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/agent.log"),
    ]
)
log = logging.getLogger(__name__)

# ─── Database ─────────────────────────────────────────────────────────────────
DB_PATH = "data/jobs.db"

def init_db():
    os.makedirs("data", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            company     TEXT,
            location    TEXT,
            posted      TEXT,
            skills      TEXT,
            apply_link  TEXT,
            salary      TEXT,
            job_type    TEXT,
            description TEXT,
            dedup_key   TEXT UNIQUE,
            found_at    TEXT,
            run_id      INTEGER
        );

        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  TEXT,
            finished_at TEXT,
            status      TEXT,
            jobs_found  INTEGER DEFAULT 0,
            email_sent  INTEGER DEFAULT 0,
            error       TEXT
        );

        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    con.commit()
    con.close()
    log.info("Database initialized at %s", DB_PATH)

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

# ─── Config helpers ───────────────────────────────────────────────────────────
def load_config() -> dict:
    con = get_db()
    rows = con.execute("SELECT key, value FROM config").fetchall()
    con.close()
    cfg = {r["key"]: r["value"] for r in rows}
    # defaults
    cfg.setdefault("keywords", json.dumps(["Python", "Machine Learning", "AI", "Data Science"]))
    cfg.setdefault("locations", json.dumps(["Remote", "Bangalore"]))
    cfg.setdefault("experience", "1 year")
    cfg.setdefault("interval_hours", "2")
    cfg.setdefault("remote_only", "false")
    cfg.setdefault("include_internships", "true")
    cfg.setdefault("agent_enabled", "false")
    return cfg

def save_config(updates: dict):
    con = get_db()
    for key, value in updates.items():
        con.execute(
            "INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
    con.commit()
    con.close()

# ─── Scheduler ────────────────────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

def reschedule(interval_hours: float):
    """Remove old job and add new one with updated interval."""
    if scheduler.get_job("job_search"):
        scheduler.remove_job("job_search")
    scheduler.add_job(
        run_search_job,
        trigger=IntervalTrigger(hours=interval_hours),
        id="job_search",
        replace_existing=True,
    )
    log.info("Scheduler set to every %.1f hour(s)", interval_hours)

# ─── Core search logic ────────────────────────────────────────────────────────
def run_search_job():
    """Called by the scheduler every N hours."""
    cfg = load_config()
    if cfg.get("agent_enabled") != "true":
        log.info("Agent disabled, skipping run.")
        return

    api_key   = cfg.get("anthropic_api_key", "")
    email_to  = cfg.get("email_to", "")
    smtp_user = cfg.get("smtp_user", "")
    smtp_pass = cfg.get("smtp_pass", "")

    if not api_key:
        log.error("No Anthropic API key configured.")
        return

    keywords   = json.loads(cfg.get("keywords", "[]"))
    locations  = json.loads(cfg.get("locations", "[]"))
    experience = cfg.get("experience", "1 year")
    remote_only = cfg.get("remote_only") == "true"
    include_intern = cfg.get("include_internships") == "true"

    # Record run start
    con = get_db()
    cur = con.execute(
        "INSERT INTO runs(started_at, status) VALUES(?, 'running')",
        (datetime.now(timezone.utc).isoformat(),)
    )
    run_id = cur.lastrowid
    con.commit()
    con.close()

    log.info("Run #%d starting — keywords: %s", run_id, keywords)

    try:
        jobs = search_jobs_with_claude(
            api_key, keywords, locations, experience, remote_only, include_intern
        )
        new_jobs = save_new_jobs(jobs, run_id)
        log.info("Run #%d — found %d total, %d new", run_id, len(jobs), len(new_jobs))

        email_sent = False
        if new_jobs and email_to and smtp_user and smtp_pass:
            send_email_alert(smtp_user, smtp_pass, email_to, new_jobs, experience, keywords)
            email_sent = True

        con = get_db()
        con.execute(
            "UPDATE runs SET finished_at=?, status='success', jobs_found=?, email_sent=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), len(new_jobs), int(email_sent), run_id)
        )
        con.commit()
        con.close()

    except Exception as exc:
        log.exception("Run #%d failed: %s", run_id, exc)
        con = get_db()
        con.execute(
            "UPDATE runs SET finished_at=?, status='error', error=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), str(exc), run_id)
        )
        con.commit()
        con.close()


def search_jobs_with_claude(api_key, keywords, locations, experience, remote_only, include_intern) -> list[dict]:
    """Call Claude with web search to find live jobs."""
    client = anthropic.Anthropic(api_key=api_key)

    loc_str = ", ".join(locations) if locations else "anywhere / remote"
    kw_str  = ", ".join(keywords)

    prompt = f"""Search the internet RIGHT NOW for the LATEST active job openings. 
Use web search to check LinkedIn Jobs, Naukri.com, Indeed, Glassdoor, AngelList/Wellfound, 
Internshala (if internships needed), and company career pages.

Search criteria:
- Keywords: {kw_str}
- Experience: {experience} (entry-level / junior)
- Location: {loc_str}
- Remote only: {remote_only}
- Include internships: {include_intern}

Return ONLY a valid JSON array. Each element must have:
{{
  "title": "exact job title",
  "company": "company name",
  "location": "city or Remote",
  "posted": "e.g. 1 day ago",
  "skills": ["Python", "ML"],
  "applyLink": "https://...",
  "salary": "salary range or null",
  "type": "Full-time or Internship or Contract",
  "description": "2 sentence summary"
}}

Return 10-15 jobs. ONLY the JSON array, nothing else."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )

    # Extract text blocks
    full_text = " ".join(
        block.text for block in response.content if hasattr(block, "text")
    )

    # Parse JSON array from response
    import re
    match = re.search(r'\[[\s\S]*\]', full_text)
    if not match:
        raise ValueError("Claude did not return a JSON array. Raw: " + full_text[:500])

    jobs = json.loads(match.group(0))
    log.info("Claude returned %d jobs", len(jobs))
    return jobs


def save_new_jobs(jobs: list[dict], run_id: int) -> list[dict]:
    """Insert jobs into DB, skip duplicates. Return only new ones."""
    con = get_db()
    new_jobs = []
    for j in jobs:
        key = (j.get("title", "") + j.get("company", "")).lower().replace(" ", "")
        try:
            con.execute(
                """INSERT INTO jobs
                   (title,company,location,posted,skills,apply_link,salary,job_type,description,dedup_key,found_at,run_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    j.get("title"), j.get("company"), j.get("location"),
                    j.get("posted"), json.dumps(j.get("skills", [])),
                    j.get("applyLink"), j.get("salary"), j.get("type"),
                    j.get("description"), key,
                    datetime.now(timezone.utc).isoformat(), run_id,
                )
            )
            new_jobs.append(j)
        except sqlite3.IntegrityError:
            pass  # duplicate
    con.commit()
    con.close()
    return new_jobs


def send_email_alert(smtp_user, smtp_pass, to_email, jobs, experience, keywords):
    """Send HTML email with job listings via Gmail SMTP."""
    kw_str = ", ".join(keywords)
    subject = f"🤖 AI Job Alert: {len(jobs)} New {kw_str} Jobs — {datetime.now().strftime('%d %b %Y')}"

    # Build HTML rows
    rows_html = ""
    for j in jobs:
        skills_badges = "".join(
            f'<span style="background:#e8e4ff;color:#5046e4;border-radius:4px;padding:2px 8px;font-size:12px;margin-right:4px">{s}</span>'
            for s in (j.get("skills") or [])[:5]
        )
        apply_btn = (
            f'<a href="{j["applyLink"]}" style="background:#5046e4;color:white;padding:6px 14px;border-radius:6px;text-decoration:none;font-size:13px">Apply →</a>'
            if j.get("applyLink") else ""
        )
        rows_html += f"""
        <div style="border:1px solid #e5e7eb;border-radius:12px;padding:18px;margin-bottom:14px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start">
            <div>
              <div style="font-size:16px;font-weight:600;color:#111">{j.get('title','')}</div>
              <div style="color:#5046e4;font-size:14px;margin-top:2px">{j.get('company','')} · {j.get('location','')}</div>
            </div>
            <span style="color:#f59e0b;font-size:12px">{j.get('posted','Recent')}</span>
          </div>
          <div style="margin-top:10px">{skills_badges}</div>
          {f'<div style="color:#6b7280;font-size:13px;margin-top:8px">{j.get("description","")}</div>' if j.get('description') else ''}
          {f'<div style="color:#059669;font-size:13px;margin-top:6px">💰 {j.get("salary")}</div>' if j.get('salary') else ''}
          <div style="margin-top:12px">{apply_btn}</div>
        </div>"""

    html_body = f"""
    <html><body style="font-family:sans-serif;max-width:640px;margin:0 auto;padding:24px;color:#111">
      <div style="background:linear-gradient(135deg,#5046e4,#0ea5e9);border-radius:16px;padding:28px;color:white;margin-bottom:24px">
        <div style="font-size:24px;font-weight:700">🤖 AI Job Hunt Agent</div>
        <div style="opacity:0.85;margin-top:6px">Found <strong>{len(jobs)}</strong> new {kw_str} jobs for you</div>
        <div style="opacity:0.7;font-size:13px;margin-top:4px">Experience: {experience} · {datetime.now().strftime('%d %b %Y, %I:%M %p IST')}</div>
      </div>
      {rows_html}
      <div style="text-align:center;color:#9ca3af;font-size:12px;margin-top:24px;border-top:1px solid #e5e7eb;padding-top:16px">
        Sent automatically by your AI Job Hunt Agent · Apply within 24 hrs for best results!
      </div>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = to_email
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, to_email, msg.as_string())

    log.info("Email sent to %s", to_email)


# ─── FastAPI App ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    cfg = load_config()
    interval = float(cfg.get("interval_hours", "2"))
    reschedule(interval)
    scheduler.start()
    log.info("Scheduler started")
    if cfg.get("agent_enabled") == "true":
        log.info("Agent was enabled, resuming...")
    yield
    scheduler.shutdown()
    log.info("Scheduler stopped")

app = FastAPI(title="AI Job Hunt Agent", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend
app.mount("/static", StaticFiles(directory="../frontend"), name="static")

@app.get("/")
def serve_ui():
    return FileResponse("../frontend/index.html")

# ─── API Models ───────────────────────────────────────────────────────────────
class ConfigUpdate(BaseModel):
    anthropic_api_key: Optional[str] = None
    email_to: Optional[str] = None
    smtp_user: Optional[str] = None
    smtp_pass: Optional[str] = None
    keywords: Optional[list[str]] = None
    locations: Optional[list[str]] = None
    experience: Optional[str] = None
    interval_hours: Optional[float] = None
    remote_only: Optional[bool] = None
    include_internships: Optional[bool] = None

# ─── Routes ──────────────────────────────────────────────────────────────────
@app.get("/api/config")
def get_config():
    cfg = load_config()
    # Never expose secrets to frontend
    safe = {k: v for k, v in cfg.items() if k not in ("anthropic_api_key","smtp_pass")}
    safe["has_api_key"]  = bool(cfg.get("anthropic_api_key"))
    safe["has_smtp_pass"] = bool(cfg.get("smtp_pass"))
    safe["keywords"]  = json.loads(safe.get("keywords", "[]"))
    safe["locations"] = json.loads(safe.get("locations", "[]"))
    return safe

@app.post("/api/config")
def update_config(body: ConfigUpdate):
    updates = {}
    if body.anthropic_api_key is not None: updates["anthropic_api_key"] = body.anthropic_api_key
    if body.email_to is not None:          updates["email_to"] = body.email_to
    if body.smtp_user is not None:         updates["smtp_user"] = body.smtp_user
    if body.smtp_pass is not None:         updates["smtp_pass"] = body.smtp_pass
    if body.keywords is not None:          updates["keywords"] = json.dumps(body.keywords)
    if body.locations is not None:         updates["locations"] = json.dumps(body.locations)
    if body.experience is not None:        updates["experience"] = body.experience
    if body.remote_only is not None:       updates["remote_only"] = str(body.remote_only).lower()
    if body.include_internships is not None: updates["include_internships"] = str(body.include_internships).lower()
    if body.interval_hours is not None:
        updates["interval_hours"] = str(body.interval_hours)
        reschedule(body.interval_hours)
    save_config(updates)
    return {"ok": True}

@app.post("/api/agent/start")
def start_agent():
    save_config({"agent_enabled": "true"})
    cfg = load_config()
    interval = float(cfg.get("interval_hours", "2"))
    reschedule(interval)
    log.info("Agent STARTED via API")
    return {"ok": True, "next_run": get_next_run_time()}

@app.post("/api/agent/stop")
def stop_agent():
    save_config({"agent_enabled": "false"})
    if scheduler.get_job("job_search"):
        scheduler.pause_job("job_search")
    log.info("Agent STOPPED via API")
    return {"ok": True}

@app.post("/api/agent/run-now")
def run_now():
    """Trigger an immediate search outside the schedule."""
    import threading
    t = threading.Thread(target=run_search_job, daemon=True)
    t.start()
    return {"ok": True, "message": "Search started in background"}

@app.get("/api/status")
def get_status():
    cfg = load_config()
    con = get_db()
    total_jobs  = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    total_runs  = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    emails_sent = con.execute("SELECT SUM(email_sent) FROM runs").fetchone()[0] or 0
    last_run    = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    job = scheduler.get_job("job_search")
    return {
        "agent_enabled": cfg.get("agent_enabled") == "true",
        "total_jobs": total_jobs,
        "total_runs": total_runs,
        "emails_sent": emails_sent,
        "next_run": str(job.next_run_time) if job and job.next_run_time else None,
        "last_run": dict(last_run) if last_run else None,
        "interval_hours": cfg.get("interval_hours", "2"),
    }

@app.get("/api/jobs")
def get_jobs(limit: int = 50, offset: int = 0):
    con = get_db()
    rows = con.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    total = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    con.close()
    jobs = []
    for r in rows:
        j = dict(r)
        j["skills"] = json.loads(j.get("skills") or "[]")
        jobs.append(j)
    return {"jobs": jobs, "total": total}

@app.get("/api/runs")
def get_runs(limit: int = 20):
    con = get_db()
    rows = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return {"runs": [dict(r) for r in rows]}

@app.delete("/api/jobs/clear")
def clear_jobs():
    con = get_db()
    con.execute("DELETE FROM jobs")
    con.commit()
    con.close()
    return {"ok": True}

def get_next_run_time():
    job = scheduler.get_job("job_search")
    return str(job.next_run_time) if job and job.next_run_time else None

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
