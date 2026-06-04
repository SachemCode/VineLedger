# Manual QA: Save for later (Pending Reviews)

Run this checklist before a real bulk import or term rollover. Use a **copy** of `school.db` when testing on production-like data.

## Setup

```bash
cd /path/to/school_system
pip install -r requirements.txt
cp school.db school.db.qa-backup   # optional safety copy
streamlit run app.py
```

Open the URL shown (usually http://localhost:8501). Default admin password for **Save now** / **Apply** is in app settings (see README).

## Per feature (clubs, grade contacts, balances)

Repeat for **Manage clubs**, **Manage grade**, and **Manage balance** under **Manage Students**.

| Step | Check |
|------|--------|
| Open the tab | Page loads; CSV template download works |
| Import or bulk edit | Preview/summary matches your spreadsheet |
| **Save for later** | Success message; pending count increases if shown |
| Hard refresh (F5) or restart Streamlit | Draft still appears under **Pending Reviews** |
| **Pending Reviews** → expand draft | Summary matches what you queued |
| **Apply** with admin password | Database updates (club membership, contacts, or balance) |
| After apply | Draft removed from list; pending count drops |

## Feature-specific notes

- **Manage clubs**: Try both manual assign-by-club and CSV import paths.
- **Manage grade**: Confirm parent names/phones/DOB after apply.
- **Manage balance**: Sponsored learners should remain at **0** balance after apply.

## If something fails

Note the tab, action (Save for later vs Apply), and whether a refresh was involved. Automated coverage for DB round-trips lives in `tests/test_pending_reviews_persistence.py`.
