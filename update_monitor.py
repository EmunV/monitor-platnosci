#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Monitor Platnosci - skrypt aktualizacji v3"""

import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import os, json, re, csv, base64, time
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

try:
    import requests
except ImportError:
    print("Brak biblioteki 'requests'. Zainstaluj: pip install requests")
    sys.exit(1)

BASE_DIR   = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
API_BASE   = "https://app.comarchbetterfly.pl/api2/public"
TOKEN_URL  = f"{API_BASE}/token"

MIN_NR_LEN = 5   # min. długość numeru faktury po normalizacji (unikamy fałszywych trafień)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
def load_config():
    if not CONFIG_PATH.exists():
        print(f"Brak pliku config.json: {BASE_DIR}")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

# ─────────────────────────────────────────────────────────────────────────────
# BETTERFLY API
# ─────────────────────────────────────────────────────────────────────────────
def get_token(client_id, client_secret):
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp  = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Authorization": f"Basic {creds}"},
        data="grant_type=client_credentials",
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Blad tokena ({resp.status_code}): {resp.text[:300]}")
    return resp.json()["access_token"]

def api_get_all(token, endpoint, params=None, label=""):
    """Pobiera wszystkie strony z endpointu Betterfly."""
    headers   = {"Authorization": f"Bearer {token}"}
    url       = f"{API_BASE}/{endpoint}"
    results   = []
    page      = 1
    page_size = 500
    seen_ids  = set()

    while page <= 20:
        p = {"page": page, "pageSize": page_size, **(params or {})}
        print(f"    strona {page}...", end=" ", flush=True)
        try:
            resp = requests.get(url, headers=headers, params=p, timeout=30)
        except requests.Timeout:
            print("timeout"); break
        except Exception as e:
            print(f"blad: {e}"); break

        if resp.status_code in (404, 401, 405):
            print(f"niedostepny ({resp.status_code})"); break
        if resp.status_code != 200:
            print(f"blad HTTP {resp.status_code}"); break

        try:
            data = resp.json()
        except Exception:
            print("blad JSON"); break

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = (data.get("Items") or data.get("Data") or
                     data.get("Records") or data.get("invoices") or
                     data.get("results") or [])
        else:
            items = []

        if not items:
            print("brak danych"); break

        new_items = []
        for item in items:
            iid = item.get("Id") or item.get("id") or item.get("Number") or id(item)
            if iid not in seen_ids:
                seen_ids.add(iid)
                new_items.append(item)

        print(f"{len(new_items)} nowych (lacznie: {len(results) + len(new_items)})")
        results.extend(new_items)

        if len(new_items) == 0 or len(items) < page_size:
            break
        page += 1
        time.sleep(0.3)

    return results

# ─────────────────────────────────────────────────────────────────────────────
# PARSOWANIE FAKTUR
# ─────────────────────────────────────────────────────────────────────────────
def flt(v):
    try:   return round(float(v or 0), 2)
    except: return 0.0

def _due_fallback(issued, days=14):
    if not issued:
        return ""
    try:
        return (datetime.strptime(issued, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    except Exception:
        return ""

def parse_inv_out(item, co):
    """Faktura sprzedaży (należność)."""
    brutto = flt(item.get("GrossTotal") or item.get("TotalGrossValue") or item.get("GrossValue") or 0)
    netto  = flt(item.get("NetTotal")   or item.get("TotalNetValue")   or item.get("NetValue")   or brutto)
    vat    = flt(item.get("VatTotal")   or item.get("TotalVatValue")   or round(brutto - netto, 2))

    pay_status = item.get("PaymentStatus", 0)
    paid   = (pay_status == 1)
    do_zap = 0.0 if paid else brutto

    purchasing = item.get("PurchasingParty") or {}
    partner = (purchasing.get("Name") or item.get("CustomerName") or item.get("BuyerName") or "").strip()
    nip_partner = (purchasing.get("TaxNumber") or purchasing.get("NIP") or
                   item.get("CustomerNIP") or item.get("BuyerNIP") or "").strip()

    nr     = (item.get("Number") or item.get("InvoiceNumber") or item.get("DocumentNumber") or "").strip()
    issued = str(item.get("IssueDate")   or item.get("SalesDate")       or item.get("Date") or "")[:10]
    due    = str(item.get("PaymentDate") or item.get("DueDate")         or item.get("PaymentDueDate") or "")[:10]
    if not due:
        due = _due_fallback(issued)

    return {
        "co": co, "t": "out", "nr": nr, "data": issued, "due": due,
        "partner": partner, "nabywca": partner, "nip_partner": nip_partner,
        "brutto": brutto, "netto": netto, "vat": vat, "do_zap": do_zap,
        "paid": paid,
        "mx": None, "ma": None, "md": None,
        "partial": False, "fv_case": "", "match_level": 0, "cancelled": False,
    }

def parse_inv_in(item, co):
    """Faktura zakupu (zobowiązanie)."""
    netto  = flt(item.get("NetTotal")   or item.get("TotalNetValue")   or item.get("NetValue")   or 0)
    vat    = flt(item.get("VatTotal")   or item.get("TotalVatValue")   or item.get("VatValue")   or 0)
    brutto = flt(item.get("GrossTotal") or item.get("TotalGrossValue") or item.get("GrossValue") or round(netto + vat, 2))

    selling = item.get("SellingParty") or {}
    partner = (selling.get("Name") or item.get("SellingPartyName") or
               item.get("SupplierName") or item.get("CustomerName") or "").strip()
    nip_partner = (selling.get("TaxNumber") or selling.get("NIP") or
                   item.get("SupplierNIP") or "").strip()

    nr     = (item.get("Number") or item.get("InvoiceNumber") or item.get("DocumentNumber") or "").strip()
    issued = str(item.get("PurchaseDate") or item.get("IssueDate") or item.get("Date") or "")[:10]
    due    = str(item.get("PaymentDate") or item.get("DueDate") or "")[:10]
    if not due:
        due = _due_fallback(issued)

    return {
        "co": co, "t": "in", "nr": nr, "data": issued, "due": due,
        "partner": partner, "nabywca": partner, "nip_partner": nip_partner,
        "brutto": brutto, "netto": netto, "vat": vat, "do_zap": brutto,
        "paid": False,
        "mx": None, "ma": None, "md": None,
        "partial": False, "fv_case": "", "match_level": 0, "cancelled": False,
    }

# ─────────────────────────────────────────────────────────────────────────────
# PARSOWANIE CSV mBANK — oba formaty (6-kol stary i 9-kol nowy)
# ─────────────────────────────────────────────────────────────────────────────
# FORMAT A (stary, 6 kolumn):
#   #Data operacji;Opis operacji;Rachunek;Kategoria;Kwota;Saldo
# FORMAT B (nowy, 9 kolumn):
#   #Data operacji;#Data księgowania;#Opis operacji;#Tytuł;#Nadawca/Odbiorca;#Numer konta;#Kwota;#Saldo;#Numer transakcji

def detect_mbank_format(header_line):
    h = header_line.lower()
    if any(x in h for x in ["#tytu", "nadawca", "odbiorca", "#numer konta", "#data ksi"]):
        return "B"
    return "A"

def clean_amount(s):
    """'1 234,56 PLN' / '-1 234,56' / '−1 234,56' → float"""
    # Normalizuj różne znaki minusa (myślnik, minus typograficzny, en-dash)
    s = s.strip()
    s = s.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    s = s.replace("\xa0", "").replace("\u00a0", "").replace(" ", "")
    s = s.replace("PLN", "").replace("zł", "").replace("ZL", "")
    # Polskie formatowanie: przecinek = separator dziesiętny, kropka = tysiące
    # Sprawdź czy to format "1.234,56" czy "1,234.56"
    if "," in s and "." in s:
        # Który jest ostatni — ten jest dziesiętny
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    # Usuń wielokrotne kropki (separator tysięcy)
    parts = s.split(".")
    if len(parts) > 2:
        s = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return round(float(s), 2)
    except Exception:
        return None

def detect_co(lines, firms, filepath=""):
    """
    Rozpoznaje firmę po:
    1. file_tag z config.json dopasowany do nazwy pliku (najszybsze i najpewniejsze)
    2. Pełny numer konta w nagłówku CSV
    3. konto_suffix od najdłuższego do najkrótszego
    """
    filename     = Path(filepath).stem.upper() if filepath else ""
    header       = "\n".join(lines[:40])
    header_ns    = re.sub(r"\s", "", header)
    sorted_firms = sorted(firms,
                          key=lambda f: len(re.sub(r"\s", "", f.get("konto_suffix", ""))),
                          reverse=True)

    # Próba 1: file_tag (opcjonalny klucz w config, np. "file_tag": "_EN_")
    for f in sorted_firms:
        tag = f.get("file_tag", "").upper()
        if tag and tag in filename:
            print(f"    detect_co: '{Path(filepath).name}' -> {f['name']} (file_tag)")
            return f["co"]

    # Próba 2: pełny numer konta (26 cyfr bez spacji)
    for f in sorted_firms:
        full_ns = re.sub(r"\s", "", f.get("konto", ""))
        if full_ns and len(full_ns) >= 20 and full_ns in header_ns:
            return f["co"]

    # Próba 3: konto_suffix >= 6 cyfr
    for f in sorted_firms:
        suf = re.sub(r"\s", "", f.get("konto_suffix", ""))
        if len(suf) >= 6 and suf in header_ns:
            return f["co"]

    # Próba 4: konto_suffix krótki (ostateczny fallback)
    for f in sorted_firms:
        suf = re.sub(r"\s", "", f.get("konto_suffix", ""))
        if suf and suf in header_ns:
            return f["co"]

    return -1

def parse_bank_csv(filepath, co, existing_keys):
    """Parsuje CSV mBanku — zwraca listę transakcji."""
    with open(filepath, encoding="utf-8-sig", errors="replace") as f:
        raw_lines = f.readlines()

    hdr_idx = None
    fmt = "A"
    for i, line in enumerate(raw_lines):
        if "#data operacji" in line.lower() or "#Data operacji" in line:
            hdr_idx = i
            fmt = detect_mbank_format(line)
            break

    if hdr_idx is None:
        print(f"  [UWAGA] {filepath} — nie znaleziono naglowka CSV")
        return []

    # indeksy kolumn
    if fmt == "B":
        COL = {"date": 0, "book": 1, "desc": 2, "title": 3, "party": 4, "iban": 5, "amount": 6}
    else:
        COL = {"date": 0, "desc": 1, "account": 2, "cat": 3, "amount": 4}

    min_cols = max(COL.values()) + 1
    txs = []

    for line in raw_lines[hdr_idx + 1:]:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        try:
            row = next(csv.reader(io.StringIO(line), delimiter=";"))
        except Exception:
            continue
        if len(row) < min_cols:
            continue

        dt = row[COL["date"]].strip()
        if not re.match(r"\d{4}-\d{2}-\d{2}", dt):
            continue

        amt = clean_amount(row[COL["amount"]])
        if amt is None:
            continue

        if fmt == "B":
            # Łączymy opis + tytuł + nadawca/odbiorca — daje dużo więcej danych do matchowania
            desc_parts = [
                row[COL["desc"]].strip(),
                row[COL["title"]].strip(),
                row[COL["party"]].strip() if COL.get("party") is not None else "",
            ]
            desc = " | ".join(p for p in desc_parts if p)
            iban = row[COL["iban"]].strip() if COL.get("iban") is not None else ""
        else:
            desc = row[COL["desc"]].strip()
            iban = ""

        key = (dt, amt, desc[:40])
        if key in existing_keys:
            continue
        existing_keys.add(key)

        dc = re.sub(r"[\x00-\x1f\x7f]", " ", desc).strip()
        txs.append({
            "co": co, "id": 0,
            "dt": dt,
            "dc": dc[:300],
            "a": amt,
            "iban": iban,
            "ref": None,
            "case": extract_case(dc),
            "inv_nr": extract_inv_nr_from_desc(dc),
        })

    return txs

# ─────────────────────────────────────────────────────────────────────────────
# WYCIĄGANIE NUMERÓW SPRAW I FAKTUR
# ─────────────────────────────────────────────────────────────────────────────
CASE_PATTERNS = [
    (r"\bK[mM][nN]?\s*(\d+[\s/]\d{2,4})\b",                     "KM"),
    (r"\bI\s*[Cc]\s+(\d+[\s/]\d{2,4})\b",                        "IC"),
    (r"\bI\s*C(?:ps|o|upr)\s+(\d+[\s/]\d{2,4})\b",               "IC"),
    (r"\b(?:[IVX]{1,4}\s+)?I\s*N\s*[Ss]\s+(\d+[\s/]\d{2,4})\b", "INS"),
    (r"\bINS(\d+/\d{2,4})\b",                                     "INS"),
    (r"\bINs(\d+/\d{2,4})\b",                                     "INS"),
    (r"\b(?:[IVX]{1,4}\s+)?G\s*[Cc]\s+(\d+[\s/]\d{2,4})\b",     "GC"),
    (r"\bIXGC\s*(\d+[\s/]\d{2,4})\b",                             "GC"),
    (r"\bGUp[-s]*[/]?(\d+/\d{4})\b",                              "GUp"),
    (r"\bNc[-\s]*[eE]\s+(\d+[\s/]\d{2,4})\b",                    "Nc-e"),
    (r"\b(?:VI{1,3}|IV|IX)\s*N\s*[Ss]\s+(\d+[\s/]\d{2,4})\b",  "INS"),
    (r"\bKs[Pp]\s*[:.]?\s*(\d+/\d{2,4})\b",                      "KsP"),
    (r"\bI\s*AC[az]?\s+(\d+[\s/]\d{2,4})\b",                     "ACa"),
    (r"\bGN\s*[bB]\s+(\d+[\s/]\d{2,4})\b",                       "GNb"),
    (r"[Ss]ygn\.?\s+akt\.?\s+([A-Z]{1,6}\s*\d+[\s/]\d{2,4})",   "SYGN"),
]

def extract_case(desc):
    found, seen = [], set()
    for pat, lbl in CASE_PATTERNS:
        for m in re.finditer(pat, desc, re.IGNORECASE):
            nr = re.sub(r"(\d)\s+(\d)", r"\1/\2", m.group(1).strip())
            k  = lbl + " " + nr
            if k not in seen:
                seen.add(k)
                found.append(k)
    return ", ".join(found)

def extract_inv_nr_from_desc(desc):
    """Wyciąga numer faktury z opisu przelewu."""
    if not desc:
        return ""
    patterns = [
        r"fa\s*(FS/\d+/\d+/\d+)",
        r"fa\s*(FV/\d+/\d+/\d+)",
        r"fa\s*(FA/\d+/\d+/\d+)",
        r",(FS/\d+/\d+/\d+)",
        r",(FV/\d+/\d+/\d+)",
        r"NUMER DOKUMENTU:\s*([A-Z]+/\d+/\d+/\d+)",
        r"\.\.([A-Z]+/\d+/\d+/\d+)\.\.",
        r"\b(FS/\d{2}/\d{1,2}/\d+)\b",
        r"\b(FV/\d+/\d+/\d+)\b",
        r"\b(FA/\d+/\d+/\d+)\b",
        r"(?:FV|FS|FA)[- ]?nr[.:\s]*([A-Z0-9/\-]+)",
    ]
    for pat in patterns:
        m = re.search(pat, desc, re.IGNORECASE)
        if m:
            return m.group(1).upper().strip()
    return ""

# ─────────────────────────────────────────────────────────────────────────────
# NORMALIZACJA I SCORING PARTNERA
# ─────────────────────────────────────────────────────────────────────────────
def norm(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())

def norm_pl(s):
    s = s.upper()
    for a, b in [("Ą","A"),("Ć","C"),("Ę","E"),("Ł","L"),("Ń","N"),
                 ("Ó","O"),("Ś","S"),("Ź","Z"),("Ż","Z")]:
        s = s.replace(a, b)
    return s

STOP_WORDS = {
    "SAD","REJONOWY","OKREGOWY","SADOWY","PRZY","WE","KOMORNIK",
    "KANCELARIA","ZOO","SPOLKA","URZAD","SADZIE","NACZELNIK",
    "SPZOO","SPOLKAZOO","JEDNOSTKA","ORGANIZACYJNA","BIURO",
    "CENTRUM","BANK","ODDZIAL","FILIA","TYTUL","TYTULEM",
    "PRZELEW","FAKTURA","PLATNOSC","UL","STR","POD","NAD",
}

def norm_tokens(s):
    """Tokenizacja nazwy partnera — min 3 znaki, bez stop-słów."""
    s = norm_pl(s)
    return [w for w in re.split(r"[^A-Z0-9]+", s)
            if len(w) >= 3 and w not in STOP_WORDS][:6]

def partner_score(partner, tx_desc):
    """0.0–1.0: dopasowanie nazwy partnera do opisu transakcji."""
    if not partner or not tx_desc:
        return 0.0
    tokens = norm_tokens(partner)
    if not tokens:
        return 0.0
    td = norm_pl(tx_desc)
    return sum(1 for t in tokens if t in td) / len(tokens)

def nip_in_desc(nip, desc):
    """Sprawdza czy NIP (10 cyfr) pojawia się w opisie transakcji."""
    if not nip or not desc:
        return False
    nip_c  = re.sub(r"[^0-9]", "", nip)
    desc_c = re.sub(r"[^0-9]", "", desc)
    return len(nip_c) >= 9 and nip_c in desc_c

def match_nr_safe(nr, text):
    """Dopasowanie numeru faktury w tekście — tylko jeśli nr jest wystarczająco długi."""
    nr_n = norm(nr)
    if len(nr_n) < MIN_NR_LEN:
        return False
    return nr_n in norm(text)

# ─────────────────────────────────────────────────────────────────────────────
# MATCHOWANIE FV ↔ TX — 4-poziomowe
#
# KLUCZ: znaki kwot w wyciągu mBanku
#   wpłata (ktoś nam płaci)  → t["a"] > 0  → pasuje do FV-out (należności)
#   wypłata (my płacimy)     → t["a"] < 0  → pasuje do FV-in  (zobowiązania)
# ─────────────────────────────────────────────────────────────────────────────
def run_matching(fv, tx, firms_by_co=None):
    tx_by_co = defaultdict(list)
    for t in tx:
        tx_by_co[t["co"]].append(t)

    # Indeks kwot (co, abs_amount) → [tx]
    amt_idx = defaultdict(list)
    for t in tx:
        amt_idx[(t["co"], round(abs(float(t["a"])), 2))].append(t)

    stats = {1: 0, 2: 0, 3: 0, 4: 0}

    for inv in fv:
        if inv.get("mx") or inv["brutto"] <= 0:
            continue

        co      = inv["co"]
        brutto  = round(float(inv["brutto"]), 2)
        nr      = inv.get("nr", "")
        partner = inv.get("partner", inv.get("nabywca", ""))
        nip_p   = inv.get("nip_partner", "")
        inv_date = inv.get("data", "")
        is_income = (inv["t"] == "out")   # FV-out = należność = oczekujemy wpłaty

        def dir_ok(t):
            a = float(t["a"])
            return a > 0 if is_income else a < 0

        # ── L1: numer faktury w opisie TX ─────────────────────────────────
        if nr:
            for t in tx_by_co[co]:
                if t.get("ref") or not dir_ok(t):
                    continue
                if match_nr_safe(nr, t["dc"]) or \
                   (t.get("inv_nr") and match_nr_safe(nr, t["inv_nr"])):
                    inv["mx"] = t["id"]; inv["ma"] = t["a"]; inv["md"] = t["dt"]
                    inv["partial"] = abs(abs(float(t["a"])) - brutto) > 1.0
                    inv["match_level"] = 1
                    t["ref"] = nr
                    stats[1] += 1
                    break

        if inv.get("mx"):
            continue

        # ── L2: dokładna kwota + partner (score ≥ 0.45) lub NIP ──────────
        candidates = [t for t in amt_idx[(co, brutto)]
                      if not t.get("ref") and dir_ok(t)]

        best, best_sc = None, 0.0
        for t in candidates:
            sc = partner_score(partner, t["dc"])
            if nip_p and nip_in_desc(nip_p, t["dc"]):
                sc = min(1.0, sc + 0.4)
            if sc > best_sc:
                best_sc = sc; best = t

        if best and best_sc >= 0.45:
            inv["mx"] = best["id"]; inv["ma"] = best["a"]; inv["md"] = best["dt"]
            inv["partial"] = False; inv["match_level"] = 2
            best["ref"] = nr; stats[2] += 1
            continue

        # ── L3: kwota ±2% lub ±2 PLN + partner (score ≥ 0.40) ───────────
        best3, best3_sc = None, 0.0
        for t in tx_by_co[co]:
            if t.get("ref") or not dir_ok(t):
                continue
            ta   = abs(float(t["a"]))
            diff = abs(ta - brutto)
            if not (diff / max(brutto, 1) < 0.02 or diff <= 2.0):
                continue
            sc = partner_score(partner, t["dc"])
            if sc > best3_sc:
                best3_sc = sc; best3 = t

        if best3 and best3_sc >= 0.40:
            inv["mx"] = best3["id"]; inv["ma"] = best3["a"]; inv["md"] = best3["dt"]
            inv["partial"] = abs(abs(float(best3["a"])) - brutto) > 1.0
            inv["match_level"] = 3
            best3["ref"] = nr; stats[3] += 1
            continue

        # ── L4: partner + data ±120 dni (score ≥ 0.55) ───────────────────
        if inv_date and partner:
            try:
                inv_dt = datetime.strptime(inv_date[:10], "%Y-%m-%d")
            except Exception:
                continue

            best4, best4_sc = None, 0.0
            for t in tx_by_co[co]:
                if t.get("ref") or not dir_ok(t):
                    continue
                try:
                    tx_dt = datetime.strptime(t["dt"][:10], "%Y-%m-%d")
                except Exception:
                    continue
                if abs((tx_dt - inv_dt).days) > 120:
                    continue
                sc = partner_score(partner, t["dc"])
                if sc > best4_sc:
                    best4_sc = sc; best4 = t

            if best4 and best4_sc >= 0.55:
                inv["mx"] = best4["id"]; inv["ma"] = best4["a"]; inv["md"] = best4["dt"]
                inv["partial"] = abs(abs(float(best4["a"])) - brutto) > 1.0
                inv["match_level"] = 4
                best4["ref"] = nr; stats[4] += 1

    total = sum(stats.values())
    print(f"  Dopasowania: L1={stats[1]} L2={stats[2]} L3={stats[3]} L4={stats[4]} lacznie={total}")
    return fv, tx, total

# ─────────────────────────────────────────────────────────────────────────────
# BUILD HTML
# ─────────────────────────────────────────────────────────────────────────────
def build_html(fv, tx, updated):
    def clean(s):
        return re.sub(r"[\x00-\x1f\x7f]", " ", s).strip() if isinstance(s, str) else s

    fv2 = [{k: clean(v) if isinstance(v, str) else v for k, v in r.items()} for r in fv]
    tx2 = [{k: clean(v) if isinstance(v, str) else v for k, v in r.items()} for r in tx]

    tpl_path = BASE_DIR / "monitor_template.html"
    if tpl_path.exists():
        tpl = tpl_path.read_text(encoding="utf-8")
    else:
        out_path = BASE_DIR / "monitor_platnosci.html"
        if out_path.exists():
            tpl = out_path.read_text(encoding="utf-8")
            tpl = re.sub(r"const FV=\[.*?\];", "const FV=%%FV%%;", tpl, flags=re.DOTALL)
            tpl = re.sub(r"const TX=\[.*?\];", "const TX=%%TX%%;", tpl, flags=re.DOTALL)
        else:
            print("BLAD: Brak pliku monitor_template.html")
            sys.exit(1)

    fv_json = json.dumps(fv2, ensure_ascii=True, separators=(",", ":"))
    tx_json = json.dumps(tx2, ensure_ascii=True, separators=(",", ":"))
    fv_b64  = base64.b64encode(fv_json.encode()).decode()
    tx_b64  = base64.b64encode(tx_json.encode()).decode()

    html = tpl
    html = html.replace("%%UPDATED%%", updated)
    html = html.replace("%%FV_B64%%", fv_b64)
    html = html.replace("%%TX_B64%%", tx_b64)
    html = html.replace("%%FV%%", fv_json)
    html = html.replace("%%TX%%", tx_json)
    return html

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("=" * 58)
    print("  Monitor Platnosci — aktualizacja danych v3")
    print("=" * 58)

    cfg         = load_config()
    firms       = cfg["firms"]
    bank_folder = Path(cfg.get("bank_folder", "./bank"))
    output_path = Path(cfg.get("output_html",  "./monitor_platnosci.html"))
    from_date   = cfg.get("fetch_from_date", "2025-01-01")
    firms_by_co = {f["co"]: f for f in firms}

    all_fv, all_tx = [], []
    tx_keys = set()

    endpoints_out = ["v1.5/invoices", "v1.5/correctiveinvoices"]
    endpoints_in  = ["v1.5/purchaseinvoices", "v1.4/vatpurchasebooks"]

    # ── Pobieranie z Betterfly ──────────────────────────────────────────────
    for firm in firms:
        co, name = firm["co"], firm["name"]
        cid, csec = firm["client_id"], firm["client_secret"]
        if "BRAK" in cid or "WPISZ" in cid:
            print(f"\n[{name}] Brak klucza API — pomijam.")
            continue

        print(f"\n[{name}] Pobieranie z Betterfly API...")
        try:
            token = get_token(cid, csec)
            print(f"  Token OK")
            params = {"dateFrom": from_date}

            # Faktury sprzedaży
            print("  Faktury sprzedazy:")
            raw_out = []
            for ep in endpoints_out:
                raw_out.extend(api_get_all(token, ep, params, ep))
            print(f"  -> {len(raw_out)} rekordow sprzedazy")

            # Faktury zakupu — deduplikacja po numerze
            print("  Faktury zakupu:")
            raw_in, seen_nrs = [], set()
            for ep in endpoints_in:
                ep_params = dict(params)
                if "vatpurchasebooks" in ep:
                    ep_params["pageSize"] = 200
                for item in api_get_all(token, ep, ep_params, ep):
                    nr = (item.get("Number") or item.get("DocumentNumber") or
                          item.get("InvoiceNumber") or str(item.get("Id", ""))).strip()
                    if nr and nr not in seen_nrs:
                        seen_nrs.add(nr)
                        raw_in.append(item)
                    elif not nr:
                        raw_in.append(item)
            print(f"  -> {len(raw_in)} rekordow zakupu")

            for item in raw_out:
                r = parse_inv_out(item, co)
                if not r["nr"]:
                    r["nr"] = f"{r['data']}_{r['brutto']:.2f}_{r['co']}"
                all_fv.append(r)

            for item in raw_in:
                r = parse_inv_in(item, co)
                if r["nr"]:
                    all_fv.append(r)

        except Exception as e:
            print(f"  BLAD: {e}")

    # ── Wyciągi bankowe ─────────────────────────────────────────────────────
    print(f"\nWyciagi bankowe: {bank_folder}")
    if bank_folder.exists():
        csv_files = sorted(bank_folder.glob("*.csv"))
        print(f"  Pliki CSV: {len(csv_files)}")
        for fp in csv_files:
            with open(fp, encoding="utf-8-sig", errors="replace") as f:
                lines = f.readlines()
            co = detect_co(lines, firms, filepath=str(fp))
            if co == -1:
                print(f"  [UWAGA] {fp.name} — nie rozpoznano firmy (sprawdz konto_suffix)")
                continue
            fname = next(f["name"] for f in firms if f["co"] == co)
            new   = parse_bank_csv(str(fp), co, tx_keys)
            base_id = len(all_tx)
            for i, t in enumerate(new):
                t["id"] = base_id + i + 1
            all_tx.extend(new)
            print(f"  OK {fp.name} → {fname}: +{len(new)} transakcji")
    else:
        bank_folder.mkdir(parents=True)
        print(f"  Utworzono folder {bank_folder}")

    if not all_fv and not all_tx:
        print("\nBrak danych — sprawdz config.json i folder bank/")
        sys.exit(0)

    # ── Wykrywanie anulowanych par (oryginał + korekta) ────────────────────
    groups = defaultdict(list)
    for f in all_fv:
        if f["t"] == "out":
            groups[(f["co"], f["partner"][:30].strip())].append(f)

    cancelled_pairs = 0
    for key, invs in groups.items():
        positives = {f["nr"]: f for f in invs if f["brutto"] > 0}
        negatives = [f for f in invs if f["brutto"] < 0]
        for kor in negatives:
            for orig_nr, orig in positives.items():
                if abs(orig["brutto"] - abs(kor["brutto"])) < 0.5:
                    orig["cancelled"] = True
                    kor["cancelled"]  = True
                    cancelled_pairs  += 1
                    break

    for f in all_fv:
        if "cancelled" not in f:
            f["cancelled"] = False

    print(f"\n  Anulowane pary: {cancelled_pairs}")

    # ── Matching ────────────────────────────────────────────────────────────
    print(f"\nDopasowywanie: {len(all_fv)} FV x {len(all_tx)} TX...")
    t0 = time.time()
    all_fv, all_tx, matched = run_matching(all_fv, all_tx, firms_by_co)
    print(f"  Czas: {round(time.time()-t0)}s  |  Dopasowano: {matched}")

    # Propagacja numerów spraw z TX → FV
    tx_map = {(t["co"], t["id"]): t for t in all_tx}
    for inv in all_fv:
        if inv.get("mx"):
            t = tx_map.get((inv["co"], inv["mx"]))
            if t and t.get("case"):
                inv["fv_case"] = t["case"]

    with_case = sum(1 for f in all_fv if f.get("fv_case"))
    print(f"  Numery spraw przypisane: {with_case} FV")

    # ── Generuj HTML ────────────────────────────────────────────────────────
    updated = datetime.now().strftime("%d.%m.%Y %H:%M")
    html    = build_html(all_fv, all_tx, updated)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    size_kb = output_path.stat().st_size // 1024

    print(f"\n{'='*58}")
    print(f"  GOTOWE! {output_path.name} ({size_kb} KB)")
    print(f"  FV: {len(all_fv)} | TX: {len(all_tx)} | Matched: {matched}")
    print(f"  Zaktualizowano: {updated}")
    print(f"{'='*58}")

if __name__ == "__main__":
    main()
