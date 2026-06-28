"""
IPO Cross Tracker — backend API
================================
Yeh ek chhota Flask server hai jo:
  1. Chittorgarh se recently-listed Mainboard + SME IPOs ki list scrape karta hai
     (naam, issue price, listing date, symbol agar mile to).
  2. Yahoo Finance se har stock ka listing-day price, lowest price aur current price
     nikaalta hai (ek hi history call se).
  3. Sab kuch saaf JSON me, CORS enabled, taaki website browser se seedhe call kar sake.

Endpoints:
  GET  /health                         -> {"ok": true}
  GET  /ipos?type=all|mainboard|sme    -> scraped IPO list
  GET  /quote?symbol=RELIANCE&since=YYYY-MM-DD
                                        -> {current, lowest, listing, ...}

NOTE (zaroori): Chittorgarh apna page layout kabhi-kabhi badalta hai. Agar /ipos
khaali ya galat aaye, to neeche SOURCES ke URL / column-matching tweak karna padega.
README me isay fix karne ke steps diye hain.
"""

import io
import json
import os
import re
import time
import datetime as dt
from html.parser import HTMLParser

import requests
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # sabhi origins se requests allow (personal tool ke liye theek hai)

# Poore browser-jaise headers — minimal headers pe Chittorgarh bot samajh ke rok deta hai
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}


def _fetch_html(url):
    """Session banao, pehle homepage khol ke cookies lo, fir asli page Referer ke saath."""
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        # handshake: homepage hit -> cookies set ho jaati hain
        s.get("https://www.chittorgarh.com/", timeout=20)
    except requests.RequestException:
        pass  # handshake fail ho to bhi seedha try karo
    resp = s.get(url, headers={"Referer": "https://www.chittorgarh.com/"}, timeout=25)
    resp.raise_for_status()
    return resp.text

# --- Chittorgarh source pages ---
# Performance tracker me Issue/Listing/Current price teeno hote hain (listing date se sorted).
# Agar toot jaaye to URL/year yahan update karna (README dekho).
YEAR = "2026"
SOURCES = {
    "mainboard": f"https://www.chittorgarh.com/ipo/ipo_perf_tracker.asp?year={YEAR}",
    # SME perf-tracker ka exact URL confirm karna padega; pehle mainboard chala ke dekho.
    "sme": f"https://www.chittorgarh.com/ipo/ipo_perf_tracker.asp?year={YEAR}&ipotype=sme",
}

# Simple in-memory cache taaki har request pe scrape na karein (Yahoo/Chittorgarh
# ko bar-bar hit karne se rate-limit lag sakta hai). TTL seconds me.
_CACHE = {}
_TTL = 60 * 30  # 30 minutes


def _cache_get(key):
    item = _CACHE.get(key)
    if item and (time.time() - item[0] < _TTL):
        return item[1]
    return None


def _cache_set(key, value):
    _CACHE[key] = (time.time(), value)


# ---------------------------------------------------------------------------
# Chittorgarh scraping  (pure stdlib — koi pandas/lxml nahi, har machine pe chale)
# ---------------------------------------------------------------------------
class _TableParser(HTMLParser):
    """HTML me se saare <table> nikaal ke list-of-rows-of-cells bana deta hai."""
    def __init__(self):
        super().__init__()
        self.tables = []          # har table = list[ list[str] ]
        self._tdepth = 0
        self._cur = None          # chalu table ke rows
        self._row = None          # chalu row ke cells
        self._cell = None         # chalu cell ka text buffer

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._tdepth += 1
            if self._tdepth == 1:
                self._cur = []
        elif tag == "tr" and self._tdepth >= 1:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            self._row.append(text)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self._cur.append(self._row)
            self._row = None
        elif tag == "table":
            if self._tdepth == 1 and self._cur is not None:
                self.tables.append(self._cur)
                self._cur = None
            self._tdepth = max(0, self._tdepth - 1)


def _pick_idx(header, *needles):
    """header (list of column names) me se woh index do jo needle match kare."""
    for i, c in enumerate(header):
        low = str(c).lower()
        if any(n in low for n in needles):
            return i
    return None


def _pick_col(columns, *needles):
    """Column ka naam dhoondo jo kisi bhi needle ko contain kare (case-insensitive)."""
    for c in columns:
        low = str(c).lower()
        if any(n in low for n in needles):
            return c
    return None


def _to_number(val):
    """'₹ 1,234.50' / '120 to 126' / '-' jaise text ko number me badlo."""
    if val is None:
        return None
    s = re.sub(r"[^0-9.\-to ]", "", str(val)).strip()
    if not s or s == "-":
        return None
    # price band "120 to 126" -> upper end le lo
    if "to" in s:
        parts = [p for p in s.split("to") if p.strip()]
        try:
            return float(parts[-1].strip())
        except (ValueError, IndexError):
            return None
    try:
        return float(s)
    except ValueError:
        return None


def scrape_chittorgarh(ipo_type):
    """ek source page se IPO rows nikaalo. ipo_type: 'mainboard' ya 'sme'."""
    cached = _cache_get(f"scrape:{ipo_type}")
    if cached is not None:
        return cached

    url = SOURCES[ipo_type]
    html = _fetch_html(url)

    parser = _TableParser()
    parser.feed(html)
    # sabse bada table (sabse zyada cells) ko IPO list maano
    tables = [t for t in parser.tables if len(t) > 1]
    if not tables:
        return []
    table = max(tables, key=lambda t: sum(len(r) for r in t))

    header = table[0]
    name_i = _pick_idx(header, "company", "ipo", "issuer", "name")
    price_i = _pick_idx(header, "issue price", "issue")
    listing_i = _pick_idx(header, "listing price", "listing open", "list price")
    current_i = _pick_idx(header, "current price", "last price", "ltp", "current")
    date_i = _pick_idx(header, "listing date", "listing", "date")
    if name_i is None:
        name_i = 0  # fallback: pehla column hi naam maano

    def cell(r, i):
        return r[i] if (i is not None and i < len(r)) else None

    rows = []
    for r in table[1:]:
        name = (cell(r, name_i) or "").strip()
        if not name or name.lower() in ("company", "ipo", "issuer", "name"):
            continue
        rows.append({
            "name": name,
            "type": ipo_type,
            "issuePrice": _to_number(cell(r, price_i)),
            "listingPrice": _to_number(cell(r, listing_i)),
            "currentPrice": _to_number(cell(r, current_i)),
            "listingDate": cell(r, date_i),
            # NSE ticker reliably nahi milta — website me user symbol bhar sakta hai.
            "symbol": None,
            "source": "scraper",
        })

    _cache_set(f"scrape:{ipo_type}", rows)
    return rows


# ---------------------------------------------------------------------------
# Yahoo Finance price history -> listing / lowest / current
# ---------------------------------------------------------------------------
def yahoo_history(symbol, suffix):
    """Yahoo v8 chart endpoint se 2 saal ki daily history laao."""
    sym = f"{symbol.upper().strip()}{suffix}"
    api = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
        "?range=2y&interval=1d"
    )
    resp = requests.get(api, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        return None
    data = resp.json()
    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        return None
    meta = result.get("meta", {})
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = [c for c in (quote.get("close") or []) if c is not None]
    lows = [l for l in (quote.get("low") or []) if l is not None]
    if not closes:
        return None
    return {
        "symbol": sym,
        "current": meta.get("regularMarketPrice") or closes[-1],
        "listing": closes[0],                                  # pehla available close ~ listing price
        "lowest": min(lows) if lows else min(closes),          # listing ke baad ka lowest
        "fiftyTwoWeekLow": meta.get("fiftyTwoWeekLow"),
        "currency": meta.get("currency", "INR"),
    }


def get_quote(symbol):
    cached = _cache_get(f"quote:{symbol.upper()}")
    if cached is not None:
        return cached
    # pehle NSE (.NS), fir BSE (.BO) try karo
    out = yahoo_history(symbol, ".NS") or yahoo_history(symbol, ".BO")
    if out is not None:
        _cache_set(f"quote:{symbol.upper()}", out)
    return out


# --- richer details: market cap, 52w, PE/EPS, sector, shareholding ---
_YSESS = {}

def _yahoo_session():
    """Cookie + crumb wali session (quoteSummary ko crumb chahiye hota hai)."""
    now = time.time()
    cur = _YSESS.get("s")
    if cur and now - cur["t"] < 600:
        return cur["sess"], cur["crumb"]
    sess = requests.Session()
    sess.headers.update(HEADERS)
    crumb = None
    try:
        sess.get("https://fc.yahoo.com", timeout=10)
        r = sess.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10)
        if r.status_code == 200 and r.text and "<" not in r.text:
            crumb = r.text.strip()
    except requests.RequestException:
        pass
    _YSESS["s"] = {"sess": sess, "crumb": crumb, "t": now}
    return sess, crumb


def _raw(x):
    if isinstance(x, dict):
        return x.get("raw")
    return x


def yahoo_summary(symbol, suffix):
    sess, crumb = _yahoo_session()
    sym = symbol.upper().strip() + suffix
    modules = "price,summaryDetail,defaultKeyStatistics,assetProfile,majorHoldersBreakdown"
    url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{sym}"
           f"?modules={modules}")
    if crumb:
        url += "&crumb=" + requests.utils.quote(crumb)
    try:
        r = sess.get(url, timeout=12)
        if r.status_code != 200:
            return None
        return (r.json().get("quoteSummary", {}).get("result") or [None])[0]
    except requests.RequestException:
        return None


def get_detail(symbol, screener=None):
    cached = _cache_get(f"detail:{symbol.upper()}:{(screener or '').upper()}")
    if cached is not None:
        return cached
    out = {"symbol": symbol.upper()}
    # 1) chart meta — hamesha milta hai (52w, day range, volume, price)
    hist = yahoo_history(symbol, ".NS") or yahoo_history(symbol, ".BO")
    suffix = ".NS"
    if hist:
        out["current"] = hist.get("current")
        out["fiftyTwoWeekLow"] = hist.get("fiftyTwoWeekLow")
        out["lowest"] = hist.get("lowest")
        suffix = ".NS" if hist.get("symbol", "").endswith(".NS") else ".BO"
    # 2) quoteSummary — market cap, PE, EPS, sector, holders (best-effort)
    res = yahoo_summary(symbol, suffix) or {}
    sd = res.get("summaryDetail", {}) or {}
    ks = res.get("defaultKeyStatistics", {}) or {}
    pr = res.get("price", {}) or {}
    ap = res.get("assetProfile", {}) or {}
    mh = res.get("majorHoldersBreakdown", {}) or {}
    out.update({
        "name": pr.get("longName") or pr.get("shortName"),
        "marketCap": _raw(pr.get("marketCap")) or _raw(sd.get("marketCap")),
        "fiftyTwoWeekHigh": _raw(sd.get("fiftyTwoWeekHigh")) or out.get("fiftyTwoWeekHigh"),
        "fiftyTwoWeekLow": _raw(sd.get("fiftyTwoWeekLow")) or out.get("fiftyTwoWeekLow"),
        "dayHigh": _raw(sd.get("dayHigh")), "dayLow": _raw(sd.get("dayLow")),
        "volume": _raw(sd.get("volume")) or _raw(sd.get("averageVolume")),
        "peRatio": _raw(sd.get("trailingPE")) or _raw(ks.get("forwardPE")),
        "eps": _raw(ks.get("trailingEps")),
        "bookValue": _raw(ks.get("bookValue")),
        "sector": ap.get("sector"), "industry": ap.get("industry"),
        "insidersPct": _raw(mh.get("insidersPercentHeld")),
        "institutionsPct": _raw(mh.get("institutionsPercentHeld")),
        "currency": pr.get("currency", "INR"),
    })

    # 3) Screener.in — slug Yahoo symbol se alag ho sakta hai
    sc = scrape_screener(screener or symbol) or {}
    if sc.get("marketCapCr") is not None:
        out["marketCap"] = sc["marketCapCr"] * 1e7        # Cr -> rupees (Yahoo jaisa unit)
    for src, dst in (("pe", "peRatio"), ("bookValue", "bookValue"), ("high", "fiftyTwoWeekHigh"),
                     ("low", "fiftyTwoWeekLow")):
        if sc.get(src) is not None:
            out[dst] = sc[src]
    for f in ("roe", "roce", "promoterHolding", "dividendYield", "faceValue"):
        if sc.get(f) is not None:
            out[f] = sc[f]
    if sc.get("sector"):
        out["sectorFull"] = sc["sector"]
    if sc.get("name"):
        out["name"] = sc["name"]
    out["shareholding"] = sc.get("shareholding")
    out["shareholdingDate"] = sc.get("shareholdingDate")
    out["hasScreener"] = bool(sc)
    _cache_set(f"detail:{symbol.upper()}:{(screener or '').upper()}", out)
    return out


def _num(s):
    if s is None:
        return None
    s = re.sub(r"[^0-9.\-]", "", str(s))
    try:
        return float(s) if s not in ("", ".", "-") else None
    except ValueError:
        return None


def _classify_shp(label):
    l = label.lower()
    if l.startswith("promoter"):
        return "promoters"
    if l.startswith("fii"):
        return "fii"
    if l.startswith("dii"):
        return "dii"
    if l.startswith("govern") or l.startswith("govt"):
        return "government"
    if l.startswith("public"):
        return "public"
    if "shareholder" in l:
        return "shareholders"
    return None


def _parse_shareholding(html):
    """Screener ke shareholding section se latest quarter ka full breakdown."""
    m = re.search(r'id="quarterly-shp"(.*?)</table>', html, re.S)
    if not m:
        m = re.search(r'id="shareholding"(.*?)</table>', html, re.S)
    if not m:
        return None, None
    block = m.group(1)
    # latest quarter label (thead ka aakhri <th>)
    date = None
    th = re.search(r"<thead>(.*?)</thead>", block, re.S)
    if th:
        ths = re.findall(r"<th[^>]*>(.*?)</th>", th.group(1), re.S)
        if ths:
            date = re.sub(r"\s+", " ", re.sub("<[^>]+>", "", ths[-1])).strip() or None
    out = {}
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", block, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
        if len(tds) < 2:
            continue
        label = re.sub(r"\s+", " ", re.sub("<[^>]+>", "", tds[0])).strip()
        key = _classify_shp(label)
        if not key:
            continue
        # latest quarter = aakhri cell jisme number ho
        val = None
        for cell in reversed(tds[1:]):
            n = _num(re.sub("<[^>]+>", "", cell))
            if n is not None:
                val = n
                break
        out[key] = val
    return (out or None), date


def scrape_screener(symbol):
    """Screener.in company page (server-rendered) se fundamentals + promoter holding."""
    key = "screener:" + symbol.upper()
    cached = _cache_get(key)
    if cached is not None:
        return cached
    sess = requests.Session()
    sess.headers.update(HEADERS)
    out = {}
    html = ""
    for path in ("/company/%s/consolidated/" % symbol.upper(), "/company/%s/" % symbol.upper()):
        try:
            r = sess.get("https://www.screener.in" + path,
                         headers={"Referer": "https://www.screener.in/"}, timeout=15)
            if r.status_code == 200 and 'id="top-ratios"' in r.text:
                html = r.text
                break
        except requests.RequestException:
            continue
    if not html:
        _cache_set(key, out)
        return out
    # top-ratios list: har <li> me ek "name" aur ek-do "number"
    block = re.search(r'id="top-ratios"(.*?)</ul>', html, re.S)
    ratios = {}
    if block:
        for li in re.findall(r"<li[^>]*>(.*?)</li>", block.group(1), re.S):
            nm = re.search(r'class="name">(.*?)</span>', li, re.S)
            nums = re.findall(r'class="number">(.*?)</span>', li, re.S)
            if nm:
                name = re.sub(r"\s+", " ", re.sub("<[^>]+>", "", nm.group(1))).strip()
                ratios[name] = [_num(x) for x in nums]

    def g(name, i=0):
        v = ratios.get(name)
        return v[i] if v and i < len(v) and v[i] is not None else None

    out["marketCapCr"] = g("Market Cap")
    out["current"] = g("Current Price")
    out["high"] = g("High / Low", 0)
    out["low"] = g("High / Low", 1)
    out["pe"] = g("Stock P/E")
    out["bookValue"] = g("Book Value")
    out["dividendYield"] = g("Dividend Yield")
    out["roce"] = g("ROCE")
    out["roe"] = g("ROE")
    out["faceValue"] = g("Face Value")
    # promoter holding meta-description me reliable hota hai
    pm = re.search(r"Promoter Holding:\s*([\d.]+)\s*%", html)
    out["promoterHolding"] = float(pm.group(1)) if pm else None
    # sector breadcrumb
    secs = re.findall(r'title="(?:Broad Sector|Sector|Broad Industry|Industry)"[^>]*>(.*?)</a>',
                      html, re.S)
    out["sector"] = " · ".join(re.sub("<[^>]+>", "", s).strip() for s in secs) or None
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    if h1:
        out["name"] = re.sub(r"\s+", " ", re.sub("<[^>]+>", "", h1.group(1))).strip()
    sh, sh_date = _parse_shareholding(html)
    out["shareholding"] = sh
    out["shareholdingDate"] = sh_date
    _cache_set(key, out)
    return out


@app.get("/screener")
def screener():
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    data = scrape_screener(symbol)
    return jsonify({"symbol": symbol.upper(), "ok": bool(data), "data": data})


@app.get("/detail")
def detail():
    symbol = request.args.get("symbol", "").strip()
    screener = request.args.get("screener", "").strip() or None
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    return jsonify(get_detail(symbol, screener))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def yahoo_search_symbol(name):
    """Company naam -> NSE/BSE symbol, Yahoo ke search API se (no key)."""
    key = "resolve:" + name.lower()
    cached = _cache_get(key)
    if cached is not None:
        return cached
    # naam saaf karo: "Ltd."/"Limited" hata do, taaki match behtar ho
    q = re.sub(r"\b(limited|ltd\.?|pvt\.?|private)\b", "", name, flags=re.I).strip(" .,")
    api = ("https://query1.finance.yahoo.com/v1/finance/search?q="
           + requests.utils.quote(q) + "&quotesCount=8&newsCount=0&listsCount=0")
    out = {"symbol": None}
    try:
        r = requests.get(api, headers=HEADERS, timeout=12)
        if r.status_code == 200:
            quotes = r.json().get("quotes", [])
            pick = None
            # pehle NSE (.NS / exchange NSI), fir BSE (.BO)
            for suf, exch in ((".NS", "NSI"), (".BO", "BSE")):
                for it in quotes:
                    sym = it.get("symbol", "")
                    if sym.endswith(suf) or it.get("exchange") == exch:
                        pick = it
                        break
                if pick:
                    break
            if pick:
                out = {
                    "symbol": pick["symbol"].rsplit(".", 1)[0],   # bina .NS/.BO ke
                    "yahoo": pick["symbol"],
                    "match": pick.get("shortname") or pick.get("longname"),
                    "exchange": pick.get("exchange"),
                }
    except requests.RequestException:
        pass
    _cache_set(key, out)
    return out


def screener_search_slug(name):
    """Company naam -> Screener slug (price symbol se alag ho sakta hai)."""
    key = "scrsearch:" + name.lower()
    cached = _cache_get(key)
    if cached is not None:
        return cached
    q = re.sub(r"\b(limited|ltd\.?|pvt\.?|private)\b", "", name, flags=re.I).strip(" .,")
    slug = None
    try:
        sess = requests.Session()
        sess.headers.update(HEADERS)
        r = sess.get("https://www.screener.in/api/company/search/?q=" + requests.utils.quote(q),
                     headers={"Referer": "https://www.screener.in/"}, timeout=12)
        if r.status_code == 200:
            arr = r.json()
            if isinstance(arr, list) and arr:
                m = re.search(r"/company/([^/]+)/", arr[0].get("url", ""))
                if m:
                    slug = m.group(1)
    except (requests.RequestException, ValueError):
        pass
    _cache_set(key, slug)
    return slug


@app.get("/resolve")
def resolve():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    out = dict(yahoo_search_symbol(name) or {"symbol": None})
    out["screener"] = screener_search_slug(name)
    return jsonify(out)


@app.get("/health")
def health():
    return jsonify({"ok": True, "time": dt.datetime.utcnow().isoformat()})


@app.get("/ipos")
def ipos():
    ipo_type = (request.args.get("type") or "all").lower()
    wanted = ["mainboard", "sme"] if ipo_type == "all" else [ipo_type]
    out, errors = [], {}
    for t in wanted:
        if t not in SOURCES:
            continue
        try:
            out.extend(scrape_chittorgarh(t))
        except Exception as e:  # ek source toote to doosra phir bhi chale
            errors[t] = str(e)
    return jsonify({"count": len(out), "ipos": out, "errors": errors})


@app.get("/quote")
def quote():
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    q = get_quote(symbol)
    if q is None:
        return jsonify({"error": "not found", "symbol": symbol}), 404
    return jsonify(q)


@app.get("/debug")
def debug():
    """Live diagnosis: server ko Chittorgarh se kya mil raha hai."""
    ipo_type = (request.args.get("type") or "mainboard").lower()
    if ipo_type not in SOURCES:
        ipo_type = "mainboard"
    out = {"type": ipo_type, "url": SOURCES[ipo_type]}
    try:
        html = _fetch_html(SOURCES[ipo_type])
        out["httpOk"] = True
        out["htmlLength"] = len(html)
        low = html.lower()
        # bot-block ke clues
        out["looksBlocked"] = any(k in low for k in
            ["captcha", "cloudflare", "access denied", "just a moment",
             "enable javascript", "are you human"])
        parser = _TableParser()
        parser.feed(html)
        tables = [t for t in parser.tables if len(t) > 1]
        out["tablesFound"] = len(tables)
        out["tableRowCounts"] = [len(t) for t in tables][:10]
        if tables:
            big = max(tables, key=lambda t: sum(len(r) for r in t))
            out["pickedHeader"] = big[0]
            out["firstDataRows"] = big[1:4]
    except Exception as e:
        out["httpOk"] = False
        out["error"] = str(e)
    return jsonify(out)


STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")


@app.get("/")
def home():
    # agar index.html repo me hai to app serve karo, warna service info
    idx = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.exists(idx):
        return send_file(idx)
    return jsonify({
        "service": "IPO Cross Tracker API",
        "endpoints": ["/health", "/quote?symbol=XYZ", "/resolve?name=...",
                      "/detail?symbol=XYZ", "/screener?symbol=XYZ", "/state"],
    })


@app.get("/state")
def get_state():
    """Saved watchlist state (cross-device sync ke liye)."""
    try:
        with open(STATE_FILE) as f:
            return jsonify({"ok": True, "state": json.load(f)})
    except (OSError, ValueError):
        return jsonify({"ok": True, "state": None})


@app.post("/state")
def set_state():
    """Watchlist state save karo. Body = pura state JSON."""
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"ok": False, "error": "invalid json"}), 400
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
        return jsonify({"ok": True, "savedAt": dt.datetime.utcnow().isoformat()})
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    # Local testing: python app.py  ->  http://localhost:5000
    app.run(host="0.0.0.0", port=5000, debug=True)
