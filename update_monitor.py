#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Monitor Platnosci - skrypt aktualizacji v2"""
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import os, sys, json, re, csv, io, base64, time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("Brak biblioteki 'requests'. Zainstaluj: pip install requests")
    sys.exit(1)

BASE_DIR    = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
API_BASE    = "https://app.comarchbetterfly.pl/api2/public"
TOKEN_URL   = f"{API_BASE}/token"

def load_config():
    if not CONFIG_PATH.exists():
        print(f"Brak pliku config.json w folderze: {BASE_DIR}")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

def get_token(client_id, client_secret):
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Authorization": f"Basic {creds}"},
        data="grant_type=client_credentials",
        timeout=20
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Blad tokena ({resp.status_code}): {resp.text[:300]}")
    return resp.json()["access_token"]

def api_get_all(token, endpoint, params=None, label=""):
    """Pobiera dane z API — jedna strona z dużym pageSize."""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{API_BASE}/{endpoint}"
    results = []
    # Betterfly zwraca wszystko na raz gdy pageSize jest duże
    # Próbujemy kolejne strony tylko jeśli dostajemy pełny pageSize
    page = 1
    page_size = 500
    seen_ids = set()
    while page <= 20:  # max 20 stron = 10 000 rekordow
        p = {"page": page, "pageSize": page_size, **(params or {})}
        print(f"    strona {page}...", end=" ", flush=True)
        try:
            resp = requests.get(url, headers=headers, params=p, timeout=45)
        except requests.Timeout:
            print("timeout")
            break
        except Exception as e:
            print(f"blad: {e}")
            break

        if resp.status_code in (404, 401, 405):
            print(f"niedostepny ({resp.status_code})")
            break
        if resp.status_code != 200:
            print(f"blad {resp.status_code}")
            break

        try:
            data = resp.json()
        except:
            print("blad JSON")
            break

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = (data.get("Items") or data.get("Data") or
                     data.get("Records") or data.get("invoices") or
                     data.get("results") or [])
            # Sprawdź total z odpowiedzi
            total = data.get("TotalCount") or data.get("Total") or data.get("total")
        else:
            items = []
            total = None

        if not items:
            print("brak danych")
            break

        # Deduplikacja po ID żeby wykryć pętlę
        new_items = []
        for item in items:
            item_id = item.get("Id") or item.get("id") or item.get("Number") or id(item)
            if item_id not in seen_ids:
                seen_ids.add(item_id)
                new_items.append(item)

        print(f"{len(new_items)} nowych rekordow (lacznie: {len(results)+len(new_items)})")
        results.extend(new_items)

        # Zakończ jeśli dostaliśmy mniej niż pageSize lub same duplikaty
        if len(new_items) == 0 or len(items) < page_size:
            break
        page += 1
        time.sleep(0.3)

    return results

def flt(v):
    try: return round(float(v or 0), 2)
    except: return 0.0

def parse_inv_out(item, co):
    # Betterfly v1.5 fields: GrossTotal, NetTotal, VatTotal, Number, PurchasingParty
    brutto = flt(item.get("GrossTotal") or item.get("TotalGrossValue") or item.get("GrossValue") or 0)
    netto  = flt(item.get("NetTotal")   or item.get("TotalNetValue")   or item.get("NetValue")   or brutto)
    vat    = flt(item.get("VatTotal")   or item.get("TotalVatValue")   or round(brutto - netto, 2))
    pay_status = item.get("PaymentStatus", 0)
    paid   = (pay_status == 1)
    do_zap = 0.0 if paid else brutto
    purchasing = item.get("PurchasingParty") or {}
    partner = (purchasing.get("Name") or item.get("CustomerName") or item.get("BuyerName") or "")
    nr     = (item.get("Number") or item.get("InvoiceNumber") or item.get("DocumentNumber") or "")
    issued = str(item.get("IssueDate") or item.get("SalesDate") or item.get("Date") or "")[:10]
    due    = str(item.get("PaymentDate") or item.get("DueDate") or item.get("PaymentDueDate") or "")[:10]
    if not due and issued:
        from datetime import datetime, timedelta
        try:
            due = (datetime.strptime(issued, "%Y-%m-%d") + timedelta(days=14)).strftime("%Y-%m-%d")
        except: pass
    return {"co": co, "t": "out", "nr": nr.strip(), "data": issued, "due": due,
            "partner": partner.strip(), "nabywca": partner.strip(),
            "brutto": brutto, "netto": netto, "vat": vat, "do_zap": do_zap,
            "paid": paid, "mx": None, "ma": None, "md": None,
            "partial": False, "fv_case": "", "match_level": 0, "cancelled": False}

def parse_inv_in(item, co):
    # Betterfly v1.5 fields: GrossTotal, NetTotal, VatTotal, SellingParty
    netto  = flt(item.get("NetTotal")   or item.get("TotalNetValue")   or item.get("NetValue")   or 0)
    vat    = flt(item.get("VatTotal")   or item.get("TotalVatValue")   or item.get("VatValue")   or 0)
    brutto = flt(item.get("GrossTotal") or item.get("TotalGrossValue") or item.get("GrossValue") or round(netto + vat, 2))
    selling = item.get("SellingParty") or {}
    # vatpurchasebooks uses CustomerName for vendor, purchaseinvoices uses SellingParty
    partner = (selling.get("Name") or item.get("SellingPartyName") or
               item.get("SupplierName") or item.get("CustomerName") or "")
    nr      = (item.get("Number") or item.get("InvoiceNumber") or item.get("DocumentNumber") or "")
    issued  = str(item.get("PurchaseDate") or item.get("IssueDate") or item.get("Date") or "")[:10]
    due_in = str(item.get("PaymentDate") or item.get("DueDate") or "")[:10]
    if not due_in and issued:
        try:
            from datetime import datetime, timedelta
            due_in = (datetime.strptime(issued, "%Y-%m-%d") + timedelta(days=14)).strftime("%Y-%m-%d")
        except: pass
    return {"co": co, "t": "in", "nr": nr.strip(), "data": issued, "due": due_in,
            "partner": partner.strip(), "nabywca": partner.strip(),
            "brutto": brutto, "netto": netto, "vat": vat, "do_zap": brutto,
            "paid": False, "mx": None, "ma": None, "md": None,
            "partial": False, "fv_case": "", "match_level": 0, "cancelled": False}

CASE_PATTERNS = [
    (r"\bK[mM][nN]?\s*(\d+[\s/]\d{2,4})\b", "KM"),
    (r"\bI\s*[Cc]\s+(\d+[\s/]\d{2,4})\b", "IC"),
    (r"\bI\s*C(?:ps|o|upr)\s+(\d+[\s/]\d{2,4})\b", "IC"),
    (r"\b(?:[IVX]{1,4}\s+)?I\s*N\s*[Ss]\s+(\d+[\s/]\d{2,4})\b", "INS"),
    (r"\bINS(\d+/\d{2,4})\b", "INS"),
    (r"\bINs(\d+/\d{2,4})\b", "INS"),
    (r"\b(?:[IVX]{1,4}\s+)?G\s*[Cc]\s+(\d+[\s/]\d{2,4})\b", "GC"),
    (r"\bIXGC\s*(\d+[\s/]\d{2,4})\b", "GC"),
    (r"\bGUp[-s]*[/]?(\d+/\d{4})\b","GUp"),
    (r"\bNc[-\s]*[eE]\s+(\d+[\s/]\d{2,4})\b", "Nc-e"),
    (r"\b(?:VI{1,3}|IV|IX)\s*N\s*[Ss]\s+(\d+[\s/]\d{2,4})\b", "INS"),
    (r"\bKs[Pp]\s*[:.]?\s*(\d+/\d{2,4})\b", "KsP"),
    (r"\bI\s*AC[az]?\s+(\d+[\s/]\d{2,4})\b", "ACa"),
    (r"\bGN\s*[bB]\s+(\d+[\s/]\d{2,4})\b", "GNb"),
    (r"[Ss]ygn\.?\s+akt\.?\s+([A-Z]{1,6}\s*\d+[\s/]\d{2,4})", "SYGN"),
]
def extract_case(desc):
    found,seen=[],set()
    for pat,lbl in CASE_PATTERNS:
        for m in re.finditer(pat,desc,re.IGNORECASE):
            _nr = re.sub(r'(\d)\s+(\d)', r'\1/\2', m.group(1).strip())
            k = lbl + ' ' + _nr
            if k not in seen: seen.add(k);found.append(k)
    return ", ".join(found)

def extract_inv_nr_from_desc(desc):
    """Wyciaga numer faktury z opisu przelewu (faFS/26/3/1, NUMER DOKUMENTU: FS/...)"""
    if not desc: return ""
    patterns = [
        r"fa\s*(FS/\d+/\d+/\d+)",       # fa FS/26/3/1
        r"fa\s*(FV/\d+/\d+/\d+)",       # fa FV/26/3/1
        r",(FS/\d+/\d+/\d+)",            # ,FS/26/3/1
        r",(FV/\d+/\d+/\d+)",            # ,FV/26/3/1
        r"NUMER DOKUMENTU:\s*([A-Z]+/\d+/\d+/\d+)",
        r"\.\.([A-Z]+/\d+/\d+/\d+)\.\.",  # ..FS/26/5/1..
        r"\b(FS/\d{2}/\d{1,2}/\d+)\b",
        r"\b(FV/\d+/\d+/\d+)\b",
    ]
    for pat in patterns:
        m = re.search(pat, desc, re.IGNORECASE)
        if m:
            return m.group(1).upper().strip()
    return ""

def detect_co(lines, firms):
    header = "\n".join(lines[:25])
    for f in firms:
        if f["konto_suffix"] in header:
            return f["co"]
    return -1

def parse_bank_csv(filepath, co, existing_keys):
    with open(filepath, encoding="utf-8-sig") as f:
        lines = f.readlines()
    hdr = next((i for i,l in enumerate(lines) if "#Data operacji" in l), None)
    if hdr is None: return []
    txs = []
    for line in lines[hdr+1:]:
        line = line.rstrip("\n")
        if not line.strip(): continue
        try: row = next(csv.reader(io.StringIO(line), delimiter=";"))
        except: continue
        if len(row)<5: continue
        dt = row[0].strip()
        if not re.match(r"\d{4}-\d{2}-\d{2}", dt): continue
        desc = row[1].strip()
        amt_raw = row[4].strip().replace(" ","").replace("\xa0","").replace("PLN","")
        try: amt = round(float(amt_raw.replace(",",".")),2)
        except: continue
        key=(dt,amt,desc[:40])
        if key in existing_keys: continue
        existing_keys.add(key)
        dc = re.sub(r"[\x00-\x1f\x7f]"," ",desc).strip()
        txs.append({"co":co,"id":len(txs)+1,"dt":dt,"dc":dc[:250],
                    "a":amt,"ref":None,"case":extract_case(dc),"inv_nr":extract_inv_nr_from_desc(dc)})
    return txs

def norm(s): return re.sub(r"[^A-Z0-9]","",s.upper())
def match_nr(nr,desc): return bool(norm(nr) and norm(nr) in norm(desc))

def norm_tokens(s):
    """Tokenize partner name for fuzzy matching."""
    s = s.upper()
    for a,b in [("Ą","A"),("Ć","C"),("Ę","E"),("Ł","L"),("Ń","N"),("Ó","O"),("Ś","S"),("Ź","Z"),("Ż","Z")]:
        s = s.replace(a,b)
    stop = {"SAD","REJONOWY","OKREGOWY","SADOWY","PRZY","SR","W","WE",
            "KOMORNIK","KANCELARIA","ZOO","SPOLKA","UL","AL","PL",
            "SADZIE","NACZELNIK","URZAD","SPZOO","SP"}
    return [w for w in re.split(r"[^A-Z0-9]+", s) if len(w)>=4 and w not in stop][:5]

def partner_score(partner, tx_desc):
    """0-1: how well invoice partner name matches TX description."""
    if not partner or not tx_desc: return 0.0
    tokens = norm_tokens(partner)
    if not tokens: return 0.0
    td = tx_desc.upper()
    for a,b in [("Ą","A"),("Ć","C"),("Ę","E"),("Ł","L"),("Ń","N"),("Ó","O"),("Ś","S"),("Ź","Z"),("Ż","Z")]:
        td = td.replace(a,b)
    return sum(1 for t in tokens if t in td) / len(tokens)

def run_matching(fv, tx):
    """4-level matching: L1=nr in desc, L2=amount+partner, L3=amount~+partner, L4=partner+date."""
    from datetime import datetime
    tx_by_co = {}
    for t in tx: tx_by_co.setdefault(t["co"],[]).append(t)

    # Build amount index per co
    amt_idx = {}
    for t in tx:
        key = (t["co"], round(abs(float(t["a"])), 2))
        amt_idx.setdefault(key, []).append(t)

    stats = {1:0, 2:0, 3:0, 4:0}
    for inv in fv:
        if inv.get("mx") or inv["brutto"] <= 0: continue
        co = inv["co"]
        brutto = round(float(inv["brutto"]), 2)
        nr = inv.get("nr","")
        partner = inv.get("partner", inv.get("nabywca",""))
        inv_date = inv.get("data","")
        direction = "out" if inv["t"] == "out" else "in"

        # L1: invoice number in TX description OR extracted inv_nr field
        for t in tx_by_co.get(co,[]):
            if direction == "out" and t["a"] <= 0: continue
            if direction == "in"  and t["a"] >= 0: continue
            if t.get("ref"): continue
            if match_nr(nr, t["dc"]) or (t.get("inv_nr") and match_nr(nr, t["inv_nr"])):
                inv["mx"]=t["id"]; inv["ma"]=t["a"]; inv["md"]=t["dt"]
                inv["partial"]=abs(t["a"]-brutto)>0.5
                inv["match_level"]=1; t["ref"]=nr; stats[1]+=1; break

        if inv.get("mx"): continue

        # L2: exact amount + strong partner match
        candidates = [t for t in amt_idx.get((co,brutto),[])
                      if not t.get("ref") and
                      (direction=="out" and t["a"]>0 or direction=="in" and t["a"]<0)]
        best, best_sc = None, 0.0
        for t in candidates:
            sc = partner_score(partner, t["dc"])
            if sc > best_sc: best_sc = sc; best = t
        if best and best_sc >= 0.5:
            inv["mx"]=best["id"]; inv["ma"]=best["a"]; inv["md"]=best["dt"]
            inv["partial"]=False; inv["match_level"]=2
            best["ref"]=nr; stats[2]+=1; continue

        # L3: amount within 1% + partner match
        for t in tx_by_co.get(co,[]):
            if t.get("ref"): continue
            ta = float(t["a"])
            if direction=="out" and ta<=0: continue
            if direction=="in"  and ta>=0: continue
            if abs(ta - brutto) / max(brutto,1) < 0.01:
                sc = partner_score(partner, t["dc"])
                if sc >= 0.4:
                    inv["mx"]=t["id"]; inv["ma"]=ta; inv["md"]=t["dt"]
                    inv["partial"]=abs(ta-brutto)>0.5; inv["match_level"]=3
                    t["ref"]=nr; stats[3]+=1; break

        if inv.get("mx"): continue

        # L4: partner name + date proximity (90 days)
        if inv_date and partner:
            try: inv_dt = datetime.strptime(inv_date[:10], "%Y-%m-%d")
            except: continue
            best4, best4_sc = None, 0.0
            for t in tx_by_co.get(co,[]):
                if t.get("ref"): continue
                ta = float(t["a"])
                if direction=="out" and ta<=0: continue
                if direction=="in"  and ta>=0: continue
                try: tx_dt = datetime.strptime(t["dt"][:10], "%Y-%m-%d")
                except: continue
                if abs((tx_dt - inv_dt).days) > 90: continue
                sc = partner_score(partner, t["dc"])
                if sc > best4_sc: best4_sc = sc; best4 = t
            if best4 and best4_sc >= 0.6:
                inv["mx"]=best4["id"]; inv["ma"]=best4["a"]; inv["md"]=best4["dt"]
                inv["partial"]=abs(best4["a"]-brutto)>0.5; inv["match_level"]=4
                best4["ref"]=nr; stats[4]+=1

    total = sum(stats.values())
    print(f"  Dopasowania: L1={stats[1]} L2={stats[2]} L3={stats[3]} L4={stats[4]} lacznie={total}")
    return fv, tx, total

def build_html(fv,tx,updated):
    def clean(s): return re.sub(r"[\x00-\x1f\x7f]"," ",s).strip() if isinstance(s,str) else s
    fv2=[{k:clean(v) if isinstance(v,str) else v for k,v in r.items()} for r in fv]
    tx2=[{k:clean(v) if isinstance(v,str) else v for k,v in r.items()} for r in tx]

    # Read HTML template from existing monitor or use embedded
    tpl_path = BASE_DIR / "monitor_template.html"
    if tpl_path.exists():
        tpl = tpl_path.read_text(encoding="utf-8")
    else:
        # Use the last generated HTML as template (strip data)
        out_path = BASE_DIR / "monitor_platnosci.html"
        if out_path.exists():
            import re as re2
            tpl = out_path.read_text(encoding="utf-8")
            tpl = re2.sub(r'const FV=\[.*?\];', 'const FV=%%FV%%;', tpl, flags=re2.DOTALL)
            tpl = re2.sub(r'const TX=\[.*?\];', 'const TX=%%TX%%;', tpl, flags=re2.DOTALL)
        else:
            print("BLAD: Brak pliku monitor_platnosci.html — uruchom skrypt pierwszy raz z Claude.")
            sys.exit(1)

    html = tpl
    html = html.replace("%%UPDATED%%", updated)
    html = html.replace("%%FV%%", json.dumps(fv2, ensure_ascii=False, separators=(',',':')))
    html = html.replace("%%TX%%", json.dumps(tx2, ensure_ascii=False, separators=(',',':')))
    return html

def main():
    print("="*55)
    print("  Monitor Platnosci — aktualizacja danych v2")
    print("="*55)

    cfg = load_config()
    firms = cfg["firms"]
    bank_folder = Path(cfg.get("bank_folder","./bank"))
    output_path = Path(cfg.get("output_html","./monitor_platnosci.html"))
    from_date   = cfg.get("fetch_from_date","2025-01-01")

    all_fv, all_tx = [], []
    tx_keys = set()

    # -- BETTERFLY API --
    endpoints_out = ["v1.5/invoices", "v1.5/correctiveinvoices"]
    endpoints_in  = ["v1.5/purchaseinvoices", "v1.4/vatpurchasebooks"]

    for firm in firms:
        co, name = firm["co"], firm["name"]
        cid, csec = firm["client_id"], firm["client_secret"]
        if "BRAK" in cid or "WPISZ" in cid:
            print(f"\n[{name}] Brak klucza API — pomijam.")
            continue

        print(f"\n[{name}] Laczenie z Betterfly...")
        try:
            token = get_token(cid, csec)
            print(f"  OK Token OK")

            params = {"dateFrom": from_date}

            # Faktury sprzedazy
            print(f"  Faktury sprzedazy:")
            raw_out = []
            for ep in endpoints_out:
                items = api_get_all(token, ep, params, ep)
                raw_out.extend(items)
            print(f"  -> lacznie: {len(raw_out)}")

            # Faktury zakupu
            print(f"  Faktury zakupu (purchaseinvoices + KSeF/vatpurchasebooks):")
            raw_in = []
            seen_nrs = set()
            raw_in = []
            for ep in endpoints_in:
                items = api_get_all(token, ep, params, ep)
                for item in items:
                    nr = (item.get("Number") or item.get("DocumentNumber") or
                          item.get("InvoiceNumber") or str(item.get("Id",""))).strip()
                    if nr and nr not in seen_nrs:
                        seen_nrs.add(nr)
                        raw_in.append(item)
                    elif not nr:
                        raw_in.append(item)
            print(f"  -> lacznie: {len(raw_in)}")

            for item in raw_out:
                r = parse_inv_out(item, co)
                if r["nr"]: all_fv.append(r)
            for item in raw_in:
                r = parse_inv_in(item, co)
                if r["nr"]: all_fv.append(r)

        except Exception as e:
            print(f"  BLAD Blad: {e}")

    # -- BANK CSV --
    print(f"\nWyciagi bankowe z folderu: {bank_folder}")
    if bank_folder.exists():
        csv_files = sorted(bank_folder.glob("*.csv"))
        print(f"  Znaleziono {len(csv_files)} plikow CSV")
        for fp in csv_files:
            with open(fp, encoding="utf-8-sig") as f:
                lines = f.readlines()
            co = detect_co(lines, firms)
            if co==-1:
                print(f"  [UWAGA] {fp.name} — nie rozpoznano firmy")
                continue
            name = next(f["name"] for f in firms if f["co"]==co)
            new = parse_bank_csv(str(fp), co, tx_keys)
            # Fix IDs globally
            base_id = len(all_tx)
            for i,t in enumerate(new): t["id"] = base_id+i+1
            all_tx.extend(new)
            print(f"  OK {fp.name} -> {name}: +{len(new)} transakcji")
    else:
        bank_folder.mkdir(parents=True)
        print(f"  Utworzono folder {bank_folder}")

    if not all_fv and not all_tx:
        print("\nBrak danych — sprawdź config.json i folder bank/")
        sys.exit(0)

    # -- DOPASOWANIE --
    # Wykryj anulowane faktury (oryginał + korekta na pełną kwotę)
    from collections import defaultdict
    groups = defaultdict(list)
    for f in all_fv:
        if f["t"] == "out":
            key = (f["co"], f["partner"][:30].strip())
            groups[key].append(f)
    cancelled_pairs = 0
    for key, invs in groups.items():
        positives = {f["nr"]: f for f in invs if f["brutto"] > 0}
        negatives = [f for f in invs if f["brutto"] < 0]
        for kor in negatives:
            for orig_nr, orig in positives.items():
                if abs(orig["brutto"] - abs(kor["brutto"])) < 0.5:
                    orig["cancelled"] = True
                    kor["cancelled"] = True
                    cancelled_pairs += 1
                    break
    for f in all_fv:
        if "cancelled" not in f:
            f["cancelled"] = False
    print(f"  Wykryto {cancelled_pairs} anulowanych par (oryginał+korekta)")

    print(f"\nDopasowywanie faktur do transakcji...")
    all_fv, all_tx, matched = run_matching(all_fv, all_tx)
    print(f"  OK Dopasowano {matched} par")

    # Propagate case numbers to invoices
    from collections import defaultdict
    tx_map = {(t["co"], t["id"]): t for t in all_tx}

    # Step 1: assign case from matched transaction (always reliable)
    for inv in all_fv:
        if inv.get("mx"):
            tx = tx_map.get((inv["co"], inv["mx"]))
            if tx and tx.get("case"):
                inv["fv_case"] = tx["case"]

    # Step 2: for unmatched invoices - propagate case ONLY if partner
    # has exactly ONE unique case number (avoids wrong assignment for
    # partners like bailiffs with many cases)
    case_from_partner = 0
    # Build map: partner -> set of case numbers from all their transactions
    partner_cases = defaultdict(set)
    for t in all_tx:
        if t.get("case") and t.get("dc"):
            tokens = norm_tokens(t["dc"])
            partner_cases[(t["co"], " ".join(tokens[:3]))].add(t["case"])

    for inv in all_fv:
        if inv.get("fv_case") or not inv.get("partner"):
            continue
        tokens = norm_tokens(inv["partner"])
        if not tokens:
            continue
        key = (inv["co"], " ".join(tokens[:3]))
        cases = partner_cases.get(key, set())
        # Only propagate if exactly ONE unique case number for this partner
        if len(cases) == 1:
            inv["fv_case"] = list(cases)[0]
            case_from_partner += 1

    with_case = sum(1 for f in all_fv if f.get("fv_case"))
    print(f"  Numery spraw: {with_case} FV (w tym {case_from_partner} przez jednoznaczny numer partnera)")

    # -- HTML --
    updated = datetime.now().strftime("%d.%m.%Y %H:%M")
    html = build_html(all_fv, all_tx, updated)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    size_kb = output_path.stat().st_size//1024
    print(f"\n{'='*55}")
    print(f"  [GOTOWE] GOTOWE!  {output_path.name}  ({size_kb} KB)")
    print(f"     FV: {len(all_fv)}  |  Transakcje: {len(all_tx)}")
    print(f"     Zaktualizowano: {updated}")
    print(f"{'='*55}")

if __name__ == "__main__":
    main()
