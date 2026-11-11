# =====================================================================
#  WEEKLY HNI BOT  —  hni_bot.py   (V1.4)
#  NIFTY 1:3:2 Call Ratio | सतत setup तपासणी | दुसरी expiry
#  Exit: Net TP 1% / Net SL 1% / expiry च्या 4 दिवस आधी 15:15
# =====================================================================
import base64
import hashlib
import hmac
import json
import logging
import re
import struct
import math
import os
import sys
import time
import datetime as dt
from zoneinfo import ZoneInfo

import requests

import config as C

IST = ZoneInfo("Asia/Kolkata")
BASE = "https://api.dhan.co/v2"
HERE = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(os.path.join(HERE, C.LOG_FILE), encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("hni")


# ---------------------------------------------------------------------
#  वेळ / तारीख
# ---------------------------------------------------------------------
def now():
    return dt.datetime.now(IST)

def hm(s):
    h, m = s.split(":")
    return dt.time(int(h), int(m))

def is_trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in C.NSE_HOLIDAYS

def market_open(t):
    return is_trading_day(t.date()) and hm("09:15") <= t.time() <= hm("15:30")

def exit_date_for(expiry):
    """expiry च्या 4 कॅलेंडर दिवस आधी; तो trading दिवस नसेल तर आधीचा trading दिवस."""
    d = expiry - dt.timedelta(days=C.EXIT_DAYS_BEFORE_EXPIRY)
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d

def r05(x):
    """₹0.05 tick ला round."""
    return round(round(float(x) / 0.05) * 0.05, 2)

def inr(x):
    s = "-" if x < 0 else ""
    return f"{s}₹{abs(x):,.0f}"


# ---------------------------------------------------------------------
#  [V1.2] TOTP ने रोज नवीन Dhan token (जुन्या icbot ची पद्धत)
# ---------------------------------------------------------------------
def totp_now(secret, step=30, digits=6):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    msg = struct.pack(">Q", int(time.time()) // step)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)

def generate_token():
    """PIN + TOTP ने Dhan कडून नवीन token. परत token किंवा None."""
    if not (C.DHAN_PIN and C.DHAN_TOTP_SECRET):
        return None, "PIN/TOTP .env मध्ये नाही"
    try:
        r = requests.post("https://auth.dhan.co/app/generateAccessToken",
                          params={"dhanClientId": str(C.DHAN_CLIENT_ID), "pin": C.DHAN_PIN,
                                  "totp": totp_now(C.DHAN_TOTP_SECRET)}, timeout=20)
        j = r.json() if r.text else {}
        d = j.get("data") if isinstance(j.get("data"), dict) else {}
        tok = j.get("accessToken") or j.get("access_token") or d.get("accessToken")
        if not tok:
            return None, str(j)[:200]
        return tok, j.get("expiryTime", "")
    except Exception as e:
        return None, str(e)

def save_token_env(tok):
    p = os.path.join(HERE, ".env")
    txt = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    if re.search(r"^DHAN_ACCESS_TOKEN=.*$", txt, flags=re.M):
        txt = re.sub(r"^DHAN_ACCESS_TOKEN=.*$", "DHAN_ACCESS_TOKEN=" + tok, txt, count=1, flags=re.M)
    else:
        txt += "\nDHAN_ACCESS_TOKEN=" + tok + "\n"
    with open(p, "w", encoding="utf-8") as f:
        f.write(txt)
    os.chmod(p, 0o600)


# ---------------------------------------------------------------------
#  Telegram
# ---------------------------------------------------------------------
class TG:
    def __init__(self):
        self.url = f"https://api.telegram.org/bot{C.TG_BOT_TOKEN}"
        self.offset = None
        self._last_err = {}

    def send(self, text):
        log.info("TG: %s", text.replace("\n", " | "))
        if not C.TG_BOT_TOKEN or not C.TG_CHAT_ID:
            return
        try:
            requests.post(f"{self.url}/sendMessage",
                          data={"chat_id": C.TG_CHAT_ID, "text": "[HNI] " + text},
                          timeout=10)
        except Exception as e:
            log.warning("TG send fail: %s", e)

    def send_throttled(self, key, text, every_sec=600):
        t = time.time()
        if t - self._last_err.get(key, 0) >= every_sec:
            self._last_err[key] = t
            self.send(text)

    def poll(self):
        if not C.TG_BOT_TOKEN:
            return []
        try:
            p = {"timeout": 0}
            if self.offset is not None:
                p["offset"] = self.offset
            r = requests.get(f"{self.url}/getUpdates", params=p, timeout=10).json()
            out = []
            for u in r.get("result", []):
                self.offset = u["update_id"] + 1
                m = u.get("message") or {}
                if str(m.get("chat", {}).get("id")) == str(C.TG_CHAT_ID):
                    out.append((m.get("text") or "").strip())
            return out
        except Exception as e:
            log.warning("TG poll fail: %s", e)
            return []


# ---------------------------------------------------------------------
#  Dhan API
# ---------------------------------------------------------------------
class DhanError(Exception):
    pass

class Dhan:
    def __init__(self):
        self.h = {"access-token": C.DHAN_ACCESS_TOKEN,
                  "client-id": C.DHAN_CLIENT_ID,
                  "Content-Type": "application/json",
                  "Accept": "application/json"}
        self._last_oc = 0.0
        self._last_q = 0.0

    def set_token(self, tok):
        C.DHAN_ACCESS_TOKEN = tok
        self.h["access-token"] = tok

    def _req(self, method, path, body=None):
        r = requests.request(method, BASE + path, headers=self.h,
                             data=json.dumps(body) if body is not None else None,
                             timeout=15)
        if r.status_code == 401:
            raise DhanError("401 — Access token चुकीचा/संपला. .env मध्ये नवीन token टाका.")
        try:
            js = r.json()
        except Exception:
            raise DhanError(f"{path} HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code >= 400:
            raise DhanError(f"{path} HTTP {r.status_code}: {str(js)[:300]}")
        return js

    # ---- option chain (Dhan मर्यादा: 3 सेकंदात 1 call) ----
    def expiries(self):
        js = self._req("POST", "/optionchain/expirylist",
                       {"UnderlyingScrip": C.UNDERLYING_SCRIP, "UnderlyingSeg": C.UNDERLYING_SEG})
        return sorted(dt.date.fromisoformat(x) for x in js.get("data", []))

    def chain(self, expiry):
        wait = 3.2 - (time.time() - self._last_oc)
        if wait > 0:
            time.sleep(wait)
        self._last_oc = time.time()
        js = self._req("POST", "/optionchain",
                       {"UnderlyingScrip": C.UNDERLYING_SCRIP, "UnderlyingSeg": C.UNDERLYING_SEG,
                        "Expiry": expiry.isoformat()})
        d = js.get("data", {})
        spot = float(d.get("last_price", 0) or 0)
        ce = {}
        for k, v in (d.get("oc") or {}).items():
            c = (v or {}).get("ce")
            if not c:
                continue
            ce[int(round(float(k)))] = {
                "sec": str(c.get("security_id")),
                "ltp": float(c.get("last_price") or 0),
                "bid": float(c.get("top_bid_price") or 0),
                "ask": float(c.get("top_ask_price") or 0),
            }
        return spot, ce

    # ---- quote (bid/ask/ltp) ----
    def quotes(self, sec_ids):
        wait = 1.1 - (time.time() - self._last_q)
        if wait > 0:
            time.sleep(wait)
        self._last_q = time.time()
        js = self._req("POST", "/marketfeed/quote", {"NSE_FNO": [int(s) for s in sec_ids]})
        raw = (js.get("data") or {}).get("NSE_FNO", {}) or {}
        out = {}
        for sid, q in raw.items():
            dep = q.get("depth") or {}
            buy = dep.get("buy") or [{}]
            sell = dep.get("sell") or [{}]
            out[str(sid)] = {"ltp": float(q.get("last_price") or 0),
                             "bid": float(buy[0].get("price") or 0),
                             "ask": float(sell[0].get("price") or 0)}
        return out

    # ---- margin ----
    def margin_for(self, legs):
        """legs = [(sec, side, qty, price)] — Dhan multi margin. चालले नाही तर None."""
        body = {"dhanClientId": C.DHAN_CLIENT_ID, "includePosition": False, "includeOrders": False,
                "scripList": [{"exchangeSegment": "NSE_FNO", "transactionType": s, "quantity": int(q),
                               "productType": "MARGIN", "securityId": str(sec),
                               "price": float(p), "triggerPrice": 0} for sec, s, q, p in legs]}
        try:
            js = self._req("POST", "/margincalculator/multi", body)
            for k in ("totalMargin", "total_margin", "totalMarginRequired"):
                if k in js and float(js[k]) > 0:
                    return float(js[k])
            d = js.get("data") or {}
            for k in ("totalMargin", "total_margin", "totalMarginRequired"):
                if k in d and float(d[k]) > 0:
                    return float(d[k])
            log.warning("margin response ओळखता आले नाही: %s", str(js)[:300])
        except Exception as e:
            log.warning("margin API fail: %s", e)
        return None

    def positions(self):
        js = self._req("GET", "/positions")
        if isinstance(js, dict):
            js = js.get("data", [])
        return js or []

    def funds(self):
        js = self._req("GET", "/fundlimit")
        avail = js.get("availabelBalance", js.get("availableBalance", 0))
        return float(avail or 0), float(js.get("utilizedAmount", 0) or 0)

    # ---- orders ----
    def place(self, sec, side, qty, price, otype="LIMIT", tag="HNI"):
        body = {"dhanClientId": C.DHAN_CLIENT_ID, "correlationId": tag[:25],
                "transactionType": side, "exchangeSegment": "NSE_FNO",
                "productType": "MARGIN", "orderType": otype, "validity": "DAY",
                "securityId": str(sec), "quantity": int(qty),
                "price": r05(price) if otype == "LIMIT" else 0,
                "triggerPrice": 0, "afterMarketOrder": False}
        js = self._req("POST", "/orders", body)
        oid = js.get("orderId")
        if not oid:
            raise DhanError(f"order id मिळाला नाही: {js}")
        return str(oid)

    def status(self, oid):
        js = self._req("GET", f"/orders/{oid}")
        if isinstance(js, list):
            js = js[0] if js else {}
        if isinstance(js.get("data"), dict):
            js = js["data"]
        return (str(js.get("orderStatus", "")).upper(),
                int(js.get("filledQty", 0) or 0),
                float(js.get("averageTradedPrice", 0) or 0))

    def cancel(self, oid):
        try:
            self._req("DELETE", f"/orders/{oid}")
        except Exception as e:
            log.warning("cancel %s: %s", oid, e)


# ---------------------------------------------------------------------
#  Charges
# ---------------------------------------------------------------------
def charges(buy_turn, sell_turn, n_orders):
    brok = C.BROKERAGE_PER_ORDER * n_orders
    stt = sell_turn * C.STT_SELL
    txn = (buy_turn + sell_turn) * C.TXN_RATE
    sebi = (buy_turn + sell_turn) * C.SEBI_RATE
    stamp = buy_turn * C.STAMP_BUY
    gst = (brok + txn + sebi) * C.GST_RATE
    return brok + stt + txn + sebi + stamp + gst

def n_slices(qty):
    return max(1, math.ceil(qty / C.FREEZE_QTY))


# ---------------------------------------------------------------------
#  Strike निवड (पर्याय A) — pure function, test करता येते
# ---------------------------------------------------------------------
def _buy_px(o):
    return o["ask"] if o["ask"] > 0 else o["ltp"]

def _sell_px(o):
    return o["bid"] if o["bid"] > 0 else o["ltp"]

def pick_combo(spot, ce, margin_per_set, lot=None):
    """
    परत: (combo dict किंवा None, कारण/तपशील string)
    क्रम: (1) L1 spot च्या सर्वात जवळ (200 पासून) (2) gap-up तोटा कमी (3) कमाल नफा जास्त
    Net = 3×Bid(L2) − Ask(L1) − 2×Ask(L3)   [entry ला प्रत्यक्ष मिळणारे भाव]
    """
    lot = lot or C.LOT_SIZE
    base = int(round(spot / C.STRIKE_STEP) * C.STRIKE_STEP)
    tried = []
    for l1 in range(C.L1_MIN_OTM, C.L1_MAX_OTM + 1, C.STRIKE_STEP):
        valid = []
        for g1 in C.GAP1_LIST:
            for g2 in range(C.STRIKE_STEP, g1 + 1, C.STRIKE_STEP):
                k1 = base + l1
                k2 = k1 + g1
                k3 = k2 + g2
                if k1 not in ce or k2 not in ce or k3 not in ce:
                    continue
                o1, o2, o3 = ce[k1], ce[k2], ce[k3]
                p1, p2, p3 = _buy_px(o1), _sell_px(o2), _buy_px(o3)
                if min(p1, p2, p3) <= 0:
                    continue
                net = 3 * p2 - p1 - 2 * p3
                pct = net * lot / margin_per_set * 100
                ok = (net >= 0 and pct <= C.CREDIT_MAX_PCT) or (net < 0 and -pct <= C.DEBIT_MAX_PCT)
                loss_pts = 2 * g2 - g1          # gap-up मधील तोटा (points, + = तोटा)
                maxp = g1 + net
                tried.append(f"{l1}/{l1+g1}/{l1+g1+g2}:{pct:+.2f}%")
                if ok:
                    valid.append({"base": base, "k1": k1, "k2": k2, "k3": k3,
                                  "otm": (l1, l1 + g1, l1 + g1 + g2),
                                  "p1": p1, "p2": p2, "p3": p3,
                                  "sec1": o1["sec"], "sec2": o2["sec"], "sec3": o3["sec"],
                                  "net": round(net, 2), "pct": round(pct, 3),
                                  "loss_pts": loss_pts, "maxp": round(maxp, 2)})
        if valid:
            valid.sort(key=lambda c: (c["loss_pts"], -c["maxp"]))
            return valid[0], "OK"
    return None, "एकही combination filter मध्ये बसले नाही. तपासले: " + ", ".join(tried[:12])


# ---------------------------------------------------------------------
#  State
# ---------------------------------------------------------------------
def load_state():
    p = os.path.join(HERE, C.STATE_FILE)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"status": "FLAT", "paused": False, "trade": None,
            "last_expiry": None, "last_exit_day": None}

def save_state(s):
    p = os.path.join(HERE, C.STATE_FILE)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


HELP_TEXT = (
    "📋 Commands:\n"
    "/status — सध्याची स्थिती (trade, MTM, TP/SL)\n"
    "/settings — सर्व settings\n"
    "/preview — आत्ता setup कसा बसतो ते (order नाही)\n"
    "/sets — सध्याचे sets पाहा\n"
    "/sets 7 — sets बदला (उदा. 7)\n"
    "/pause — नवीन entry बंद (चालू trade चालू राहतो)\n"
    "/resume — नवीन entry पुन्हा सुरू\n"
    "/exitnow — चालू trade लगेच बंद\n"
    "/help — ही यादी")


# ---------------------------------------------------------------------
#  Bot
# ---------------------------------------------------------------------
class Bot:
    def __init__(self):
        self.d = Dhan()
        self.tg = TG()
        self.s = load_state()
        self.last_scan = 0.0
        self.last_mon = 0.0
        self.last_status = time.time()
        self.n_orders = 0
        self.token_day = self.s.get("token_day")   # [V1.4] restart नंतरही लक्षात
        self.last_refresh_try = 0.0
        self.spot_seen = {}          # [V1.3] {date: (पहिला spot, वेळ, बदलला?)}
        self.entry_block_until = 0.0

    # ---------- [V1.2] token ----------
    def refresh_token(self, reason):
        if time.time() - self.last_refresh_try < 120:
            return False
        self.last_refresh_try = time.time()
        tok, info = generate_token()
        if tok:
            self.d.set_token(tok)
            save_token_env(tok)
            self.token_day = now().date().isoformat()
            self.s["token_day"] = self.token_day
            save_state(self.s)
            self.tg.send(f"🔑 नवीन Dhan token तयार ({reason}) | मुदत: {info or '२४ तास'}")
            return True
        self.tg.send_throttled("tokfail", f"❌ Token बनला नाही ({reason}): {info}", 900)
        return False

    def token_check(self, t):
        if (is_trading_day(t.date()) and t.time() >= hm(C.TOKEN_REFRESH_TIME)
                and self.token_day != t.date().isoformat()):
            # [V1.4] आजचा token आधीच चालत असेल (restart) तर नवीन बनवू नका — Dhan "Invalid TOTP" देतो
            if self.token_day is None or self.token_day == "":
                try:
                    self.d.funds()
                    self.token_day = t.date().isoformat()
                    self.s["token_day"] = self.token_day
                    save_state(self.s)
                    log.info("सध्याचा token चालतो — नवीन बनवला नाही")
                    return
                except Exception:
                    pass
            self.refresh_token("रोजचा")

    # ---------- expiry ----------
    def target_expiry(self, today):
        ex = [e for e in self.d.expiries() if e >= today]
        if len(ex) < 2:
            return None
        return ex[1]                      # चालू सोडून दुसरी

    # ---------- order execute (chase) ----------
    def exec_leg(self, sec, side, qty, label):
        """पूर्ण qty भरेपर्यंत limit chase → शेवटी market. परत (filled_qty, avg)."""
        filled, value = 0, 0.0
        remaining = qty
        while remaining > 0:
            part = min(remaining, C.FREEZE_QTY)
            got, val = self._exec_slice(sec, side, part, label)
            filled += got
            value += val
            if got < part:
                break
            remaining -= part
        avg = value / filled if filled else 0.0
        return filled, avg

    def _exec_slice(self, sec, side, qty, label):
        if not C.LIVE:
            q = self.d.quotes([sec])[sec]
            px = q["ask"] if side == "BUY" else q["bid"]
            return qty, (px or q["ltp"]) * qty
        left, value = qty, 0.0
        for attempt in range(C.CHASE_TRIES):
            q = self.d.quotes([sec]).get(sec, {})
            px = q.get("ask") if side == "BUY" else q.get("bid")
            if not px:
                px = q.get("ltp", 0)
            if px <= 0:
                time.sleep(C.CHASE_WAIT_SEC)
                continue
            oid = self.d.place(sec, side, left, px, "LIMIT", f"HNI-{label}")
            self.n_orders += 1
            time.sleep(C.CHASE_WAIT_SEC)
            st, fq, avg = self.d.status(oid)
            if st != "TRADED":
                self.d.cancel(oid)
                time.sleep(1)
                st, fq, avg = self.d.status(oid)
            if fq > 0:
                value += fq * avg
                left -= fq
            if st == "REJECTED" and fq == 0:
                log.error("%s rejected", label)
            if left <= 0:
                return qty, value
        if left > 0 and C.MARKET_FALLBACK:
            oid = self.d.place(sec, side, left, 0, "MARKET", f"HNI-{label}-M")
            self.n_orders += 1
            for _ in range(10):
                time.sleep(1.5)
                st, fq, avg = self.d.status(oid)
                if st in ("TRADED", "REJECTED", "CANCELLED", "EXPIRED"):
                    break
            if fq > 0:
                value += fq * avg
                left -= fq
        return qty - left, value

    # ---------- sizing ----------
    def per_set_margin(self, c):
        lot = C.LOT_SIZE
        legs = [(c["sec1"], "BUY", lot, c["p1"]), (c["sec2"], "SELL", 3 * lot, c["p2"]),
                (c["sec3"], "BUY", 2 * lot, c["p3"])]
        m = self.d.margin_for(legs)
        if not m:
            self.tg.send_throttled("mfb", f"⚠️ Dhan margin API चालला नाही — fallback {inr(C.MARGIN_PER_SET_FALLBACK)}/set वापरतो")
            return float(C.MARGIN_PER_SET_FALLBACK), "fallback"
        return m, "dhan"

    def sets_for(self, m_set):
        """[V1.1] Sets = तुम्ही /sets ने दिलेले. उपलब्ध fund कमी असेल तर कमी करतो."""
        sets = int(self.s.get("sets") or 0)
        if sets <= 0:
            return 0
        try:
            avail, _ = self.d.funds()
            fit = int(avail // m_set)
            if fit < sets:
                self.tg.send_throttled("fund", f"⚠️ उपलब्ध fund {inr(avail)} मध्ये फक्त {fit} sets बसतात (दिलेले {sets})")
                sets = fit
        except Exception as e:
            log.warning("fundlimit: %s", e)
        return max(sets, 0)

    # ---------- [V1.3] market खरंच चालू आहे का ----------
    def market_live(self, spot):
        """सुट्टीच्या दिवशी spot बदलत नाही. आज spot बदलल्याचे दिसले तरच True."""
        d = now().date().isoformat()
        rec = self.spot_seen.get(d)
        if rec is None:
            self.spot_seen = {d: [spot, time.time(), False]}
            return False
        if rec[2]:
            return True
        if abs(spot - rec[0]) >= 0.05:
            rec[2] = True
            return True
        if time.time() - rec[1] >= C.LIVE_CHECK_MIN_SEC * 5:
            self.tg.send_throttled("mktclosed", f"🔕 Spot {spot} बदलत नाही — market बंद/सुट्टी समजतो, entry नाही", 3600)
        return False

    # ---------- scan / entry ----------
    def can_enter(self, t, expiry):
        if self.s.get("paused"):
            return False, "paused"
        if not (hm(C.SCAN_START) <= t.time() <= hm(C.SCAN_END)):
            return False, "scan वेळ नाही"
        if C.NO_REENTRY_SAME_DAY and self.s.get("last_exit_day") == t.date().isoformat():
            return False, "आज exit झाले"
        if time.time() < self.entry_block_until:
            return False, "entry फसल्यानंतर थांबणे"
        if time.time() - float(self.s.get("last_exit_ts") or 0) < C.REENTRY_GAP_SEC:
            return False, "exit नंतर 1 मिनिट थांबणे"
        if C.ONE_TRADE_PER_EXPIRY and self.s.get("last_expiry") == expiry.isoformat():
            return False, "या expiry चा trade झाला"
        if t.date() >= exit_date_for(expiry):
            return False, "exit तारीख आली"
        return True, ""

    def find_setup(self, expiry):
        spot, ce = self.d.chain(expiry)
        if spot <= 0 or not ce:
            return None, None, "option chain रिकामा"
        m_guess = float(C.MARGIN_PER_SET_FALLBACK)
        c, why = pick_combo(spot, ce, m_guess)
        if not c:
            return None, spot, why
        m_set, src = self.per_set_margin(c)
        if src == "dhan" and abs(m_set - m_guess) / m_guess > 0.02:
            c2, why2 = pick_combo(spot, ce, m_set)       # खऱ्या margin ने पुन्हा
            if not c2:
                return None, spot, why2
            if c2["sec2"] != c["sec2"] or c2["sec1"] != c["sec1"] or c2["sec3"] != c["sec3"]:
                m_set, src = self.per_set_margin(c2)
            c = c2
        c["m_set"] = round(m_set, 2)
        c["m_src"] = src
        return c, spot, "OK"

    def try_entry(self, t, preview_only=False):
        expiry = self.target_expiry(t.date())
        if not expiry:
            return
        ok, why = self.can_enter(t, expiry)
        if not ok and not preview_only:
            return
        c, spot, why = self.find_setup(expiry)
        if spot and not preview_only and not self.market_live(spot):
            return
        if not c:
            if preview_only:
                self.tg.send(f"🔎 Preview: setup नाही — {why}")
            else:
                self.tg.send_throttled("nosetup", f"🔎 Setup अजून नाही (spot {spot}). {why[:300]}", 1800)
            return
        sets = self.sets_for(c["m_set"])
        exd = exit_date_for(expiry)
        lot = C.LOT_SIZE
        q1, q2, q3 = lot * sets, 3 * lot * sets, 2 * lot * sets
        kind = "Credit" if c["net"] >= 0 else "Debit"
        msg = (f"{'🔎 PREVIEW' if preview_only else '🟢 SETUP मिळाला'}\n"
               f"Spot {spot:.1f} → base {c['base']} | Expiry {expiry:%d-%b-%Y} | Exit {exd:%d-%b} {C.EXIT_TIME}\n"
               f"BUY {c['k1']} CE ×{sets} lot @~{c['p1']}\n"
               f"SELL {c['k2']} CE ×{3*sets} lot @~{c['p2']}\n"
               f"BUY {c['k3']} CE ×{2*sets} lot @~{c['p3']}\n"
               f"OTM {c['otm'][0]}/{c['otm'][1]}/{c['otm'][2]} | {kind} {abs(c['net']):.2f} pts = {abs(c['pct']):.2f}%\n"
               f"Margin/set {inr(c['m_set'])} ({c['m_src']}) | Sets {sets} | Deployed ~{inr(c['m_set']*sets)}\n"
               f"Gap-up तोटा {c['loss_pts']} pts/set | कमाल नफा {c['maxp']} pts/set")
        if preview_only or sets < 1:
            if sets < 1:
                if not self.s.get("sets"):
                    msg += (f"\n❓ Sets ठरलेले नाहीत. 10 लाखात ~{int(C.CAPITAL // c['m_set'])} sets बसतात.\n"
                            f"Telegram वर पाठवा: /sets <संख्या>   (उदा. /sets 20)")
                    if not preview_only:
                        self.tg.send_throttled("asksets", msg, 1800)
                        return
                else:
                    msg += "\n⛔ Fund कमी — trade नाही"
            self.tg.send(msg)
            return
        self.tg.send(msg + f"\n{'⚡ LIVE' if C.LIVE else '🧪 DRY'} entry सुरू…")
        self.safe_enter(c, sets, expiry, exd)

    def safe_enter(self, c, sets, expiry, exd):
        """[V1.3] entry मध्ये कोणताही error आला तरी Dhan वरील भरलेले legs बंद करतो."""
        try:
            self.enter(c, sets, expiry, exd)
        except Exception as e:
            log.exception("entry")
            self.tg.send(f"❌ Entry मध्ये error: {str(e)[:250]}\nDhan वरील भरलेले legs तपासून बंद करतो…")
            if self.s.get("status") != "OPEN":
                self.reconcile_unwind([c["sec1"], c["sec2"], c["sec3"]])
        finally:
            if self.s.get("status") != "OPEN":
                self.entry_block_until = time.time() + C.ENTRY_FAIL_COOLDOWN_MIN * 60
                self.tg.send(f"⏳ Entry झाली नाही — {C.ENTRY_FAIL_COOLDOWN_MIN} मिनिटे नवीन प्रयत्न नाही.")

    def reconcile_unwind(self, secs):
        """[V1.3] Dhan positions मधून या strikes चे उघडे qty शोधून बंद करतो."""
        try:
            pos = self.d.positions()
        except Exception as e:
            self.tg.send(f"🚨 Positions वाचता आले नाहीत ({e}). Dhan app मध्ये लगेच हाताने तपासा!")
            return
        left = []
        for p in pos:
            sid = str(p.get("securityId"))
            q = int(p.get("netQty", 0) or 0)
            if sid in secs and q != 0:
                side = "SELL" if q > 0 else "BUY"
                try:
                    got, _ = self.exec_leg(sid, side, abs(q), "FIX")
                except Exception as e:
                    got = 0
                    log.error("fix %s: %s", sid, e)
                if got < abs(q):
                    left.append(f"{p.get('tradingSymbol', sid)} {q}")
        if left:
            self.tg.send("🚨 हे positions अजून उघडे आहेत — Dhan मध्ये हाताने बंद करा: " + ", ".join(left))
        else:
            self.tg.send("✅ Dhan वर या strikes चे कोणतेही उघडे position नाही.")

    def enter(self, c, sets, expiry, exd):
        lot = C.LOT_SIZE
        legs = [
            {"name": "L1", "strike": c["k1"], "sec": c["sec1"], "side": "BUY",  "qty": lot * sets},
            {"name": "L3", "strike": c["k3"], "sec": c["sec3"], "side": "BUY",  "qty": 2 * lot * sets},
            {"name": "L2", "strike": c["k2"], "sec": c["sec2"], "side": "SELL", "qty": 3 * lot * sets},
        ]                                   # क्रम: आधी BUY, मग SELL
        try:
            _, used_before = self.d.funds()
        except Exception:
            used_before = None
        self.n_orders = 0
        done = []
        for lg in legs:
            got, avg = self.exec_leg(lg["sec"], lg["side"], lg["qty"], f"IN-{lg['name']}")
            if got < lg["qty"]:
                lg2 = dict(lg, qty=got, avg=avg)
                if got > 0:
                    done.append(lg2)
                self.tg.send(f"❌ Entry अपूर्ण: {lg['name']} {lg['strike']} फक्त {got}/{lg['qty']}. "
                             f"भरलेले legs परत बंद करतो.")
                self.unwind(done)
                self.s["last_exit_day"] = now().date().isoformat()
                save_state(self.s)
                return
            lg["avg"] = round(avg, 2)
            done.append(lg)
        buy_t = sum(l["qty"] * l["avg"] for l in done if l["side"] == "BUY")
        sell_t = sum(l["qty"] * l["avg"] for l in done if l["side"] == "SELL")
        entry_ch = charges(buy_t, sell_t, self.n_orders)
        deployed = c["m_set"] * sets
        if C.LIVE and used_before is not None:
            try:
                time.sleep(2)
                _, used_after = self.d.funds()
                real = used_after - used_before
                if real > 0.3 * deployed:
                    deployed = real
            except Exception:
                pass
        units = sum(l["qty"] for l in done)
        tp_net = C.TP_NET_PCT / 100 * deployed
        exit_cost_est = C.SLIPPAGE_RS * units + charges(buy_t, sell_t, 3 * n_slices(3 * lot * sets))
        if C.TARGET_MODE == "GROSS":
            # [V1.3] MTM ±1.1% — monitor net शी तुलना करतो, म्हणून खर्च आत धरून रूपांतर
            costs = entry_ch + exit_cost_est
            tp_net = C.TP_GROSS_PCT / 100 * deployed - costs
            sl_net = C.SL_GROSS_PCT / 100 * deployed + costs
        elif C.SL_MODE == "SYMMETRIC":
            # [V1.1] MTM मध्ये target जेवढा दूर तेवढाच SL: net SL = net TP + 2 × (charges + slippage)
            sl_net = tp_net + 2 * (entry_ch + exit_cost_est)
        else:
            sl_net = C.SL_NET_PCT / 100 * deployed
        self.s.update({
            "status": "OPEN",
            "last_expiry": expiry.isoformat(),
            "trade": {"expiry": expiry.isoformat(), "exit_date": exd.isoformat(),
                      "sets": sets, "legs": done, "legs_orig": [dict(l) for l in done],
                      "deployed": round(deployed, 2),
                      "entry_time": now().isoformat(timespec="seconds"),
                      "net_entry_pts": round((sell_t - buy_t) / (lot * sets), 2),
                      "entry_charges": round(entry_ch, 2), "units": units,
                      "tp_net": round(tp_net, 2), "sl_net": round(sl_net, 2),
                      "exit_cost_est": round(exit_cost_est, 2), "peak": 0.0, "low": 0.0},
        })
        save_state(self.s)
        tr = self.s["trade"]
        legtxt = "\n".join(f"{l['side']} {l['strike']} CE {l['qty']} @ {l['avg']}" for l in done)
        self.tg.send(
            f"✅ ENTRY पूर्ण ({sets} sets)\n{legtxt}\n"
            f"Net entry {tr['net_entry_pts']:+.2f} pts/set | Charges {inr(entry_ch)}\n"
            f"Deployed {inr(deployed)}\n"
            f"🎯 Net TP {inr(tp_net)} → MTM trigger +{inr(tp_net + entry_ch + exit_cost_est)}\n"
            f"🛑 Net SL {inr(-sl_net)} → MTM trigger {inr(-(sl_net - entry_ch - exit_cost_est))}\n"
            f"⏰ Time exit {exd:%d-%b} {C.EXIT_TIME}")

    def unwind(self, legs):
        for lg in legs:
            if lg.get("qty", 0) <= 0:
                continue
            side = "SELL" if lg["side"] == "BUY" else "BUY"
            got, _ = self.exec_leg(lg["sec"], side, lg["qty"], f"UW-{lg['name']}")
            if got < lg["qty"]:
                self.tg.send(f"🚨 UNWIND अपूर्ण {lg['name']} {lg['strike']} — Dhan वर लगेच हाताने तपासा!")

    # ---------- monitor ----------
    def mtm(self):
        tr = self.s["trade"]
        q = self.d.quotes([l["sec"] for l in tr["legs"]])
        total = 0.0
        for l in tr["legs"]:
            o = q.get(l["sec"], {})
            mark = (o["bid"] + o["ask"]) / 2 if o.get("bid", 0) > 0 and o.get("ask", 0) > 0 else o.get("ltp", 0)
            if mark <= 0:
                return None
            sign = 1 if l["side"] == "BUY" else -1
            total += sign * (mark - l["avg"]) * l["qty"]
        return total

    def monitor(self, t):
        tr = self.s["trade"]
        if tr.get("exiting"):                         # अपूर्ण exit — आधी तो पूर्ण करा
            self.exit_trade(tr.get("exit_reason", "↻ EXIT RETRY"), 0)
            return
        m = self.mtm()
        if m is None:
            return
        net = m - tr["entry_charges"] - tr["exit_cost_est"]
        tr["peak"] = max(tr.get("peak", 0.0), net)
        tr["low"] = min(tr.get("low", 0.0), net)
        exd = dt.date.fromisoformat(tr["exit_date"])
        reason = None
        if net >= tr["tp_net"]:
            reason = "🎯 TARGET"
        elif net <= -tr["sl_net"]:
            reason = "🛑 SL"
        elif t.date() > exd or (t.date() == exd and t.time() >= hm(C.EXIT_TIME)):
            reason = "⏰ TIME EXIT"
        if time.time() - self.last_status >= C.STATUS_EVERY_MIN * 60:
            self.last_status = time.time()
            self.tg.send(f"📊 Status: MTM {inr(m)} | Net (खर्च वजा) {inr(net)} | "
                         f"TP {inr(tr['tp_net'])} / SL {inr(-tr['sl_net'])} | Exit {exd:%d-%b}")
        if reason:
            self.exit_trade(reason, net, m)

    def exit_trade(self, reason, est_net, mtm_at=None):
        tr = self.s["trade"]
        if mtm_at is not None and "mtm_at_trigger" not in tr:
            tr["mtm_at_trigger"] = round(mtm_at, 2)
        if not tr.get("exiting"):
            self.tg.send(f"{reason} — exit सुरू (अंदाजे net {inr(est_net)})")
        tr["exiting"] = True
        tr["exit_reason"] = reason
        save_state(self.s)
        order = sorted(tr["legs"], key=lambda l: 0 if l["side"] == "SELL" else 1)  # आधी SELL leg बंद
        self.n_orders = 0
        pnl = tr.get("realized", 0.0)
        ex_buy, ex_sell = tr.get("ex_buy", 0.0), tr.get("ex_sell", 0.0)
        remaining = []
        for l in order:
            side = "BUY" if l["side"] == "SELL" else "SELL"
            got, avg = self.exec_leg(l["sec"], side, l["qty"], f"OUT-{l['name']}")
            if got > 0:
                if side == "BUY":
                    ex_buy += got * avg
                else:
                    ex_sell += got * avg
                sign = 1 if l["side"] == "BUY" else -1
                pnl += sign * (avg - l["avg"]) * got
            if got < l["qty"]:
                remaining.append(dict(l, qty=l["qty"] - got))
        if remaining:
            tr["legs"] = remaining
            tr["realized"], tr["ex_buy"], tr["ex_sell"] = pnl, ex_buy, ex_sell
            tr["exit_orders"] = tr.get("exit_orders", 0) + self.n_orders
            save_state(self.s)
            self.tg.send("🚨 Exit अपूर्ण — उरलेले legs पुढच्या फेरीत पुन्हा बंद करतो. Dhan तपासा.")
            return
        exit_ch = charges(ex_buy, ex_sell, self.n_orders + tr.get("exit_orders", 0))
        net = pnl - tr["entry_charges"] - exit_ch
        pct = net / tr["deployed"] * 100 if tr["deployed"] else 0
        mt = tr.get("mtm_at_trigger")
        slip = (mt - pnl) if mt is not None else 0.0
        tr["slip"] = round(slip, 2)
        slip_txt = (f"\n📉 Slippage: trigger वेळी MTM {inr(mt)} → प्रत्यक्ष {inr(pnl)} = {inr(slip)} "
                    f"(₹{slip / max(tr.get('units', 1), 1):.2f}/unit)") if mt is not None else ""
        self.tg.send(f"🏁 EXIT पूर्ण: {reason}{slip_txt}\nGross {inr(pnl)} | Charges {inr(tr['entry_charges'] + exit_ch)}\n"
                     f"NET {inr(net)} ({pct:+.2f}% of {inr(tr['deployed'])})\n"
                     f"Trade मधील कमाल net {inr(tr.get('peak', 0))} / किमान {inr(tr.get('low', 0))}")
        self._journal(tr, reason, pnl, exit_ch, net)
        self.s.update({"status": "FLAT", "trade": None, "last_exit_day": now().date().isoformat(),
                       "last_exit_ts": time.time()})
        save_state(self.s)

    def _journal(self, tr, reason, gross, exit_ch, net):
        p = os.path.join(HERE, "hni_trades.csv")
        new = not os.path.exists(p)
        with open(p, "a", encoding="utf-8") as f:
            if new:
                f.write("entry_time,exit_time,expiry,sets,strikes,net_entry_pts,deployed,mtm_at_trigger,gross,slippage,charges,net,reason\n")
            ks = "/".join(str(l["strike"]) for l in sorted(tr.get("legs_orig", tr["legs"]), key=lambda x: x["strike"]))
            f.write(f"{tr['entry_time']},{now().isoformat(timespec='seconds')},{tr['expiry']},{tr['sets']},"
                    f"{ks},{tr['net_entry_pts']},{tr['deployed']},{tr.get('mtm_at_trigger', '')},{gross:.2f},{tr.get('slip', '')},"
                    f"{tr['entry_charges'] + exit_ch:.2f},{net:.2f},{reason}\n")

    # ---------- Telegram commands ----------
    def commands(self):
        for cmd in self.tg.poll():
            c = cmd.lower().split()
            if not c:
                continue
            if c[0] == "/status":
                tr = self.s.get("trade")
                if self.s["status"] == "OPEN" and tr:
                    m = self.mtm()
                    net = (m or 0) - tr["entry_charges"] - tr["exit_cost_est"]
                    self.tg.send(f"📊 OPEN {tr['sets']} sets | MTM {inr(m or 0)} | Net {inr(net)} | "
                                 f"TP {inr(tr['tp_net'])} SL {inr(-tr['sl_net'])} | Exit {tr['exit_date']}")
                else:
                    self.tg.send(f"FLAT | paused={self.s.get('paused')} | last expiry {self.s.get('last_expiry')}")
            elif c[0] == "/pause":
                self.s["paused"] = True
                save_state(self.s)
                self.tg.send("⏸ नवीन entry बंद (चालू trade चालू राहील)")
            elif c[0] == "/resume":
                self.s["paused"] = False
                save_state(self.s)
                self.tg.send("▶️ नवीन entry सुरू")
            elif c[0] == "/exitnow":
                if self.s["status"] == "OPEN":
                    self.exit_trade("✋ MANUAL EXIT", 0)
                else:
                    self.tg.send("चालू trade नाही")
            elif c[0] == "/preview":
                self.try_entry(now(), preview_only=True)
            elif c[0] == "/sets":
                if len(c) > 1 and c[1].isdigit():
                    self.s["sets"] = int(c[1])
                    save_state(self.s)
                    self.tg.send(f"✅ Sets = {c[1]} (1:3:2 × {c[1]}). पुढच्या setup पासून लागू.")
                else:
                    self.tg.send(f"सध्याचे sets: {self.s.get('sets') or 'ठरलेले नाहीत'}. बदलायला: /sets 20")
            elif c[0] == "/settings":
                self.tg.send(self.settings_text())
            elif c[0] in ("/help", "/start", "/commands"):
                self.tg.send(HELP_TEXT)
            else:
                self.tg.send("❓ ही command माहीत नाही.\n" + HELP_TEXT)

    def settings_text(self):
        if C.TARGET_MODE == "GROSS":
            tpsl = f"TP/SL: GROSS MTM +{C.TP_GROSS_PCT}% / −{C.SL_GROSS_PCT}% (margin वर)"
        else:
            tpsl = f"TP/SL: NET +{C.TP_NET_PCT}% / SL {C.SL_MODE} {C.SL_NET_PCT}%"
        return (f"⚙️ Settings — HNI Bot {C.VERSION}\n"
                f"Mode: {'LIVE' if C.LIVE else 'DRY'} | paused={self.s.get('paused')}\n"
                f"Sets: {self.s.get('sets') or 'ठरलेले नाहीत'} (1:3:2) | Lot {C.LOT_SIZE} | Freeze {C.FREEZE_QTY}\n"
                f"Strikes: L1 {C.L1_MIN_OTM}–{C.L1_MAX_OTM} OTM, अंतर {list(C.GAP1_LIST)}\n"
                f"Filter: Credit ≤{C.CREDIT_MAX_PCT}% / Debit ≤{C.DEBIT_MAX_PCT}%\n"
                f"{tpsl} | Slippage गृहीत ₹{C.SLIPPAGE_RS}/unit\n"
                f"Entry: {C.SCAN_START}–{C.SCAN_END}, दर {C.SCAN_EVERY_SEC} सेकंद | Expiry: दुसरी\n"
                f"Exit: expiry च्या {C.EXIT_DAYS_BEFORE_EXPIRY} दिवस आधी {C.EXIT_TIME}\n"
                f"Re-entry: exit नंतर {C.REENTRY_GAP_SEC} सेकंद | फसल्यास {C.ENTRY_FAIL_COOLDOWN_MIN} मिनिटे थांबणे\n"
                f"Token: रोज {C.TOKEN_REFRESH_TIME} (आज: {self.token_day or '—'})")

    # ---------- main loop ----------
    def run(self):
        self.tg.send(f"🤖 HNI Bot {C.VERSION} सुरू | {'LIVE' if C.LIVE else 'DRY'} | state {self.s['status']}")
        while True:
            try:
                self.commands()
                t = now()
                self.token_check(t)
                if market_open(t):
                    if self.s["status"] == "OPEN":
                        if time.time() - self.last_mon >= C.MONITOR_EVERY_SEC:
                            self.last_mon = time.time()
                            self.monitor(t)
                    elif time.time() - self.last_scan >= C.SCAN_EVERY_SEC:
                        self.last_scan = time.time()
                        self.try_entry(t)
                    time.sleep(1)
                else:
                    time.sleep(15)
            except DhanError as e:
                log.error("Dhan: %s", e)
                if "401" in str(e) and self.refresh_token("401"):
                    continue
                self.tg.send_throttled("dhan", f"⚠️ Dhan error: {e}", 600)
                time.sleep(10)
            except Exception as e:
                log.exception("loop")
                self.tg.send_throttled("loop", f"⚠️ Error: {e}", 600)
                time.sleep(10)


if __name__ == "__main__":
    if not C.DHAN_ACCESS_TOKEN or not C.DHAN_CLIENT_ID:
        print(".env मध्ये DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN भरा")
        sys.exit(1)
    Bot().run()
