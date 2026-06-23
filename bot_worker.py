#!/usr/bin/env python3
"""
MEXC AI Signal Bot — worker
Συνδυάζει το AI scoring/μνήμη engine (v15: scoring, regime classification,
KDJ/MACD/EMA confirmation, SQLite pattern-learning) με πραγματικό paper trading
(άνοιγμα/κλείσιμο εικονικών θέσεων με TP/SL — όπως στο παλιό bot).
Τρέχει 24/7 ανεξάρτητα· γράφει log/trades σε αρχεία που διαβάζει το app.py.
"""
import json, time, threading, statistics, os, sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.request import urlopen, Request
from collections import deque

TZ = ZoneInfo("Europe/Athens")

def now_str():
    return datetime.now(TZ).strftime("%H:%M:%S")

def today_str():
    return datetime.now(TZ).strftime("%Y-%m-%d")

def fmt_price(p):
    """Δυναμικά δεκαδικά ψηφία ώστε τιμές πολύ μικρές (π.χ. PEPE ~0.00001234) να μην εμφανίζονται ως 0.0000."""
    p = float(p)
    if p == 0: return "0.0000"
    if p >= 1: return f"{p:.4f}"
    if p >= 0.01: return f"{p:.6f}"
    return f"{p:.8f}"

BASE = "https://contract.mexc.com"

DATA_DIR    = "/data"
os.makedirs(DATA_DIR, exist_ok=True)

LOG_FILE    = "/data/bot_log.txt"
TRADES_FILE = "/data/bot_trades.json"
OPEN_FILE   = "/data/bot_open_trades.json"
CONFIG_FILE = "/data/bot_config.json"
STOP_FILE   = "/tmp/bot_stop"        # ephemeral: always starts running after restart
RESET_FILE  = "/tmp/bot_reset"
CLEAR_TRADES_FILE = "/tmp/bot_clear_trades"
MEMORY_DB   = "/data/mexc_memory.db"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT", "").strip()

DEFAULT_CONFIG = {
    "pairs":    ["NEAR_USDT","BTC_USDT","ETH_USDT","SOL_USDT",
                 "BNB_USDT","XRP_USDT","DOGE_USDT","ADA_USDT"],  # default 8 — χρήστης μπορεί να αλλάξει από UI
    "interval": 60,
    "tp_pct":   1.5,
    "sl_pct":   1.0,
}
SENTIMENT_PAIRS   = ["SOL_USDT","ETH_USDT","XRP_USDT","NEAR_USDT"]
TREND_WATCH_PAIRS = ["INJ_USDT","NEAR_USDT","ATOM_USDT","BNB_USDT"]

# ── Δείκτες/καταστάσεις ανά ζεύγος ─────────────────────────
price_history={}; cache={}; latch={}; neg_count={}; pos_count={}; hv_history={}
absorption_alert={}; failed_move={}
trend15={}; trend1h={}; trend4h={}; bias_state={}
sentiment_cache={}

LATCH_SEC=1200; WIN=0.7; VOL_MIN=2.0; DELTA_MIN=40
INTERVAL_1H="Min60"
INTERVAL_4H="Hour4"

# ── Paper trading ──────────────────────────────────────────
paper_trades = {}        # pair -> {direction, entry, entry_ts, sig_id, score, pattern_key}
pair_last_selection = {}

# ── Circuit breaker: σταματά νέα trades μετά από σερί ζημιών ──
LOSS_STREAK_LIMIT = 6
COOLDOWN_MINUTES = 45
consecutive_losses = 0
cooldown_until = 0.0

DATA_LOCK = threading.RLock()
LOG_LOCK  = threading.RLock()
DB_LOCK   = threading.RLock()

SCORE_MIN = 90
MAX_SELECTIONS_PER_DAY = 999
SELECTION_COOLDOWN_SEC = 5 * 60
DATA_Q_MIN_CNT_2M = 10
DATA_Q_MAX_VOL_RATIO = 55.0
DATA_Q_EXTREME_DELTA = 95.0
DATA_Q_EXTREME_MIN_CNT = 15

# ── Log / config / αρχεία dashboard ────────────────────────
def add_log(text):
    line = f"[{now_str()}]  {text}\n"
    with LOG_LOCK:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > 600:
                with open(LOG_FILE, "w", encoding="utf-8") as f:
                    f.writelines(lines[-600:])
        except Exception:
            pass

def read_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg

def load_trades():
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_trades(trades):
    with open(TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False)

def save_open_trades():
    data = []
    for pair, pt in paper_trades.items():
        data.append({
            "pair": pair, "direction": pt["direction"], "entry": pt["entry"],
            "time": pt["entry_ts"], "sig_id": pt.get("sig_id",""),
            "score": pt.get("score"), "pattern_key": pt.get("pattern_key",""),
        })
    with open(OPEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def restore_open_trades():
    if not os.path.exists(OPEN_FILE):
        return
    try:
        with open(OPEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for ot in data:
            pair = ot["pair"]
            paper_trades[pair] = {
                "direction": ot["direction"], "entry": ot["entry"], "entry_ts": ot["time"],
                "sig_id": ot.get("sig_id",""), "score": ot.get("score"),
                "pattern_key": ot.get("pattern_key",""),
            }
            pair_last_selection[pair] = ot["time"]
        if paper_trades:
            add_log(f"↩️ Αποκατάσταση {len(paper_trades)} ανοικτών paper trades από αρχείο")
    except Exception as e:
        add_log(f"ERR restore_open_trades: {e}")

# ── SQLite "μνήμη" — μαθαίνει win-rate ανά pattern ─────────
def init_db():
    with DB_LOCK:
        con = sqlite3.connect(MEMORY_DB, timeout=10, check_same_thread=False)
        try:
            con.execute("PRAGMA journal_mode=WAL;"); con.execute("PRAGMA synchronous=NORMAL;")
            con.execute("""CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, pair TEXT NOT NULL,
                price REAL, delta_2m REAL, delta_30s REAL, count_2m INTEGER, vol_ratio REAL,
                absorption TEXT, trend5m TEXT, trend15m TEXT, trend1h TEXT, trend4h TEXT,
                market TEXT, raw_signal TEXT, direction TEXT, score INTEGER,
                selected INTEGER DEFAULT 0, pattern_key TEXT, ai_label TEXT);""")
            con.execute("""CREATE TABLE IF NOT EXISTS outcomes (
                sig_id TEXT PRIMARY KEY, entry_ts INTEGER NOT NULL, pair TEXT NOT NULL,
                direction TEXT NOT NULL, entry_price REAL NOT NULL, score INTEGER,
                pattern_key TEXT, result_pct REAL, result_label TEXT);""")
            con.execute("""CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);""")
            con.commit()
        finally:
            con.close()

def db_set_meta(k, v):
    with DB_LOCK:
        con = sqlite3.connect(MEMORY_DB, timeout=10, check_same_thread=False)
        try:
            con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
            con.commit()
        finally:
            con.close()

def db_get_meta(k, default=""):
    with DB_LOCK:
        con = sqlite3.connect(MEMORY_DB, timeout=10, check_same_thread=False)
        try:
            cur = con.execute("SELECT v FROM meta WHERE k=?", (k,))
            row = cur.fetchone()
            return row[0] if row and row[0] is not None else default
        finally:
            con.close()

def clamp(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)

def bucket(val, step, lo=-999, hi=999):
    try:
        v = float(val)
    except Exception:
        v = 0.0
    v = clamp(v, lo, hi)
    return int(round(v/step)*step)

def make_pattern_key(d):
    ab = d.get("absorption","NONE"); t5 = d.get("trend5m","?"); t15 = d.get("trend15m","?")
    t1 = d.get("trend1h","?"); t4 = d.get("trend4h","?"); direction = d.get("direction","")
    d2 = bucket(d.get("delta_2m",0),10,-200,200); d30 = bucket(d.get("delta_30s",0),10,-200,200)
    vr = bucket(d.get("vol_ratio",1.0),1,0,50)
    bias = d.get("bias_dir","NEUTRAL")
    # Το bias (ανοδική/πτωτική φάση αγοράς) μπαίνει στο κλειδί ώστε οι στατιστικές
    # win-rate ενός pattern να μη μπερδεύονται ανάμεσα σε διαφορετικές φάσεις αγοράς —
    # ένα pattern που κέρδιζε σε ανοδική φάση δεν πρέπει να "δανείζει" εμπιστοσύνη
    # σε πτωτική φάση και αντίστροφα.
    return f"{ab}|{direction}|bias={bias}|d2={d2}|d30={d30}|vr={vr}|5={t5}|15={t15}|1h={t1}|4h={t4}"

def infer_trade_direction(d):
    dr = d.get("direction") or ""
    if dr in ("LONG","SHORT"):
        return dr
    rs = d.get("raw_signal","")
    if rs == "ANODOS": return "LONG"
    if rs == "PTWSH":  return "SHORT"
    ab = d.get("absorption","NONE")
    if ab in ("TREND_UP","BUY_STRONG","BUY"):     return "LONG"
    if ab in ("TREND_DOWN","SELL_STRONG","SELL"): return "SHORT"
    return ""

def trend_matches(trend, direction):
    if trend == "NEUTRAL" or not direction:
        return 0
    if direction == "LONG":
        return 1 if trend == "UP" else (-1 if trend == "DOWN" else 0)
    if direction == "SHORT":
        return 1 if trend == "DOWN" else (-1 if trend == "UP" else 0)
    return 0

def pattern_stats(pattern_key):
    with DB_LOCK:
        con = sqlite3.connect(MEMORY_DB, timeout=10, check_same_thread=False)
        try:
            cur = con.execute("SELECT COUNT(*) AS n, SUM(CASE WHEN result_label='WIN' THEN 1 ELSE 0 END) AS wins "
                              "FROM outcomes WHERE pattern_key=? AND result_label IS NOT NULL", (pattern_key,))
            row = cur.fetchone() or (0, 0)
            n = int(row[0] or 0); wins = int(row[1] or 0)
            if n <= 0: return 0, 0.5
            return n, (wins+1)/(n+2)
        finally:
            con.close()

def record_observation(d, score, pattern_key, selected=0):
    ts = int(time.time())
    with DB_LOCK:
        con = sqlite3.connect(MEMORY_DB, timeout=10, check_same_thread=False)
        try:
            con.execute(
                "INSERT INTO observations(ts,pair,price,delta_2m,delta_30s,count_2m,vol_ratio,absorption,"
                "trend5m,trend15m,trend1h,trend4h,market,raw_signal,direction,score,selected,pattern_key,ai_label) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, d.get("pair"), d.get("price"), d.get("delta_2m"), d.get("delta_30s"),
                 d.get("count_2m"), d.get("vol_ratio"), d.get("absorption"), d.get("trend5m"),
                 d.get("trend15m"), d.get("trend1h"), d.get("trend4h"), d.get("market_sentiment"),
                 d.get("raw_signal"), d.get("direction"), int(score), int(selected), pattern_key,
                 d.get("ai_label","")))
            con.commit()
        finally:
            con.close()

def record_outcome(sig_id, entry_ts, pair, direction, entry_price, score, pattern_key, result_pct, result_label):
    with DB_LOCK:
        con = sqlite3.connect(MEMORY_DB, timeout=10, check_same_thread=False)
        try:
            con.execute(
                "INSERT INTO outcomes(sig_id,entry_ts,pair,direction,entry_price,score,pattern_key,result_pct,result_label) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(sig_id) DO UPDATE SET "
                "result_pct=excluded.result_pct, result_label=excluded.result_label",
                (sig_id, int(entry_ts), pair, direction, float(entry_price),
                 int(score) if score is not None else None, pattern_key,
                 float(result_pct) if result_pct is not None else None, result_label))
            con.commit()
        finally:
            con.close()

def selection_gate(pair=""):
    now = time.time(); today = today_str()
    last_day = db_get_meta("last_trade_day",""); last_count = int(db_get_meta("last_trade_count","0") or 0)
    if last_day != today: last_count = 0
    if last_count >= MAX_SELECTIONS_PER_DAY: return False, "daily"
    if pair:
        last_ts = pair_last_selection.get(pair, 0)
        if last_ts > 0 and (now - last_ts) < SELECTION_COOLDOWN_SEC:
            rem = int(SELECTION_COOLDOWN_SEC - (now - last_ts))
            return False, f"cooldown {rem//60}m{rem%60:02d}s"
    return True, ""

def mark_selected_today(sig_id, pair, score):
    today = today_str()
    last_day = db_get_meta("last_trade_day",""); last_count = int(db_get_meta("last_trade_count","0") or 0)
    if last_day != today: last_count = 0
    last_count += 1
    db_set_meta("last_trade_day", today); db_set_meta("last_trade_count", str(last_count))
    db_set_meta("last_selection_ts", str(int(time.time())))
    pair_last_selection[pair] = int(time.time())
    db_set_meta("last_sig_id", sig_id); db_set_meta("last_pair", pair); db_set_meta("last_score", str(score))

# ── AI regime classification & scoring ─────────────────────
def ai_classify(d):
    want = infer_trade_direction(d)
    t5 = d.get("trend5m","NEUTRAL"); t15 = d.get("trend15m","NEUTRAL")
    t1 = d.get("trend1h","NEUTRAL"); t4 = d.get("trend4h","NEUTRAL")
    ab = d.get("absorption","NONE")
    d2m = float(d.get("delta_2m",0) or 0); d30 = float(d.get("delta_30s",0) or 0)
    vr = float(d.get("vol_ratio",1.0) or 1.0)
    pc = float(d.get("price_change_2m",0) or 0)
    comp = float(d.get("compression",1.0) or 1.0)
    accel = bool(d.get("accelerating", False))
    mu = bool(d.get("moving_up", False)); md = bool(d.get("moving_down", False))
    flat = bool(d.get("flat", False))
    extreme_delta = abs(d2m) >= 90 or abs(d30) >= 90
    huge_vol = vr >= 10; vol_spike = vr >= 8
    micro_conflict = t5 != "NEUTRAL" and t15 != "NEUTRAL" and t5 != t15
    structure_alignment = 0
    if want:
        m5, m15 = trend_matches(t5, want), trend_matches(t15, want)
        if m5 >= 0 and m15 >= 0 and (m5+m15) >= 2: structure_alignment = 20
        elif m15 == 1 and m5 >= 0: structure_alignment = 14
        elif micro_conflict: structure_alignment = 4
        elif m15 == 1 or m5 == 1: structure_alignment = 10
        else: structure_alignment = 5
    mtf_alignment = 11
    if want:
        h4 = trend_matches(t4, want)
        if h4 == 1: mtf_alignment += 6
        elif h4 == -1: mtf_alignment -= 6
        h1 = trend_matches(t1, want)
        if h1 == 1: mtf_alignment += 8
        elif h1 == -1: mtf_alignment -= 8
    mtf_alignment = int(clamp(mtf_alignment, 0, 22))
    volatility_quality = 8
    if VOL_MIN <= vr <= 8: volatility_quality = 15
    elif 8 < vr <= 14: volatility_quality = 11
    elif vr > 14: volatility_quality = 4
    elif vr < 1.0: volatility_quality = 6
    delta_sustainability = 8
    if want:
        same_sign = (d2m >= 0 and d30 >= 0) or (d2m <= 0 and d30 <= 0)
        r = abs(d30) / (abs(d2m) + 1e-6)
        if same_sign and abs(d2m) >= 25:
            if 0.08 <= r <= 1.25: delta_sustainability = 16
            elif r < 0.08 and extreme_delta: delta_sustainability = 3
            else: delta_sustainability = 10
        elif same_sign:
            delta_sustainability = 11
    continuation_after_spike = 0
    if want == "LONG":
        if ab == "TREND_UP" and mu and trend_matches(t5, want) == 1 and trend_matches(t15, want) == 1:
            continuation_after_spike = 14
        elif ab in ("BUY_STRONG","BUY") and flat and trend_matches(t15, want) == 1:
            continuation_after_spike = 11 + (3 if accel else 0)
    elif want == "SHORT":
        if ab == "TREND_DOWN" and md and trend_matches(t5, want) == 1 and trend_matches(t15, want) == 1:
            continuation_after_spike = 14
        elif ab in ("SELL_STRONG","SELL") and flat and trend_matches(t15, want) == 1:
            continuation_after_spike = 11 + (3 if accel else 0)
    label = "CONTINUATION"
    htf_against = (want == "LONG" and t4 == "DOWN") or (want == "SHORT" and t4 == "UP")
    if extreme_delta and huge_vol and (micro_conflict or htf_against or comp <= 0.55):
        label = "LIQUIDATION_SPIKE"
    elif micro_conflict:
        label = "FAKE_BREAKOUT"
    elif extreme_delta and (comp <= 0.55 or (vol_spike and not continuation_after_spike)):
        label = "EXHAUSTION"
    elif abs(pc) >= 0.85 and vol_spike:
        label = "LATE_ENTRY"
    elif continuation_after_spike >= 12 and not extreme_delta:
        label = "CONTINUATION"
    elif ab == "NONE" or not want:
        label = "NO_SETUP"
    penalties = []
    if label == "LIQUIDATION_SPIKE": penalties.append(("regime_liq", 28))
    elif label == "FAKE_BREAKOUT": penalties.append(("regime_fake", 18))
    elif label == "EXHAUSTION": penalties.append(("regime_exhaust", 22))
    elif label == "LATE_ENTRY": penalties.append(("regime_late", 14))
    elif label == "NO_SETUP": penalties.append(("regime_none", 8))
    trend_aligned = (t5 in ("UP","DOWN") and t15 in ("UP","DOWN") and t5 == t15)
    structure_ok = ((ab in ("TREND_UP","TREND_DOWN") and trend_aligned) or
                    (ab in ("BUY_STRONG","SELL_STRONG","BUY","SELL") and t15 in ("UP","DOWN")))
    compression_exhaustion = (comp <= 0.55)
    unstable_event = (extreme_delta and vr >= 10 and (not structure_ok or not trend_aligned or compression_exhaustion))
    if unstable_event:
        penalties.append(("unstable_spike", 22))
    elif extreme_delta and vr >= 6 and not trend_aligned:
        penalties.append(("delta_structure", 10))
    return {"label": label, "want": want, "structure_alignment": structure_alignment,
            "mtf_alignment": mtf_alignment, "volatility_quality": volatility_quality,
            "delta_sustainability": delta_sustainability,
            "continuation_after_spike": continuation_after_spike, "penalties": penalties}

def score_pair(d):
    ac = ai_classify(d); ab = d.get("absorption","NONE"); raw = d.get("raw_signal","NEUTRAL")
    direction = d.get("direction",""); cnt = int(d.get("count_2m",0) or 0)
    if ab in ("TREND_UP","TREND_DOWN"): absorption_base = 38
    elif ab in ("BUY_STRONG","SELL_STRONG"): absorption_base = 30
    elif ab in ("BUY","SELL"): absorption_base = 24
    else: absorption_base = 0
    activity_bonus = 4 if cnt >= 10 else 0
    latch_bonus = 8 if raw in ("ANODOS","PTWSH") else 0
    ob = float(d.get("ob_ratio",1.0) or 1.0); fr = float(d.get("funding_rate",0) or 0)
    fp = float(d.get("fair_price",0) or 0); lp = float(d.get("price",0) or 0)
    want = infer_trade_direction(d)
    ob_bonus = 0
    bid_wall = float(d.get("ob_bid_wall",0) or 0); ask_wall = float(d.get("ob_ask_wall",0) or 0)
    if want == "SHORT":
        if ob >= 2.0: ob_bonus = 8
        elif ob >= 1.5: ob_bonus = 5
        if ask_wall >= 40: ob_bonus += 5
    elif want == "LONG":
        if ob <= 0.5: ob_bonus = 8
        elif ob <= 0.7: ob_bonus = 5
        if bid_wall >= 40: ob_bonus += 5
    fr_bonus = 0
    if want == "SHORT" and fr > 0: fr_bonus = 6
    elif want == "LONG" and fr < 0: fr_bonus = 6
    hv_change = float(d.get("hv_change",0) or 0)
    hv_bonus = 6 if (want == "SHORT" and hv_change > 0.1) or (want == "LONG" and hv_change > 0.1) else (-4 if hv_change < -0.1 else 0)
    fp_bonus = 0
    if fp > 0 and lp > 0:
        diff = (lp-fp)/fp*100
        if want == "SHORT" and diff > 0.1: fp_bonus = 6
        elif want == "LONG" and diff < -0.1: fp_bonus = 6
    subtotal = (absorption_base + latch_bonus + activity_bonus +
                int(ac["structure_alignment"]) + int(ac["mtf_alignment"]) +
                int(ac["volatility_quality"]) + int(ac["delta_sustainability"]) +
                int(ac["continuation_after_spike"]) + ob_bonus + fr_bonus + fp_bonus + hv_bonus)
    bias_dir = bias_state.get(d.get("pair",""), {}).get("direction","NEUTRAL")
    pen_sum = sum(int(p) for _, p in ac.get("penalties", []))
    base = int(subtotal - pen_sum)
    d_for_pattern = dict(d); d_for_pattern["direction"] = direction; d_for_pattern["bias_dir"] = bias_dir
    pkey = make_pattern_key(d_for_pattern); n, wr = pattern_stats(pkey)
    evidence = min(1.0, n/15.0); adj = int(round((wr-0.5)*30*evidence))
    score = int(clamp(base+adj, 0, 100))
    dbg = {"base": base, "adj": adj, "n": n, "wr": wr, "ai": ac, "absorption_base": absorption_base,
           "subtotal": subtotal, "pen_sum": pen_sum}
    vr = float(d.get("vol_ratio",1.0) or 1.0); ad2 = abs(float(d.get("delta_2m",0) or 0))
    cap_hi = 100; dq_notes = []
    if cnt < DATA_Q_MIN_CNT_2M: cap_hi = min(cap_hi,72); dq_notes.append(f"cnt2m<{DATA_Q_MIN_CNT_2M}")
    if vr > DATA_Q_MAX_VOL_RATIO: cap_hi = min(cap_hi,82); dq_notes.append(f"vr>{DATA_Q_MAX_VOL_RATIO:g}")
    if ad2 >= DATA_Q_EXTREME_DELTA and cnt < DATA_Q_EXTREME_MIN_CNT: cap_hi = min(cap_hi,75); dq_notes.append("extΔ+lowCnt")
    score = int(min(score, cap_hi)); dbg["dq_cap"] = cap_hi if dq_notes else None; dbg["dq_notes"] = dq_notes
    return score, pkey, dbg

# ── Δίκτυο: τιμές, κεριά, orderbook, sentiment ─────────────
def fetch(url):
    try:
        r = urlopen(Request(url, headers={"User-Agent":"Mozilla/5.0"}), timeout=8)
        return json.loads(r.read())
    except Exception as e:
        add_log(f"ERR fetch: {e}")
        return None

def telegram_send(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return False
    try:
        payload = json.dumps({"chat_id": TELEGRAM_CHAT, "text": text, "disable_web_page_preview": True}).encode("utf-8")
        req = Request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=payload,
                      headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"}, method="POST")
        urlopen(req, timeout=8).read()
        return True
    except Exception as e:
        add_log(f"ERR telegram: {e}")
        return False

def get_price(pair):
    t = fetch(f"{BASE}/api/v1/contract/ticker?symbol={pair}")
    if t and t.get("data"):
        return float(t["data"].get("lastPrice", 0))
    return 0

def delta_by_time(trades, sec=120):
    now = int(time.time()*1000); cut = now - (sec*1000)
    buy = sell = 0.0; cnt = 0
    for tr in trades:
        if tr.get("t",0) < cut: continue
        v = float(tr.get("v",0)); o = tr.get("O",0)
        if o == 1: buy += v
        elif o == 3: sell += v
        cnt += 1
    tot = buy+sell
    return round(((buy-sell)/tot*100) if tot > 0 else 0, 2), cnt

def fetch_orderbook(pair, limit=20):
    d = fetch(f"{BASE}/api/v1/contract/depth/{pair}?limit={limit}")
    if not d or not d.get("data"):
        return {"ratio": 1.0, "max_bid": 0, "max_ask": 0, "bid_wall": 0, "ask_wall": 0}
    asks = d["data"].get("asks", []); bids = d["data"].get("bids", [])
    total_ask = sum(float(x[1]) for x in asks); total_bid = sum(float(x[1]) for x in bids)
    max_ask = max((float(x[1]) for x in asks), default=0); max_bid = max((float(x[1]) for x in bids), default=0)
    ratio = round(total_ask/total_bid, 2) if total_bid > 0 else 0.0
    bid_wall = round(max_bid/total_bid*100, 1) if total_bid > 0 else 0
    ask_wall = round(max_ask/total_ask*100, 1) if total_ask > 0 else 0
    return {"ratio": ratio, "max_bid": max_bid, "max_ask": max_ask, "bid_wall": bid_wall, "ask_wall": ask_wall}

def fetch_klines(pair, interval="Min1", limit=10):
    d = fetch(f"{BASE}/api/v1/contract/kline/{pair}?interval={interval}&limit={limit}")
    if not d or not d.get("data"):
        return []
    rows = d["data"]; candles = []
    try:
        H=rows.get("high",[]); L=rows.get("low",[]); V=rows.get("vol",[]); C=rows.get("close",[]); O=rows.get("open",[])
        for i in range(len(H)):
            h, l = float(H[i]), float(L[i])
            v = float(V[i]) if i < len(V) else 0
            c = float(C[i]) if i < len(C) else 0
            o = float(O[i]) if i < len(O) else 0
            candles.append({"h": h, "l": l, "v": v, "c": c, "o": o, "range": h-l})
    except Exception:
        pass
    return candles

def get_trend(pair, interval, limit=6):
    candles = fetch_klines(pair, interval, limit)
    if len(candles) < 3: return "NEUTRAL"
    last3 = candles[-3:]
    up = sum(1 for c in last3 if c["c"] > c["o"])
    down = sum(1 for c in last3 if c["c"] < c["o"])
    if up >= 2: return "UP"
    if down >= 2: return "DOWN"
    return "NEUTRAL"

def calc_bias(pair):
    candles = fetch_klines(pair, "Min15", 45)
    if len(candles) < 40: return None
    closes = [c["c"] for c in candles]; price = closes[-1]
    ma6 = sum(closes[-6:])/6; ma12 = sum(closes[-12:])/12; ma24 = sum(closes[-24:])/24
    if ma6 == 0 or ma12 == 0 or ma24 == 0: return None
    b6 = round((price-ma6)/ma6*100, 4); b12 = round((price-ma12)/ma12*100, 4); b24 = round((price-ma24)/ma24*100, 4)
    if b24 > b12 > b6: direction = "BULLISH"
    elif b6 > b12 > b24: direction = "BEARISH"
    else: direction = "NEUTRAL"
    return {"b6": b6, "b12": b12, "b24": b24, "direction": direction}

def calc_vol(candles):
    if len(candles) < 6: return 1.0, False, False
    vols = [c["v"] for c in candles if c["v"] > 0]
    if len(vols) < 6: return 1.0, False, False
    prev = statistics.mean(vols[-6:-3]); rec = statistics.mean(vols[-3:])
    if prev == 0: return 1.0, False, False
    ratio = round(rec/prev, 2); closes = [c["c"] for c in candles[-3:]]
    up = closes[-1] >= closes[0] if len(closes) >= 2 else False
    down = closes[-1] <= closes[0] if len(closes) >= 2 else False
    return ratio, (ratio >= VOL_MIN and up), (ratio >= VOL_MIN and down)

def fetch_sentiment():
    for pair in SENTIMENT_PAIRS:
        try:
            t15 = get_trend(pair, "Min15", 6)
            trend15[pair] = t15; sentiment_cache[pair] = t15
        except Exception:
            pass

def calc_market():
    up = sum(1 for t in sentiment_cache.values() if t == "UP")
    down = sum(1 for t in sentiment_cache.values() if t == "DOWN")
    n = len(sentiment_cache) if sentiment_cache else len(cache)
    if n == 0: return "NEUTRAL"
    if up/n >= 0.5: return "BULLISH"
    if down/n >= 0.5: return "BEARISH"
    return "NEUTRAL"

def trend15_loop():
    while True:
        cfg = read_config()
        pl = list(TREND_WATCH_PAIRS)
        for p in cfg.get("pairs", []):
            if p not in pl: pl.append(p)
        for pair in pl:
            trend15[pair] = get_trend(pair, "Min15", 6)
            trend1h[pair] = get_trend(pair, INTERVAL_1H, 6)
            trend4h[pair] = get_trend(pair, INTERVAL_4H, 6)
            b = calc_bias(pair)
            if b: bias_state[pair] = b
        time.sleep(90)

def fetch_pair(pair):
    d = {"pair": pair}
    t = fetch(f"{BASE}/api/v1/contract/ticker?symbol={pair}")
    if t and t.get("data"):
        td = t["data"]; d["price"] = float(td.get("lastPrice", 0)); d["change24h"] = float(td.get("riseFallRate", 0))*100
    tr = fetch(f"{BASE}/api/v1/contract/deals/{pair}?limit=500")
    if tr and tr.get("data") and isinstance(tr["data"], list):
        raw = tr["data"]
        d["delta_2m"], d["count_2m"] = delta_by_time(raw, 120)
        d["delta_1m"], d["count_1m"] = delta_by_time(raw, 60)
        d["delta_30s"], cnt30 = delta_by_time(raw, 30)
        d["delta"] = d["delta_2m"]
        d["accelerating"] = (d["delta_30s"] > d["delta_1m"] > 0 and d["delta_30s"] > 15 and cnt30 >= 3)
    else:
        d["delta"] = d["delta_2m"] = d["delta_1m"] = d["delta_30s"] = 0.0
        d["accelerating"] = False; d["count_2m"] = d["count_1m"] = 0
    candles = fetch_klines(pair, "Min1", 10)
    if candles:
        vr, vb_up, vb_down = calc_vol(candles)
        d["vol_ratio"] = vr; d["vol_bullish"] = vb_up; d["vol_bearish"] = vb_down
        ranges = [c["range"] for c in candles if c["range"] > 0]
        d["compression"] = round(statistics.mean(ranges[-2:])/statistics.mean(ranges), 3) if len(ranges) >= 4 else 1.0
    else:
        d["vol_ratio"] = 1.0; d["vol_bullish"] = False; d["vol_bearish"] = False; d["compression"] = 1.0
    with DATA_LOCK:
        if pair not in price_history: price_history[pair] = deque(maxlen=12)
        price_history[pair].append(d.get("price", 0))
        prices = list(price_history[pair])
    if len(prices) >= 12 and prices[-12] > 0:
        pc = (prices[-1]-prices[-12])/prices[-12]*100
        d["price_change_2m"] = round(pc, 3); d["flat"] = abs(pc) < 0.25; d["moving_up"] = pc > 0.15; d["moving_down"] = pc < -0.15
    elif len(prices) >= 4 and prices[-4] > 0:
        pc = (prices[-1]-prices[-4])/prices[-4]*100
        d["price_change_2m"] = round(pc, 3); d["flat"] = abs(pc) < 0.25; d["moving_up"] = pc > 0.15; d["moving_down"] = pc < -0.15
    else:
        d["price_change_2m"] = 0.0; d["flat"] = False; d["moving_up"] = False; d["moving_down"] = False
    ob_data = fetch_orderbook(pair)
    d["ob_ratio"] = ob_data["ratio"]; d["ob_max_bid"] = ob_data["max_bid"]; d["ob_max_ask"] = ob_data["max_ask"]
    d["ob_bid_wall"] = ob_data["bid_wall"]; d["ob_ask_wall"] = ob_data["ask_wall"]
    ticker2 = fetch(f"{BASE}/api/v1/contract/ticker?symbol={pair}")
    if ticker2 and ticker2.get("data"):
        td2 = ticker2["data"]; cur_hv = float(td2.get("holdVol", 0) or 0)
        d["hold_vol"] = cur_hv
        prev_hv = hv_history.get(pair, 0)
        d["hv_change"] = round((cur_hv-prev_hv)/prev_hv*100, 3) if prev_hv > 0 else 0.0
        hv_history[pair] = cur_hv
        d["funding_rate"] = float(td2.get("fundingRate", 0) or 0)
        d["fair_price"] = float(td2.get("fairPrice", 0) or 0)
        d["index_price"] = float(td2.get("indexPrice", 0) or 0)
    else:
        d["hold_vol"] = 0.0; d["funding_rate"] = 0.0; d["fair_price"] = 0.0; d["index_price"] = 0.0
    t5 = get_trend(pair, "Min5", 6); d["trend5m"] = t5
    t15 = trend15.get(pair, "NEUTRAL"); d["trend15m"] = t15
    d["trend1h"] = trend1h.get(pair, "NEUTRAL"); d["trend4h"] = trend4h.get(pair, "NEUTRAL")
    d2m = d.get("delta_2m", 0); cnt = d.get("count_2m", 0)
    vb_up = d.get("vol_bullish", False); vb_down = d.get("vol_bearish", False)
    vr = d.get("vol_ratio", 1.0); accel = d.get("accelerating", False)
    moving_up = d.get("moving_up", False); moving_down = d.get("moving_down", False); flat = d.get("flat", False)
    if cnt >= 10 and d2m >= DELTA_MIN and moving_up and vr >= VOL_MIN and t5 == "UP" and t15 == "UP":
        d["absorption"] = "TREND_UP"
    elif cnt >= 10 and d2m <= -DELTA_MIN and moving_down and vr >= VOL_MIN and t5 == "DOWN" and t15 == "DOWN":
        d["absorption"] = "TREND_DOWN"
    elif cnt >= 10 and flat and d2m >= DELTA_MIN and vb_up and t15 == "UP":
        d["absorption"] = "BUY_STRONG" if accel else "BUY"
    elif cnt >= 10 and flat and d2m <= -DELTA_MIN and vb_down and t15 == "DOWN":
        d["absorption"] = "SELL_STRONG" if accel else "SELL"
    else:
        d["absorption"] = "NONE"
    if pair not in neg_count: neg_count[pair] = 0
    if pair not in pos_count: pos_count[pair] = 0
    if d2m < -20: neg_count[pair] += 1
    else: neg_count[pair] = 0
    if d2m > 20: pos_count[pair] += 1
    else: pos_count[pair] = 0
    with DATA_LOCK:
        mkt = calc_market()
    d["market_sentiment"] = mkt; ab = d["absorption"]; now = time.time()
    with DATA_LOCK:
        if pair not in latch: latch[pair] = {"signal": "NEUTRAL", "until": 0, "direction": ""}
        if latch[pair].get("direction") == "LONG" and neg_count[pair] >= 2:
            latch[pair] = {"signal": "NEUTRAL", "until": 0, "direction": ""}
        if latch[pair].get("direction") == "SHORT" and pos_count[pair] >= 2:
            latch[pair] = {"signal": "NEUTRAL", "until": 0, "direction": ""}
        la = latch[pair]["until"] > now
        lr = max(0, int(latch[pair]["until"]-now)) if la else 0
        if ab in ["TREND_UP","BUY_STRONG","BUY"]:
            latch[pair] = {"signal": "ANODOS", "until": now+LATCH_SEC, "direction": "LONG"}
            neg_count[pair] = 0; d["raw_signal"] = "ANODOS"; d["direction"] = "LONG"; d["latch_remaining"] = LATCH_SEC
        elif ab in ["TREND_DOWN","SELL_STRONG","SELL"]:
            latch[pair] = {"signal": "PTWSH", "until": now+LATCH_SEC, "direction": "SHORT"}
            pos_count[pair] = 0; d["raw_signal"] = "PTWSH"; d["direction"] = "SHORT"; d["latch_remaining"] = LATCH_SEC
        elif la:
            d["raw_signal"] = latch[pair]["signal"]; d["direction"] = latch[pair].get("direction",""); d["latch_remaining"] = lr
        else:
            d["raw_signal"] = "NEUTRAL"; d["direction"] = ""; d["latch_remaining"] = 0
    d["time"] = now_str()
    return d

# ── KDJ/MACD/EMA confirmation layer (επιβεβαίωση, όχι φίλτρο-μπλόκο) ──
def _ema(data, period):
    k = 2 / (period + 1)
    result = [data[0]]
    for v in data[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result

def _macd_hist(closes, fast=2, slow=41, signal=18):
    ml = [f - s for f, s in zip(_ema(closes, fast), _ema(closes, slow))]
    sl = _ema(ml, signal)
    return ml[-1] - sl[-1]

def _kdj_golden(candles, k_period=7):
    """True = Golden Cross (K>D, bullish), False = Dead Cross"""
    K, D = [], []
    for i in range(len(candles)):
        w = candles[max(0, i-k_period+1):i+1]
        hi = max(c["h"] for c in w); lo = min(c["l"] for c in w)
        rsv = (candles[i]["c"] - lo) / (hi - lo) * 100 if hi != lo else 50
        kv = (2/3) * K[-1] + (1/3) * rsv if K else 50.0
        dv = (2/3) * D[-1] + (1/3) * kv if D else 50.0
        K.append(kv); D.append(dv)
    return K[-1] > D[-1]

def kdj_check(pair, direction):
    """
    agrees=True  → συμφωνεί (DOUBLE CONFIRMED)
    agrees=False → διαφωνεί (απλή σημείωση, δεν μπλοκάρει)
    agrees=None  → σφάλμα/ανεπαρκή δεδομένα (αγνοείται)
    """
    try:
        c15 = fetch_klines(pair, "Min15", 60)
        c5  = fetch_klines(pair, "Min5",  60)
        if len(c15) < 15 or len(c5) < 15:
            return None, "not enough candles"
        cl15 = [c["c"] for c in c15]
        cl5  = [c["c"] for c in c5]
        e5_15 = _ema(cl15, 5); h15 = _macd_hist(cl15, 2, 41, 18); gx15 = _kdj_golden(c15, 7)
        price = cl15[-1]
        e5_5 = _ema(cl5, 5); e15_5 = _ema(cl5, 15); h5 = _macd_hist(cl5, 2, 19, 40); gx5 = _kdj_golden(c5, 9)
        bull15 = sum([price > e5_15[-1], h15 > 0, gx15])
        bull5  = sum([cl5[-1] > e5_5[-1], e5_5[-1] > e15_5[-1], h5 > 0, gx5])
        gx_tag = "🟢GX" if gx15 else "🔴DX"
        if direction == "LONG":
            agrees = bull15 >= 2 and bull5 >= 3 and gx15
            detail = f"KDJ 15m:{bull15}/3 5m:{bull5}/4 {gx_tag}"
        else:
            bear15 = 3 - bull15; bear5 = 4 - bull5
            agrees = bear15 >= 2 and bear5 >= 3 and not gx15
            detail = f"KDJ 15m:{bear15}/3 5m:{bear5}/4 {gx_tag}"
        return agrees, detail
    except Exception as e:
        return None, f"kdj_err:{e}"

def claude_approve(d, mkt):
    want = d.get("direction","")
    t1 = d.get("trend1h","NEUTRAL"); t5 = d.get("trend5m","NEUTRAL"); t15 = d.get("trend15m","NEUTRAL")
    label = d.get("ai_label",""); ob = float(d.get("ob_ratio",1.0) or 1.0); hv_change = float(d.get("hv_change",0) or 0)
    bid_wall = float(d.get("ob_bid_wall",0) or 0); ask_wall = float(d.get("ob_ask_wall",0) or 0)
    vr = float(d.get("vol_ratio",1.0) or 1.0); ab = d.get("absorption","NONE")
    d2m = float(d.get("delta_2m",0) or 0); d30s = float(d.get("delta_30s",0) or 0)
    same_sign = (d2m > 0 and d30s > 0) or (d2m < 0 and d30s < 0)
    if not same_sign:
        add_log(f"  FILTER: NO — d2m={d2m:.1f}% d30={d30s:.1f}% αντίθετα"); return False, "d2m/d30 αντίθετα"
    vol_spike = vr >= 10.0; is_trend = ab in ("TREND_UP","TREND_DOWN")
    if vol_spike and is_trend and same_sign:
        add_log(f"  FILTER: YES — vol spike {vr:.1f}x TREND"); return True, f"vol spike {vr:.1f}x"
    if mkt == "NEUTRAL":
        add_log("  FILTER: NO — market NEUTRAL χωρίς κατεύθυνση"); return False, "market NEUTRAL"
    if want == "LONG" and mkt == "BEARISH":
        add_log("  FILTER: NO — LONG σε BEARISH market"); return False, "LONG σε BEARISH market"
    if want == "SHORT" and mkt == "BULLISH":
        add_log("  FILTER: NO — SHORT σε BULLISH market"); return False, "SHORT σε BULLISH market"
    if want == "LONG" and t1 != "UP":
        add_log(f"  FILTER: NO — 1h={t1} οχι UP για LONG"); return False, f"1h={t1} οχι UP"
    if want == "SHORT" and t1 != "DOWN":
        add_log(f"  FILTER: NO — 1h={t1} οχι DOWN για SHORT"); return False, f"1h={t1} οχι DOWN"
    if label in ("FAKE_BREAKOUT","LIQUIDATION_SPIKE"):
        add_log(f"  FILTER: NO — regime={label}"); return False, f"regime={label}"
    if want == "SHORT" and ob < 0.8 and bid_wall >= 40:
        add_log(f"  FILTER: NO — bid wall={bid_wall:.0f}%"); return False, f"bid wall {bid_wall:.0f}%"
    if want == "LONG" and ob > 2.0 and ask_wall >= 40:
        add_log(f"  FILTER: NO — ask wall={ask_wall:.0f}%"); return False, f"ask wall {ask_wall:.0f}%"
    if hv_change < -0.2:
        add_log(f"  FILTER: NO — hv={hv_change:.3f}% κλείνουν θέσεις"); return False, "hv μειώνεται"
    pair_name = d.get("pair",""); now_t = time.time(); fm = failed_move.get(pair_name); is_retest = False
    if fm:
        age = (now_t - fm["time"]) / 3600; same_dir = fm["direction"] == want
        price_now = float(d.get("price",0) or 0); prev_price = fm["price"]
        price_diff = abs(price_now-prev_price)/prev_price*100 if prev_price > 0 else 999
        if same_dir and age <= 3.0 and vr >= 5.0 and price_diff <= 0.5:
            is_retest = True
            add_log(f"  RETEST: {pair_name} {want} vol={vr:.1f}x diff={price_diff:.2f}% — επιβεβαιωμένο!")
    if is_retest:
        add_log("  FILTER: YES — RETEST επιβεβαιωμένο!"); return True, "RETEST"
    kdj_ok, kdj_detail = kdj_check(pair_name, want)
    d["kdj_agrees"] = kdj_ok; d["kdj_detail"] = kdj_detail
    if kdj_ok is True:
        add_log(f"  KDJ: ✅ συμφωνεί — {kdj_detail}")
    elif kdj_ok is False:
        add_log(f"  KDJ: ⚠️ διαφωνεί — {kdj_detail}")
    add_log("  FILTER: YES — όλοι οι κανόνες OK")
    return True, "OK"

# ── AI επιλογή σήματος → άνοιγμα πραγματικής εικονικής θέσης ──
def ai_select_and_emit(pairs_data, mkt):
    if time.time() < cooldown_until:
        remain = int((cooldown_until - time.time()) / 60) + 1
        add_log(f"  AI: σε παύση (circuit breaker) — {remain}λ ακόμα")
        return
    scored = []
    for d in pairs_data:
        score, pkey, dbg = score_pair(d)
        d["score"] = score; d["pattern_key"] = pkey; d["score_dbg"] = dbg
        d["ai_label"] = dbg.get("ai", {}).get("label", "")
        record_observation(d, score, pkey, selected=0)
        scored.append(d)
    if not scored:
        return
    scored_sorted = sorted(scored, key=lambda x: int(x.get("score",0) or 0), reverse=True)
    trace_parts = []
    for x in scored_sorted[:4]:
        ps = int(x.get("score",0) or 0)
        notes = x.get("score_dbg", {}).get("dq_notes") or []
        suff = (" [" + ",".join(notes) + "]") if notes else ""
        trace_parts.append(f"{x.get('pair')}={ps}{suff}")
    add_log(f"  AI_TRACE rank: {' > '.join(trace_parts)}")

    # Σκληρό μπλοκ: όταν η αγορά έχει ξεκάθαρη φάση (BULLISH/BEARISH), δεν ανοίγουμε
    # trade αντίθετο στη φάση — δεν αρκεί απλά να είναι σπανιότερο (ποινή στο score),
    # τα δεδομένα δείχνουν ότι σχεδόν πάντα χάνει (π.χ. LONG σε bearish αγορά).
    best = None
    blocked_counter_bias = []
    for cand in scored_sorted:
        c_pair = cand.get("pair", "?")
        c_dir = cand.get("direction", "")
        c_bias_dir = bias_state.get(c_pair, {}).get("direction", "NEUTRAL")
        counter_bias = (c_bias_dir == "BULLISH" and c_dir == "SHORT") or (c_bias_dir == "BEARISH" and c_dir == "LONG")
        if counter_bias:
            blocked_counter_bias.append(f"{c_pair}={int(cand.get('score',0) or 0)}")
            continue
        best = cand
        break
    if best is None:
        add_log(f"  AI: NO TRADE (όλοι οι υποψήφιοι αντίθετοι στη φάση αγοράς) [{', '.join(blocked_counter_bias)}]")
        return
    if blocked_counter_bias:
        add_log(f"  AI: μπλοκαρισμένοι (counter-bias) πριν τον επιλεγμένο: {', '.join(blocked_counter_bias)}")

    best_score = int(best.get("score", 0) or 0)
    pair = best.get("pair", "?")
    direction = best.get("direction", "")
    bias = bias_state.get(pair, {}); bias_dir = bias.get("direction", "NEUTRAL")
    bias_aligned = (direction == "LONG" and bias_dir == "BULLISH") or (direction == "SHORT" and bias_dir == "BEARISH")
    effective_min = 80 if bias_aligned else SCORE_MIN
    if best_score < effective_min:
        lbl = best.get("score_dbg", {}).get("ai", {}).get("label", "?")
        bias_note = f" BIAS={bias_dir}" if bias_aligned else ""
        add_log(f"  AI: NO TRADE (best {pair} score={best_score}/100 <{effective_min}{bias_note}) regime={lbl}")
        return
    if pair in paper_trades:
        add_log(f"  AI: {pair} έχει ήδη ανοιχτή θέση → NO TRADE (score={best_score}/100)")
        return
    ok, why = selection_gate(pair)
    if not ok:
        if why == "daily":
            add_log(f"  AI: DAILY LIMIT ({MAX_SELECTIONS_PER_DAY}/day) → NO TRADE (best {pair} score={best_score}/100)")
        else:
            add_log(f"  AI: {why.upper()} → NO TRADE (best {pair} score={best_score}/100)")
        return
    approved, reason = claude_approve(best, mkt)
    if not approved:
        add_log(f"  Claude: REJECTED {pair} — {reason}")
        return

    entry_price = float(best.get("price", 0) or 0)
    if entry_price <= 0:
        add_log(f"  AI: {pair} score={best_score}/100 αλλά invalid price=0 → NO TRADE")
        return

    now = time.time()
    sig_id = f"{pair}_{int(now)}"
    trade_direction = direction or ("LONG" if best.get("raw_signal") == "ANODOS" else "SHORT")
    pattern_key = best.get("pattern_key", "")

    paper_trades[pair] = {
        "direction": trade_direction, "entry": entry_price, "entry_ts": now,
        "sig_id": sig_id, "score": best_score, "pattern_key": pattern_key,
    }
    save_open_trades()
    add_log(f"  📊 {pair} PAPER ΑΝΟΙΞΕ: {trade_direction} @ {fmt_price(entry_price)}  "
            f"score={best_score}/100  regime={best.get('ai_label','?')}")

    record_observation(best, best_score, pattern_key, selected=1)
    record_outcome(sig_id, int(now), pair, trade_direction, entry_price, best_score, pattern_key, None, None)
    mark_selected_today(sig_id, pair, best_score)

    kdj_ok = best.get("kdj_agrees"); kdj_detail = best.get("kdj_detail", "")
    if kdj_ok is True:
        kdj_tag = f"⚡ DOUBLE CONFIRMED\n{kdj_detail}\n"
        add_log(f"  ⚡ DOUBLE CONFIRMED — {kdj_detail}")
    elif kdj_ok is False:
        kdj_tag = f"⚠️ KDJ διαφωνεί — {kdj_detail}\n"
    else:
        kdj_tag = ""

    d2m = float(best.get("delta_2m", 0) or 0); d30 = float(best.get("delta_30s", 0) or 0)
    vr = float(best.get("vol_ratio", 1.0) or 1.0); ab = best.get("absorption", "NONE")
    arrow = "🟢 LONG" if trade_direction == "LONG" else "🔴 SHORT"
    telegram_send(
        f"{kdj_tag}"
        f"{arrow} — {pair} (PAPER, score={best_score}/100)\n"
        f"Τιμή: {fmt_price(entry_price)}  d2m={d2m:.1f}%  d30={d30:.1f}%  vol={vr:.1f}x\n"
        f"absorption={ab}  regime={best.get('ai_label','?')}  mkt={mkt}\n"
        f"5m={best.get('trend5m','?')} 15m={best.get('trend15m','?')} 1h={best.get('trend1h','?')} 4h={best.get('trend4h','?')}\n"
        f"⏰ {now_str()}"
    )

# ── Έλεγχος ανοιχτών θέσεων: κλείσιμο σε TP/SL, καταγραφή & μνήμη ──
def check_open_trades(trades, tp_pct, sl_pct):
    for pair in list(paper_trades.keys()):
      try:
        pt = paper_trades[pair]
        entry = pt["entry"]; direction = pt["direction"]
        if entry <= 0:
            add_log(f"  ⚠️ {pair} άκυρη θέση (entry=0) → ακύρωση")
            del paper_trades[pair]
            save_open_trades()
            continue
        price = get_price(pair)
        if price <= 0:
            continue
        pct = (price-entry)/entry*100 if direction == "LONG" else (entry-price)/entry*100

        if pct >= tp_pct:
            result, result_gr = "WIN", "KERDOS ✅"
        elif pct <= -sl_pct:
            result, result_gr = "LOSS", "ZIMIA ❌"
        else:
            el = (time.time()-pt["entry_ts"])/60
            add_log(f"  📊 {pair} {direction} @ {fmt_price(entry)} | {pct:+.2f}% | {el:.0f}λ")
            continue

        entry_dt = datetime.fromtimestamp(pt["entry_ts"], TZ)
        exit_dt  = datetime.now(TZ)
        el_min   = (exit_dt - entry_dt).total_seconds()/60
        trades.append({
            "Ημερομηνία":  entry_dt.strftime("%d/%m/%Y"),
            "Ώρα Εισόδου": entry_dt.strftime("%H:%M:%S"),
            "Ώρα Εξόδου":  exit_dt.strftime("%H:%M:%S"),
            "Ζεύγος":      pair,
            "Κατ/νση":     direction,
            "Είσοδος":     f"{fmt_price(entry)}",
            "Έξοδος":      f"{fmt_price(price)}",
            "% P&L":       f"{pct:+.2f}%",
            "Αποτ/μα":     result_gr,
            "Διάρκεια":    f"{el_min:.0f}λ",
        })
        save_trades(trades)
        add_log(f"  📊 {pair} {direction} {pct:+.2f}% {result_gr}")
        record_outcome(pt["sig_id"], int(pt["entry_ts"]), pair, direction, entry,
                       pt.get("score"), pt.get("pattern_key",""), pct, result)
        telegram_send(
            f"📊 {pair} PAPER — {result_gr}\n"
            f"{direction} {fmt_price(entry)} → {fmt_price(price)}\n"
            f"{pct:+.2f}%   {el_min:.0f}λ   "
            f"({entry_dt.strftime('%d/%m %H:%M')} → {exit_dt.strftime('%H:%M')})"
        )
        if result == "LOSS":
            failed_move[pair] = {"direction": direction, "price": entry, "time": time.time(), "result": "ZIMIA"}
        global consecutive_losses, cooldown_until
        if result == "LOSS":
            consecutive_losses += 1
            if consecutive_losses >= LOSS_STREAK_LIMIT and time.time() >= cooldown_until:
                cooldown_until = time.time() + COOLDOWN_MINUTES * 60
                add_log(f"  🛑 CIRCUIT BREAKER: {consecutive_losses} συνεχόμενες ζημιές → "
                        f"παύση νέων trades για {COOLDOWN_MINUTES}λ")
                telegram_send(f"🛑 Circuit breaker: {consecutive_losses} συνεχόμενες ζημιές.\n"
                              f"Παύση νέων trades για {COOLDOWN_MINUTES} λεπτά.")
        else:
            consecutive_losses = 0
        del paper_trades[pair]
        save_open_trades()
      except Exception as e:
        add_log(f"  TRADE CHECK ERR {pair}: {e}")

# ── Εκκίνηση & main loop (24/7) ────────────────────────────
init_db()
add_log("="*44)
add_log("MEXC AI Signal Bot ξεκίνησε")
add_log("="*44)

trades = load_trades()
restore_open_trades()

for _pair in TREND_WATCH_PAIRS:
    trend15[_pair] = get_trend(_pair, "Min15", 6)
    trend1h[_pair] = get_trend(_pair, INTERVAL_1H, 6)
    trend4h[_pair] = get_trend(_pair, INTERVAL_4H, 6)
    add_log(f"  {_pair}: 15m={trend15[_pair]} 1h={trend1h[_pair]} 4h={trend4h[_pair]}")

threading.Thread(target=trend15_loop, daemon=True).start()

while True:
    if os.path.exists(CLEAR_TRADES_FILE):
        paper_trades.clear()
        trades.clear()
        save_trades([])
        save_open_trades()
        consecutive_losses = 0
        cooldown_until = 0.0
        add_log("🗑️ ΚΑΘΑΡΙΣΜΑ: μηδενίστηκαν trades και ανοιχτές θέσεις (η μνήμη μάθησης ΔΕΝ αγγίχτηκε)")
        os.remove(CLEAR_TRADES_FILE)

    if os.path.exists(RESET_FILE):
        paper_trades.clear()
        trades.clear()
        save_trades([])
        save_open_trades()
        consecutive_losses = 0
        cooldown_until = 0.0
        try:
            for ext in ("", "-wal", "-shm"):
                p = MEMORY_DB + ext
                if os.path.exists(p):
                    os.remove(p)
        except Exception as e:
            add_log(f"ERR reset MEMORY_DB: {e}")
        init_db()
        add_log("🧠 ΠΛΗΡΕΣ RESET: μηδενίστηκαν trades, ανοιχτές θέσεις και μνήμη μάθησης")
        os.remove(RESET_FILE)

    if os.path.exists(STOP_FILE):
        time.sleep(5)
        continue

    cfg = read_config()
    pairs    = cfg["pairs"]
    interval = int(cfg["interval"])
    tp_pct   = float(cfg["tp_pct"])
    sl_pct   = float(cfg["sl_pct"])

    if not pairs:
        time.sleep(interval)
        continue

    mkt = calc_market()
    add_log(f"── Κύκλος {now_str()} | {mkt} | {len(pairs)} ζεύγη ──")
    cycle_data = []
    fetch_sentiment()

    for pair in pairs:
        try:
            data = fetch_pair(pair)
            with DATA_LOCK:
                cache[pair] = data
            cycle_data.append(data)

            ab = data.get("absorption", "NONE"); sig = data.get("raw_signal", "")
            d2m = data.get("delta_2m", 0); d30 = data.get("delta_30s", 0)
            vr = data.get("vol_ratio", 1.0)
            t5 = data.get("trend5m","?"); t15 = data.get("trend15m","?")
            t1 = data.get("trend1h","?"); t4 = data.get("trend4h","?")
            tf = f"5m={t5} 15m={t15} 1h={t1} 4h={t4}"
            if ab in ("TREND_UP", "TREND_DOWN"):
                add_log(f"  * {ab} * {pair}: {data.get('price','?')} d2m={d2m:.1f}% vol={vr:.1f}x {tf}")
            elif ab != "NONE" or sig not in ("NEUTRAL", ""):
                ob = data.get("ob_ratio", 0); bw = data.get("ob_bid_wall", 0); aw = data.get("ob_ask_wall", 0)
                add_log(f"  >> {pair}: {data.get('price','?')} d2m={d2m:.1f}% d30={d30:.1f}% vol={vr:.1f}x "
                        f"{tf} {ab} {sig} ob={ob:.1f} bw={bw:.0f}% aw={aw:.0f}%")
            else:
                ob = data.get("ob_ratio", 0); hv = data.get("hold_vol", 0)
                add_log(f"  {pair}: {data.get('price','?')} d2m={d2m:.1f}% vol={vr:.1f}x {tf} ob={ob:.1f} hv={hv:.0f}")

            vr_now = data.get("vol_ratio", 1.0); d2m_now = data.get("delta_2m", 0)
            flat_now = data.get("flat", False); direction_now = data.get("direction", "")
            now_t = time.time()
            last_alert = absorption_alert.get(pair, 0)
            bias_now = bias_state.get(pair, {}); bias_dir_now = bias_now.get("direction", "NEUTRAL")
            bias_abs_ok = (direction_now == "LONG" and bias_dir_now == "BULLISH") or (direction_now == "SHORT" and bias_dir_now == "BEARISH")
            if (vr_now <= 0.5 and flat_now and abs(d2m_now) >= 40 and direction_now and mkt != "NEUTRAL"
                    and bias_abs_ok and (now_t - last_alert) > 300):
                absorption_alert[pair] = now_t
                direction_sym = "LONG 📈" if direction_now == "LONG" else "SHORT 📉"
                add_log(f"  ⚡ ABSORPTION {pair} {direction_sym} vol={vr_now:.1f}x d2m={d2m_now:.1f}%")
                telegram_send(f"⚡ ABSORPTION {pair}\n{direction_sym} — ετοιμάσου!\nvol={vr_now:.1f}x d2m={d2m_now:.1f}% {mkt}")
        except Exception as e:
            add_log(f"  x {pair}: {e}")

    try:
        ai_select_and_emit(cycle_data, mkt)
    except Exception as e:
        add_log(f"  AI ERR: {e}")

    try:
        check_open_trades(trades, tp_pct, sl_pct)
    except Exception as e:
        add_log(f"  TRADE CHECK ERR: {e}")

    time.sleep(interval)
