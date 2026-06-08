#!/usr/bin/env python3
"""
Signal Bot Dashboard — διαβάζει από αρχεία που γράφει το bot_worker.py
"""
import json
import os
import subprocess
import sys
import time
import streamlit as st

st.set_page_config(page_title="MEXC AI Signal Bot", page_icon="📈", layout="wide")

# ── Auto-launch worker με το ίδιο Python (venv-safe) ───────
_PID_FILE = "/tmp/worker_pid"

def _worker_alive():
    if not os.path.exists(_PID_FILE):
        return False
    try:
        pid = int(open(_PID_FILE).read().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        return False

if not _worker_alive():
    worker_path = os.path.join(os.path.dirname(__file__), "bot_worker.py")
    proc = subprocess.Popen([sys.executable, worker_path])
    with open(_PID_FILE, "w") as f:
        f.write(str(proc.pid))

LOG_FILE       = "/data/bot_log.txt"
TRADES_FILE    = "/data/bot_trades.json"
OPEN_FILE      = "/data/bot_open_trades.json"
CONFIG_FILE    = "/data/bot_config.json"
STOP_FILE      = "/tmp/bot_stop"

ALL_PAIRS = ["NEAR_USDT","BTC_USDT","ETH_USDT","SOL_USDT",
             "BNB_USDT","XRP_USDT","DOGE_USDT","ADA_USDT"]

def bot_running():
    return not os.path.exists(STOP_FILE)

def read_log():
    if not os.path.exists(LOG_FILE): return []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        return f.readlines()

def read_trades():
    if not os.path.exists(TRADES_FILE): return []
    try:
        with open(TRADES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def read_open_trades():
    if not os.path.exists(OPEN_FILE): return []
    try:
        with open(OPEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"pairs": ALL_PAIRS, "interval": 60, "tp_pct": 1.5, "sl_pct": 1.0}

cfg = load_config()

# ── Sidebar ────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ Ρυθμίσεις")
    st.caption(f"Token: {'✅ Set' if os.environ.get('TELEGRAM_TOKEN') else '❌ Missing'}")
    st.caption(f"Chat:  {'✅ Set' if os.environ.get('TELEGRAM_CHAT')  else '❌ Missing'}")
    st.divider()
    sel_pairs = st.multiselect("Ζεύγη", options=ALL_PAIRS,
                                default=cfg.get("pairs", ALL_PAIRS))
    interval  = st.number_input("Interval (δευτ.)", value=cfg.get("interval",60),
                                 min_value=10, step=10)
    tp_pct    = st.number_input("Take Profit %",    value=cfg.get("tp_pct",1.5),
                                 min_value=0.1, step=0.1, format="%.1f")
    sl_pct    = st.number_input("Stop Loss %",      value=cfg.get("sl_pct",1.0),
                                 min_value=0.1, step=0.1, format="%.1f")
    if st.button("💾 Αποθήκευση ρυθμίσεων", use_container_width=True):
        save_config({"pairs":sel_pairs,"interval":interval,
                     "tp_pct":tp_pct,"sl_pct":sl_pct})
        st.success("Αποθηκεύτηκε!")

# ── Header ─────────────────────────────────────────────────
st.title("📈 MEXC AI Signal Bot")
running = bot_running()
c1, c2, c3 = st.columns(3)
c1.metric("Κατάσταση",  "🟢 Τρέχει" if running else "🔴 Παύση")
c2.metric("Take Profit", f"+{tp_pct}%")
c3.metric("Stop Loss",   f"-{sl_pct}%")

b1, b2, _ = st.columns([1, 1, 6])
if b1.button("▶ Εκκίνηση", type="primary", disabled=running, use_container_width=True):
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)
    st.rerun()

if b2.button("⏹ Παύση", disabled=not running, use_container_width=True):
    open(STOP_FILE, "w").close()
    st.rerun()

# ── Tabs ───────────────────────────────────────────────────
tab_log, tab_trades = st.tabs(["📋 Live Log", "📊 Paper Trades"])

with tab_log:
    lines = read_log()
    st.text_area("Log output",
                 value="".join(reversed(lines[-100:])),
                 height=440, label_visibility="hidden")

with tab_trades:
    open_trades = read_open_trades()
    if open_trades:
        st.subheader("🔓 Ανοικτές Θέσεις")
        now_ts = time.time()
        open_rows = []
        for ot in open_trades:
            el = (now_ts - ot["time"]) / 60
            open_rows.append({
                "Ζεύγος": ot["pair"],
                "Κατ/νση": ot["direction"],
                "Είσοδος": f"{ot['entry']:.4f}",
                "Σκορ AI": ot.get("score", "—"),
                "Διάρκεια": f"{el:.0f}λ",
                "Κατάσταση": "🔓 Ανοικτό",
            })
        st.dataframe(open_rows, use_container_width=True, hide_index=True)
        st.divider()

    trades = read_trades()
    st.subheader("📋 Κλειστές Θέσεις")
    if trades:
        st.dataframe(trades, use_container_width=True, hide_index=True)
        wins = sum(1 for t in trades if "KERDOS" in t.get("Αποτ/μα",""))
        ca, cb, cc = st.columns(3)
        ca.metric("Σύνολο",   len(trades))
        cb.metric("Κέρδη ✅",  wins)
        cc.metric("Ζημίες ❌", len(trades)-wins)

        csv_lines = []
        if trades:
            cols = list(trades[0].keys())
            csv_lines.append(",".join(cols))
            for t in trades:
                csv_lines.append(",".join(str(t.get(c, "")) for c in cols))
        st.download_button(
            "⬇️ Λήψη ιστορικού paper trading (CSV)",
            data="\n".join(csv_lines),
            file_name="paper_trades.csv",
            mime="text/csv",
            use_container_width=True,
        )
    else:
        st.info("Δεν υπάρχουν κλειστά trades ακόμα.")

# ── Auto-refresh κάθε 10 δευτ. ────────────────────────────
time.sleep(10)
st.rerun()
