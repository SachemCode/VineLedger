# VineLedger

VineLedger helps your school manage student fees, payments, expenses, and term-by-term billing. It runs in the browser using Streamlit and stores data in a single SQLite file (`school.db` by default, or the path in `VINELEDGER_SQLITE_PATH`).

---

## Getting started

1. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. **Configure the operator gate (required).** VineLedger will not load until all **five** passwords are set in the environment (they are never stored in the repo):

   - `VINELEDGER_GATE_USER1_PASSWORD`
   - `VINELEDGER_GATE_USER2_PASSWORD`
   - `VINELEDGER_GATE_USER3_PASSWORD`
   - `VINELEDGER_GATE_USER4_PASSWORD`
   - `VINELEDGER_GATE_USER5_PASSWORD`

   Optional: `VINELEDGER_GATE_IDLE_SECONDS` (default **900** = 15 minutes). After that many seconds **without any interaction** (clicks, navigation, form edits that trigger a rerun), the session ends and the user must sign in again. A background check runs about every minute so timeouts still apply if the tab is left open without clicks.

   For **local development only**, you can put the same variables in a file named **`.env`** in the project folder (see [`.env.example`](.env.example)). On startup, `app.py` loads missing keys from `.env`. That file is listed in **`.gitignore`** so it is not committed; do not copy real passwords into the repo.

   Example (macOS/Linux, current shell only):

   ```bash
   export VINELEDGER_GATE_USER1_PASSWORD='choose-a-strong-password-1'
   export VINELEDGER_GATE_USER2_PASSWORD='choose-a-strong-password-2'
   export VINELEDGER_GATE_USER3_PASSWORD='choose-a-strong-password-3'
   export VINELEDGER_GATE_USER4_PASSWORD='choose-a-strong-password-4'
   export VINELEDGER_GATE_USER5_PASSWORD='choose-a-strong-password-5'
   ```

3. Start the app:
   ```bash
   streamlit run app.py
   ```
4. Open the link shown in the terminal (usually http://localhost:8501).

### Receipt PDFs (disk and printing)

- Generated receipt PDFs are written under your OS temp directory in a folder named **`vineledger_receipts`**, not inside the project tree, so high daily volume does not bloat the repo.
- On each new browser session, the app removes files in that folder older than **`VINELEDGER_RECEIPT_TTL_HOURS`** hours (default **48**). Set to **0** to disable TTL cleanup.
- **`pip install -r requirements.txt`** pulls **`streamlit[pdf]`** so **Generate receipts** can show an in-app PDF preview (use the browser’s Print dialog). Without the PDF extra, use **Download** or **Open for printing** instead.

### One server, several devices (Tailscale, LAN, tablets)

Run **Streamlit only on the computer that should own the database** (the “server”), then open the app from phones or other PCs using that machine’s Tailscale or LAN URL (for example `http://100.x.x.x:8501`). Every device is just another **browser** talking to the **same** Python process and the **same** `school.db` file.

- If saves seem to work on the host but **not** on other devices, you were often hitting SQLite **`database is locked`** when two people saved at once. The app now opens the DB with **WAL mode**, an **8s busy timeout**, and **`check_same_thread=False`**, which is what Streamlit expects when many sessions write to one file. You may see **`school.db-wal`** and **`school.db-shm`** next to `school.db` while the app is running; that is normal for WAL.
- Point every launch at the **same file path** if you ever change directories: set **`VINELEDGER_SQLITE_PATH`** to an absolute path (see [`.env.example`](.env.example)).

#### Deployment checklist (one database, one backup target)

- Run **`streamlit run app.py` on exactly one computer** (the server that owns the data).
- Other phones and PCs must open **only that server’s URL** (Tailscale, LAN IP, or `localhost` on the host itself). Do **not** run Streamlit on those devices for the same school—each local run would use its **own** `school.db` and you would get **split data and split backups**.
- On the server, set **`VINELEDGER_SQLITE_PATH`** in `.env` to an **absolute** path so every app start uses the same file regardless of the shell’s current directory.

**Streamlit always re-runs the whole script** after most clicks (including **Save now** with the admin password). That can feel like a “full refresh”; it is normal. Your gate session and sidebar choices live in **session state** and survive those reruns unless the tab is closed or the gate **idle timeout** signs you out.

### Backup (single source of truth)

Back up **only** the SQLite file on the **Streamlit host**—the path from `VINELEDGER_SQLITE_PATH`, or `school.db` in the project directory if unset. Other devices do not hold the canonical database.

Use the included script for an **online** copy (consistent snapshot; OK while the app is running):

```bash
cd /path/to/school_system
python scripts/backup_school_db.py
```

Optional arguments:

```bash
python scripts/backup_school_db.py --src /absolute/path/to/school.db --dest-dir /absolute/path/to/backups
```

Backups are written as `backups/school_YYYYMMDD_HHMMSS.db` by default. Schedule this with **cron** or **Task Scheduler** on the same machine that runs VineLedger. Do not copy `school.db` from other laptops that were never meant to run the server—those files are not your live data.

#### Streamlit Community Cloud

The container filesystem is **not** a reliable archive. Use **Configuration → Database backup** in the app (admin password): download a WAL-safe `.db` snapshot, then upload it to a **private** Google Drive folder (or another off-site store) on a cadence you choose. GitHub holds **code**, not your hosted SQLite file.

**Pending Reviews and “Save for later”** are stored in the same SQLite file (`pending_reviews` table). They are **not** held only in browser memory. If drafts vanish after a platform **redeploy** or **disk reset**, the whole database file was replaced—restore from your most recent downloaded backup.

At the gate, **type** your account slug (**user1** … **user5**) and the matching password. Use separate accounts so `gate_audit` in SQLite can show which operator signed in, signed out, timed out, or failed login. Query example:

```bash
sqlite3 school.db "SELECT ts, user_slug, event, detail FROM gate_audit ORDER BY id DESC LIMIT 50"
```

**Business actions** (add student, payments, expenses, staff, manage student, receipts) are logged in **`app_action_audit`** with the gate user, a short summary, `save_mode` (`immediate`, `pending_review`, or `approved_from_pending` when a pending draft is applied), and optional JSON `detail`. Manual and bank-import payments also get a stable **`internal_payment_id`** on each row (9 uppercase letters and digits, e.g. `K4P2M8QX1`) for cross-referencing in audits and on receipts.

```bash
sqlite3 school.db "SELECT ts, user_slug, action_area, save_mode, summary FROM app_action_audit ORDER BY id DESC LIMIT 50"
```

```bash
sqlite3 school.db "SELECT id, internal_payment_id, student_id, amount, payment_date FROM payments ORDER BY id DESC LIMIT 20"
```

The default admin password for protected actions is **1234** (set in the app code). Change this before using VineLedger with real data.

---

## What you can do

### Students

- Add learners one at a time or import a spreadsheet.
- View lists by grade or co-curricular club.
- Search and edit records under **Manage Students**.
- Mark someone as **transferred** or **schedule deletion** (with a grace period before permanent removal).
- When deleting, you must choose a **reason** (duplicate record, no longer at the school, etc.).
- Tick **New Admission?** when adding a brand-new learner so the join date is recorded for reports.

### Fees and balances

- Fee amounts live under **Configuration → Fee Structure**.
- Each student’s balance reflects their fees minus payments. When the school calendar is set up, balances follow **term billing** (see below).
- A small **(co)** above a balance means money was **carried over** from a previous term. Click it to see details under **Payment Management → Carry On**.
- **Recovering balances:** If balances disappear but staff had saved under **Manage Students**, check **`app_action_audit`** (see query above). You can replay the latest audited balance per learner with  
  `python scripts/recover_balances_from_audit.py school.db`  
  Review the output, then add **`--apply`** to write those values back into `students`.

### Payments

Under **Payment Management** you can:

- Upload and match **bank statements** as **PDF**, **CSV**, or **Excel** (exports with columns such as *Transaction details*, *Credit (money in)*, and *Debit (money out)*). Rows with money in **Debit** are treated as **outgoing** and skipped; **Credit** rows are incoming payments.
- The parser pulls the M-Pesa-style **U…** reference from the first line of the narration (after the `254…` payer phone) and shows it as **M-Pesa U code** in the match table. When you record **Add payment** with that same code in **Transaction / reference code**, the matcher can strongly prefer the right learner on import.
- **Add payment**: optional **Actual payer** name and phone — if **Guardian 2** is empty on the student, those are saved there; otherwise they are appended to the payment notes.
- Add cash or other payments manually (balances update after each saved payment; learners on **Not set** balance get a computed **set** / **cleared** status when term billing or the fee formula applies).
- Review **carry-on** balances (with a search bar).
- Approve saved payments from **Pending Reviews**.

Receipts can be generated from **Generate Receipts** once a template is uploaded.

### School calendar and terms

Under **Configuration → School Calendar** you set Term 1–3 opening and closing dates. The system can:

- Warn you on the Dashboard before a term closes.
- Close a term and open the next one on the dates you set.
- Bill active students when a new term opens.
- After Term 3 closes: mark **Grade 9** leavers as **Graduated**, save a read-only copy to `data/graduated_students.jsonl` (not shown in View Students), then promote all other active grades up one step (Grade 8 becomes the new Grade 9, and so on).

### Dashboard at a glance

The Dashboard shows:

- How many students you have, and how many use transport or meals.
- **New admissions** this term and this school year (based on join dates).
- **Active students** (graduates are excluded from enrolment counts; their records are archived on disk).
- **Student exits** this term (transfers and scheduled deletions only—not routine Grade 9 graduation).
- Outstanding balances (students with carry-on listed first).
- A grade distribution chart.

### Staff and expenses

Staff records and school expenses are available from the sidebar (some areas need the admin password).

---

## Typical day-to-day tasks

**Register a new learner**

Go to **Add Student**, fill in the form, leave **New Admission?** checked if they are joining now, then save.

**Record a payment**

Use **Payment Management → Add payment**, or upload a bank statement and match lines to students.

**See who still owes fees**

Check **Dashboard → Outstanding Balances**, or open **View Students** for a grade.

**End a term**

Set dates in **School Calendar**, then rely on the Dashboard automation or use **Run term actions now** after the closing date.

**Remove a student record**

In **Manage Students**, choose the learner, click **Delete Student Record**, pick a reason, confirm, then enter the admin password to schedule deletion. The record is removed permanently after the grace period (25 days by default).

---

## Where data is stored

Everything is in `school.db` in the project folder. Back up this file regularly. It holds student profiles, payments, fees, term billing, calendar settings, and pending drafts waiting for approval.

---

## Tips

- **Bulk import** is for bringing in names from an old list; it does not mark everyone as a new admission.
- **Pending reviews** (sidebar badges) let you save student edits, payments, or expenses and approve them later with a password.
- If term dates are missing, admission and exit counts on the Dashboard use the school year label instead.

---

## Need more detail?

Read comments in `app.py`, `utils.py`, `school_calendar.py`, and `database.py`. For automated checks, see the `tests/` folder.
