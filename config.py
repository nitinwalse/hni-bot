# =====================================================================
#  WEEKLY HNI BOT  —  config.py   (V1.0)
#  सगळी settings इथेच. Keys/token .env file मध्ये (code मध्ये नाही).
# =====================================================================
import os

def _load_env(path=".env"):
    """.env मधील KEY=VALUE वाचतो (python-dotenv ची गरज नाही)."""
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, path)
    if not os.path.exists(p):
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env()

VERSION = "V1.1"

# ---------- Keys (.env मधून) ----------
DHAN_CLIENT_ID    = os.getenv("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "")
TG_BOT_TOKEN      = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID        = os.getenv("TG_CHAT_ID", "")

# ---------- Mode ----------
LIVE = True                 # True = खरे orders. False = फक्त गणित + Telegram (order नाही)

# ---------- Index ----------
UNDERLYING_SCRIP = 13       # NIFTY (Dhan security id)
UNDERLYING_SEG   = "IDX_I"
LOT_SIZE         = 65       # NIFTY lot size — बदलला तर इथे बदला
FREEZE_QTY       = 1800     # एका order मधील कमाल qty (NSE freeze) — NSE वर तपासून ठेवा
STRIKE_STEP      = 100      # spot 100 च्या पटीत round (सर्व strategies साठी कायम)

# ---------- भांडवल / sets ----------
CAPITAL          = 1000000  # या strategy साठी एकूण भांडवल (₹10 लाख)
# [V1.1] Sets आता Telegram वरून: /sets 20  (तोपर्यंत bot फक्त setup + margin/set कळवतो, order नाही)
MARGIN_PER_SET_FALLBACK = 45000   # Dhan margin API चालला नाही तर हा आकडा (1 set = 1+3+2)

# ---------- Strike निवड (पर्याय A) ----------
L1_MIN_OTM  = 200           # सुरुवात 200 OTM
L1_MAX_OTM  = 600           # कमाल 600 OTM
GAP1_LIST   = (100, 200, 300)   # L1 -> L2 अंतर
# L2 -> L3 अंतर 100 पासून, पण GAP1 पेक्षा जास्त नाही (code मध्ये)

CREDIT_MAX_PCT = 0.6        # credit <= 0.6% (margin वर)
DEBIT_MAX_PCT  = 0.2        # debit  <= 0.2%

# ---------- Target / SL (NET, margin वर %) ----------
TP_NET_PCT   = 1.0          # net target %
SL_MODE      = "SYMMETRIC"  # [V1.1] SYMMETRIC = target जेवढ्या MTM अंतरावर तेवढ्याच अंतरावर SL
                            #        NET_PCT   = खाली SL_NET_PCT प्रमाणे net SL
SL_NET_PCT   = 1.5          # फक्त SL_MODE = "NET_PCT" असेल तेव्हा
SLIPPAGE_RS  = 0.5          # exit वेळी प्रत्येक unit वर ₹0.5 घसरण गृहीत

# ---------- वेळा (IST) ----------
SCAN_START   = "09:45"      # setup तपासणी सुरू
SCAN_END     = "15:00"      # यानंतर नवीन entry नाही
EXIT_TIME    = "15:15"      # time-exit दिवशी या वेळी exit
EXIT_DAYS_BEFORE_EXPIRY = 4 # expiry च्या 4 कॅलेंडर दिवस आधी (सुट्टी/शनिवार-रविवार असल्यास आधीचा trading दिवस)
SCAN_EVERY_SEC    = 60      # setup किती सेकंदांनी तपासायचा
MONITOR_EVERY_SEC = 5       # trade चालू असताना P&L किती सेकंदांनी
STATUS_EVERY_MIN  = 60      # Telegram status किती मिनिटांनी

ONE_TRADE_PER_EXPIRY = False  # [V1.1] एका expiry मध्ये अनेक trades चालतील
NO_REENTRY_SAME_DAY  = False  # [V1.1] exit च्या दिवशीही नवीन trade चालेल
REENTRY_GAP_SEC      = 60     # [V1.1] exit नंतर किमान 1 मिनिट थांबून नवीन setup

# ---------- Order chase ----------
CHASE_TRIES    = 6          # limit order किती वेळा नव्या bid/ask वर
CHASE_WAIT_SEC = 4          # प्रत्येक प्रयत्नानंतर थांबणे
MARKET_FALLBACK = True      # प्रयत्न संपले तर MARKET order

# ---------- Charges (Dhan + सरकारी) — दर बदलले तर इथे ----------
BROKERAGE_PER_ORDER = 20.0
STT_SELL   = 0.001          # 0.1% sell premium
TXN_RATE   = 0.0003503      # NSE txn (premium turnover)
SEBI_RATE  = 0.000001
STAMP_BUY  = 0.00003
GST_RATE   = 0.18

# ---------- NSE सुट्ट्या (YYYY-MM-DD) — NSE च्या यादीतून भरा ----------
NSE_HOLIDAYS = {
    "2026-11-24",
    # "2026-12-25",
}

STATE_FILE = "hni_state.json"
LOG_FILE   = "hni_bot.log"
