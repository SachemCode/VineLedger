# Utility Functions for VineLedger School Management System
#
# PDF bank-statement parsing, payment matching, receipt PDF generation, fee calculation,
# student persist/verify helpers, bulk import, and balance sync.
#
# When school_calendar has a current term, sync_student_fees_from_db() updates only
# that term's billing row and recomputes balance from the full ledger; otherwise it
# uses the legacy single-term formula (current fees minus total_paid).

import json
import os
import pdfplumber
import re
import tempfile
import time
import uuid
from pathlib import Path
from reportlab.pdfgen import canvas

# Learner codes on receipts and in the DB use this prefix (see get_next_student_code).
STUDENT_CODE_PREFIX = "VINE"


def display_student_code(raw):
    """
    Normalize student code for display (receipts, filenames): VINE + 4 digits for legacy numeric-only codes.
    Codes that already start with VINE (any case) are uppercased; other non-numeric codes pass through unchanged.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    su = s.upper().replace(" ", "")
    if su.startswith(STUDENT_CODE_PREFIX):
        return su
    if s.isdigit():
        return f"{STUDENT_CODE_PREFIX}{int(s):04d}"
    return s


def receipt_pdf_cache_dir():
    """Directory for generated receipt PDFs (OS temp, not the project folder)."""
    d = Path(tempfile.gettempdir()) / "vineledger_receipts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_receipt_pdf_path(prefix="receipt"):
    """
    Return a unique absolute path for a new receipt PDF under the temp cache.
    ``prefix`` is sanitized for the filename stem (debugging); uniqueness is from a UUID.
    """
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(prefix).strip())[:80] or "receipt"
    p = receipt_pdf_cache_dir() / f"{safe}_{uuid.uuid4().hex}.pdf"
    return str(p)


def sweep_stale_receipt_cache_files(max_age_hours=None):
    """
    Delete ``*.pdf`` files in the receipt temp cache older than ``max_age_hours``.
    If ``max_age_hours`` is None, read ``VINELEDGER_RECEIPT_TTL_HOURS`` (default 48); if <= 0, skip.
    Returns the number of files removed.
    """
    if max_age_hours is None:
        try:
            max_age_hours = float(os.environ.get("VINELEDGER_RECEIPT_TTL_HOURS", "48"))
        except ValueError:
            max_age_hours = 48.0
    if max_age_hours <= 0:
        return 0
    d = receipt_pdf_cache_dir()
    cutoff = time.time() - max_age_hours * 3600.0
    removed = 0
    try:
        for f in d.iterdir():
            if f.suffix.lower() != ".pdf":
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


def normalize_payment_reference_key(ref):
    """Normalize M-Pesa / bank reference codes for comparison (case-insensitive, no spaces)."""
    if ref is None:
        return ""
    s = str(ref).strip().upper()
    s = re.sub(r"\s+", "", s)
    return s


def parse_mpesa_u_code_from_transaction_details(details_cell):
    """
    M-Pesa paybill line: ``254… UEK… 07… PAYER NAME`` — the code starting with U is the transaction id.
    Uses the first line of a multi-line narration cell.
    """
    if details_cell is None:
        return None
    first = str(details_cell).replace("\r", "\n").split("\n")[0].strip()
    if not first:
        return None
    tokens = first.split()
    for i, tok in enumerate(tokens):
        digits = "".join(ch for ch in tok if ch.isdigit())
        if len(digits) >= 12 and digits.startswith("254"):
            for j in range(i + 1, min(i + 6, len(tokens))):
                cand = tokens[j].strip().upper()
                if len(cand) >= 8 and cand.startswith("U") and re.fullmatch(r"U[A-Z0-9]+", cand):
                    return cand
    return None


def parse_currency_amount_loose(cell):
    """Parse a money cell from CSV/PDF tables; returns 0.0 when blank or unparsable."""
    if cell is None:
        return 0.0
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        try:
            return float(cell)
        except (TypeError, ValueError):
            return 0.0
    s = str(cell).strip()
    if not s or s.lower() in ("-", "—", "nan", "none"):
        return 0.0
    s = s.replace(" ", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _extract_bank_tabular_to_transactions(df):
    """
    Rows from a bank export / PDF table: treat **Debit (money out)** as outgoing and skip;
    use **Credit (money in)** as amount for incoming payments.
    """
    from datetime import datetime

    if df is None or df.empty:
        return []
    df = df.copy()
    df.columns = [str(c).replace("\n", " ").strip() for c in df.columns]
    colmap = {c: str(c).lower().replace("\n", " ") for c in df.columns}

    def _col(pred):
        for c, low in colmap.items():
            if pred(low):
                return c
        return None

    c_details = _col(lambda h: "transaction" in h and "detail" in h)
    if c_details is None:
        c_details = _col(lambda h: h in ("details", "description", "narration", "particulars"))
    c_credit = _col(lambda h: "credit" in h and "debit" not in h)
    if c_credit is None:
        c_credit = _col(lambda h: "credit" in h)
    c_debit = _col(lambda h: "debit" in h)

    if c_credit is None:
        return []

    if c_details is None and len(df.columns) > 0:
        c_details = df.columns[0]

    out = []
    for _, row in df.iterrows():
        debit_val = parse_currency_amount_loose(row.get(c_debit)) if c_debit else 0.0
        credit_val = parse_currency_amount_loose(row.get(c_credit))
        if debit_val > 0.01:
            continue
        if credit_val <= 0.01:
            continue
        details = row.get(c_details, "")
        ucode = parse_mpesa_u_code_from_transaction_details(details) or ""
        first_line = str(details).replace("\r", "\n").split("\n")[0].strip()
        phone = None
        name = ""
        parts = first_line.split()
        if parts:
            d0 = "".join(ch for ch in parts[0] if ch.isdigit())
            if len(d0) >= 12 and d0.startswith("254"):
                phone = d0[:12]
            if len(parts) >= 2:
                uix = None
                for j, p in enumerate(parts[1:], start=1):
                    pu = p.strip().upper()
                    if len(pu) >= 8 and pu.startswith("U") and re.fullmatch(r"U[A-Z0-9]+", pu):
                        uix = j
                        break
                if uix is not None and uix + 1 < len(parts):
                    rest = parts[uix + 1 :]
                    name = " ".join(rest).strip()
        desc = str(details).replace("\n", " ").strip()
        if len(desc) > 800:
            desc = desc[:800] + "…"
        out.append(
            {
                "phone": phone,
                "name": name,
                "amount": float(credit_val),
                "description": desc,
                "mpesa_u_code": ucode,
                "date": datetime.now().strftime("%Y-%m-%d"),
            }
        )
    return out


def _legacy_extract_transactions_from_pdf_pages(pdf_file):
    """Line-based PDF text (older statements); skips lines mentioning Debit; prefers Credit."""
    import concurrent.futures
    from datetime import datetime

    PATTERNS = {
        "phone_name_amount": re.compile(
            r"^(254\d{9})\s+(.+?)\s+\d{1,3}[.,]\d{3}[.,]\d{2}\s+\d{1,3}[.,]\d{3}[.,]\d{2}"
        ),
        "name_amount": re.compile(r"([A-Z\s]+[A-Z]+)\s+\d{1,3}[.,]\d{3}[.,]\d{2}"),
        "amount_only": re.compile(r"(\d{1,3}[.,]\d{3}[.,]\d{2})"),
        "credit_check": re.compile(r"Credit", re.IGNORECASE),
        "debit_check": re.compile(r"Debit", re.IGNORECASE),
    }

    def process_page(page_text):
        page_transactions = []
        if not page_text:
            return page_transactions
        for line in page_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if PATTERNS["debit_check"].search(line):
                continue
            if not PATTERNS["credit_check"].search(line):
                continue
            ucode = parse_mpesa_u_code_from_transaction_details(line) or ""
            match = PATTERNS["phone_name_amount"].search(line)
            if match:
                phone = match.group(1)
                name = match.group(2).strip()
                amount_match = PATTERNS["amount_only"].search(line)
                if amount_match:
                    amount = float(amount_match.group(1).replace(",", ""))
                    page_transactions.append(
                        {
                            "phone": phone,
                            "name": name,
                            "amount": amount,
                            "description": f"{phone} {name}",
                            "mpesa_u_code": ucode,
                            "date": datetime.now().strftime("%Y-%m-%d"),
                        }
                    )
                continue
            match = PATTERNS["name_amount"].search(line)
            if match:
                name = match.group(1).strip()
                amount_match = PATTERNS["amount_only"].search(line)
                if amount_match:
                    amount = float(amount_match.group(1).replace(",", ""))
                    page_transactions.append(
                        {
                            "phone": None,
                            "name": name,
                            "amount": amount,
                            "description": name,
                            "mpesa_u_code": ucode,
                            "date": datetime.now().strftime("%Y-%m-%d"),
                        }
                    )
        return page_transactions

    all_transactions = []
    try:
        with pdfplumber.open(pdf_file) as pdf:
            pages = pdf.pages
            total_pages = len(pages)
            if total_pages <= 3:
                for page in pages:
                    text = page.extract_text()
                    if text:
                        all_transactions.extend(process_page(text))
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    page_texts = []
                    for page in pages:
                        text = page.extract_text()
                        page_texts.append(text)
                    future_to_page = {executor.submit(process_page, text): i for i, text in enumerate(page_texts)}
                    for future in concurrent.futures.as_completed(future_to_page):
                        try:
                            all_transactions.extend(future.result())
                        except Exception as e:
                            print(f"Error processing page: {e}")
        return all_transactions
    except Exception as e:
        print(f"Error extracting transactions: {e}")
        return []


def _extract_transactions_pdf(pdf_buf):
    """Try structured tables first (M-Pesa / columnar statements), then legacy text."""
    import pandas as pd

    extracted = []
    try:
        pdf_buf.seek(0)
    except Exception:
        pass
    try:
        with pdfplumber.open(pdf_buf) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    if not table or len(table) < 2:
                        continue
                    hdr = [str(x or "").strip() for x in table[0]]
                    if not any(hdr):
                        continue
                    body = []
                    for r in table[1:]:
                        if not r:
                            continue
                        body.append([("" if c is None else str(c)) for c in r])
                    if not body:
                        continue
                    ncols = max(len(hdr), max(len(x) for x in body))
                    hdr = (hdr + [""] * ncols)[:ncols]
                    norm_rows = []
                    for r in body:
                        r = (r + [""] * ncols)[:ncols]
                        norm_rows.append(r)
                    tdf = pd.DataFrame(norm_rows, columns=hdr)
                    extracted.extend(_extract_bank_tabular_to_transactions(tdf))
    except Exception as e:
        print(f"Error extracting PDF tables: {e}")

    if extracted:
        return extracted

    try:
        pdf_buf.seek(0)
    except Exception:
        pass
    return _legacy_extract_transactions_from_pdf_pages(pdf_buf)


def extract_transactions(file):
    """
    Incoming payments from **PDF**, **CSV**, or **Excel** bank exports.

    Ignores outgoing rows when the **Debit** column has an amount. Parses the M-Pesa-style
    **U…** transaction code from the narration (first line) for matching to recorded payments.
    """
    import io

    name = (getattr(file, "name", None) or "").lower()

    try:
        if hasattr(file, "seek"):
            file.seek(0)
    except Exception:
        pass

    if name.endswith(".csv"):
        import pandas as pd

        try:
            df = pd.read_csv(file, dtype=str, encoding_errors="replace", on_bad_lines="skip")
        except TypeError:
            df = pd.read_csv(file, dtype=str, encoding_errors="replace")
        return _extract_bank_tabular_to_transactions(df)

    if name.endswith(".xlsx"):
        import pandas as pd

        df = pd.read_excel(file, dtype=str, engine="openpyxl")
        return _extract_bank_tabular_to_transactions(df)

    if name.endswith(".xls"):
        import pandas as pd

        df = pd.read_excel(file, dtype=str)
        return _extract_bank_tabular_to_transactions(df)

    try:
        if hasattr(file, "seek"):
            file.seek(0)
    except Exception:
        pass
    raw = file.read() if hasattr(file, "read") else b""
    buf = io.BytesIO(raw)
    return _extract_transactions_pdf(buf)


def normalize_kenya_msisdn(phone_str):
    """
    Canonical Kenyan mobile for storage and matching: 12 digits starting with 254 (no '+').
    Accepts +254…, 254… (as in bank PDFs), or 9 digits starting with 7 or 1.

    Local numbers starting with 0 must be exactly 10 digits (including the leading 0), e.g. 0712345678
    or 0135448776 (same +254 national format as 07… / 01…).

    If a value starts with 0 but is not exactly 10 digits, it is not converted to 254… (returns digits only).
    Non‑Kenya numbers: returns digits-only strip when no 254 / valid 0-local / 9-digit mobile rule applies.
    """
    import re

    if phone_str is None:
        return ""
    s = str(phone_str).strip()
    if not s or s.lower() == "nan":
        return ""
    s = re.sub(r"[\s\-\(\)\.\u00a0']", "", s)
    if s.startswith("+"):
        s = s[1:]
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    # International 2547… or 2541… (12 digits: 254 + 9-digit national subscriber number)
    if digits.startswith("254") and len(digits) >= 12 and digits[3] in ("7", "1"):
        return digits[:12]
    if digits.startswith("254") and len(digits) == 12:
        return digits
    # Local 0XXXXXXXXX — must be exactly 10 digits including the leading 0 (e.g. 071… or 01…)
    if digits.startswith("0"):
        if len(digits) == 10 and digits[1] in ("7", "1"):
            return "254" + digits[1:10]
        return digits
    # Nine digits starting with 7 or 1 (no country, no leading 0)
    if len(digits) == 9 and digits[0] in ("7", "1"):
        return "254" + digits
    return digits


def bank_description_contains_msisdn(description, canonical_254):
    """
    True if a bank/PDF line references the same handset as canonical_254 (254…12 digits).
    Handles +254…, spaces, and local 07… form. Bank lines in this project use 254… without +.
    """
    import re

    if not description or not canonical_254 or len(canonical_254) < 12:
        return False
    tail = canonical_254[-9:]
    desc = str(description)
    candidates = (
        canonical_254,
        "+" + canonical_254,
        "0" + tail,
        "+" + canonical_254[:3] + " " + canonical_254[3:6] + " " + canonical_254[6:9] + " " + canonical_254[9:12],
    )
    for c in candidates:
        if c and c in desc:
            return True
    compact = re.sub(r"\s+", "", desc)
    for c in (canonical_254, "+" + canonical_254, "0" + tail):
        if c and c in compact:
            return True
    digits_only = re.sub(r"\D", "", desc)
    if canonical_254 in digits_only:
        return True
    if ("0" + tail) in digits_only:
        return True
    return False


def load_payment_transaction_id_hints(conn):
    """
    Map normalized payment reference (e.g. M-Pesa **U…** code) to student id(s) that already
    have that value on a `payments.transaction_id` row — boosts bank-statement matching when
    staff recorded **Add payment** with the same code first.
    """
    from collections import defaultdict

    m = defaultdict(set)
    for sid, tid in conn.execute(
        "SELECT student_id, transaction_id FROM payments WHERE transaction_id IS NOT NULL AND TRIM(transaction_id) != ''"
    ).fetchall():
        k = normalize_payment_reference_key(tid)
        if len(k) >= 6:
            m[k].add(int(sid))
    return dict(m)


def match_payment(tx, students, payment_hints=None):
    best = None
    best_score = 0

    for _, s in students.iterrows():
        score = 0
        desc = str(tx.get("description") or "")
        if payment_hints:
            u = normalize_payment_reference_key(tx.get("mpesa_u_code") or "")
            if u and u in payment_hints and int(s["id"]) in payment_hints[u]:
                score += 96
        raw_phone = str(s.get("parent_phone") or "").strip()
        pnorm = normalize_kenya_msisdn(raw_phone)
        tx_phone = normalize_kenya_msisdn(tx.get("phone"))

        if s["student_code"] in desc:
            score += 100

        if pnorm and tx_phone and tx_phone == pnorm:
            score += 78
        elif pnorm and bank_description_contains_msisdn(desc, pnorm):
            score += 70
        elif raw_phone and raw_phone in desc:
            score += 70

        raw_phone2 = str(s.get("parent2_phone") or "").strip()
        pnorm2 = normalize_kenya_msisdn(raw_phone2)
        if pnorm2 and tx_phone and tx_phone == pnorm2:
            score += 78
        elif pnorm2 and bank_description_contains_msisdn(desc, pnorm2):
            score += 70
        elif raw_phone2 and raw_phone2 in desc:
            score += 70

        if s["parent_name"].lower() in desc.lower():
            score += 40
        p2name = str(s.get("parent2_name") or "").strip()
        if p2name and p2name.lower() in desc.lower():
            score += 40

        if score > best_score:
            best_score = score
            best = s

    return best, best_score

def generate_receipt(student, amount, receipt_number=None, payment=None):
    import html
    import datetime

    from reportlab.lib.pagesizes import letter
    from reportlab.lib.colors import black, blue, grey
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    if receipt_number is None:
        receipt_number = f"RCP{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"

    filename = new_receipt_pdf_path(f"receipt_plain_{receipt_number}")
    doc = SimpleDocTemplate(filename, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []

    if payment is not None:
        _tid = payment_display_reference(payment)
        if _tid:
            tid_style = ParagraphStyle(
                name="receipt_tid",
                parent=styles["Normal"],
                fontSize=7,
                leading=8,
                textColor=grey,
                alignment=TA_RIGHT,
            )
            story.append(Paragraph(html.escape(f"Ref: {_tid[:52]}"), tid_style))
            story.append(Spacer(1, 4))

    # School Header
    title_style = styles["Title"]
    title_style.textColor = blue
    title_style.alignment = 1  # Center

    story.append(Paragraph("SCHOOL FEE RECEIPT", title_style))
    story.append(Spacer(1, 12))

    # Receipt details
    normal_style = styles["Normal"]

    _pm_line = "Bank Transfer"
    if payment is not None:
        _tl = _receipt_transaction_type_label(payment)
        if _tl:
            _pm_line = _tl
        else:
            _raw = _receipt_safe_str(_row_get(payment, "payment_method", ""), "").strip()
            if _raw:
                _pm_line = html.escape(_raw[:80])

    receipt_info = f"""
    <b>Receipt Number:</b> {receipt_number}<br/>
    <b>Date:</b> {datetime.datetime.now().strftime('%B %d, %Y')}<br/>
    <b>Payment Method:</b> {_pm_line}
    """
    story.append(Paragraph(receipt_info, normal_style))
    story.append(Spacer(1, 20))
    
    # Student Information
    student_info = f"""
    <b>STUDENT INFORMATION</b><br/>
    <b>Name:</b> {student['name']} ({html.escape(display_student_code(student.get('student_code')))})<br/>
    <b>Grade:</b> {student['grade']}<br/>
    <b>Parent/Guardian:</b> {_receipt_single_guardian_name(student)}<br/>
    <b>Phone:</b> {_receipt_single_guardian_phone(student)}
    """
    story.append(Paragraph(student_info, normal_style))
    story.append(Spacer(1, 20))
    
    _pay_desc_line = ""
    if payment is not None:
        vals = _receipt_field_values(student, payment, receipt_number)
        ld = str(vals.get("line_description") or "").strip()
        if ld:
            _pay_desc_line = f"<b>Payment description:</b> {html.escape(ld)}<br/>"

    payment_info = f"""
    <b>PAYMENT DETAILS</b><br/>
    {_pay_desc_line}<b>Amount Paid:</b> KSH {amount:,.0f}<br/>
    <b>Previous Balance:</b> KSH {student['balance'] + amount:,.0f}<br/>
    <b>Outstanding Balance:</b> KSH {student['balance']:,.0f}
    """
    story.append(Paragraph(payment_info, normal_style))
    story.append(Spacer(1, 20))
    
    # Additional Services
    services = []
    if student.get('has_transport'):
        services.append(f"Transport: {student.get('transport_route', 'N/A')}")
    if student.get('extra_classes'):
        services.append(f"Extra Classes: {student['extra_classes']}")
    
    if services:
        services_info = f"<b>ADDITIONAL SERVICES</b><br/>" + "<br/>".join(services)
        story.append(Paragraph(services_info, normal_style))
        story.append(Spacer(1, 20))
    
    # Footer
    footer = f"""
    <i>Thank you for your payment!</i><br/>
    <b>Total Paid to Date:</b> KSH {student['total_paid']:,.0f}
    """
    story.append(Paragraph(footer, normal_style))
    
    doc.build(story)
    return filename


def generate_receipts_combined_plain_pdf(student, payments_list):
    """
    Build one letter-size PDF of plain (ReportLab) receipts, placing **two receipts per page**
    when there is more than one payment. A single payment returns the same one-page file as
    ``generate_receipt`` (no empty half-page).
    """
    import os

    import fitz  # PyMuPDF

    if not payments_list:
        raise ValueError("payments_list is empty")

    tmp_paths = []
    for p in payments_list:
        amt = _receipt_safe_float(_row_get(p, "amount", 0))
        pid = int(_receipt_safe_float(_row_get(p, "id", 0), 0))
        rn = f"RCP{pid:06d}"
        tmp_paths.append(generate_receipt(student, amt, rn, payment=p))

    if len(tmp_paths) == 1:
        return tmp_paths[0]

    sc = (
        display_student_code(_receipt_safe_str(_row_get(student, "student_code", "0000"), "0000"))
        or "0000"
    ).replace("/", "-")
    out_name = new_receipt_pdf_path(f"receipts_combined_{sc}_{len(payments_list)}")

    w, h = 612.0, 792.0
    half = h / 2.0
    try:
        doc = fitz.open()
        for i in range(0, len(tmp_paths), 2):
            page = doc.new_page(width=w, height=h)
            s1 = fitz.open(tmp_paths[i])
            page.show_pdf_page(fitz.Rect(0, 0, w, half), s1, 0)
            s1.close()
            if i + 1 < len(tmp_paths):
                s2 = fitz.open(tmp_paths[i + 1])
                page.show_pdf_page(fitz.Rect(0, half, w, h), s2, 0)
                s2.close()
        doc.save(out_name)
        doc.close()
        return out_name
    finally:
        for pth in tmp_paths:
            try:
                if os.path.isfile(pth):
                    os.remove(pth)
            except OSError:
                pass


def generate_bulk_plain_receipts_pdf(student_payment_pairs):
    """
    Build one letter PDF of plain (ReportLab) receipts for many (student, payment) pairs.
    Two receipts per page when possible; order follows ``student_payment_pairs``.
    """
    import os

    import fitz  # PyMuPDF

    if not student_payment_pairs:
        raise ValueError("student_payment_pairs is empty")

    tmp_paths = []
    for student, payment in student_payment_pairs:
        amt = _receipt_safe_float(_row_get(payment, "amount", 0))
        pid = int(_receipt_safe_float(_row_get(payment, "id", 0), 0))
        rn = f"RCP{pid:06d}"
        tmp_paths.append(generate_receipt(student, amt, rn, payment=payment))

    if len(tmp_paths) == 1:
        return tmp_paths[0]

    out_name = new_receipt_pdf_path(f"receipts_bulk_{len(tmp_paths)}")
    w, h = 612.0, 792.0
    half = h / 2.0
    try:
        doc = fitz.open()
        for i in range(0, len(tmp_paths), 2):
            page = doc.new_page(width=w, height=h)
            s1 = fitz.open(tmp_paths[i])
            page.show_pdf_page(fitz.Rect(0, 0, w, half), s1, 0)
            s1.close()
            if i + 1 < len(tmp_paths):
                s2 = fitz.open(tmp_paths[i + 1])
                page.show_pdf_page(fitz.Rect(0, half, w, h), s2, 0)
                s2.close()
        doc.save(out_name)
        doc.close()
        return out_name
    finally:
        for pth in tmp_paths:
            try:
                if os.path.isfile(pth):
                    os.remove(pth)
            except OSError:
                pass


def _row_get(row, key, default=None):
    if row is None:
        return default
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _receipt_safe_float(val, default=0.0):
    import pandas as pd

    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _receipt_safe_str(val, default=""):
    import pandas as pd

    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    return str(val).strip() or default


def payment_display_reference(payment) -> str:
    """
    One user-facing payment reference for receipts and history.

    ``transaction_id`` holds the bank / M-Pesa / form reference when provided; for newer rows
    ``internal_payment_id`` is kept identical. Legacy rows may differ; we prefer the bank ref.
    """
    tx = _receipt_safe_str(_row_get(payment, "transaction_id", ""), "").strip()
    ip = _receipt_safe_str(_row_get(payment, "internal_payment_id", ""), "").strip()
    if tx and ip and tx != ip:
        return tx
    return tx or ip or ""


def _receipt_single_guardian_name(student):
    """One parent/guardian line for receipts: primary name, else second."""
    p1 = _receipt_safe_str(_row_get(student, "parent_name", ""), "")
    p2 = _receipt_safe_str(_row_get(student, "parent2_name", ""), "")
    return p1 if p1 else p2


def _receipt_single_guardian_phone(student):
    """One phone line for receipts: primary, else second (normalized when possible)."""
    ph1 = _receipt_safe_str(_row_get(student, "parent_phone", ""), "")
    ph2_raw = _row_get(student, "parent2_phone", "")
    ph2 = normalize_kenya_msisdn(ph2_raw) or _receipt_safe_str(ph2_raw, "")
    if ph1:
        return ph1
    return ph2


def _receipt_layout_path():
    import os

    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "receipt_layout.json")


def _load_receipt_layout():
    """Optional JSON next to this module: maps named fields to positions on the scaled template image."""
    import json
    import os

    path = _receipt_layout_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def _receipt_transaction_type_label(payment):
    """
    Short label for receipt text in brackets, e.g. Cash, Mobile transfer, Bank transfer.
    Returns "" when unknown or missing.
    """
    if payment is None:
        return ""
    raw = _receipt_safe_str(_row_get(payment, "payment_method", ""), "").strip().lower()
    if not raw:
        return ""
    compact = raw.replace(" ", "").replace("-", "")
    if "cash" in raw or raw in ("petty cash",):
        return "Cash"
    if "mpesa" in compact or "mobilemoney" in compact:
        return "Mobile transfer"
    if raw in ("mobile transfer", "mobile money", "m-pesa"):
        return "Mobile transfer"
    if raw in ("kcb",):
        return "KCB"
    if raw in ("equity",):
        return "Equity"
    if raw in ("family bank", "familybank"):
        return "Family Bank"
    if "bank" in raw or raw in ("eft", "wire", "rtgs", "pesalink"):
        return "Bank transfer"
    return _receipt_safe_str(_row_get(payment, "payment_method", ""), "").strip()[:48]


def _receipt_field_values(student, payment, receipt_number):
    """Strings keyed for receipt_layout.json `fields` entries."""
    amt = _receipt_safe_float(_row_get(payment, "amount", 0))
    bal = _receipt_safe_float(_row_get(student, "balance", 0))
    tot = _receipt_safe_float(_row_get(student, "total_paid", 0))
    prev_bal = bal + amt
    pdate = _receipt_safe_str(_row_get(payment, "payment_date", ""))[:10]
    purpose = _receipt_safe_str(_row_get(payment, "purpose", ""), "School Fees")
    ref = payment_display_reference(payment)
    txid = ref if ref else "N/A"
    sname = _receipt_safe_str(_row_get(student, "name", ""), "Student")
    scode = display_student_code(_receipt_safe_str(_row_get(student, "student_code", ""), ""))
    grade = _receipt_safe_str(_row_get(student, "grade", ""), "")
    parent = _receipt_single_guardian_name(student)
    phone = _receipt_single_guardian_phone(student)
    desc = _receipt_safe_str(_row_get(payment, "description", ""), "")
    amt_ksh = f"KSH {amt:,.0f}"
    line_desc = purpose if not desc else f"{purpose} — {desc}"
    ptype = _receipt_transaction_type_label(payment)
    if ptype:
        line_desc = f"{line_desc} ({ptype})"
    if len(line_desc) > 120:
        line_desc = line_desc[:117] + "..."

    l2_amt = _receipt_safe_float(_row_get(payment, "receipt_line_2_amount", None), 0.0)
    l2_desc = _receipt_safe_str(_row_get(payment, "receipt_line_2_description", ""), "")
    l2_purpose = _receipt_safe_str(_row_get(payment, "receipt_line_2_purpose", ""), "")
    line2_desc = ""
    line2_qty = ""
    line2_amt_s = ""
    line2_tot_s = ""
    combined = amt
    if l2_amt > 0:
        combined = amt + l2_amt
        line2_qty = "1"
        if l2_desc:
            line2_desc = l2_desc[:120] if len(l2_desc) <= 120 else l2_desc[:117] + "..."
        elif l2_purpose:
            line2_desc = l2_purpose[:120] if len(l2_purpose) <= 120 else l2_purpose[:117] + "..."
        else:
            line2_desc = "Item 2"
        line2_desc = f"{line2_desc} ({ptype})" if ptype and line2_desc else line2_desc
        if len(line2_desc) > 120:
            line2_desc = line2_desc[:117] + "..."
        line2_amt_s = f"KSH {l2_amt:,.0f}"
        line2_tot_s = line2_amt_s

    total_paid_today_ksh = f"KSH {combined:,.0f}"
    balance_remaining_ksh = f"KSH {bal:,.0f}"
    internal_pay_id = ref if ref else ""

    return {
        "receipt_number": receipt_number,
        "receipt_title": f"RECEIPT {receipt_number}",
        "student_name": sname,
        "student_code": scode,
        "grade": grade,
        "parent_name": parent,
        "parent_phone": phone,
        "purpose": purpose,
        "amount": amt_ksh,
        "amount_numeric": f"{amt:,.0f}",
        "payment_date": pdate,
        "transaction_id": txid,
        "internal_payment_id": internal_pay_id,
        "description": desc,
        # Invoice-style table (line 1 = this payment; line 2 optional via receipt_line_2_* on payment)
        "line_quantity": "1",
        "line_description": line_desc,
        "line_amount": amt_ksh,
        "line_total": amt_ksh,
        "line2_quantity": line2_qty,
        "line2_description": line2_desc,
        "line2_amount": line2_amt_s,
        "line2_total": line2_tot_s,
        "total_paid_today": total_paid_today_ksh,
        "school_balance_remaining": balance_remaining_ksh,
        "invoice_subtotal": total_paid_today_ksh,
        "invoice_total": balance_remaining_ksh,
        "balance_outstanding": f"{bal:,.0f}",
        "balance_outstanding_ksh": f"KSH {bal:,.0f}",
        "balance_before_payment": f"{prev_bal:,.0f}",
        "balance_before_ksh": f"KSH {prev_bal:,.0f}",
        "total_paid_system": f"{tot:,.0f}",
        "total_paid_ksh": f"KSH {tot:,.0f}",
    }


def _canvas_xy_from_fractions(x0, y_bottom, tw, th, x_frac, y_frac, origin):
    """origin 'bottom': y_frac is from bottom of image upward. 'top': y_frac is from top downward."""
    px = x0 + float(x_frac) * tw
    if origin == "top":
        py = y_bottom + th - float(y_frac) * th
    else:
        py = y_bottom + float(y_frac) * th
    return px, py


def _canvas_draw_text_aligned(c, text, px, py, font, size, align, fill_color):
    c.setFont(font, size)
    w = c.stringWidth(text, font, size)
    if align == "right":
        px -= w
    elif align == "center":
        px -= w / 2
    c.setFillColor(fill_color)
    c.drawString(px, py, text)


def _canvas_draw_text_with_halo(c, text, px, py, font, size, align, fill_color, halo_color):
    """Thin outline so text stays readable on busy artwork without a white panel."""
    c.setFont(font, size)
    w = c.stringWidth(text, font, size)
    if align == "right":
        px -= w
    elif align == "center":
        px -= w / 2
    for dx, dy in ((-0.6, 0), (0.6, 0), (0, -0.6), (0, 0.6), (-0.45, -0.45), (0.45, 0.45), (-0.45, 0.45), (0.45, -0.45)):
        c.setFillColor(halo_color)
        c.drawString(px + dx, py + dy, text)
    c.setFillColor(fill_color)
    c.drawString(px, py, text)


def _pdf_template_first_page_to_png(pdf_path):
    """Rasterize first page of a PDF template to a temporary PNG (requires PyMuPDF)."""
    import os
    import tempfile

    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None
    tmp_path = None
    doc = None
    try:
        doc = fitz.open(pdf_path)
        if doc.page_count < 1:
            return None
        page = doc.load_page(0)
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
        tmp_fd, tmp_path = tempfile.mkstemp(suffix="_receipt_tpl.png")
        os.close(tmp_fd)
        pix.save(tmp_path)
        return tmp_path
    except Exception:
        if tmp_path:
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
        return None
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


def _draw_filled_receipt_panel(
    c,
    ir,
    iw,
    ih,
    student,
    payment,
    slot_bottom,
    slot_top,
    page_w,
    margin,
):
    """
    Draw one template image + overlay into a vertical band [slot_bottom, slot_top] (PDF coords, y up).
    """
    from reportlab.lib import colors

    max_w = page_w - 2 * margin
    slot_h = float(slot_top) - float(slot_bottom)
    if slot_h <= 20 or max_w <= 20:
        return
    scale = min(max_w / float(iw), slot_h / float(ih))
    tw, th = iw * scale, ih * scale
    x0 = (page_w - tw) / 2
    y_bottom = float(slot_bottom) + max(0.0, (slot_h - th) / 2.0)

    c.drawImage(ir, x0, y_bottom, width=tw, height=th, preserveAspectRatio=True, mask="auto")

    receipt_number = f"RCP{int(_row_get(payment, 'id', 0) or 0):06d}"
    values = _receipt_field_values(student, payment, receipt_number)
    layout = _load_receipt_layout()
    fields = {}
    if layout and isinstance(layout.get("fields"), dict):
        fields = layout["fields"]
    global_origin = layout.get("coordinate_origin", "bottom") if layout else "bottom"
    if global_origin not in ("bottom", "top"):
        global_origin = "bottom"

    ink = colors.HexColor("#0f172a")
    halo = colors.white

    if fields:
        for key, spec in fields.items():
            if not isinstance(spec, dict):
                continue
            raw = values.get(key)
            if raw is None:
                continue
            text = str(raw).strip()
            if not text:
                continue
            if len(text) > 160:
                text = text[:157] + "..."
            try:
                x_frac = float(spec.get("x", 0))
                y_frac = float(spec.get("y", 0))
            except (TypeError, ValueError):
                continue
            origin = spec.get("origin", global_origin)
            if origin not in ("bottom", "top"):
                origin = global_origin
            px, py = _canvas_xy_from_fractions(x0, y_bottom, tw, th, x_frac, y_frac, origin)
            font = _receipt_safe_str(spec.get("font"), "Helvetica") or "Helvetica"
            try:
                fs = float(spec.get("size", 10))
            except (TypeError, ValueError):
                fs = 10.0
            align = _receipt_safe_str(spec.get("align"), "left") or "left"
            if align not in ("left", "right", "center"):
                align = "left"
            ch = spec.get("color", "#0f172a")
            try:
                fill_c = colors.HexColor(str(ch)) if ch else ink
            except Exception:
                fill_c = ink
            use_halo = bool(spec.get("halo", True))
            if use_halo:
                _canvas_draw_text_with_halo(c, text, px, py, font, fs, align, fill_c, halo)
            else:
                _canvas_draw_text_aligned(c, text, px, py, font, fs, align, fill_c)
    else:
        lines = [
            ("Helvetica-Bold", 11, values["receipt_title"]),
            ("Helvetica", 9, f"{values['student_name']}   ·   Code {values['student_code']}"),
            ("Helvetica", 9, f"Parent / guardian: {values['parent_name']}    Grade: {values['grade']}"),
            ("Helvetica", 9, f"Tel: {values['parent_phone']}   ·   {values['line_description'][:100]}"),
            ("Helvetica-Bold", 10, f"Total paid today:  {values['total_paid_today']}"),
            ("Helvetica", 9, f"Payment date: {values['payment_date']}     Ref: {values['transaction_id']}"),
            (
                "Helvetica-Bold",
                10,
                f"School balance remaining:  {values['school_balance_remaining']}",
            ),
            ("Helvetica-Oblique", 8.5, f"Total paid to date (system):  {values['total_paid_ksh']}"),
        ]
        left = x0 + max(8, tw * 0.04)
        ty = y_bottom + max(th * 0.06, 10)
        for font, fs, text in lines:
            t = text if len(text) <= 105 else text[:102] + "..."
            _canvas_draw_text_with_halo(c, t, left, ty, font, fs, "left", ink, halo)
            ty += fs + 6
            if ty > y_bottom + th - 8:
                break

    _tid = payment_display_reference(payment)
    if _tid and not (fields and "internal_payment_id" in fields):
        fs_tid = 6.5
        ry = y_bottom + th - 8.0
        rx = x0 + tw - 4.0
        tshow = _tid[:44] + ("…" if len(_tid) > 44 else "")
        _canvas_draw_text_with_halo(c, f"Ref: {tshow}", rx, ry, "Helvetica", fs_tid, "right", ink, halo)


def _build_filled_receipt_pdf_multi(bg_image_path, output_pdf, rows):
    """
    rows: list of (student, payment) in print order (e.g. newest first).
    Up to **two** receipts per letter page (stacked); additional pairs continue on new pages.
    """
    from reportlab.pdfgen import canvas as pdfcanvas
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.lib.utils import ImageReader

    if not rows:
        raise ValueError("rows is empty")

    page_w, page_h = letter
    margin = 0.38 * inch
    gutter = 0.14 * inch
    c = pdfcanvas.Canvas(output_pdf, pagesize=letter)
    ir = ImageReader(bg_image_path)
    iw, ih = ir.getSize()

    i = 0
    while i < len(rows):
        chunk = rows[i : i + 2]
        if len(chunk) == 1:
            slot_bottom = margin
            slot_top = page_h - margin
            _draw_filled_receipt_panel(
                c, ir, iw, ih, chunk[0][0], chunk[0][1], slot_bottom, slot_top, page_w, margin
            )
        else:
            usable = page_h - 2 * margin - gutter
            h_each = usable / 2.0
            lower_bottom = margin
            lower_top = margin + h_each
            upper_bottom = margin + h_each + gutter
            upper_top = page_h - margin
            _draw_filled_receipt_panel(
                c, ir, iw, ih, chunk[0][0], chunk[0][1], upper_bottom, upper_top, page_w, margin
            )
            _draw_filled_receipt_panel(
                c, ir, iw, ih, chunk[1][0], chunk[1][1], lower_bottom, lower_top, page_w, margin
            )
        c.showPage()
        i += 2
    c.save()


def _build_filled_receipt_pdf_with_image_canvas(bg_image_path, output_pdf, student, payment, receipt_number):
    """Backward-compatible single-receipt entry (receipt_number ignored; derived from payment)."""
    _build_filled_receipt_pdf_multi(bg_image_path, output_pdf, [(student, payment)])


def create_filled_template(template_path, student, payment, template_type, extra_payment=None):
    """
    Build a printable PDF from the uploaded Canva (or other) receipt artwork.

    - **PNG / JPEG**: artwork is scaled to fit the page; text is drawn on top of the art.
    - **PDF**: first page is **rasterized** (PyMuPDF) when available, then the same overlay applies.
      If PyMuPDF is not installed, falls back to the built‑in ReportLab receipt (correct data, not your art).
    - **Optional ``receipt_layout.json``** (beside ``utils.py``): define ``fields`` with fractional ``x`` / ``y``
      positions so each value lands in your template blanks instead of the default bottom strip.
    - **extra_payment**: optional second payment for the **same student**; when set with a raster/image template,
      both receipts are drawn on **one letter page** (stacked, two per page) to save paper.

    Bank statement → match → balance updates happen elsewhere; this function only renders the receipt PDF.
    """
    import os

    sc = (
        display_student_code(_receipt_safe_str(_row_get(student, "student_code", "0000"), "0000"))
        or "0000"
    ).replace("/", "-")
    pid = int(_receipt_safe_float(_row_get(payment, "id", 0), 0))
    rows = [(student, payment)]
    if extra_payment is not None:
        pid2 = int(_receipt_safe_float(_row_get(extra_payment, "id", 0), 0))
        output_filename = new_receipt_pdf_path(f"filled_{sc}_{pid}_{pid2}")
        rows.append((student, extra_payment))
    else:
        output_filename = new_receipt_pdf_path(f"filled_{sc}_{pid}")
    tmp_png = None

    try:
        path_lower = str(template_path).lower()
        is_image = template_type.startswith("image") or path_lower.endswith(
            (".png", ".jpg", ".jpeg", ".webp")
        )
        bg_path = str(template_path)

        if not is_image and path_lower.endswith(".pdf"):
            tmp_png = _pdf_template_first_page_to_png(str(template_path))
            if tmp_png and os.path.isfile(tmp_png):
                bg_path = tmp_png
                is_image = True

        if is_image and os.path.isfile(bg_path):
            _build_filled_receipt_pdf_multi(bg_path, output_filename, rows)
            return output_filename

        # PDF but raster failed → data-only receipt (first payment only when dual fallback)
        _rn = f"RCP{int(_row_get(payment, 'id', 0) or 0):06d}"
        return generate_receipt(
            student,
            _receipt_safe_float(_row_get(payment, "amount", 0)),
            _rn,
            payment=payment,
        )

    except Exception:
        _rn = f"RCP{int(_row_get(payment, 'id', 0) or 0):06d}"
        return generate_receipt(
            student,
            _receipt_safe_float(_row_get(payment, "amount", 0)),
            _rn,
            payment=payment,
        )
    finally:
        if tmp_png:
            try:
                if os.path.isfile(tmp_png):
                    os.remove(tmp_png)
            except OSError:
                pass


def create_filled_templates_bulk(template_path, template_type, student_payment_pairs):
    """
    One PDF with many filled template receipts: **two per letter page** when the template is
    raster/image-based. ``student_payment_pairs`` is a list of ``(student, payment)`` with
    ``purpose`` already set on each payment row as needed.

    Falls back to ``generate_bulk_plain_receipts_pdf`` when the template cannot be rasterized.
    """
    import os

    rows = list(student_payment_pairs)
    if not rows:
        raise ValueError("No payments selected for receipts.")

    sc = (
        display_student_code(_receipt_safe_str(_row_get(rows[0][0], "student_code", "0000"), "0000"))
        or "0000"
    ).replace("/", "-")
    output_filename = new_receipt_pdf_path(f"filled_receipts_bulk_{sc}_{len(rows)}n")
    tmp_png = None

    try:
        path_lower = str(template_path).lower()
        is_image = template_type.startswith("image") or path_lower.endswith(
            (".png", ".jpg", ".jpeg", ".webp")
        )
        bg_path = str(template_path)

        if not is_image and path_lower.endswith(".pdf"):
            tmp_png = _pdf_template_first_page_to_png(str(template_path))
            if tmp_png and os.path.isfile(tmp_png):
                bg_path = tmp_png
                is_image = True

        if is_image and os.path.isfile(bg_path):
            _build_filled_receipt_pdf_multi(bg_path, output_filename, rows)
            return output_filename

        return generate_bulk_plain_receipts_pdf(rows)

    except Exception:
        return generate_bulk_plain_receipts_pdf(rows)
    finally:
        if tmp_png:
            try:
                if os.path.isfile(tmp_png):
                    os.remove(tmp_png)
            except OSError:
                pass


def interview_fee_amount_for_grade(grade):
    """
    One-time interview fee (KSH): 500 for Playgroup through Grade 6; 700 for Grade 7 and above.
    """
    import pandas as pd

    if grade is None or (isinstance(grade, float) and pd.isna(grade)):
        return 500.0
    s = str(grade).strip().lower()
    if s.startswith("grade "):
        rest = s[6:].strip()
        try:
            n = int(rest.split()[0])
            if n >= 7:
                return 700.0
        except (ValueError, IndexError):
            pass
    return 500.0


def calculate_student_fees(
    conn,
    grade,
    transport_route_id=None,
    co_curricular_ids=None,
    has_meal=False,
    include_admission=False,
    include_interview=False,
):
    """
    Calculate total expected fees for a student based on their selections.
    
    Args:
        conn: Database connection
        grade: Student's grade (e.g., "Grade 1", "PP1")
        transport_route_id: ID of selected transport route fee item (or None)
        co_curricular_ids: List of fee item IDs for co-curricular activities (or None/empty)
        has_meal: Boolean, whether student takes meals
        include_admission: If True, add one-time admission fee (KSH 1,000) from fee_structure (non-interview admission rows)
        include_interview: If True, add one-time interview fee (500 or 700 by grade)
    
    Returns:
        dict with:
            - fee_breakdown: list of (fee_name, fee_amount) tuples
            - mandatory_total: tuition + per-term mandatory (excludes admission)
            - optional_total: transport + co-curricular + meal
            - admission_total: one-time admission fee portion (0 if not included)
            - interview_total: one-time interview fee (0 if not included)
            - grand_total: total of all fees
    """
    import sqlite3
    import pandas as pd

    def _grade_ready_for_fees(g):
        if g is None or (isinstance(g, float) and pd.isna(g)):
            return False
        s = str(g).strip().lower()
        if not s or s in ("unassigned", "pending", "tbd", "none", "nan"):
            return False
        return True

    if not _grade_ready_for_fees(grade):
        return {
            "fee_breakdown": [],
            "mandatory_total": 0.0,
            "optional_total": 0.0,
            "admission_total": 0.0,
            "interview_total": 0.0,
            "grand_total": 0.0,
        }
    
    fee_breakdown = []
    mandatory_total = 0.0
    optional_total = 0.0
    admission_total = 0.0
    interview_total = 0.0
    
    # 1. Tuition fee based on grade
    c = conn.cursor()
    c.execute("SELECT id, fee_name, fee_amount FROM fee_structure WHERE fee_category='tuition' AND grade_applicable=?", (grade,))
    tuition = c.fetchone()
    if tuition:
        fee_breakdown.append((tuition[1], tuition[2]))
        mandatory_total += tuition[2]
    
    # 2. Mandatory per-term fees
    c.execute("SELECT fee_name, fee_amount FROM fee_structure WHERE fee_category='mandatory' AND is_optional=0")
    for row in c.fetchall():
        fee_breakdown.append((row[0], row[1]))
        mandatory_total += row[1]
    
    # 3. Transport fee (optional)
    if transport_route_id:
        c.execute("SELECT fee_name, fee_amount FROM fee_structure WHERE id=?", (transport_route_id,))
        transport = c.fetchone()
        if transport:
            fee_breakdown.append((f"Transport: {transport[0]}", transport[1]))
            optional_total += transport[1]
    
    # 4. Co-curricular activities (optional)
    if co_curricular_ids:
        for cc_id in co_curricular_ids:
            c.execute("SELECT fee_name, fee_amount FROM fee_structure WHERE id=?", (cc_id,))
            cc = c.fetchone()
            if cc:
                fee_breakdown.append((cc[0], cc[1]))
                optional_total += cc[1]
    
    # 5. Meal program (optional)
    if has_meal:
        c.execute("SELECT fee_name, fee_amount FROM fee_structure WHERE fee_category='meal' AND is_optional=1")
        meal = c.fetchone()
        if meal:
            fee_breakdown.append((meal[0], meal[1]))
            optional_total += meal[1]

    # 6. One-time admission fee (optional) — exclude interview rows in fee_structure; interview is grade-based below.
    if include_admission:
        c.execute(
            """SELECT fee_name, fee_amount FROM fee_structure
               WHERE fee_category='admission'
                 AND LOWER(TRIM(fee_name)) NOT LIKE '%interview%'
               ORDER BY fee_name"""
        )
        for row in c.fetchall():
            fee_breakdown.append((f"Admission: {row[0]}", row[1]))
            admission_total += row[1]
    if include_interview:
        iv = interview_fee_amount_for_grade(grade)
        fee_breakdown.append(("Admission: Interview fee", iv))
        interview_total += iv

    grand_total = mandatory_total + optional_total + admission_total + interview_total

    return {
        "fee_breakdown": fee_breakdown,
        "mandatory_total": mandatory_total,
        "optional_total": optional_total,
        "admission_total": admission_total,
        "interview_total": interview_total,
        "grand_total": grand_total,
    }


def student_is_sponsored(student_row):
    """True when the learner is fully sponsored (balance defaults to zero)."""
    return student_row_bool(student_row, "is_sponsored", False)


def student_row_bool(student, key, default=False):
    """Read a boolean-like field from a pandas Series or a student record dict."""
    import pandas as pd

    if student is None:
        return default
    if isinstance(student, dict):
        if key not in student:
            return default
        v = student.get(key)
    else:
        if key not in student.index:
            return default
        v = student[key]
    if pd.isna(v):
        return default
    if isinstance(v, bool):
        return v
    try:
        return bool(int(v))
    except (TypeError, ValueError):
        return bool(v)


def parse_co_curricular_ids(raw, conn=None, student_id=None):
    """Return fee_structure ids from JSON column, legacy plain names, or student_fee_items."""
    import json
    import pandas as pd

    ids = []
    if isinstance(raw, (list, tuple)):
        for x in raw:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                pass
    if ids:
        seen = set()
        return [i for i in ids if not (i in seen or seen.add(i))]

    if raw is not None and not (isinstance(raw, float) and pd.isna(raw)):
        s = str(raw).strip()
        if s:
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    for x in parsed:
                        try:
                            ids.append(int(x))
                        except (TypeError, ValueError):
                            pass
                elif isinstance(parsed, (int, float)):
                    ids.append(int(parsed))
            except (json.JSONDecodeError, TypeError, ValueError):
                if conn is not None:
                    row = conn.execute(
                        """SELECT id FROM fee_structure
                           WHERE fee_category='co_curricular' AND TRIM(fee_name)=? COLLATE NOCASE""",
                        (s,),
                    ).fetchone()
                    if row:
                        ids.append(int(row[0]))

    if not ids and conn is not None and student_id is not None:
        rows = conn.execute(
            """
            SELECT fee_item_id FROM student_fee_items
            WHERE student_id=? AND fee_item_id IN (
                SELECT id FROM fee_structure WHERE fee_category='co_curricular'
            )
            """,
            (int(student_id),),
        ).fetchall()
        ids = [int(r[0]) for r in rows]

    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def co_curricular_ids_to_json(ids):
    """Serialize club fee ids for students.co_curricular_activities."""
    import json

    clean = []
    for x in ids or []:
        try:
            clean.append(int(x))
        except (TypeError, ValueError):
            continue
    return json.dumps(clean) if clean else None


def save_student_co_curricular(conn, student_id, co_curricular_ids, do_commit=True):
    """Persist club selections on the student row and student_fee_items junction."""
    sid = int(student_id)
    if isinstance(co_curricular_ids, (list, tuple)):
        ids = []
        for x in co_curricular_ids:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                pass
    else:
        ids = parse_co_curricular_ids(co_curricular_ids, conn=conn, student_id=sid)
    cc_json = co_curricular_ids_to_json(ids)
    conn.execute(
        "UPDATE students SET co_curricular_activities=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (cc_json, sid),
    )
    conn.execute(
        """DELETE FROM student_fee_items WHERE student_id=? AND fee_item_id IN (
            SELECT id FROM fee_structure WHERE fee_category='co_curricular')""",
        (sid,),
    )
    for fid in ids:
        conn.execute(
            "INSERT INTO student_fee_items (student_id, fee_item_id) VALUES (?, ?)",
            (sid, fid),
        )
    if do_commit:
        conn.commit()


def _normalize_person_name(name):
    """Lowercase, collapsed whitespace for name matching."""
    return " ".join(str(name or "").strip().lower().split())


def get_co_curricular_name_to_id(conn):
    """Map co-curricular fee_name → fee_structure id."""
    return {
        str(r[1]).strip(): int(r[0])
        for r in conn.execute(
            "SELECT id, fee_name FROM fee_structure WHERE fee_category='co_curricular' ORDER BY fee_name"
        ).fetchall()
    }


def resolve_club_fee_id(conn, club_name, name_to_id=None):
    """Resolve a club label to fee_structure id (exact name, alias, or substring)."""
    raw = str(club_name or "").strip()
    if not raw:
        return None
    name_to_id = name_to_id if name_to_id is not None else get_co_curricular_name_to_id(conn)
    if not name_to_id:
        return None
    names_lower = {n.lower(): i for n, i in name_to_id.items()}
    pl = _normalize_person_name(raw)
    pl = _CC_LEGACY_FEE_ALIASES.get(pl, pl)
    if pl in names_lower:
        return names_lower[pl]
    for n, i in name_to_id.items():
        nl = n.lower()
        if pl == nl or pl in nl or nl in pl:
            return i
    return None


def resolve_students_by_names(conn, names, *, active_only=True):
    """
    Match spreadsheet names to student ids.

    Returns dict: matched [{input_name, student_id, student_name, grade}],
    ambiguous [{input_name, candidates: [{student_id, student_name, grade}, ...]}],
    unmatched [input_name strings].
    """
    import pandas as pd

    wanted = []
    seen = set()
    for n in names or []:
        s = str(n or "").strip()
        if not s or s.lower() == "nan":
            continue
        nk = _normalize_person_name(s)
        if nk and nk not in seen:
            seen.add(nk)
            wanted.append((s, nk))

    if not wanted:
        return {"matched": [], "ambiguous": [], "unmatched": []}

    df = pd.read_sql(
        "SELECT id, name, grade, COALESCE(status, 'Active') AS status FROM students ORDER BY name",
        conn,
    )
    if active_only and not df.empty:
        df = df[df["status"].astype(str).str.strip().eq("Active")]

    by_norm = {}
    for _, row in df.iterrows():
        nk = _normalize_person_name(row["name"])
        if not nk:
            continue
        by_norm.setdefault(nk, []).append(
            {
                "student_id": int(row["id"]),
                "student_name": str(row["name"]),
                "grade": str(row.get("grade") or "—"),
            }
        )

    matched, ambiguous, unmatched = [], [], []
    for input_name, nk in wanted:
        hits = by_norm.get(nk, [])
        if len(hits) == 1:
            matched.append({"input_name": input_name, **hits[0]})
        elif len(hits) > 1:
            ambiguous.append({"input_name": input_name, "candidates": hits})
        else:
            unmatched.append(input_name)
    return {"matched": matched, "ambiguous": ambiguous, "unmatched": unmatched}


def enroll_students_in_club(
    conn,
    club_id,
    student_ids,
    *,
    mode="add",
    resync_fees=True,
    do_commit=True,
):
    """
    Assign learners to a co-curricular club.

    mode='add': add club to each student’s existing clubs.
    mode='replace': club membership becomes exactly student_ids (for this club only).
    """
    cid = int(club_id)
    target = sorted({int(s) for s in (student_ids or [])})
    updated = 0
    skipped_inactive = []

    if mode == "replace":
        had_club = []
        for row in conn.execute(
            "SELECT id, co_curricular_activities FROM students WHERE COALESCE(status, 'Active') = 'Active'"
        ).fetchall():
            sid = int(row[0])
            existing = parse_co_curricular_ids(row[1], conn=conn, student_id=sid)
            if cid in existing:
                had_club.append(sid)
        affected = sorted(set(had_club) | set(target))
    else:
        affected = target

    for sid in affected:
        rec = get_student_record(conn, sid)
        if rec is None:
            continue
        if str(rec.get("status") or "Active").strip() != "Active":
            skipped_inactive.append(sid)
            continue
        existing = parse_co_curricular_ids(
            rec.get("co_curricular_activities"), conn=conn, student_id=sid
        )
        if mode == "replace":
            new_ids = [x for x in existing if x != cid]
            if sid in target:
                if cid not in new_ids:
                    new_ids.append(cid)
        else:
            new_ids = list(existing)
            if sid in target and cid not in new_ids:
                new_ids.append(cid)
            elif sid not in target:
                continue
        if sorted(new_ids) != sorted(existing):
            save_student_co_curricular(conn, sid, new_ids, do_commit=False)
            updated += 1
            if resync_fees:
                sync_student_fees_from_db(conn, sid, do_commit=False)

    if do_commit:
        conn.commit()
    return {"updated": updated, "skipped_inactive": skipped_inactive}


CLUB_ROSTER_IMPORT_TEMPLATE_CSV = (
    "club,student_name\n"
    "Drama,John Smith\n"
    "Drama,Jane Doe\n"
    "Football,Peter Kimani\n"
)


def parse_club_roster_dataframe(df):
    """
    Parse club roster spreadsheet rows.

    Supports:
      - club + student_name (one student per row)
      - club + students (semicolon/comma-separated names per row)
      - Wide layout: first column = club, other columns = one name per column
    """
    import pandas as pd
    import re

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all")
    if df.empty:
        return []

    club_col = _detect_column(
        df, ["club", "club_name", "activity", "co_curricular", "co-curricular"]
    )
    name_col = _detect_column(
        df, ["student_name", "name", "student", "learner", "member", "member_name"]
    )
    names_col = _detect_column(
        df, ["students", "members", "student_names", "names", "roster"]
    )

    rows = []

    def _add(club, student_name, sheet_row):
        club_s = str(club or "").strip()
        name_s = str(student_name or "").strip()
        if not club_s or not name_s or name_s.lower() == "nan":
            return
        rows.append(
            {"club_name": club_s, "student_name": name_s, "sheet_row": int(sheet_row)}
        )

    if club_col and name_col:
        for i, row in df.iterrows():
            _add(row[club_col], row[name_col], i + 2)
    elif club_col and names_col:
        for i, row in df.iterrows():
            club_s = str(row[club_col] or "").strip()
            if not club_s:
                continue
            parts = re.split(r"[;|,\n]+", str(row[names_col] or ""))
            for p in parts:
                _add(club_s, p, i + 2)
    elif len(df.columns) >= 2:
        club_col = df.columns[0]
        name_cols = list(df.columns[1:])
        for i, row in df.iterrows():
            club_s = str(row[club_col] or "").strip()
            if not club_s:
                continue
            if len(name_cols) == 1 and names_col is None:
                cell = row[name_cols[0]]
                if cell is None or (isinstance(cell, float) and pd.isna(cell)):
                    continue
                parts = re.split(r"[;|,\n]+", str(cell))
                for p in parts:
                    _add(club_s, p, i + 2)
            else:
                for nc in name_cols:
                    val = row[nc]
                    if val is None or (isinstance(val, float) and pd.isna(val)):
                        continue
                    _add(club_s, val, i + 2)
    return rows


def import_club_roster_assignments(
    conn,
    assignments,
    *,
    mode="add",
    dry_run=False,
    resync_fees=True,
):
    """
    Apply parsed club roster rows. assignments: list from parse_club_roster_dataframe.

    Returns clubs_processed, students_updated, errors, preview (matched rows), unresolved.
    """
    name_to_id = get_co_curricular_name_to_id(conn)
    if not name_to_id:
        return {
            "clubs_processed": 0,
            "students_updated": 0,
            "errors": [(1, "No co-curricular clubs in fee structure. Add clubs under Fee Structure first.")],
            "preview": [],
            "unresolved": {"unmatched_names": [], "ambiguous": [], "unknown_clubs": []},
        }

    by_club = {}
    for row in assignments or []:
        club_name = row["club_name"]
        cid = resolve_club_fee_id(conn, club_name, name_to_id)
        if cid is None:
            by_club.setdefault(("__unknown__", club_name), []).append(row)
        else:
            by_club.setdefault(("ok", cid, club_name), []).append(row)

    preview = []
    errors = []
    unknown_clubs = []
    all_unmatched = []
    all_ambiguous = []
    total_updated = 0
    clubs_processed = 0

    for key, group_rows in by_club.items():
        if key[0] == "__unknown__":
            unknown_clubs.append(key[1])
            for r in group_rows:
                errors.append((r["sheet_row"], f"Unknown club: {key[1]!r}"))
            continue

        _, cid, club_label = key
        names = [r["student_name"] for r in group_rows]
        resolution = resolve_students_by_names(conn, names)
        for u in resolution["unmatched"]:
            all_unmatched.append({"club": club_label, "student_name": u})
        for a in resolution["ambiguous"]:
            all_ambiguous.append({"club": club_label, **a})

        for m in resolution["matched"]:
            preview.append(
                {
                    "club": club_label,
                    "student_name": m["input_name"],
                    "matched_name": m["student_name"],
                    "grade": m["grade"],
                    "student_id": m["student_id"],
                    "status": "OK",
                }
            )
        for a in resolution["ambiguous"]:
            for c in a["candidates"]:
                preview.append(
                    {
                        "club": club_label,
                        "student_name": a["input_name"],
                        "matched_name": c["student_name"],
                        "grade": c["grade"],
                        "student_id": c["student_id"],
                        "status": "Ambiguous — fix in Manage Students",
                    }
                )
        for u in resolution["unmatched"]:
            preview.append(
                {
                    "club": club_label,
                    "student_name": u,
                    "matched_name": "—",
                    "grade": "—",
                    "student_id": None,
                    "status": "Not found",
                }
            )

        if dry_run:
            clubs_processed += 1
            continue

        student_ids = [m["student_id"] for m in resolution["matched"]]
        if student_ids:
            rep = enroll_students_in_club(
                conn,
                cid,
                student_ids,
                mode=mode,
                resync_fees=resync_fees,
                do_commit=True,
            )
            total_updated += rep["updated"]
            clubs_processed += 1

    return {
        "clubs_processed": clubs_processed,
        "students_updated": total_updated,
        "errors": errors,
        "preview": preview,
        "unresolved": {
            "unmatched_names": all_unmatched,
            "ambiguous": all_ambiguous,
            "unknown_clubs": sorted(set(unknown_clubs)),
        },
    }


# --- Grade roster: parent/guardian contact + date of birth (bulk fill) -----------------

GRADE_ROSTER_IMPORT_TEMPLATE_CSV = (
    "grade,student_name,parent_guardian_names,parent_phone,parent2_name,parent2_phone,date_of_birth\n"
    "Grade 1,Jane Doe,Mary Doe & John Doe,254712345678,,,2018-05-12\n"
    "Grade 1,John Smith,Peter Smith,254700000001,,,2017-11-03\n"
)


def split_parent_guardian_names(cell):
    """Split one cell into parent/guardian 1 and 2 (e.g. 'Mary & John')."""
    import re

    if cell is None:
        return "", ""
    s = str(cell).strip()
    if not s or s.lower() == "nan":
        return "", ""
    parts = re.split(r"\s*(?:&|/|;|\||\n|\band\b)\s*", s, maxsplit=1, flags=re.IGNORECASE)
    p1 = parts[0].strip()
    p2 = parts[1].strip() if len(parts) > 1 else ""
    return p1, p2


def split_parent_phone_numbers(cell):
    """Split one cell into one or two phone numbers."""
    import re

    if cell is None:
        return "", ""
    raw = _excel_whole_number_as_text(cell)
    s = str(raw if raw is not None else cell).strip()
    if not s or s.lower() == "nan":
        return "", ""
    parts = re.split(r"[,;/|\n]+", s)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return "", ""
    p1 = normalize_kenya_msisdn(parts[0]) or parts[0]
    p2 = ""
    if len(parts) > 1:
        p2 = normalize_kenya_msisdn(parts[1]) or parts[1]
    return p1, p2


def parse_date_of_birth_cell(cell):
    """Parse spreadsheet DOB to ISO date string or None."""
    import pandas as pd
    import re
    from datetime import date as date_cls

    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return None
    if isinstance(cell, date_cls):
        return cell.strftime("%Y-%m-%d")
    raw = _excel_whole_number_as_text(cell)
    s = str(raw if raw is not None else cell).strip()
    if not s or s.lower() == "nan":
        return None
    try:
        iso_like = bool(re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", s))
        ts = pd.to_datetime(s, dayfirst=not iso_like, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def grade_roster_detected_columns(df):
    """
    Which spreadsheet columns map to grade-roster import fields (same rules as parse_grade_roster_dataframe).
    Pass a DataFrame with raw column headers (e.g. from read_student_spreadsheet).
    """
    if df is None or df.empty:
        return {
            "grade": None,
            "student_name": None,
            "parent": None,
            "parent2": None,
            "phone": None,
            "phone2": None,
            "dob": None,
        }
    dc = df.copy()
    dc.columns = [str(c).strip() for c in dc.columns]
    return {
        "grade": _detect_column(
            dc, ["grade", "class", "level", "form", "grade_name", "class_name"]
        ),
        "student_name": _detect_column(
            dc,
            [
                "student_name",
                "name",
                "student",
                "learner_name",
                "learner",
                "pupil",
            ],
        ),
        "parent": _detect_column(
            dc,
            [
                "parent/guardian names (1)",
                "parent guardian names (1)",
                "parent/guardian name (1)",
                "parent_guardian_names",
                "parent_guardian_name",
                "parent_guardian",
                "parents_guardian_names",
                "parent(s)_guardian_name(s)",
                "parent_name",
                "parent",
                "guardian",
                "guardian_name",
                "parents",
                "parent_names",
                "mother",
                "mother_name",
                "mothers_name",
                "guardians",
            ],
        ),
        "parent2": _detect_column(
            dc,
            [
                "parent/guardian names (2)",
                "parent guardian names (2)",
                "parent/guardian name (2)",
                "parent2_name",
                "parent_2_name",
                "second_parent_name",
                "father",
                "father_name",
                "fathers_name",
                "mother_name_2",
                "guardian2_name",
                "second_guardian",
                "parent_b",
            ],
        ),
        "phone": _detect_column(
            dc,
            [
                "phone (1)",
                "parent phone (1)",
                "phone number (1)",
                "parent_phone",
                "parents_number",
                "parents_phone",
                "parent_number",
                "parent_phone_number",
                "phone_number",
                "telephone",
                "telephone_number",
                "tel",
                "tel_no",
                "phone_no",
                "mobile_number",
                "cell_phone",
                "contact",
                "contacts",
                "contact_number",
                "contact_phone",
                "primary_contact",
                "parent_tel",
                "guardian_phone",
                "guardian_cell",
                "guardian_telephone",
                "mpesa",
                "m_pesa",
                "mpesa_no",
                "m_pesa_no",
                "phone",
                "mobile",
                "parents_mobile",
                "parent_contact",
            ],
        ),
        "phone2": _detect_column(
            dc,
            [
                "phone (2)",
                "parent phone (2)",
                "parent2_phone",
                "parent_2_phone",
                "second_parent_phone",
                "father_phone",
                "mother_phone",
                "guardian2_phone",
                "phone_2",
                "second_phone",
                "mobile_2",
                "tel_2",
            ],
        ),
        "dob": _detect_column(
            dc,
            [
                "date_of_birth",
                "dob",
                "student_date_of_birth",
                "birth_date",
                "birthdate",
                "date_of_birth_(dd/mm/yyyy)",
            ],
        ),
    }


def parse_grade_roster_dataframe(df, *, default_grade=None):
    """
    Parse grade contact roster rows from a spreadsheet.

    Columns (flexible headers): optional grade column; student name; parent/guardian
    name(s) in one cell (e.g. ``Mary & John``) or split across **parent** + **parent2**
    columns (e.g. Mother / Father); phone(s) in one cell (comma-separated) or **phone**
    + **phone2** columns; date of birth.

    If ``default_grade`` is set (e.g. from the upload **filename**) and normalizes to a
    class in REAL_GRADES, that grade is used for **every row** so one-file-per-class
    sheets do not need a grade column.
    """
    import pandas as pd

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all")
    if df.empty:
        return []

    det = grade_roster_detected_columns(df)
    col_grade = det["grade"]
    col_name = det["student_name"]
    col_parent = det["parent"]
    col_parent2 = det.get("parent2")
    col_phone = det["phone"]
    col_phone2 = det.get("phone2")
    col_dob = det["dob"]

    forced_file_grade = None
    if default_grade:
        fg = _normalize_grade(default_grade)
        if fg in REAL_GRADES:
            forced_file_grade = fg

    sheet_grade = forced_file_grade
    if not sheet_grade:
        sheet_grade = _normalize_grade(default_grade) if default_grade else None
    if not sheet_grade:
        _hdr_ex = {col_name} if col_name else set()
        sheet_grade = _consensus_grade_from_column_headers(df, _hdr_ex)

    rows = []
    for i, row in df.iterrows():
        name_val = _cell(row, col_name) if col_name else None
        name_s = str(name_val).strip() if name_val is not None else ""
        if not name_s or name_s.lower() == "nan":
            continue

        if forced_file_grade:
            grade_s = forced_file_grade
        else:
            g_raw = _cell(row, col_grade) if col_grade else None
            grade_s = _normalize_grade(g_raw) if g_raw is not None else None
            if not grade_s:
                grade_s = sheet_grade
            if not grade_s and g_raw is not None:
                grade_s = infer_grade_from_text(str(g_raw).strip())
            if not grade_s:
                grade_s = INCOMPLETE_GRADE_LABEL

        p1, p2 = split_parent_guardian_names(_cell(row, col_parent) if col_parent else None)
        if col_parent2:
            v2n = _cell(row, col_parent2)
            if v2n is not None:
                s2 = str(v2n).strip()
                if s2 and s2.lower() != "nan":
                    p2 = s2

        ph1, ph2 = split_parent_phone_numbers(_cell(row, col_phone) if col_phone else None)
        if col_phone2:
            raw2 = _cell(row, col_phone2)
            if raw2 is not None:
                t2 = _excel_whole_number_as_text(raw2)
                s2 = str(t2 if t2 is not None else raw2).strip()
                if s2 and s2.lower() != "nan":
                    ph2n = normalize_kenya_msisdn(s2) or s2
                    ph2 = ph2 or ph2n

        dob = parse_date_of_birth_cell(_cell(row, col_dob) if col_dob else None)

        rows.append(
            {
                "grade": grade_s,
                "student_name": name_s,
                "parent_name": p1,
                "parent2_name": p2,
                "parent_phone": ph1,
                "parent2_phone": ph2,
                "date_of_birth": dob,
                "sheet_row": int(i) + 2,
            }
        )
    return rows


def resolve_students_in_grade_by_names(conn, grade, names):
    """Match names to Active students in one grade (same rules as resolve_students_by_names)."""
    import pandas as pd

    grade_norm = _normalize_grade(grade) or str(grade or "").strip()
    wanted = []
    seen = set()
    for n in names or []:
        s = str(n or "").strip()
        if not s:
            continue
        nk = _normalize_person_name(s)
        if nk and nk not in seen:
            seen.add(nk)
            wanted.append((s, nk))

    if not wanted:
        return {"matched": [], "ambiguous": [], "unmatched": []}

    df = pd.read_sql(
        """SELECT id, name, grade, COALESCE(status, 'Active') AS status FROM students
           WHERE grade = ? ORDER BY name""",
        conn,
        params=(grade_norm,),
    )
    if not df.empty:
        df = df[df["status"].astype(str).str.strip().eq("Active")]

    by_norm = {}
    for _, row in df.iterrows():
        nk = _normalize_person_name(row["name"])
        if not nk:
            continue
        by_norm.setdefault(nk, []).append(
            {
                "student_id": int(row["id"]),
                "student_name": str(row["name"]),
                "grade": str(row.get("grade") or grade_norm),
            }
        )

    matched, ambiguous, unmatched = [], [], []
    for input_name, nk in wanted:
        hits = by_norm.get(nk, [])
        if len(hits) == 1:
            matched.append({"input_name": input_name, **hits[0]})
        elif len(hits) > 1:
            ambiguous.append({"input_name": input_name, "candidates": hits})
        else:
            unmatched.append(input_name)
    return {"matched": matched, "ambiguous": ambiguous, "unmatched": unmatched}


def _transport_fields_from_record(rec):
    """has_transport, route id, and transport_choice for persist_student_edit."""
    has_t = int(rec.get("has_transport") or 0)
    tid = rec.get("transport_route_id")
    if has_t and tid is not None:
        try:
            tid = int(tid)
            return has_t, tid, str(tid)
        except (TypeError, ValueError):
            pass
    return 0, None, "__none__"


def apply_student_contact_patch(conn, student_id, patch, *, resync_fees=False):
    """Update parent/guardian and DOB fields only; other fields stay as in DB."""
    rec = get_student_record(conn, student_id)
    if rec is None:
        raise ValueError(f"Student id {student_id} not found.")
    has_t, tid, tc = _transport_fields_from_record(rec)
    merged = merge_student_edit_payload(
        conn,
        student_id,
        {
            "name": rec.get("name"),
            "grade": rec.get("grade"),
            "parent_name": patch.get("parent_name"),
            "parent_phone": patch.get("parent_phone"),
            "parent2_name": patch.get("parent2_name"),
            "parent2_phone": patch.get("parent2_phone"),
            "date_of_birth": patch.get("date_of_birth"),
            "has_transport": has_t,
            "selected_transport_id": tid,
            "transport_choice": tc,
            "has_meal": rec.get("has_meal"),
            "include_admission_fees": rec.get("include_admission_fees"),
            "is_sponsored": rec.get("is_sponsored"),
            "balance": rec.get("balance"),
        },
    )
    persist_student_edit(conn, student_id, merged, resync_fees=resync_fees)


def grade_roster_row_to_patch(row):
    """Build contact patch dict from a parsed roster row (non-empty fields only)."""
    patch = {}
    for key in (
        "parent_name",
        "parent2_name",
        "parent_phone",
        "parent2_phone",
        "date_of_birth",
    ):
        val = row.get(key)
        if val is None:
            continue
        if key == "date_of_birth":
            if val:
                patch[key] = val
        elif str(val).strip():
            patch[key] = str(val).strip()
    return patch


def import_grade_roster_updates(conn, parsed_rows, *, dry_run=False, resync_fees=False):
    """Apply parsed grade roster rows to matched Active students."""
    preview = []
    errors = []
    updated = 0
    all_unmatched = []
    all_ambiguous = []

    by_grade = {}
    for row in parsed_rows or []:
        by_grade.setdefault(row["grade"], []).append(row)

    for grade, group in by_grade.items():
        names = [r["student_name"] for r in group]
        resolution = resolve_students_in_grade_by_names(conn, grade, names)
        name_to_match = {m["input_name"]: m for m in resolution["matched"]}
        all_unmatched.extend(
            {"grade": grade, "student_name": u} for u in resolution["unmatched"]
        )
        all_ambiguous.extend({"grade": grade, **a} for a in resolution["ambiguous"])

        for row in group:
            sn = row["student_name"]
            patch = grade_roster_row_to_patch(row)
            if not patch:
                preview.append(
                    {
                        "grade": grade,
                        "student_name": sn,
                        "status": "Skipped (no contact data)",
                        "student_id": None,
                    }
                )
                continue
            match = name_to_match.get(sn)
            if not match:
                amb = next((a for a in resolution["ambiguous"] if a["input_name"] == sn), None)
                if amb:
                    preview.append(
                        {
                            "grade": grade,
                            "student_name": sn,
                            "status": "Ambiguous name in grade",
                            "student_id": None,
                        }
                    )
                else:
                    preview.append(
                        {
                            "grade": grade,
                            "student_name": sn,
                            "status": "Not found in grade",
                            "student_id": None,
                        }
                    )
                continue

            sid = int(match["student_id"])
            preview.append(
                {
                    "grade": grade,
                    "student_name": sn,
                    "matched_name": match["student_name"],
                    "status": "OK",
                    "student_id": sid,
                }
            )
            if dry_run:
                continue
            try:
                apply_student_contact_patch(conn, sid, patch, resync_fees=resync_fees)
                updated += 1
            except ValueError as ex:
                errors.append((row.get("sheet_row", 0), str(ex)))

    return {
        "updated": updated,
        "preview": preview,
        "errors": errors,
        "unresolved": {"unmatched": all_unmatched, "ambiguous": all_ambiguous},
    }


def persist_grade_bulk_contact_edits(conn, original_df, edited_df, *, resync_fees=False):
    """
    Compare bulk-edit dataframes (must include id column) and save contact changes.

    Returns dict: updated, skipped, errors [(student_id, message)].
    """
    import pandas as pd

    if original_df is None or edited_df is None or original_df.empty:
        return {"updated": 0, "skipped": 0, "errors": []}

    orig = original_df.set_index("id")
    edit = edited_df.set_index("id")
    updated = 0
    skipped = 0
    errors = []

    for sid in orig.index.astype(int):
        if sid not in edit.index:
            continue
        o = orig.loc[sid]
        e = edit.loc[sid]
        patch = {}
        for col in ("parent_name", "parent2_name"):
            ov = "" if pd.isna(o.get(col)) else str(o.get(col) or "").strip()
            ev = "" if pd.isna(e.get(col)) else str(e.get(col) or "").strip()
            if ev != ov and ev:
                patch[col] = ev
        for col in ("parent_phone", "parent2_phone"):
            ov = normalize_kenya_msisdn(o.get(col)) or str(o.get(col) or "").strip()
            ev_raw = e.get(col)
            ev = normalize_kenya_msisdn(ev_raw) or (
                "" if pd.isna(ev_raw) else str(ev_raw or "").strip()
            )
            if ev != ov and ev:
                patch[col] = ev
        od = parse_date_of_birth_cell(o.get("date_of_birth"))
        ed = parse_date_of_birth_cell(e.get("date_of_birth"))
        if ed and ed != od:
            patch["date_of_birth"] = ed

        if not patch:
            skipped += 1
            continue
        try:
            apply_student_contact_patch(conn, int(sid), patch, resync_fees=resync_fees)
            updated += 1
        except ValueError as ex:
            errors.append((int(sid), str(ex)))

    return {"updated": updated, "skipped": skipped, "errors": errors}


# --- Balance roster (bulk set outstanding balance by grade) ----------------------------

BALANCE_ROSTER_IMPORT_TEMPLATE_CSV = (
    "grade,student_name,balance\n"
    "Grade 1,Jane Doe,15000\n"
    "Grade 1,John Smith,0\n"
)


def _spreadsheet_cell_str(cell):
    """Trim a spreadsheet cell for header detection."""
    import pandas as pd

    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return ""
    raw = _excel_whole_number_as_text(cell)
    s = str(raw if raw is not None else cell).strip()
    if not s or s.lower() == "nan":
        return ""
    return s


def _make_spreadsheet_column_names(row_values):
    """Build unique column labels from one header row (fee rosters with blank cells)."""
    cols = []
    seen = {}
    for i, v in enumerate(row_values):
        base = _spreadsheet_cell_str(v) or f"column_{i}"
        key = _norm_header_key(base) or f"column_{i}"
        n = seen.get(key, 0)
        if n:
            cols.append(f"{base}__{n + 1}")
        else:
            cols.append(base)
        seen[key] = n + 1
    return cols


_BALANCE_NAME_ALIASES = [
    "names",
    "student_name",
    "student_names",
    "name",
    "student",
    "learner_name",
    "learner",
    "pupil",
    "learner_names",
]

_BALANCE_AMOUNT_ALIASES = [
    "balance",
    "balances",
    "school_balance",
    "student_balance",
    "outstanding",
    "outstanding_balance",
    "fee_balance",
    "amount_owed",
    "balance_ksh",
    "most_recent_balance",
    "bal",
]

_BALANCE_GRADE_ALIASES = ["grade", "class", "level", "form", "grade_name", "class_name"]


def balance_roster_detected_columns(df):
    """Which columns map to grade / student name / balance (after header promotion)."""
    if df is None or df.empty:
        return {"grade": None, "student_name": None, "balance": None}
    dc = df.copy()
    dc.columns = [str(c).strip() for c in dc.columns]
    return {
        "grade": _detect_column(dc, _BALANCE_GRADE_ALIASES),
        "student_name": _detect_column(dc, _BALANCE_NAME_ALIASES),
        "balance": _detect_column(dc, _BALANCE_AMOUNT_ALIASES),
    }


def prepare_balance_roster_dataframe(df):
    """
    Fee spreadsheets often have a title row, then a header row (e.g. NAMES, BALANCE), then data.

    When read with pandas header=0, row 1 becomes column names and the real headers sit in the
    first data row. Scan the first rows and promote the row that contains name + balance headers.
    Returns (dataframe, first_data_file_row_1based).
    """
    import pandas as pd

    if df is None or df.empty:
        return df, 2

    work = df.copy()
    work.columns = [str(c).strip() for c in work.columns]
    work = work.dropna(how="all")
    if work.empty:
        return work, 2

    det = balance_roster_detected_columns(work)
    if det["student_name"] and det["balance"]:
        return work, 2

    max_scan = min(15, len(work))
    for idx in range(max_scan):
        row_vals = work.iloc[idx].tolist()
        if not any(_spreadsheet_cell_str(v) for v in row_vals):
            continue
        candidate_cols = _make_spreadsheet_column_names(row_vals)
        body = work.iloc[idx + 1 :].copy()
        if body.empty:
            continue
        body.columns = candidate_cols
        body = body.dropna(how="all")
        det = balance_roster_detected_columns(body)
        if det["student_name"] and det["balance"]:
            return body.reset_index(drop=True), idx + 3

    return work, 2


def parse_balance_cell(cell):
    """Parse currency/balance cell to float (KSH, commas allowed)."""
    import pandas as pd
    import re

    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return None
    raw = _excel_whole_number_as_text(cell)
    s = str(raw if raw is not None else cell).strip()
    if not s or s.lower() in ("nan", "none", "-", "—"):
        return None
    s = re.sub(r"^(ksh|kes)\s*", "", s, flags=re.IGNORECASE)
    s = s.replace(",", "").replace(" ", "")
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_balance_roster_dataframe(df, *, default_grade=None):
    """Parse grade + student name + balance rows from a spreadsheet."""
    import pandas as pd

    df, data_start_file_row = prepare_balance_roster_dataframe(df)
    if df.empty:
        return []

    col_grade = _detect_column(df, _BALANCE_GRADE_ALIASES)
    col_name = _detect_column(df, _BALANCE_NAME_ALIASES)
    col_bal = _detect_column(df, _BALANCE_AMOUNT_ALIASES)
    if not col_name or not col_bal:
        return []

    sheet_grade = _normalize_grade(default_grade) if default_grade else None
    if not sheet_grade:
        _hdr_ex = {col_name, col_bal}
        if col_grade:
            _hdr_ex.add(col_grade)
        sheet_grade = _consensus_grade_from_column_headers(df, _hdr_ex)

    _skip_name_labels = frozenset(
        {
            "names",
            "name",
            "student_name",
            "student names",
            "learner",
            "total",
            "totals",
            "sum",
            "grand total",
        }
    )

    rows = []
    for i, row in df.iterrows():
        name_val = _cell(row, col_name)
        name_s = str(name_val).strip() if name_val is not None else ""
        if not name_s or name_s.lower() == "nan":
            continue
        if name_s.strip().lower() in _skip_name_labels:
            continue
        entry = parse_balance_editor_value(_cell(row, col_bal))
        if entry is None:
            continue
        bal_status, bal_amt = entry
        g_raw = _cell(row, col_grade) if col_grade else None
        grade_s = _normalize_grade(g_raw) if g_raw is not None else None
        if not grade_s:
            grade_s = sheet_grade
        if not grade_s and g_raw is not None:
            grade_s = infer_grade_from_text(str(g_raw).strip())
        if not grade_s:
            grade_s = INCOMPLETE_GRADE_LABEL
        rows.append(
            {
                "grade": grade_s,
                "student_name": name_s,
                "balance_status": bal_status,
                "balance": bal_amt,
                "sheet_row": int(data_start_file_row) + int(i),
            }
        )
    return rows


BALANCE_STATUS_NOT_SET = "not_set"
BALANCE_STATUS_CLEARED = "cleared"
BALANCE_STATUS_SET = "set"

BALANCE_DISPLAY_NOT_SET = "Not set"
BALANCE_DISPLAY_CLEARED = "Cleared"


def _normalize_balance_status(raw, *, balance_set=None, balance=None):
    """Map stored values (and legacy balance_set) to not_set | cleared | set."""
    if raw is not None and not (isinstance(raw, float) and str(raw) == "nan"):
        s = str(raw).strip().lower().replace(" ", "_")
        if s in ("not_set", "notset", "unset"):
            return BALANCE_STATUS_NOT_SET
        if s in ("cleared", "clear"):
            return BALANCE_STATUS_CLEARED
        if s in ("set", "amount", "value"):
            return BALANCE_STATUS_SET
    if balance_set is not None and int(balance_set or 0) == 0:
        return BALANCE_STATUS_NOT_SET
    if balance_set is not None and int(balance_set or 0) == 1:
        try:
            b = float(balance or 0)
        except (TypeError, ValueError):
            b = 0.0
        if abs(b) < 0.01:
            return BALANCE_STATUS_CLEARED
        return BALANCE_STATUS_SET
    return BALANCE_STATUS_SET


def student_balance_status(student_row):
    """Return not_set, cleared, or set for a student row (dict or Series)."""
    if student_row is None:
        return BALANCE_STATUS_NOT_SET
    if isinstance(student_row, dict):
        return _normalize_balance_status(
            student_row.get("balance_status"),
            balance_set=student_row.get("balance_set"),
            balance=student_row.get("balance"),
        )
    import pandas as pd

    raw = student_row["balance_status"] if "balance_status" in student_row.index else None
    bset = student_row["balance_set"] if "balance_set" in student_row.index else None
    bal = student_row["balance"] if "balance" in student_row.index else None
    if raw is not None and not pd.isna(raw):
        return _normalize_balance_status(raw, balance_set=bset, balance=bal)
    if bset is not None and not pd.isna(bset) and int(bset or 0) == 0:
        return BALANCE_STATUS_NOT_SET
    try:
        b = float(bal or 0)
    except (TypeError, ValueError):
        b = 0.0
    if bset is not None and not pd.isna(bset) and int(bset or 0) == 1:
        if abs(b) < 0.01:
            return BALANCE_STATUS_CLEARED
        return BALANCE_STATUS_SET
    return BALANCE_STATUS_SET


def student_balance_is_set(student_row):
    """True when balance has been entered (cleared or a KSH amount), not still 'Not set'."""
    return student_balance_status(student_row) != BALANCE_STATUS_NOT_SET


def student_balance_is_entered(student_row):
    """Alias for student_balance_is_set — balance is Cleared or has an amount."""
    return student_balance_is_set(student_row)


def student_balance_is_outstanding(student_row):
    """True when status is set and amount is greater than zero."""
    if student_balance_status(student_row) != BALANCE_STATUS_SET:
        return False
    if isinstance(student_row, dict):
        bal = student_row.get("balance")
    else:
        bal = student_row.get("balance")
    try:
        return float(bal or 0) > 0.01
    except (TypeError, ValueError):
        return False


def sum_outstanding_balance_rows(student_rows_df):
    """
    Sum the balance column only for rows that count as real outstanding debt.
    Excludes **Not set** and **Cleared** (and zero balances) so totals match list displays.
    """
    if student_rows_df is None or student_rows_df.empty or "balance" not in student_rows_df.columns:
        return 0.0
    total = 0.0
    for _, r in student_rows_df.iterrows():
        if not student_balance_is_outstanding(r):
            continue
        try:
            total += float(r.get("balance") or 0)
        except (TypeError, ValueError):
            continue
    return total


def format_student_balance_display(balance=None, balance_set=None, balance_status=None, student_row=None):
    """Human-readable balance: Not set, Cleared, or KSH amount."""
    if student_row is not None:
        balance_status = student_balance_status(student_row)
        balance = student_row.get("balance") if isinstance(student_row, dict) else student_row.get("balance")
    status = balance_status
    if status is None:
        status = _normalize_balance_status(None, balance_set=balance_set, balance=balance)
    if status == BALANCE_STATUS_NOT_SET:
        return BALANCE_DISPLAY_NOT_SET
    if status == BALANCE_STATUS_CLEARED:
        return BALANCE_DISPLAY_CLEARED
    return f"KSH {float(balance or 0):,.0f}"


def balance_editor_display(student_row):
    """Value for bulk-edit / spreadsheet cells."""
    st = student_balance_status(student_row)
    if st == BALANCE_STATUS_NOT_SET:
        return BALANCE_DISPLAY_NOT_SET
    if st == BALANCE_STATUS_CLEARED:
        return BALANCE_DISPLAY_CLEARED
    if isinstance(student_row, dict):
        bal = student_row.get("balance")
    else:
        bal = student_row.get("balance")
    try:
        return f"{float(bal or 0):,.0f}".replace(",", "")
    except (TypeError, ValueError):
        return "0"


def parse_balance_editor_value(cell):
    """
  Parse a balance cell from UI or import.
  Returns (status, amount) with status in not_set|cleared|set, or None to skip unchanged row.
    """
    import pandas as pd

    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return None
    s = str(cell).strip()
    if not s:
        return None
    sl = s.lower().replace("_", " ")
    if sl in ("not set", "notset", "unset", "—", "-", "n/a", "na"):
        return (BALANCE_STATUS_NOT_SET, None)
    if sl in ("cleared", "clear", "nil", "none", "zero", "0", "paid", "paid up"):
        return (BALANCE_STATUS_CLEARED, 0.0)
    val = parse_balance_cell(cell)
    if val is None:
        return None
    if abs(float(val)) < 0.01:
        return (BALANCE_STATUS_CLEARED, 0.0)
    return (BALANCE_STATUS_SET, float(val))


def apply_student_balance_entry(conn, student_id, balance_status, amount=None, *, do_commit=True):
    """Apply one of not_set, cleared, or set (with KSH amount)."""
    sid = int(student_id)
    rec = get_student_record(conn, sid)
    if rec is None:
        raise ValueError(f"Student id {sid} not found.")
    st_rec = str(rec.get("status") or "Active").strip() or "Active"
    if st_rec != "Active":
        raise ValueError(
            f"Cannot update balance (status: {st_rec}). Only Active records can be edited."
        )

    status = _normalize_balance_status(balance_status)
    from school_calendar import align_student_term_ledger_to_balance

    if status == BALANCE_STATUS_NOT_SET:
        conn.execute(
            """
            UPDATE students
            SET balance=NULL, balance_set=0, balance_status=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (BALANCE_STATUS_NOT_SET, sid),
        )
        align_student_term_ledger_to_balance(conn, sid, balance=0.0, do_commit=False)
        if do_commit:
            conn.commit()
        return None

    if int(rec.get("is_sponsored") or 0):
        final_bal = 0.0
        status = BALANCE_STATUS_CLEARED
    elif status == BALANCE_STATUS_CLEARED:
        final_bal = 0.0
    else:
        final_bal = float(amount if amount is not None else 0)

    conn.execute(
        """
        UPDATE students
        SET balance=?, balance_set=1, balance_status=?,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (final_bal, status, sid),
    )
    align_student_term_ledger_to_balance(conn, sid, balance=final_bal, do_commit=False)
    if do_commit:
        conn.commit()
    return final_bal


def apply_student_balance_override(conn, student_id, balance):
    """Set outstanding balance from a numeric import (0 → Cleared, else KSH amount)."""
    val = float(balance)
    if abs(val) < 0.01:
        return apply_student_balance_entry(conn, student_id, BALANCE_STATUS_CLEARED, 0.0)
    return apply_student_balance_entry(conn, student_id, BALANCE_STATUS_SET, val)


def import_balance_roster_updates(conn, parsed_rows, *, dry_run=False):
    """Apply balance rows to matched Active students in each grade."""
    preview = []
    errors = []
    updated = 0
    all_unmatched = []

    by_grade = {}
    for row in parsed_rows or []:
        by_grade.setdefault(row["grade"], []).append(row)

    for grade, group in by_grade.items():
        names = [r["student_name"] for r in group]
        resolution = resolve_students_in_grade_by_names(conn, grade, names)
        name_to_match = {m["input_name"]: m for m in resolution["matched"]}
        all_unmatched.extend(
            {"grade": grade, "student_name": u} for u in resolution["unmatched"]
        )

        for row in group:
            sn = row["student_name"]
            match = name_to_match.get(sn)
            _bst = row.get("balance_status", BALANCE_STATUS_SET)
            _bamt = row.get("balance")
            if not match:
                preview.append(
                    {
                        "grade": grade,
                        "student_name": sn,
                        "balance": _bamt,
                        "balance_status": _bst,
                        "status": "Not found in grade",
                        "student_id": None,
                    }
                )
                continue
            sid = int(match["student_id"])
            preview.append(
                {
                    "grade": grade,
                    "student_name": sn,
                    "matched_name": match["student_name"],
                    "balance": _bamt,
                    "balance_status": _bst,
                    "status": "OK",
                    "student_id": sid,
                }
            )
            if dry_run:
                continue
            try:
                apply_student_balance_entry(
                    conn, sid, row.get("balance_status", BALANCE_STATUS_SET), row.get("balance")
                )
                updated += 1
            except ValueError as ex:
                errors.append((row.get("sheet_row", 0), str(ex)))

    return {
        "updated": updated,
        "preview": preview,
        "errors": errors,
        "unresolved": {"unmatched": all_unmatched},
    }


def persist_balance_bulk_edits(conn, original_df, edited_df):
    """Compare bulk balance editor and save changes."""
    import pandas as pd

    if original_df is None or edited_df is None or original_df.empty:
        return {"updated": 0, "skipped": 0, "errors": []}

    orig = original_df.set_index("id")
    edit = edited_df.set_index("id")
    updated = 0
    skipped = 0
    errors = []

    for sid in orig.index.astype(int):
        if sid not in edit.index:
            continue
        o_entry = parse_balance_editor_value(orig.loc[sid].get("balance"))
        n_entry = parse_balance_editor_value(edit.loc[sid].get("balance"))
        if n_entry is None:
            skipped += 1
            continue
        if o_entry == n_entry:
            skipped += 1
            continue
        try:
            n_status, n_amt = n_entry
            apply_student_balance_entry(conn, int(sid), n_status, n_amt)
            updated += 1
        except ValueError as ex:
            errors.append((int(sid), str(ex)))

    return {"updated": updated, "skipped": skipped, "errors": errors}


def _parse_meal_program_cell(cell):
    if cell is None or (isinstance(cell, float) and str(cell) == "nan"):
        return None
    s = str(cell).strip().lower()
    if not s:
        return None
    if s in ("yes", "y", "1", "true", "on"):
        return 1
    if s in ("no", "n", "0", "false", "off"):
        return 0
    return None


def persist_meal_program_bulk_edits(conn, original_df, edited_df):
    """Update has_meal for active students from a grade bulk editor."""
    import pandas as pd

    if original_df is None or edited_df is None or original_df.empty:
        return {"updated": 0, "skipped": 0, "errors": []}

    orig = original_df.set_index("id")
    edit = edited_df.set_index("id")
    updated = 0
    skipped = 0
    errors = []

    for sid in orig.index.astype(int):
        if sid not in edit.index:
            continue
        ob = int(orig.loc[sid].get("has_meal") or 0)
        nb = _parse_meal_program_cell(edit.loc[sid].get("meals"))
        if nb is None:
            nb = _parse_meal_program_cell(edit.loc[sid].get("has_meal"))
        if nb is None:
            skipped += 1
            continue
        if int(nb) == ob:
            skipped += 1
            continue
        rec = get_student_record(conn, int(sid))
        if rec is None:
            errors.append((int(sid), "Student not found."))
            continue
        status = str(rec.get("status") or "Active").strip() or "Active"
        if status != "Active":
            errors.append((int(sid), f"Cannot update (status: {status})."))
            continue
        conn.execute(
            "UPDATE students SET has_meal=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(nb), int(sid)),
        )
        sync_student_fees_from_db(conn, int(sid), do_commit=False)
        updated += 1

    if updated:
        conn.commit()
    return {"updated": updated, "skipped": skipped, "errors": errors}


def persist_transport_users_bulk_edits(conn, original_df, edited_df):
    """Update school transport route for active students from a grade bulk editor."""
    import pandas as pd

    if original_df is None or edited_df is None or original_df.empty:
        return {"updated": 0, "skipped": 0, "errors": []}

    orig = original_df.set_index("id")
    edit = edited_df.set_index("id")
    updated = 0
    skipped = 0
    errors = []

    for sid in orig.index.astype(int):
        if sid not in edit.index:
            continue
        o_choice = str(orig.loc[sid].get("transport_choice") or "__none__").strip()
        n_choice = str(edit.loc[sid].get("transport_choice") or "__none__").strip()
        if n_choice == o_choice:
            skipped += 1
            continue
        has_transport = 1 if n_choice and n_choice != "__none__" else 0
        transport_id = int(n_choice) if has_transport else None
        rec = get_student_record(conn, int(sid))
        if rec is None:
            errors.append((int(sid), "Student not found."))
            continue
        status = str(rec.get("status") or "Active").strip() or "Active"
        if status != "Active":
            errors.append((int(sid), f"Cannot update (status: {status})."))
            continue
        conn.execute(
            """UPDATE students SET has_transport=?, transport_route_id=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (has_transport, transport_id, int(sid)),
        )
        sync_student_fees_from_db(conn, int(sid), do_commit=False)
        updated += 1

    if updated:
        conn.commit()
    return {"updated": updated, "skipped": skipped, "errors": errors}


def scheduled_student_deletion_datetime_str():
    """ISO datetime for student record removal grace period."""
    from datetime import datetime, timedelta

    from database import STUDENT_DELETION_GRACE_DAYS

    return (datetime.now() + timedelta(days=STUDENT_DELETION_GRACE_DAYS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def schedule_student_transfer(conn, student_id, transfer_reason=None, *, do_commit=True):
    """Mark an Active student as Transferred and schedule permanent deletion."""
    from datetime import date

    sid = int(student_id)
    rec = get_student_record(conn, sid)
    if rec is None:
        raise ValueError(f"Student id {sid} not found.")
    status = str(rec.get("status") or "Active").strip() or "Active"
    if status != "Active":
        raise ValueError(f"Only Active students can be transferred (status: {status}).")
    deletion_date = scheduled_student_deletion_datetime_str()
    cur = conn.execute(
        """
        UPDATE students
        SET status = 'Transferred',
            transfer_reason = ?,
            deletion_scheduled = ?,
            exited_at = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            (transfer_reason or "").strip() or None,
            deletion_date,
            date.today().isoformat(),
            sid,
        ),
    )
    if cur.rowcount != 1:
        raise ValueError("Could not mark student as transferred (no matching row).")
    if do_commit:
        conn.commit()
    return {"deletion_scheduled": deletion_date}


def schedule_student_deletion(conn, student_id, deletion_reason, *, do_commit=True):
    """Mark an Active student as Scheduled for Deletion."""
    from datetime import date

    sid = int(student_id)
    rec = get_student_record(conn, sid)
    if rec is None:
        raise ValueError(f"Student id {sid} not found.")
    status = str(rec.get("status") or "Active").strip() or "Active"
    if status != "Active":
        raise ValueError(f"Only Active students can be scheduled for deletion (status: {status}).")
    reason = (deletion_reason or "").strip()
    if not reason:
        raise ValueError("Deletion reason is required.")
    deletion_date = scheduled_student_deletion_datetime_str()
    cur = conn.execute(
        """
        UPDATE students
        SET status = 'Scheduled for Deletion',
            deletion_scheduled = ?,
            deletion_reason = ?,
            exited_at = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (deletion_date, reason, date.today().isoformat(), sid),
    )
    if cur.rowcount != 1:
        raise ValueError("Could not schedule deletion (no matching row).")
    if do_commit:
        conn.commit()
    return {"deletion_scheduled": deletion_date, "deletion_reason": reason}


def get_student_record(conn, student_id):
    """Return one student row as a dict, or None."""
    cur = conn.execute("SELECT * FROM students WHERE id=?", (int(student_id),))
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def apply_optional_other_payer_for_payment(conn, student_id, payment_row_id, other_name, other_phone, *, do_commit=True):
    """
    Optional **Add payment** payer details: if guardian 2 is empty, save as parent2;
    otherwise append a short note to the payment row description.
    """
    on = (other_name or "").strip()
    op = (other_phone or "").strip()
    if not on and not op:
        return
    opn = normalize_kenya_msisdn(op) if op else ""
    rec = get_student_record(conn, int(student_id))
    if not rec:
        return
    p2n = (rec.get("parent2_name") or "").strip()
    p2p = (rec.get("parent2_phone") or "").strip()
    if not p2n and not p2p:
        conn.execute(
            "UPDATE students SET parent2_name=?, parent2_phone=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (on, opn, int(student_id)),
        )
        if do_commit:
            conn.commit()
        return
    extra = "Other payer"
    if on:
        extra += f": {on}"
    if opn:
        extra += f" ({opn})"
    conn.execute(
        "UPDATE payments SET description = TRIM(COALESCE(description, '') || ' | ' || ?) WHERE id=?",
        (extra[:500], int(payment_row_id)),
    )
    if do_commit:
        conn.commit()


def graduated_students_archive_path():
    """Append-only JSONL archive for Grade 9 leavers (not shown in View Students)."""
    return Path(__file__).resolve().parent / "data" / "graduated_students.jsonl"


def archive_graduated_student_record(conn, student_id, *, archived_at=None):
    """Append a read-only snapshot of a graduated learner to the archive file."""
    import json
    from datetime import datetime

    sid = int(student_id)
    rec = get_student_record(conn, sid)
    if rec is None:
        return False
    billing_rows = conn.execute(
        """SELECT term_id, amount_billed, opening_balance, grade_at_billing,
                  admission_included, interview_fee_included, fee_breakdown_json
           FROM student_term_billing WHERE student_id = ? ORDER BY term_id""",
        (sid,),
    ).fetchall()
    billing_cols = [
        "term_id",
        "amount_billed",
        "opening_balance",
        "grade_at_billing",
        "admission_included",
        "interview_fee_included",
        "fee_breakdown_json",
    ]
    payload = {
        "archived_at": (archived_at or datetime.now()).replace(microsecond=0).isoformat(),
        "student": {k: rec[k] for k in rec},
        "term_billing": [dict(zip(billing_cols, r)) for r in billing_rows],
    }
    path = graduated_students_archive_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str) + "\n")
    return True


def merge_student_edit_payload(conn, student_id, form_payload):
    """Combine form values with the current DB row so optional blanks do not wipe existing data."""
    rec = get_student_record(conn, student_id) or {}

    def _optional_text(form_key, db_key):
        v = form_payload.get(form_key)
        if v is None or (isinstance(v, str) and not str(v).strip()):
            return rec.get(db_key)
        return v

    name = (form_payload.get("name") or rec.get("name") or "").strip()
    grade = form_payload.get("grade") or rec.get("grade")
    return {
        "name": name or rec.get("name"),
        "grade": grade,
        "parent_name": _optional_text("parent_name", "parent_name") or "",
        "parent_phone": _optional_text("parent_phone", "parent_phone") or "",
        "parent2_name": _optional_text("parent2_name", "parent2_name") or "",
        "parent2_phone": _optional_text("parent2_phone", "parent2_phone") or "",
        "date_of_birth": form_payload.get("date_of_birth") if form_payload.get("date_of_birth") else rec.get("date_of_birth"),
        "transport_choice": form_payload.get("transport_choice"),
        "has_transport": form_payload.get("has_transport"),
        "selected_transport_id": form_payload.get("selected_transport_id"),
        "has_meal": form_payload.get("has_meal"),
        "include_admission_fees": form_payload.get("include_admission_fees", rec.get("include_admission_fees") or 0),
        "include_interview_fee": form_payload.get("include_interview_fee", rec.get("include_interview_fee") or 0),
        "co_curricular_ids": form_payload.get("co_curricular_ids") if form_payload.get("co_curricular_ids") is not None else parse_co_curricular_ids(
            rec.get("co_curricular_activities"), conn=conn, student_id=student_id
        ),
        "is_sponsored": int(
            form_payload.get("is_sponsored")
            if form_payload.get("is_sponsored") is not None
            else int(student_is_sponsored(rec))
        ),
        "balance_status": form_payload.get("balance_status")
        if form_payload.get("balance_status") is not None
        else student_balance_status(rec),
        "balance": form_payload.get("balance")
        if form_payload.get("balance") is not None
        else rec.get("balance"),
    }


def persist_student_edit(conn, student_id, payload, *, apply_balance_override=True, resync_fees=True):
    """
    Write a full Manage Students edit to the database.

    payload: name, grade, parent_name, parent_phone, parent2_name, parent2_phone,
    date_of_birth (ISO or None),
    has_transport, selected_transport_id, has_meal, include_admission_fees, include_interview_fee,
    co_curricular_ids (list), is_sponsored (0/1), balance (optional — applied after fee resync).

    Raises ValueError if the student is transferred or scheduled for deletion.
    """
    sid = int(student_id)
    rec = get_student_record(conn, sid)
    if rec:
        status = str(rec.get("status") or "Active").strip() or "Active"
        if status != "Active":
            raise ValueError(
                f"Cannot edit student record (status: {status}). "
                "Only Active records can be edited (graduated, transferred, and "
                "scheduled-for-deletion records are read-only)."
            )
    phone_n = normalize_kenya_msisdn(payload.get("parent_phone", ""))
    phone2_n = normalize_kenya_msisdn(payload.get("parent2_phone", ""))
    has_transport = int(bool(payload.get("has_transport")))
    transport_id = payload.get("selected_transport_id") if has_transport else None
    is_sponsored = int(bool(payload.get("is_sponsored", 0)))

    conn.execute(
        """
        UPDATE students
        SET name = ?, grade = ?, parent_name = ?, parent_phone = ?,
            parent2_name = ?, parent2_phone = ?,
            date_of_birth = ?,
            has_transport = ?, transport_route_id = ?, has_meal = ?,
            include_admission_fees = ?, include_interview_fee = ?, is_sponsored = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            payload["name"],
            payload["grade"],
            payload["parent_name"],
            phone_n,
            payload.get("parent2_name") or "",
            phone2_n,
            payload.get("date_of_birth"),
            has_transport,
            transport_id,
            int(bool(payload.get("has_meal"))),
            int(payload.get("include_admission_fees", 0)),
            int(payload.get("include_interview_fee", 0)),
            is_sponsored,
            sid,
        ),
    )
    save_student_co_curricular(conn, sid, payload.get("co_curricular_ids") or [], do_commit=False)
    conn.commit()

    if resync_fees:
        sync_student_fees_from_db(conn, sid, do_commit=True)

    if apply_balance_override and payload.get("balance_status") is not None:
        apply_student_balance_entry(
            conn,
            sid,
            payload["balance_status"],
            payload.get("balance"),
            do_commit=True,
        )
    elif apply_balance_override and payload.get("balance") is not None:
        apply_student_balance_override(conn, sid, payload["balance"])
    elif is_sponsored and student_balance_status(get_student_record(conn, sid) or {}) == BALANCE_STATUS_NOT_SET:
        apply_student_balance_entry(conn, sid, BALANCE_STATUS_CLEARED, 0.0, do_commit=True)

    return get_student_record(conn, sid)


def verify_student_edit_saved(conn, student_id, payload):
    """Return (True, []) if DB matches payload after persist_student_edit."""
    sid = int(student_id)
    rec = get_student_record(conn, sid)
    if rec is None:
        return False, [f"Student id {sid} not found in database."]

    mismatches = []
    phone_n = normalize_kenya_msisdn(payload.get("parent_phone", ""))
    phone2_n = normalize_kenya_msisdn(payload.get("parent2_phone", ""))

    def _eq(label, expected, actual):
        if expected is None and (actual is None or str(actual).strip() == ""):
            return
        if str(expected) != str(actual):
            mismatches.append(f"{label}: expected {expected!r}, saved {actual!r}")

    _eq("name", payload.get("name"), rec.get("name"))
    _eq("grade", payload.get("grade"), rec.get("grade"))
    _eq("parent_name", payload.get("parent_name"), rec.get("parent_name"))
    _eq("parent_phone", phone_n, rec.get("parent_phone"))
    _eq("parent2_name", payload.get("parent2_name"), rec.get("parent2_name"))
    _eq("parent2_phone", phone2_n, rec.get("parent2_phone"))
    _eq("date_of_birth", payload.get("date_of_birth"), rec.get("date_of_birth"))
    _eq("has_transport", int(bool(payload.get("has_transport"))), int(rec.get("has_transport") or 0))
    _eq("has_meal", int(bool(payload.get("has_meal"))), int(rec.get("has_meal") or 0))
    _eq(
        "include_admission_fees",
        int(payload.get("include_admission_fees", 0)),
        int(rec.get("include_admission_fees") or 0),
    )
    _eq(
        "include_interview_fee",
        int(payload.get("include_interview_fee", 0)),
        int(rec.get("include_interview_fee") or 0),
    )
    _eq(
        "is_sponsored",
        int(bool(payload.get("is_sponsored", 0))),
        int(rec.get("is_sponsored") or 0),
    )

    exp_tid = payload.get("selected_transport_id") if payload.get("has_transport") else None
    act_tid = rec.get("transport_route_id") if int(rec.get("has_transport") or 0) else None
    if exp_tid is None and act_tid is None:
        pass
    elif exp_tid is not None and act_tid is not None and int(exp_tid) == int(act_tid):
        pass
    else:
        mismatches.append(
            f"transport_route_id: expected {exp_tid!r}, saved {act_tid!r}"
        )

    exp_cc = set(
        parse_co_curricular_ids(payload.get("co_curricular_ids"), conn=conn, student_id=sid)
    )
    act_cc = set(
        parse_co_curricular_ids(rec.get("co_curricular_activities"), conn=conn, student_id=sid)
    )
    if exp_cc != act_cc:
        mismatches.append(f"co_curricular_ids: expected {sorted(exp_cc)}, saved {sorted(act_cc)}")

    exp_bs = payload.get("balance_status")
    if exp_bs is not None:
        act_bs = student_balance_status(rec)
        if exp_bs != act_bs:
            mismatches.append(f"balance_status: expected {exp_bs!r}, saved {act_bs!r}")
    if payload.get("balance_status") == BALANCE_STATUS_SET and payload.get("balance") is not None:
        exp_bal = float(payload["balance"])
        act_bal = float(rec.get("balance") or 0)
        if abs(exp_bal - act_bal) > 0.009:
            mismatches.append(f"balance: expected {exp_bal:.2f}, saved {act_bal:.2f}")

    return len(mismatches) == 0, mismatches


def sync_student_fees_from_db(conn, student_id, do_commit=True):
    """
    Recalculate current-term billing (if calendar configured) and balance from ledger.
    Falls back to single-term formula when no school term is active.
    """
    import pandas as pd

    from school_calendar import get_current_term, upsert_current_term_billing

    student = pd.read_sql("SELECT * FROM students WHERE id=?", conn, params=(student_id,))
    if student.empty:
        return None

    if get_current_term(conn):
        result = upsert_current_term_billing(conn, student_id, do_commit=do_commit)
        if result:
            return result.get("fee_result")
        return None

    student_row = student.iloc[0]
    grade = student_row["grade"]
    has_transport = bool(student_row["has_transport"])
    transport_route_id = student_row["transport_route_id"] if has_transport else None
    has_meal = bool(student_row["has_meal"])
    include_admission = student_row_bool(student_row, "include_admission_fees", False)
    include_interview = student_row_bool(student_row, "include_interview_fee", False)

    co_curricular_ids = parse_co_curricular_ids(
        student_row.get("co_curricular_activities"), conn=conn, student_id=student_id
    )

    fee_result = calculate_student_fees(
        conn,
        grade,
        transport_route_id,
        co_curricular_ids,
        has_meal,
        include_admission=include_admission,
        include_interview=include_interview,
    )

    total_paid = student_row["total_paid"] or 0.0
    if student_is_sponsored(student_row):
        new_balance = float(student_row.get("balance") or 0)
        conn.execute(
            "UPDATE students SET balance=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_balance, student_id),
        )
    else:
        new_balance = max(float(fee_result["grand_total"]) - float(total_paid), 0.0)
        if new_balance <= 0.0001:
            new_balance = 0.0
            bstat = BALANCE_STATUS_CLEARED
        else:
            bstat = BALANCE_STATUS_SET
        conn.execute(
            """
            UPDATE students SET balance=?, balance_set=1, balance_status=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=?
            """,
            (new_balance, bstat, student_id),
        )

    if do_commit:
        conn.commit()

    return fee_result


def resync_all_student_balances(conn):
    """Recompute balance for every student from current fee_structure and selections."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM students")
    ids = [row[0] for row in cur.fetchall()]
    for sid in ids:
        sync_student_fees_from_db(conn, sid, do_commit=False)
    conn.commit()
    return len(ids)


# --- Bulk student import (CSV / Excel) -----------------------------------------


def max_student_code_numeric_suffix(conn):
    """Highest numeric suffix from VINE#### codes or legacy all-digit codes."""
    max_n = 0
    for (code,) in conn.execute("SELECT student_code FROM students").fetchall():
        c = str(code or "").strip().upper()
        if c.startswith(STUDENT_CODE_PREFIX):
            rest = c[len(STUDENT_CODE_PREFIX) :]
            if rest.isdigit():
                max_n = max(max_n, int(rest))
        elif c.isdigit():
            max_n = max(max_n, int(c))
    return max_n


def format_student_sequential_code(num):
    """Format sequential learner code: VINE0001, VINE0002, …"""
    return f"{STUDENT_CODE_PREFIX}{int(num):04d}"


def get_next_student_code(conn):
    """Next unused sequential student code (VINE prefix)."""
    return format_student_sequential_code(max_student_code_numeric_suffix(conn) + 1)


STUDENT_IMPORT_TEMPLATE_CSV = (
    "student_code,name,parent_name,parent_phone,parent2_name,parent2_phone,grade,transport_route,has_meal,include_admission,include_interview,co_curricular\n"
    ",John Smith,,,,,,,\n"
    ",Jane Doe,Mary Doe,254712345678,Grade 1,,,,,\n"
)


def _norm_header_key(h):
    s = str(h).strip().lower().replace("/", "_")
    s = s.replace(" ", "_").replace("-", "_")
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_")


def _detect_column(df, aliases):
    mapping = {_norm_header_key(c): c for c in df.columns}
    for a in aliases:
        k = _norm_header_key(a)
        if k in mapping:
            return mapping[k]
    return None


# Bulk import: which spreadsheet column maps to which student field (auto-detect aliases).
STUDENT_IMPORT_NAME_ALIASES = [
    "name",
    "student_name",
    "learner_name",
    "child_name",
    "student",
    "pupil",
]

STUDENT_IMPORT_OPTIONAL_SPECS = (
    ("parent_name", ["parent_name", "parent", "guardian", "guardian_name", "parent_guardian_name"]),
    ("parent_phone", ["parent_phone", "phone", "mobile", "telephone", "tel", "msisdn", "parent_guardian_phone"]),
    ("parent2_name", ["parent2_name", "parent_2_name", "guardian2_name", "second_parent", "parent2"]),
    ("parent2_phone", ["parent2_phone", "parent_2_phone", "guardian2_phone", "second_parent_phone", "parent2_phone"]),
    ("grade", ["grade", "class", "level", "form"]),
    (
        "student_code",
        ["student_code", "code", "adm_no", "admission_number", "student_id", "learner_code"],
    ),
    ("transport_route", ["transport_route", "transport", "route", "bus_route", "bus"]),
    ("has_meal", ["has_meal", "meal", "lunch", "meals"]),
    ("include_admission", ["include_admission", "admission", "admission_fees", "include_admission_fees"]),
    ("include_interview", ["include_interview", "interview", "interview_fee", "include_interview_fee"]),
    ("co_curricular", ["co_curricular", "clubs", "activities", "extras", "optional_clubs"]),
)


def suggest_student_import_name_column(df):
    """Best-effort header to use for student name (same rules as bulk import auto-detect)."""
    dc = df.copy()
    dc.columns = [str(c).strip() for c in dc.columns]
    return _detect_column(dc, STUDENT_IMPORT_NAME_ALIASES)


def _cell(row, col_name):
    if not col_name or col_name not in row.index:
        return None
    v = row[col_name]
    import pandas as pd
    if pd.isna(v):
        return None
    return v


def _excel_whole_number_as_text(val):
    """Turn 254712345678.0 from Excel into a clean string for phones/codes."""
    import pandas as pd
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, float) and val == int(val) and abs(val) < 1e15:
        return str(int(val))
    if isinstance(val, int):
        return str(val)
    return val


def _parse_bool_cell(val, default=False):
    if val is None:
        return default
    import pandas as pd
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)) and not pd.isna(val):
        try:
            return bool(int(val))
        except (TypeError, ValueError):
            pass
    s = str(val).strip().lower()
    if s in ("", "nan", "none", "no", "n", "0", "false", "f"):
        return False
    if s in ("yes", "y", "1", "true", "t"):
        return True
    return default


INCOMPLETE_GRADE_LABEL = "Unassigned"

STUDENT_STATUS_ACTIVE = "Active"
STUDENT_STATUS_GRADUATED = "Graduated"

REAL_GRADES = [
    "Playgroup", "PP1", "PP2", "Grade 1", "Grade 2", "Grade 3", "Grade 4", "Grade 5",
    "Grade 6", "Grade 7", "Grade 8", "Grade 9",
]

GRADE_CHOICES_EDIT = [INCOMPLETE_GRADE_LABEL] + REAL_GRADES

# Display order for progression through the school (Playgroup → … → Grade 9).
GRADE_PROGRESSION_ARROWS = " → ".join(REAL_GRADES)


def grade_sort_key(grade):
    """Sort key: Playgroup, PP1, PP2, Grade 1–9, then unassigned, then unknown."""
    g = (str(grade or "").strip()) or INCOMPLETE_GRADE_LABEL
    if g in REAL_GRADES:
        return (REAL_GRADES.index(g), g)
    if g == INCOMPLETE_GRADE_LABEL:
        return (len(REAL_GRADES), g)
    return (len(REAL_GRADES) + 1, g)


def sort_grade_labels(labels):
    """Return grade labels sorted in school progression order."""
    return sorted(labels, key=grade_sort_key)


def grade_progression_through(grade):
    """Path from Playgroup through the learner's current class (for display)."""
    g = (str(grade or "").strip())
    if g not in REAL_GRADES:
        return None
    return " → ".join(REAL_GRADES[: REAL_GRADES.index(g) + 1])


def sort_dataframe_by_grade(df, grade_col="grade", *, then_by=None):
    """Sort a copy of df by class progression, optionally then by another column."""
    if df is None or df.empty or grade_col not in df.columns:
        return df
    out = df.copy()
    out["_grade_ord"] = out[grade_col].map(lambda g: grade_sort_key(g))
    sort_cols = ["_grade_ord"]
    if then_by and then_by in out.columns:
        sort_cols.append(then_by)
    out = out.sort_values(sort_cols).drop(columns=["_grade_ord"])
    return out


def _normalize_grade(val):
    import pandas as pd
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return None
    if s.lower() in ("unassigned", "pending", "tbd", "none"):
        return INCOMPLETE_GRADE_LABEL
    sl = s.lower().replace(" ", "")
    for ag in REAL_GRADES:
        if ag.lower().replace(" ", "") == sl:
            return ag
    for ag in REAL_GRADES:
        if ag.lower() == s.lower():
            return ag
    return None


def infer_grade_from_text(text):
    """
    Best-effort: extract a REAL_GRADES value from free text (filename, column title, etc.).
    Returns None if no confident match.
    """
    import re

    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None

    ng = _normalize_grade(s)
    if ng and ng in REAL_GRADES:
        return ng

    low = s.lower().replace("_", " ").replace("-", " ")
    compact = re.sub(r"\s+", "", low)
    m = re.search(r"grade(\d{1,2})(?![0-9])", compact)
    if m:
        cand = f"Grade {int(m.group(1))}"
        if cand in REAL_GRADES:
            return cand
    # e.g. g1, class3, year7
    m = re.search(r"\b(?:grade|class|year)\s*(\d{1,2})\b", low)
    if m:
        cand = f"Grade {int(m.group(1))}"
        if cand in REAL_GRADES:
            return cand
    m = re.search(r"\bg\s*(\d{1,2})\b", low)
    if m:
        cand = f"Grade {int(m.group(1))}"
        if cand in REAL_GRADES:
            return cand
    m = re.search(r"\bpp\s*1\b", low)
    if m:
        return "PP1"
    m = re.search(r"\bpp\s*2\b", low)
    if m:
        return "PP2"
    if re.search(r"\bplaygroup\b", low):
        return "Playgroup"

    for g in sorted(REAL_GRADES, key=len, reverse=True):
        if g.lower() in low:
            return g
    return None


def infer_grade_from_filename(filename):
    """Infer grade from upload basename (e.g. `Grade_1_term2.csv` → Grade 1)."""
    if not filename:
        return None
    import os

    base = os.path.basename(str(filename))
    for suf in (".csv", ".xlsx", ".xls", ".XLSX", ".CSV"):
        if base.lower().endswith(suf.lower()):
            base = base[: -len(suf)]
            break
    return infer_grade_from_text(base)


def _infer_grade_from_column_header_label(col):
    """If a spreadsheet column title names a class (e.g. 'Grade 1', 'PP1 Fees'), return that grade."""
    s = str(col).strip()
    ng = _normalize_grade(s)
    if ng and ng in REAL_GRADES:
        return ng
    return infer_grade_from_text(s)


def _consensus_grade_from_column_headers(df, exclude_columns):
    """
    If headers consistently refer to one class (e.g. one 'Grade 1' column, or several 'Grade 1 …' columns),
    return that grade. If headers imply multiple different classes (fee matrix), return None.
    """
    ex = set(exclude_columns or [])
    hits = []
    for c in df.columns:
        if c in ex:
            continue
        g = _infer_grade_from_column_header_label(c)
        if g:
            hits.append((c, g))
    if not hits:
        return None
    grades = {g for _, g in hits}
    if len(grades) == 1:
        return next(iter(grades))
    return None


def preview_sheet_grade_for_bulk_import(df, name_column, source_filename):
    """
    What grade will be applied to rows that have no grade cell (for UI hint).
    Same header/filename priority as bulk_import (headers win if they agree on one class).
    """
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    ex = set()
    if name_column and name_column in df.columns:
        ex.add(name_column)
    return _consensus_grade_from_column_headers(df, ex) or infer_grade_from_filename(source_filename)


def read_student_spreadsheet(uploaded_file):
    """Load a CSV or Excel file from a Streamlit UploadedFile into a DataFrame."""
    import io
    import pandas as pd

    name = uploaded_file.name.lower()
    raw = uploaded_file.getvalue()
    bio = io.BytesIO(raw)
    if name.endswith(".csv"):
        df = pd.read_csv(bio)
    elif name.endswith(".xlsx") or name.endswith(".xls"):
        df = pd.read_excel(bio)
    else:
        raise ValueError("Unsupported file type. Use .csv or .xlsx")
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _resolve_transport_cell(cell, transport_rows):
    """
    transport_rows: list of (id, fee_name, route_label)
    Returns (has_transport, transport_route_id or None).
    """
    import pandas as pd
    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return False, None
    s = str(cell).strip()
    if not s or s.lower() in ("no", "none", "n", "0", "-", "false"):
        return False, None
    s_low = s.lower()
    for tid, fee_name, route in transport_rows:
        for c in (fee_name, route):
            if not c:
                continue
            cl = c.lower()
            if s_low == cl or s_low in cl or cl in s_low:
                return True, int(tid)
    return False, None


_CC_LEGACY_FEE_ALIASES = {
    "keyboard / music instruments": "musical instruments",
    "keyboard/music instruments": "musical instruments",
    "keyboard": "musical instruments",
    "music instruments": "musical instruments",
    "french / chinese": "french",
    "french/chinese": "french",
    "chinese": "french",
}


def _parse_co_curricular_cell(cell, name_to_id):
    """Map semicolon/comma/pipe-separated names to fee_structure ids."""
    import pandas as pd
    import re

    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return []
    s = str(cell).strip()
    if not s or s.lower() == "nan":
        return []
    parts = re.split(r"[;|,/\n]+", s)
    ids = []
    names_lower = {n.lower(): i for n, i in name_to_id.items()}
    for p in parts:
        p = p.strip()
        if not p:
            continue
        pl = p.lower()
        pl = _CC_LEGACY_FEE_ALIASES.get(pl, pl)
        fid = None
        if pl in names_lower:
            fid = names_lower[pl]
        else:
            for n, i in name_to_id.items():
                nl = n.lower()
                if pl == nl or pl in nl or nl in pl:
                    fid = i
                    break
        if fid is not None and fid not in ids:
            ids.append(fid)
    return ids


def bulk_import_students_from_dataframe(
    df,
    conn,
    *,
    name_column=None,
    ignore_other_columns=False,
    column_overrides=None,
    source_filename=None,
):
    """
    Insert students from a spreadsheet-style DataFrame.
    Required columns (flexible names): name only — parent, phone, grade, etc. are optional
    (missing grade defaults to 'Unassigned' with zero fees until completed in Manage Students).
    Optional: student_code, transport_route, has_meal, include_admission, include_interview, co_curricular.

    name_column: exact header in df for the learner's name (overrides auto-detect). If None, auto-detect.
    ignore_other_columns: if True, do not read any optional field from the sheet (old exports with balances, etc.).
    column_overrides: optional dict logical_key -> sheet column name or None. None means "skip this field".
        Keys: parent_name, parent_phone, grade, student_code, transport_route, has_meal,
        include_admission, include_interview, co_curricular. Omitted keys use auto-detect unless ignore_other_columns is True.
    source_filename: upload basename used to infer class (e.g. Grade_1_list.csv) when rows have no grade cell.

    Returns dict: imported, skipped, errors [(spreadsheet_row, message), ...], new_ids, grade_hints
    """
    import json
    import pandas as pd

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all")

    if df.empty:
        return {
            "imported": 0,
            "skipped": 0,
            "errors": [(1, "File has no data rows.")],
            "new_ids": [],
            "grade_hints": {},
        }

    overrides = column_overrides or {}
    for ok, ov in overrides.items():
        if ov is not None and ov not in df.columns:
            return {
                "imported": 0,
                "skipped": len(df),
                "errors": [
                    (
                        1,
                        f"Column mapping for {ok!r} points to unknown column {ov!r}. "
                        f"Available: {', '.join(map(str, df.columns))}",
                    )
                ],
                "new_ids": [],
                "grade_hints": {},
            }

    c = conn.cursor()
    c.execute(
        "SELECT id, fee_name, COALESCE(transport_route, '') FROM fee_structure WHERE fee_category='transport'"
    )
    transport_rows = [(int(r[0]), r[1] or "", r[2] or "") for r in c.fetchall()]
    c.execute("SELECT id, fee_name FROM fee_structure WHERE fee_category='co_curricular'")
    name_to_id = {r[1]: int(r[0]) for r in c.fetchall()}

    if name_column is not None:
        nc = str(name_column).strip()
        if nc not in df.columns:
            return {
                "imported": 0,
                "skipped": len(df),
                "errors": [
                    (
                        1,
                        f"Unknown student name column {nc!r}. Available: {', '.join(map(str, df.columns))}",
                    )
                ],
                "new_ids": [],
                "grade_hints": {},
            }
        col_name = nc
    else:
        col_name = _detect_column(df, STUDENT_IMPORT_NAME_ALIASES)

    if ignore_other_columns:
        col_parent = col_phone = col_parent2 = col_phone2 = None
        col_grade = col_code = col_transport = col_meal = col_adm = col_int = col_cc = None
    else:
        _ro = {}
        for lk, al in STUDENT_IMPORT_OPTIONAL_SPECS:
            if lk in overrides:
                _ro[lk] = overrides[lk]
            else:
                _ro[lk] = _detect_column(df, al)
        col_parent = _ro["parent_name"]
        col_phone = _ro["parent_phone"]
        col_parent2 = _ro["parent2_name"]
        col_phone2 = _ro["parent2_phone"]
        col_grade = _ro["grade"]
        col_code = _ro["student_code"]
        col_transport = _ro["transport_route"]
        col_meal = _ro["has_meal"]
        col_adm = _ro["include_admission"]
        col_int = _ro["include_interview"]
        col_cc = _ro["co_curricular"]

    if not col_name:
        return {
            "imported": 0,
            "skipped": len(df),
            "errors": [
                (
                    1,
                    "Missing required column: need at least one name column "
                    "(e.g. name, student_name, learner_name).",
                )
            ],
            "new_ids": [],
            "grade_hints": {},
        }

    existing_rows = conn.execute("SELECT student_code FROM students").fetchall()
    existing_codes = {str(r[0]).strip() for r in existing_rows if r[0] is not None}
    seen_in_file = set()

    mx = max_student_code_numeric_suffix(conn)
    try:
        next_num = int(mx) + 1 if mx is not None else 1
    except (TypeError, ValueError):
        next_num = 1

    _hdr_exclude = {col_name}
    if col_grade:
        _hdr_exclude.add(col_grade)
    header_consensus = _consensus_grade_from_column_headers(df, _hdr_exclude)
    filename_grade = infer_grade_from_filename(source_filename)
    sheet_default = header_consensus or filename_grade
    grade_hints = {
        "from_filename": filename_grade,
        "from_headers": header_consensus,
        "sheet_default": sheet_default,
    }

    errors = []
    new_ids = []
    imported = 0

    for row_offset, (idx, row) in enumerate(df.iterrows(), start=2):
        name = _cell(row, col_name)
        name = str(name).strip() if name is not None else ""
        if not name or name.lower() == "nan":
            errors.append((row_offset, "Skipped: empty name"))
            continue

        parent = ""
        if col_parent:
            pv = _cell(row, col_parent)
            parent = str(pv).strip() if pv is not None else ""

        phone = ""
        if col_phone:
            phone_raw = _cell(row, col_phone)
            if phone_raw is not None:
                phone_t = _excel_whole_number_as_text(phone_raw)
                phone = str(phone_t if phone_t is not None else phone_raw).strip()
                phone = normalize_kenya_msisdn(phone)

        parent2 = ""
        if col_parent2:
            pv2 = _cell(row, col_parent2)
            parent2 = str(pv2).strip() if pv2 is not None else ""

        phone2 = ""
        if col_phone2:
            pr2 = _cell(row, col_phone2)
            if pr2 is not None:
                pt2 = _excel_whole_number_as_text(pr2)
                phone2 = str(pt2 if pt2 is not None else pr2).strip()
                phone2 = normalize_kenya_msisdn(phone2)

        g_raw = _cell(row, col_grade) if col_grade else None
        explicit_incomplete = False
        grade = None
        if g_raw is not None and str(g_raw).strip() and str(g_raw).strip().lower() != "nan":
            ng = _normalize_grade(g_raw)
            if ng in REAL_GRADES:
                grade = ng
            elif ng == INCOMPLETE_GRADE_LABEL:
                explicit_incomplete = True
                grade = INCOMPLETE_GRADE_LABEL
            else:
                ig = infer_grade_from_text(str(g_raw).strip())
                grade = ig if ig else None

        if grade is None or (grade == INCOMPLETE_GRADE_LABEL and not explicit_incomplete):
            if sheet_default:
                grade = sheet_default
            elif grade is None:
                grade = INCOMPLETE_GRADE_LABEL

        code = None
        raw_code = _cell(row, col_code) if col_code else None
        if raw_code is not None:
            rc = _excel_whole_number_as_text(raw_code)
            cs = str(rc if rc is not None else raw_code).strip()
            if cs and cs.lower() != "nan":
                csu = cs.upper().replace(" ", "")
                if csu.startswith(STUDENT_CODE_PREFIX):
                    code = csu
                elif cs.isdigit():
                    code = format_student_sequential_code(int(cs))
                else:
                    code = cs

        if not code:
            code = format_student_sequential_code(next_num)
            while code in existing_codes or code in seen_in_file:
                next_num += 1
                code = format_student_sequential_code(next_num)
        else:
            if code in seen_in_file:
                errors.append((row_offset, f"Skipped: duplicate student_code in file ({code})"))
                continue
            if code in existing_codes:
                errors.append((row_offset, f"Skipped: student_code already in database ({code})"))
                continue

        tcell = _cell(row, col_transport) if col_transport else None
        has_transport, transport_route_id = _resolve_transport_cell(tcell, transport_rows)

        has_meal = _parse_bool_cell(_cell(row, col_meal) if col_meal else None, False)
        include_adm = _parse_bool_cell(_cell(row, col_adm) if col_adm else None, False)
        include_int = _parse_bool_cell(_cell(row, col_int) if col_int else None, False)

        cc_ids = _parse_co_curricular_cell(_cell(row, col_cc) if col_cc else None, name_to_id)
        cc_json = json.dumps(cc_ids) if cc_ids else None

        try:
            cur = conn.execute(
                """INSERT INTO students
                (student_code, name, parent_name, parent_phone, parent2_name, parent2_phone,
                 grade, balance, total_paid,
                 has_transport, transport_route_id, has_meal, co_curricular_activities, extra_classes, include_admission_fees, include_interview_fee)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    code,
                    name,
                    parent,
                    phone,
                    parent2,
                    phone2,
                    grade,
                    0.0,
                    0.0,
                    int(has_transport),
                    transport_route_id,
                    int(has_meal),
                    cc_json,
                    "",
                    int(include_adm),
                    int(include_int),
                ),
            )
            sid = cur.lastrowid
            new_ids.append(sid)
            existing_codes.add(code)
            seen_in_file.add(code)
            cu = str(code).strip().upper()
            if cu.startswith(STUDENT_CODE_PREFIX):
                suf = cu[len(STUDENT_CODE_PREFIX) :]
                if suf.isdigit():
                    next_num = max(next_num, int(suf) + 1)
            elif cu.isdigit():
                next_num = max(next_num, int(cu) + 1)
            imported += 1
            if str(code).isdigit():
                try:
                    next_num = max(next_num, int(code) + 1)
                except ValueError:
                    pass
        except Exception as e:
            errors.append((row_offset, f"Database error: {e}"))

    conn.commit()

    for j, sid in enumerate(new_ids):
        sync_student_fees_from_db(conn, sid, do_commit=(j == len(new_ids) - 1))

    skipped = len(df) - imported
    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "new_ids": new_ids,
        "grade_hints": grade_hints,
    }


def dedupe_students_in_grade_by_name(conn, grade, dry_run=False):
    """
    Merge duplicate student rows in one grade that share the same name (trimmed, case-insensitive).

    Chooses a keeper per duplicate group: most payment rows + fee selections + total_paid, then lowest id.
    Repoints payments, merges student_fee_items, sums total_paid, fills missing parent/phone from duplicates,
    merges co-curricular JSON, then deletes the extra student rows.

    Returns dict: dry_run, grade, groups_merged, rows_removed, log (list of str).
    """
    import json
    import pandas as pd

    cur = conn.cursor()
    log = []
    df = pd.read_sql("SELECT * FROM students WHERE grade = ?", conn, params=(grade,))
    if df.empty:
        return {
            "dry_run": dry_run,
            "grade": grade,
            "groups_merged": 0,
            "rows_removed": 0,
            "log": ["No students in this grade."],
        }

    df["_nk"] = df["name"].fillna("").astype(str).str.strip().str.lower()
    groups_merged = 0
    rows_removed = 0
    affected_keepers = []

    for nk, grp in df.groupby("_nk", sort=False):
        if not nk:
            continue
        ids = [int(x) for x in grp["id"].tolist()]
        if len(ids) < 2:
            continue

        def _score(sid):
            r = grp.loc[grp["id"] == sid].iloc[0]
            npay = cur.execute("SELECT COUNT(*) FROM payments WHERE student_id=?", (sid,)).fetchone()[0]
            nfi = cur.execute("SELECT COUNT(*) FROM student_fee_items WHERE student_id=?", (sid,)).fetchone()[0]
            tp = float(r.get("total_paid") or 0)
            plen = len(str(r.get("parent_phone") or "").strip())
            nlen = len(str(r.get("parent_name") or "").strip())
            return (npay * 1_000_000 + nfi * 10_000 + tp * 10 + plen + nlen, -sid)

        keeper = max(ids, key=_score)
        remove_ids = [i for i in ids if i != keeper]
        name_disp = grp.loc[grp["id"] == keeper, "name"].iloc[0]
        log.append(
            f"Group {name_disp!r}: keep id={keeper}, merge ids={remove_ids}"
        )
        groups_merged += 1

        if dry_run:
            rows_removed += len(remove_ids)
            continue

        rows = [grp.loc[grp["id"] == i].iloc[0] for i in ids]
        merged_tp = sum(float(r.get("total_paid") or 0) for r in rows)

        def _first_nonempty(field):
            for r in sorted(rows, key=lambda x: (0 if int(x["id"]) == keeper else 1, int(x["id"]))):
                s = str(r.get(field) or "").strip()
                if s:
                    return s
            return ""

        parent_name = _first_nonempty("parent_name")
        parent_phone = normalize_kenya_msisdn(_first_nonempty("parent_phone"))
        parent2_name = _first_nonempty("parent2_name")
        parent2_phone = normalize_kenya_msisdn(_first_nonempty("parent2_phone"))
        has_meal = 1 if any(int(r.get("has_meal") or 0) for r in rows) else 0
        include_adm = 1 if any(int(r.get("include_admission_fees") or 0) for r in rows) else 0
        include_int = 1 if any(int(r.get("include_interview_fee") or 0) for r in rows) else 0

        has_tr = 0
        tr_id = None
        for r in rows:
            try:
                ht = int(r.get("has_transport") or 0)
            except (TypeError, ValueError):
                ht = 0
            if ht:
                has_tr = 1
                tid = r.get("transport_route_id")
                if tid is None:
                    continue
                if isinstance(tid, float) and pd.isna(tid):
                    continue
                try:
                    tr_id = int(tid)
                    break
                except (TypeError, ValueError):
                    continue

        cc_ids = []
        for r in rows:
            raw = r.get("co_curricular_activities")
            if raw is None or (isinstance(raw, float) and pd.isna(raw)):
                continue
            try:
                arr = json.loads(raw) if isinstance(raw, str) else list(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            for x in arr:
                try:
                    xi = int(x)
                except (TypeError, ValueError):
                    continue
                if xi not in cc_ids:
                    cc_ids.append(xi)
        cc_json = json.dumps(cc_ids) if cc_ids else None

        for rid in remove_ids:
            cur.execute("UPDATE payments SET student_id=? WHERE student_id=?", (keeper, rid))
            cur.execute(
                """
                INSERT INTO student_fee_items (student_id, fee_item_id)
                SELECT ?, sfi.fee_item_id FROM student_fee_items sfi
                WHERE sfi.student_id = ?
                AND NOT EXISTS (
                    SELECT 1 FROM student_fee_items e
                    WHERE e.student_id = ? AND e.fee_item_id = sfi.fee_item_id
                )
                """,
                (keeper, rid, keeper),
            )
            cur.execute(
                "SELECT term_id FROM student_term_billing WHERE student_id=?",
                (rid,),
            )
            for (_tid,) in cur.fetchall():
                dup = cur.execute(
                    "SELECT 1 FROM student_term_billing WHERE student_id=? AND term_id=?",
                    (keeper, _tid),
                ).fetchone()
                if dup:
                    cur.execute(
                        "DELETE FROM student_term_billing WHERE student_id=? AND term_id=?",
                        (rid, _tid),
                    )
                else:
                    cur.execute(
                        "UPDATE student_term_billing SET student_id=? WHERE student_id=? AND term_id=?",
                        (keeper, rid, _tid),
                    )
            cur.execute("DELETE FROM student_fee_items WHERE student_id=?", (rid,))
            cur.execute("DELETE FROM student_term_billing WHERE student_id=?", (rid,))
            cur.execute("DELETE FROM students WHERE id=?", (rid,))
            rows_removed += 1

        cur.execute(
            """
            UPDATE students SET
                parent_name = ?, parent_phone = ?, parent2_name = ?, parent2_phone = ?,
                total_paid = ?,
                has_meal = ?, include_admission_fees = ?, include_interview_fee = ?,
                has_transport = ?, transport_route_id = ?,
                co_curricular_activities = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                parent_name or "",
                parent_phone or "",
                parent2_name or "",
                parent2_phone or "",
                merged_tp,
                has_meal,
                include_adm,
                include_int,
                has_tr,
                tr_id,
                cc_json,
                keeper,
            ),
        )
        affected_keepers.append(keeper)

    if not dry_run and affected_keepers:
        conn.commit()
        for i, kid in enumerate(affected_keepers):
            sync_student_fees_from_db(conn, kid, do_commit=(i == len(affected_keepers) - 1))

    return {
        "dry_run": dry_run,
        "grade": grade,
        "groups_merged": groups_merged,
        "rows_removed": rows_removed,
        "log": log,
    }


# --- Pending reviews (Save for later) — persisted in SQLite across page refresh ---

DRAFT_TYPE_STUDENT = "student"
DRAFT_TYPE_STUDENT_TRANSFER = "student_transfer"
DRAFT_TYPE_STUDENT_DELETION = "student_deletion"
DRAFT_TYPE_MANUAL_PAYMENT = "manual_payment"
DRAFT_TYPE_EXPENSE = "expense"
DRAFT_TYPE_CLUB_BULK = "club_bulk"
DRAFT_TYPE_GRADE_CONTACT_BULK = "grade_contact_bulk"
DRAFT_TYPE_BALANCE_BULK = "balance_bulk"
DRAFT_TYPE_MEAL_BULK = "meal_bulk"
DRAFT_TYPE_TRANSPORT_BULK = "transport_bulk"


def _pending_review_json_dumps(payload):
    return json.dumps(payload, default=str)


def _pending_review_json_loads(raw):
    return json.loads(raw) if raw else None


def fetch_all_pending_reviews(conn):
    """Load all pending-review drafts from the database."""
    students = {}
    student_transfers = {}
    student_deletions = {}
    payments = []
    expenses = []
    club_drafts = []
    grade_contact_drafts = []
    balance_drafts = []
    meal_drafts = []
    transport_drafts = []
    try:
        rows = conn.execute(
            "SELECT draft_type, draft_key, payload_json FROM pending_reviews ORDER BY id"
        ).fetchall()
    except Exception:
        return {
            "students": students,
            "student_transfers": student_transfers,
            "student_deletions": student_deletions,
            "payments": payments,
            "expenses": expenses,
            "club_drafts": club_drafts,
            "grade_contact_drafts": grade_contact_drafts,
            "balance_drafts": balance_drafts,
            "meal_drafts": meal_drafts,
            "transport_drafts": transport_drafts,
        }
    for draft_type, draft_key, payload_json in rows:
        try:
            payload = _pending_review_json_loads(payload_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if draft_type == DRAFT_TYPE_STUDENT:
            try:
                students[int(draft_key)] = payload
            except (TypeError, ValueError):
                pass
        elif draft_type == DRAFT_TYPE_STUDENT_TRANSFER:
            try:
                student_transfers[int(draft_key)] = payload
            except (TypeError, ValueError):
                pass
        elif draft_type == DRAFT_TYPE_STUDENT_DELETION:
            try:
                student_deletions[int(draft_key)] = payload
            except (TypeError, ValueError):
                pass
        elif draft_type == DRAFT_TYPE_MANUAL_PAYMENT:
            payments.append(payload)
        elif draft_type == DRAFT_TYPE_EXPENSE:
            expenses.append(payload)
        elif draft_type == DRAFT_TYPE_CLUB_BULK and isinstance(payload, dict):
            club_drafts.append(payload)
        elif draft_type == DRAFT_TYPE_GRADE_CONTACT_BULK and isinstance(payload, dict):
            grade_contact_drafts.append(payload)
        elif draft_type == DRAFT_TYPE_BALANCE_BULK and isinstance(payload, dict):
            balance_drafts.append(payload)
        elif draft_type == DRAFT_TYPE_MEAL_BULK and isinstance(payload, dict):
            meal_drafts.append(payload)
        elif draft_type == DRAFT_TYPE_TRANSPORT_BULK and isinstance(payload, dict):
            transport_drafts.append(payload)
    return {
        "students": students,
        "student_transfers": student_transfers,
        "student_deletions": student_deletions,
        "payments": payments,
        "expenses": expenses,
        "club_drafts": club_drafts,
        "grade_contact_drafts": grade_contact_drafts,
        "balance_drafts": balance_drafts,
        "meal_drafts": meal_drafts,
        "transport_drafts": transport_drafts,
    }


def upsert_pending_bulk_draft(conn, draft_type, draft, *, do_commit=True):
    """Persist a club/grade/balance/meal/transport bulk draft (dict with id, kind, label, payload)."""
    draft_id = str(draft.get("id") or "")
    if not draft_id:
        raise ValueError("bulk draft requires an id")
    conn.execute(
        """
        INSERT INTO pending_reviews (draft_type, draft_key, payload_json, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(draft_type, draft_key) DO UPDATE SET
            payload_json = excluded.payload_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (draft_type, draft_id, _pending_review_json_dumps(draft)),
    )
    if do_commit:
        conn.commit()


def delete_pending_bulk_draft(conn, draft_type, draft_id, *, do_commit=True):
    conn.execute(
        "DELETE FROM pending_reviews WHERE draft_type=? AND draft_key=?",
        (draft_type, str(draft_id)),
    )
    if do_commit:
        conn.commit()


def upsert_pending_student_review(conn, student_id, payload, *, do_commit=True):
    sid = int(student_id)
    conn.execute(
        """
        INSERT INTO pending_reviews (draft_type, draft_key, payload_json, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(draft_type, draft_key) DO UPDATE SET
            payload_json = excluded.payload_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (DRAFT_TYPE_STUDENT, str(sid), _pending_review_json_dumps(payload)),
    )
    if do_commit:
        conn.commit()


def delete_pending_student_review(conn, student_id, *, do_commit=True):
    conn.execute(
        "DELETE FROM pending_reviews WHERE draft_type=? AND draft_key=?",
        (DRAFT_TYPE_STUDENT, str(int(student_id))),
    )
    if do_commit:
        conn.commit()


def upsert_pending_student_transfer_review(conn, student_id, payload, *, do_commit=True):
    sid = int(student_id)
    conn.execute(
        """
        INSERT INTO pending_reviews (draft_type, draft_key, payload_json, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(draft_type, draft_key) DO UPDATE SET
            payload_json = excluded.payload_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (DRAFT_TYPE_STUDENT_TRANSFER, str(sid), _pending_review_json_dumps(payload)),
    )
    if do_commit:
        conn.commit()


def delete_pending_student_transfer_review(conn, student_id, *, do_commit=True):
    conn.execute(
        "DELETE FROM pending_reviews WHERE draft_type=? AND draft_key=?",
        (DRAFT_TYPE_STUDENT_TRANSFER, str(int(student_id))),
    )
    if do_commit:
        conn.commit()


def upsert_pending_student_deletion_review(conn, student_id, payload, *, do_commit=True):
    sid = int(student_id)
    conn.execute(
        """
        INSERT INTO pending_reviews (draft_type, draft_key, payload_json, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(draft_type, draft_key) DO UPDATE SET
            payload_json = excluded.payload_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (DRAFT_TYPE_STUDENT_DELETION, str(sid), _pending_review_json_dumps(payload)),
    )
    if do_commit:
        conn.commit()


def delete_pending_student_deletion_review(conn, student_id, *, do_commit=True):
    conn.execute(
        "DELETE FROM pending_reviews WHERE draft_type=? AND draft_key=?",
        (DRAFT_TYPE_STUDENT_DELETION, str(int(student_id))),
    )
    if do_commit:
        conn.commit()


def insert_pending_manual_payment_review(conn, draft, *, do_commit=True):
    draft_id = str(draft.get("id") or "")
    if not draft_id:
        raise ValueError("manual payment draft requires an id")
    conn.execute(
        """
        INSERT INTO pending_reviews (draft_type, draft_key, payload_json, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(draft_type, draft_key) DO UPDATE SET
            payload_json = excluded.payload_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (DRAFT_TYPE_MANUAL_PAYMENT, draft_id, _pending_review_json_dumps(draft)),
    )
    if do_commit:
        conn.commit()


def delete_pending_manual_payment_review(conn, draft_id, *, do_commit=True):
    conn.execute(
        "DELETE FROM pending_reviews WHERE draft_type=? AND draft_key=?",
        (DRAFT_TYPE_MANUAL_PAYMENT, str(draft_id)),
    )
    if do_commit:
        conn.commit()


def insert_pending_expense_review(conn, draft, *, do_commit=True):
    draft_id = str(draft.get("id") or "")
    if not draft_id:
        raise ValueError("expense draft requires an id")
    conn.execute(
        """
        INSERT INTO pending_reviews (draft_type, draft_key, payload_json, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(draft_type, draft_key) DO UPDATE SET
            payload_json = excluded.payload_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (DRAFT_TYPE_EXPENSE, draft_id, _pending_review_json_dumps(draft)),
    )
    if do_commit:
        conn.commit()


def delete_pending_expense_review(conn, draft_id, *, do_commit=True):
    conn.execute(
        "DELETE FROM pending_reviews WHERE draft_type=? AND draft_key=?",
        (DRAFT_TYPE_EXPENSE, str(draft_id)),
    )
    if do_commit:
        conn.commit()