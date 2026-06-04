"""
Global app gate: five operator accounts (user1–user5) with env-only passwords,
idle timeout, and audit logging (see database.log_gate_event).
"""

from __future__ import annotations

import os
import time
from datetime import timedelta
from pathlib import Path

import streamlit as st

from database import log_gate_event

_GATE_DIR = Path(__file__).resolve().parent
_LOGO_DARK = _GATE_DIR / "Vinegrape logo white background.webp"
_LOGO_LIGHT = _GATE_DIR / "Vinegrape_Academy_Logo-removebg-preview.png"


def _render_gate_page_header(*, subtitle: str) -> None:
    """Logo + title row (matches main app header; CSS is already injected by app.py)."""
    theme = st.session_state.get("theme_selector", "Dark")
    logo_path = _LOGO_DARK if theme == "Dark" else _LOGO_LIGHT
    col_logo, col_title = st.columns([1, 8])
    with col_logo:
        if logo_path.is_file():
            st.image(str(logo_path), width=80)
    with col_title:
        st.markdown(
            '<h1 class="main-header" style="text-align: left; padding: 0; margin: 0;">VineLedger</h1>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<p class="sub-header" style="text-align: left; margin: 0;">{subtitle}</p>',
            unsafe_allow_html=True,
        )

GATE_USER_SLUGS = ("user1", "user2", "user3", "user4", "user5")

_GATE_ENV_KEYS = {
    "user1": "VINELEDGER_GATE_USER1_PASSWORD",
    "user2": "VINELEDGER_GATE_USER2_PASSWORD",
    "user3": "VINELEDGER_GATE_USER3_PASSWORD",
    "user4": "VINELEDGER_GATE_USER4_PASSWORD",
    "user5": "VINELEDGER_GATE_USER5_PASSWORD",
}


def gate_idle_seconds() -> int:
    raw = os.environ.get("VINELEDGER_GATE_IDLE_SECONDS", "900")
    try:
        n = int(raw)
    except ValueError:
        return 900
    return max(60, n)


def load_gate_password_map() -> tuple[dict[str, str], list[str]]:
    """Return (slug -> password, list of missing env var names). All five must be set."""
    passwords: dict[str, str] = {}
    missing: list[str] = []
    for slug in GATE_USER_SLUGS:
        key = _GATE_ENV_KEYS[slug]
        val = os.environ.get(key)
        if val is None or str(val).strip() == "":
            missing.append(key)
        else:
            passwords[slug] = str(val)
    return passwords, missing


def clear_gate_session() -> None:
    for k in (
        "gate_user",
        "gate_last_activity_ts",
        "gate_login_ts",
        "protected_tabs_unlocked",
        "protected_tabs_unlock_gate_user",
        "authenticated",
        "password_entered",
        "bank_statement_upload_authorized",
    ):
        st.session_state.pop(k, None)


def touch_gate_activity() -> None:
    """Call once per full app run after the user passed the gate (any interaction / rerun)."""
    if st.session_state.get("gate_user"):
        st.session_state.gate_last_activity_ts = time.time()


@st.fragment(run_every=timedelta(seconds=60))
def gate_idle_watch_fragment() -> None:
    """Periodic idle check; does not refresh activity (only full script runs do)."""
    from database import log_gate_event_ephemeral

    slug = st.session_state.get("gate_user")
    if not slug:
        return
    last = st.session_state.get("gate_last_activity_ts")
    if last is None:
        return
    if time.time() - float(last) >= gate_idle_seconds():
        log_gate_event_ephemeral(str(slug), "idle_timeout", None)
        clear_gate_session()
        st.rerun()


def render_global_gate(conn) -> None:
    """
    Require gate login before the rest of the app. Uses st.stop() until authenticated.
    When authenticated, mounts the idle fragment and returns (caller runs main UI).
    """
    passwords, missing = load_gate_password_map()
    if missing:
        _render_gate_page_header(
            subtitle="Sign in is unavailable until gate passwords are configured.",
        )
        st.error("**Gate not configured.** Set all of the following environment variables (non-empty):")
        for m in missing:
            st.code(m, language="text")
        st.caption("Restart the app after setting variables. No passwords are stored in the repository.")
        st.stop()

    gate_user = st.session_state.get("gate_user")
    idle_sec = gate_idle_seconds()

    if gate_user:
        last = st.session_state.get("gate_last_activity_ts")
        if last is not None and time.time() - float(last) >= idle_sec:
            _slug = str(gate_user)
            log_gate_event(conn, _slug, "idle_timeout", None)
            clear_gate_session()
            st.warning("Session timed out after a period of inactivity. Please sign in again.")
            st.rerun()

        gate_idle_watch_fragment()
        return

    _gate_flash_err = st.session_state.pop("_vine_app_flash_error", None)
    if _gate_flash_err:
        st.error(_gate_flash_err)
    _gate_flash_warn = st.session_state.pop("_vine_app_flash_warn", None)
    if _gate_flash_warn:
        st.warning(_gate_flash_warn)

    _render_gate_page_header(
        subtitle="Sign in to continue — sessions time out after idle periods.",
    )
    st.caption(
        "Enter your operator account name and password."
    )
    col1, col2 = st.columns([1, 1])
    with col1:
        raw_account = st.text_input(
            "Account",
            placeholder="e.g. user1",
            key="gate_login_account",
            help="Type your account slug (user1 … user5). Not a dropdown.",
        )
    with col2:
        pw = st.text_input("Password", type="password", key="gate_login_password")

    if st.button("Sign in", type="primary", key="gate_login_submit"):
        raw = (raw_account or "").strip().lower()
        slug = None
        for s in GATE_USER_SLUGS:
            if raw == s.lower():
                slug = s
                break
        if slug is None:
            st.session_state["_vine_app_flash_error"] = (
                "Unknown account. Enter **user1** through **user5** (the exact slug, any case)."
            )
            if raw:
                log_gate_event(conn, "unknown", "login_failed", f"unknown_account:{raw[:80]}")
            st.session_state.pop("gate_login_password", None)
            st.rerun()
        else:
            expected = passwords.get(str(slug), "")
            if pw == expected:
                st.session_state.gate_user = str(slug)
                st.session_state.gate_login_ts = time.time()
                st.session_state.gate_last_activity_ts = time.time()
                log_gate_event(conn, str(slug), "login", None)
                st.session_state.pop("gate_login_password", None)
                st.session_state.pop("gate_login_account", None)
                st.rerun()
            else:
                log_gate_event(conn, str(slug), "login_failed", None)
                st.session_state["_vine_app_flash_error"] = "Incorrect password."
                st.session_state.pop("gate_login_password", None)
                st.rerun()

    st.stop()
