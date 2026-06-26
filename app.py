#!/usr/bin/env python3
"""
Signal Bot Dashboard — διαβάζει από αρχεία που γράφει το bot_worker.py
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import streamlit as st

TZ = ZoneInfo("Europe/Athens")

st.set_page_config(page_title="Giatros v8", page_icon="🩺", layout="wide")

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
RESET_FILE     = "/tmp/bot_reset"
CLEAR_TRADES_FILE = "/tmp/bot_clear_trades"
MEMORY_DB      = "/data/mexc_memory.db"

ALL_PAIRS = [
    # Majors
    "BTC_USDT","ETH_USDT","BNB_USDT","XRP_USDT","SOL_USDT",
    # Large caps
    "ADA_USDT","DOGE_USDT","TRX_USDT","LTC_USDT","AVAX_USDT",
    "DOT_USDT","LINK_USDT","ATOM_USDT","UNI_USDT","NEAR_USDT",
    # Mid caps
    "INJ_USDT","ARB_USDT","OP_USDT","APT_USDT","SUI_USDT",
    "ICP_USDT","FIL_USDT","AAVE_USDT","GRT_USDT","IMX_USDT",
    "STX_USDT","SEI_USDT","WLD_USDT","JUP_USDT","PENDLE_USDT",
    # Meme / high-vol
    "PEPE_USDT","WIF_USDT","BONK_USDT","FLOKI_USDT","SHIB_USDT",
]

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

# ── Help dialog ────────────────────────────────────────────
@st.dialog("❓ Οδηγός Χρήσης — MEXC AI Signal Bot", width="large")
def show_help():
    st.markdown("""
## Τι είναι αυτή η εφαρμογή;
Ένας αυτοματοποιημένος bot που παρακολουθεί ζεύγη crypto στο MEXC Futures,
αναλύει την αγορά με AI και ανοίγει **εικονικές (paper) θέσεις** για να
δοκιμάζει στρατηγικές χωρίς πραγματικό κεφάλαιο. Μαθαίνει από κάθε trade
και βελτιώνει τις επόμενες επιλογές του.

---

## Πώς δουλεύει η AI μηχανή;

### 1. Συλλογή δεδομένων
Κάθε κύκλο (default: κάθε 60 δευτ.) ο bot μαζεύει για κάθε ζεύγος:
- **Τιμή & κινήσεις** — τελευταία τιμή, μεταβολή 2 λεπτών
- **Volume** — σύγκριση με μέσο όρο (>2x = αυξημένη δραστηριότητα)
- **Orderbook** — ανισορροπία αγοραστών/πωλητών
- **Funding rate** — κόστος θέσης (δείκτης υπερθέρμανσης αγοράς)
- **Τάσεις 4 χρονικών πλαισίων** — 5λ / 15λ / 1ω / 4ω (BULLISH/BEARISH/NEUTRAL)
- **KDJ + MACD + EMA** — τεχνικοί δείκτες επιβεβαίωσης

### 2. AI Scoring (0–100)
Κάθε ζεύγος παίρνει **βαθμολογία 0–100** βάσει:
- Ευθυγράμμιση τάσεων (5λ/15λ/1ω/4ω)
- Ισορροπία orderbook
- Επίπεδο volume
- Funding rate
- Σχέση τιμής με fair value
- **Ιστορικό win-rate** του συγκεκριμένου pattern (SQLite μνήμη)

Μόνο ζεύγη με **score ≥ 90/100** προχωρούν στο επόμενο βήμα.

### 3. Ταξινόμηση καθεστώτος (Regime)
Το AI αναγνωρίζει 6 τύπους αγοράς:
| Καθεστώς | Σημασία |
|---|---|
| `CONTINUATION` | Υγιής τάση, ευνοϊκό για είσοδο |
| `LIQUIDATION_SPIKE` | Απότομη κίνηση από ρευστοποιήσεις |
| `FAKE_BREAKOUT` | Ψεύτικη διάσπαση, αποφυγή |
| `EXHAUSTION` | Εξάντληση τάσης, επικίνδυνο |
| `LATE_ENTRY` | Αργή είσοδος, μειωμένο score |
| `NO_SETUP` | Δεν υπάρχει σαφές σήμα |

### 4. claude_approve — Τελικό φίλτρο
Πριν ανοίξει οποιαδήποτε θέση, περνάει από 6 ελέγχους:
1. Αγορά γενικά bullish/bearish (από 4 βασικά ζεύγη);
2. Τάση 1 ώρας ευθυγραμμισμένη με σήμα;
3. Δεν υπάρχει μεγάλο orderbook wall μπροστά;
4. Hold-volume αυξάνεται (κόσμος μπαίνει, όχι βγαίνει);
5. Retest detection — η τιμή επανήλθε σε σημείο στήριξης/αντίστασης;
6. KDJ Golden/Death Cross επιβεβαιώνει κατεύθυνση; → tag **"DOUBLE CONFIRMED ⚡"**

---

## Paper Trading — Πώς λειτουργεί;

### Άνοιγμα θέσης
Όταν ένα ζεύγος περάσει **όλα** τα παραπάνω φίλτρα:
- Ανοίγει αυτόματα εικονική θέση LONG ή SHORT
- Καταγράφεται: ζεύγος, κατεύθυνση, τιμή εισόδου, ώρα (ελληνική)
- Στέλνεται ειδοποίηση Telegram (αν ρυθμιστεί)

### Κλείσιμο θέσης
Σε κάθε κύκλο υπολογίζεται το live P&L:
- **Take Profit** (default +1.5%): κλείνει ως **KERDOS ✅**
- **Stop Loss** (default -1.0%): κλείνει ως **ZIMIA ❌**
- Καταγράφεται: ημερομηνία, ώρα εισόδου/εξόδου, % P&L, διάρκεια

### Μνήμη & Εκμάθηση
Κάθε κλειστό trade (WIN/LOSS) αποθηκεύεται στη **SQLite βάση** (`mexc_memory.db`).
Το pattern (συνδυασμός τάσεων, volume, KDJ κ.λπ.) συνδέεται με το αποτέλεσμα.
Στον επόμενο κύκλο, **ίδια patterns με υψηλό win-rate** παίρνουν μπόνους στο score
ενώ patterns με χαμηλό win-rate "τιμωρούνται" — ο bot γίνεται πιο στοχευμένος με τον καιρό.

---

## Ρυθμίσεις (Sidebar)

| Ρύθμιση | Τι κάνει | Default |
|---|---|---|
| **Ζεύγη** | Ποια crypto παρακολουθεί | 8 βασικά |
| **Interval** | Πόσο συχνά σκανάρει (δευτ.) | 60 |
| **Take Profit %** | Κέρδος για κλείσιμο θέσης | 1.5% |
| **Stop Loss %** | Ζημία για κλείσιμο θέσης | 1.0% |

> Μετά από κάθε αλλαγή πάτα **"Αποθήκευση ρυθμίσεων"**.

---

## Live Log — Τι σημαίνουν τα μηνύματα;

```
── Κύκλος 14:32:05 | BULLISH | 8 ζεύγη ──
  NEAR_USDT  score=94  CONTINUATION  DOUBLE CONFIRMED ⚡  → LONG
  📊 NEAR_USDT PAPER ΑΝΟΙΞΕ: LONG @ 4.1230
  📊 BTC_USDT LONG @ 67500 | +0.82% | 12λ     ← ανοικτή θέση, δεν έκλεισε ακόμα
  📊 ETH_USDT LONG +1.52% KERDOS ✅            ← έκλεισε με κέρδος
```

| Σύμβολο | Σημασία |
|---|---|
| `score=XX` | Βαθμολογία AI (max 100) |
| `DOUBLE CONFIRMED ⚡` | KDJ επιβεβαίωσε κατεύθυνση |
| `KERDOS ✅` | Trade έκλεισε κερδοφόρο |
| `ZIMIA ❌` | Trade έκλεισε με ζημία |
| `NO TRADE` | Κανένα ζεύγος δεν πέρασε τα φίλτρα |

---

## Paper Trades tab
- **Ανοικτές Θέσεις**: trades που τρέχουν αυτή τη στιγμή
- **Κλειστές Θέσεις**: πλήρες ιστορικό με P&L
- **Κουμπί CSV**: κατέβασε το ιστορικό σε Excel/spreadsheet

---

## Telegram ειδοποιήσεις (προαιρετικό)
Στο Railway → Variables πρόσθεσε:
- `TELEGRAM_TOKEN` = το token του Telegram bot σου
- `TELEGRAM_CHAT` = το chat ID σου

Θα λαμβάνεις μήνυμα κάθε φορά που ανοίγει ή κλείνει θέση.
""")

# ── Journal / Ημερολόγιο αλλαγών dialog ─────────────────────
@st.dialog("📝 Ημερολόγιο Αλλαγών", width="large")
def show_journal():
    st.markdown("""
### 26/06/2026

**Δύο αλλαγές στη μνήμη μάθησης**, ύστερα από συζήτηση για το ρίσκο να
"μαυρολιστάρεται" μόνιμα ένα μοτίβο που έτυχε να αποτύχει στην αρχή χωρίς
ποτέ να ξαναδοκιμαστεί:

1. **Η εμπιστοσύνη πια δεν "παγώνει" στα 15 δείγματα** — παλιά, μετά τις 15
   δοκιμές ενός μοτίβου η επίδραση στο σκορ έφτανε στο ανώτατο και
   σταματούσε να αλλάζει όσα νέα δείγματα κι αν μαζευτούν. Τώρα η επίδραση
   συνεχίζει να μεγαλώνει ασυμπτωτικά όσο μαζεύονται περισσότερα δείγματα
   (200 δοκιμές μετράνε παραπάνω από 15, χωρίς ποτέ να "κλειδώνει" οριστικά).
2. **Exploration**: σπάνια (5% πιθανότητα, μέχρι 3 φορές/μέρα), το bot
   δοκιμάζει σκόπιμα ένα μοτίβο με χαμηλό σκορ (αλλά όχι τελείως άσχετο, και
   μόνο αν έχει λιγότερα από 30 δείγματα) ώστε να συνεχίζει να μαθαίνει γι'
   αυτό, αντί να το αγνοεί μόνιμα. Επισημαίνεται στο log/Telegram ως
   "🔬 EXPLORATION".

**Στις Κλειστές Θέσεις** προστέθηκαν 3 νέες στήλες: **Σκορ Εισόδου** (με τι
σκορ μπήκε το trade), **Δείγματα Pattern** και **Νίκες/Ζημίες Pattern**
(πόσες φορές έχει ξανασυναντηθεί αυτό το μοτίβο και με τι αποτέλεσμα, μέχρι
και αυτή τη συναλλαγή).

---

### 23/06/2026 (συνέχεια)

Προστέθηκε στήλη **"Ώρα Εισόδου"** (ώρα Ελλάδας) στον πίνακα "Ανοικτές
Θέσεις", ώστε να φαίνεται πότε άνοιξε κάθε ανοιχτή θέση, όχι μόνο η
διάρκεια σε λεπτά.

---

### 23/06/2026

**Ανάλυση CSV (44 trades, 22-23/06)**: SHORT win-rate 77,4% (24/31), LONG
win-rate μόλις 7,7% (1/13) — η αγορά ήταν σταθερά πτωτική (BEARISH) σε όλο
το διάστημα, και σχεδόν όλα τα LONG trades (κόντρα στη φάση) έχαναν.

**Διόρθωση**: το `-12` μαλακό penalty στα counter-bias trades δεν αρκούσε —
απλά τα έκανε σπανιότερα, όχι μηδενικά. Μπήκε **σκληρό μπλοκάρισμα**: όταν
η αγορά έχει ξεκάθαρη φάση (BULLISH/BEARISH), το bot **δεν ανοίγει καθόλου**
trade αντίθετο στη φάση, ανεξάρτητα από το σκορ — προσπερνά τον υποψήφιο και
ψάχνει τον επόμενο καλύτερο που συμφωνεί με τη φάση. Αν όλοι οι υποψήφιοι
είναι counter-bias, δεν ανοίγει κανένα trade αυτόν τον κύκλο.

---

### 22/06/2026

**Πρόβλημα που αναφέρθηκε**: μετά το reset μνήμης της 21/06, σερί 18
συνεχόμενων ζημιών (15,4% win-rate σε 26 trades, και LONG και SHORT να χάνουν).

**Διάγνωση**: δεν είναι το ίδιο πρόβλημα με πριν (τότε έχανε μόνο η μία
κατεύθυνση επειδή η αγορά είχε γυρίσει φάση). Εδώ χάνουν όλα μαζί, αμέσως
μετά το reset της μνήμης μάθησης — δηλαδή το bot διάλεγε trades βασισμένο
μόνο στο "ωμό" τεχνικό σκορ, χωρίς ακόμα τη διόρθωση από τα στατιστικά
win-rate ανά pattern (χρειάζονται ~15 trades ανά pattern για να "γεμίσει"
η εμπιστοσύνη). Σε μια ασταθή/choppy αγορά αυτό σημαίνει πολλές σερί ζημιές
μέχρι να ξαναχτιστεί η μνήμη.

**Διόρθωση**: προστέθηκε **circuit breaker** — αν συμβούν 6 συνεχόμενες
ζημιές, το bot σταματά να ανοίγει νέα trades για 45 λεπτά (στέλνει και
ειδοποίηση Telegram), ώστε να μη συνεχίζει να "αιμορραγεί" σε ξεκάθαρα
κακή περίοδο. Μετά τα 45 λεπτά ξαναξεκινά κανονικά. Ο μετρητής σερί
μηδενίζεται και σε κάθε reset (καθάρισμα trades ή πλήρες reset).

---

### 21/06/2026 (διόρθωση bug reset)

**Πρόβλημα που αναφέρθηκε**: τα κουμπιά reset μηδένιζαν το αρχείο στιγμιαία,
αλλά τα trades "ξαναγύριζαν" μετά από λίγο.

**Αιτία**: ο worker φόρτωνε τη λίστα των trades **μία φορά στην εκκίνηση**
σε μεταβλητή μνήμης, και μετά από κάθε κλείσιμο θέσης την ξανάγραφε ολόκληρη
στο αρχείο. Το reset άδειαζε το αρχείο, αλλά όχι αυτή τη λίστα μνήμης — έτσι
στο επόμενο κλείσιμο θέσης, η παλιά (γεμάτη) λίστα ξαναγραφόταν πάνω στο
άδειο αρχείο και "επανέφερε" τα παλιά trades.

**Διόρθωση**: το reset καθαρίζει πλέον και τη λίστα στη μνήμη του worker
(`trades.clear()`), όχι μόνο το αρχείο. Επίσης διορθώθηκε το πλήρες reset
να σβήνει και τα βοηθητικά αρχεία `-wal`/`-shm` του SQLite (μνήμη μάθησης),
ώστε να μην παραμένουν "κατάλοιπα" παλιάς μνήμης.

---

### 21/06/2026 (συνέχεια)

**Δύο ξεχωριστά κουμπιά reset** (πριν ήταν ένα συνδυαστικό):
- **🗑️ Καθάρισμα Trades**: μηδενίζει μόνο το ιστορικό trades/ανοιχτές θέσεις,
  η μνήμη μάθησης (AI) παραμένει άθικτη — για όταν θες απλά να "καθαρίσεις
  την οθόνη" χωρίς να χάσεις ό,τι έχει μάθει το bot.
- **🧠 Πλήρες Reset (+μνήμη)**: μηδενίζει trades ΚΑΙ τη μνήμη μάθησης — για
  πλήρες restart από την αρχή.
- Κάθε κουμπί θέλει το δικό του κουτάκι επιβεβαίωσης πριν ενεργοποιηθεί.

---

### 21/06/2026

**Πρόβλημα που εντοπίστηκε**: το win-rate έπεσε από 43.8% (18/06) σε 40.8%
(441 trades), με τις μέρες 19-20/06 να πέφτουν στο ~22%. Βρέθηκε ότι η
κατεύθυνση SHORT πήγαινε 57-67% τις μέρες 9/17/18 αλλά κατέρρευσε σε
15-20% τις 19-20, όταν η αγορά γύρισε από πτωτική σε ανοδική.

**Αιτία**: το `pattern_stats()` υπολόγιζε win-rate σαν lifetime μέσο όρο
όλων των αποτελεσμάτων ενός pattern, χωρίς να ξεχωρίζει σε ποια φάση
αγοράς (ανοδική/πτωτική) έγινε η κάθε νίκη/ήττα. Έτσι ένα pattern που
κέρδιζε σε πτωτική φάση συνέχιζε να θεωρείται "καλό" και μετά τη
στροφή της αγοράς σε ανοδική — η μνήμη δεν "καταλάβαινε" ότι η συνθήκη
άλλαξε.

**Διόρθωση**:
1. Το `bias_dir` (ανοδική/πτωτική/ουδέτερη φάση, υπολογισμένη από
   κινητούς μέσους 15λ) μπήκε μέσα στο `pattern_key` — η μνήμη μαθαίνει
   πλέον **ξεχωριστά ανά φάση αγοράς**, ώστε ένα pattern SHORT σε
   πτωτική φάση να μην "δανείζει" εμπιστοσύνη σε SHORT setups όταν η
   αγορά είναι ανοδική, και αντίστροφα.
2. Προστέθηκε ποινή (-12 score) σε trade που είναι αντίθετο με τη
   τρέχουσα φάση αγοράς (π.χ. SHORT ενώ η φάση είναι BULLISH) — πριν
   το bias λειτουργούσε μόνο ως μπόνους για ευθυγραμμισμένα trades,
   ποτέ ως φρένο για αντίθετα.

**Σημείωση**: επειδή το pattern_key άλλαξε, η παλιά ιστορία μάθησης
(`mexc_memory.db`) δεν θα ταιριάζει πλέον με τα νέα κλειδιά — λειτουργεί
ουσιαστικά σαν soft-reset, η μνήμη θα ξαναχτιστεί από την αρχή αλλά αυτή
τη φορά σωστά χωρισμένη ανά φάση αγοράς.

---

### 14/06/2026

**Persistence στο Railway**
- Συνδέσαμε Volume στο `/data` ώστε log, ιστορικό trades, ρυθμίσεις και η
  μνήμη μάθησης (`mexc_memory.db`) να επιβιώνουν σε redeploy/restart.

**Νέα ζεύγη**
- Η λίστα επιλογής ζευγαριών μεγάλωσε σε 35 (majors, large/mid caps, meme).

**Νέο κουμπί Βοήθεια**
- Προστέθηκε αναλυτικός οδηγός χρήσης (πώς λειτουργεί το AI scoring, η
  επιβεβαίωση KDJ/MACD/EMA, η μνήμη/μάθηση, το paper trading, κ.λπ.).

**Διόρθωση bug: θέσεις με Είσοδος = 0.0000**
- Βρέθηκε ότι όταν η τιμή από το MEXC δεν έρχεται σωστά, ανοιγόταν εικονική
  θέση με entry=0. Αυτό προκαλούσε σφάλμα διαίρεσης με το μηδέν που "πάγωνε"
  τον έλεγχο TP/SL για **όλα** τα ζεύγη σε κάθε κύκλο — γι' αυτό έβλεπες
  θέσεις σαν το SOL_USDT να μένουν ανοιχτές για μέρες.
- Διορθώθηκε: δεν ανοίγει πλέον θέση αν η τιμή είναι 0, ο έλεγχος κάθε
  ζεύγους γίνεται ξεχωριστά (ώστε ένα πρόβλημα σε ένα ζεύγος να μην μπλοκάρει
  τα άλλα), και τυχόν "φαντάσματα" με entry=0 αφαιρούνται αυτόματα.

**Διόρθωση εμφάνισης: τιμές PEPE κ.λπ. ως 0.0000**
- Ήταν μόνο πρόβλημα εμφάνισης (στρογγυλοποίηση 4 δεκαδικών) — οι υπολογισμοί
  P&L ήταν σωστοί. Προστέθηκε δυναμική ακρίβεια δεκαδικών ώστε πολύ μικρές
  τιμές (π.χ. 0.00001234) να εμφανίζονται κανονικά.

**Ανάλυση 176 κλειστών trades**
- 69 νίκες / 107 ηττες = 39.2% win-rate, πολύ κοντά στο breakeven (~40% για
  TP 1.5% / SL 1.0%) — δηλαδή το bot είναι περίπου ισόπαλο, όχι "χάνει".
- **Αποφασίσαμε**: όχι reset της μνήμης μάθησης (δεν είναι κατεστραμμένη,
  απλά χρειάζεται περισσότερα δεδομένα).
- **Αποφασίσαμε**: να περιμένουμε για περισσότερα trades πριν αλλάξουμε το
  `SCORE_MIN` (τρέχουσα τιμή 90) ή τη σχέση TP%/SL%.
""")

# ── Header ─────────────────────────────────────────────────
st.title("📈 MEXC AI Signal Bot")
running = bot_running()
c1, c2, c3, c4, c5 = st.columns([2, 2, 2, 1, 1])
c1.metric("Κατάσταση",  "🟢 Τρέχει" if running else "🔴 Παύση")
c2.metric("Take Profit", f"+{tp_pct}%")
c3.metric("Stop Loss",   f"-{sl_pct}%")
with c4:
    st.write("")
    if st.button("❓ Βοήθεια", use_container_width=True):
        show_help()
with c5:
    st.write("")
    if st.button("📝 Ημερολόγιο", use_container_width=True):
        show_journal()

b1, b2, b3, b4 = st.columns([1, 1, 1.4, 1.4])
if b1.button("▶ Εκκίνηση", type="primary", disabled=running, use_container_width=True):
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)
    st.rerun()

if b2.button("⏹ Παύση", disabled=not running, use_container_width=True):
    open(STOP_FILE, "w").close()
    st.rerun()

with b3:
    confirm_clear = st.checkbox("Επιβεβαίωση")
    if st.button("🗑️ Καθάρισμα Trades", disabled=not confirm_clear, use_container_width=True,
                  help="Μηδενίζει μόνο το ιστορικό trades/θέσεων. Η μνήμη μάθησης (AI) ΔΕΝ αγγίζεται."):
        for f in (TRADES_FILE, OPEN_FILE):
            if os.path.exists(f):
                os.remove(f)
        open(CLEAR_TRADES_FILE, "w").close()
        st.success("Μηδενίστηκε το ιστορικό trades. Η μνήμη μάθησης παραμένει άθικτη.")
        st.rerun()

with b4:
    confirm_reset = st.checkbox("Επιβεβαίωση πλήρη")
    if st.button("🧠 Πλήρες Reset (+μνήμη)", disabled=not confirm_reset, use_container_width=True,
                  help="Μηδενίζει trades, θέσεις ΚΑΙ τη μνήμη μάθησης (AI ξεκινά από το μηδέν)."):
        for f in (TRADES_FILE, OPEN_FILE, MEMORY_DB, MEMORY_DB + "-wal", MEMORY_DB + "-shm"):
            if os.path.exists(f):
                os.remove(f)
        open(RESET_FILE, "w").close()
        st.success("Μηδενίστηκαν trades, θέσεις και μνήμη μάθησης.")
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
            entry_dt = datetime.fromtimestamp(ot["time"], TZ)
            open_rows.append({
                "Ζεύγος": ot["pair"],
                "Κατ/νση": ot["direction"],
                "Ώρα Εισόδου": entry_dt.strftime("%d/%m %H:%M:%S"),
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
