# VineLedger deployment and data persistence

VineLedger stores **all** school data (students, fees, payments, expenses, staff, **Pending Reviews / Save for later**) in a **single SQLite file** (`school.db` by default, or the path in `VINELEDGER_SQLITE_PATH`).

## If “correct” data disappears after a restart or redeploy

That almost always means the **SQLite file on the server was replaced or wiped**, not that Streamlit “forgot” in-browser state. Pending drafts and approved rows live in the **same file**; if the file is new or empty, **everything** looks reset.

Common causes:

- **Streamlit Community Cloud** — container disk is often **ephemeral**. A new deploy, cold start, or platform maintenance can give you a **fresh empty** `school.db`.
- **Running Streamlit from two different directories** on one machine — each cwd can point at a **different** `school.db`.
- **Restoring an old backup** over a newer file by mistake.

## What to do in production

1. **Turn on the in-app warning** (Streamlit Cloud): in **App settings → Secrets**, add:

   ```toml
   [vineledger]
   ephemeral_storage = true
   ```

   After sign-in, the sidebar shows a **data hosting** alert so staff know to back up.

2. **Download the database regularly**: **Configuration → Database backup** (admin password). Store files in a **private** off-site location (cloud drive, school server). Do this **after** bulk balance work or end of day.

3. **Prefer a persistent host** for real production load: one machine or VM with a **fixed absolute** `VINELEDGER_SQLITE_PATH`, nightly copies of that file, or a managed database (would require app changes beyond SQLite).

4. **Never rely on GitHub** for live `school.db` — it is not in the repo by default (and should not be committed: PII + size).

## Self-check

- Confirm in the app (or logs) which path is in use — **Database backup** page shows the resolved SQLite path caption.
- After any major data entry session, **download a snapshot** before redeploying the app on Community Cloud.
