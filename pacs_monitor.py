import requests
from bs4 import BeautifulSoup
from datetime import datetime, date
import re
import time

URLS_CANDIDATES = [
    "https://rdvma18.apps.paris.fr/rdvma18/jsp/site/Portal.jsp?page=appointment&view=getViewAppointmentCalendar&id_form=44",
    "https://rdvma18.apps.paris.fr/rdvma18/jsp/site/Portal.jsp?page=appointment&view=getViewAppointmentCalendar&id_form=44&anchor=step3",
    "https://rdvma18.apps.paris.fr/rdvma18/jsp/site/Portal.jsp?page=appointment&id_form=44&anchor=step3",
]

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
})

# --- Regex ciblées (début uniquement) ---
PAT_JSON_START = re.compile(r'"(?:start|startDate)"\s*:\s*"(?P<iso>\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::\d{2})?Z?"')
PAT_URL_BEGIN  = re.compile(r'(?:\b(?:beginning_date_time|ing_date_time|start|startDate)\s*=\s*)(?P<iso>\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::\d{2})?Z?', re.IGNORECASE)
PAT_JSON_END   = re.compile(r'"(?:end|endDate)"\s*:\s*"(?P<iso>\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::\d{2})?Z?"')
PAT_URL_END    = re.compile(r'(?:\b(?:ending_date_time|end|endDate)\s*=\s*)(?P<iso>\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::\d{2})?Z?', re.IGNORECASE)
PAT_ANY_ISO    = re.compile(r'(?P<iso>\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::\d{2})?Z?')


def fetch_first_soup():
    for u in URLS_CANDIDATES:
        try:
            r = session.get(u, timeout=30)
            r.raise_for_status()
            return BeautifulSoup(r.content, "html.parser"), r.url, r.text
        except Exception:
            continue
    raise ConnectionError("Impossible de charger la page PACS.")


def extract_slots_from_scripts(soup, base_url, full_text):
    starts_tmp, end_set = [], set()

    def norm_iso(dt_iso):
        iso = dt_iso.rstrip('Z')
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(iso, fmt)
            except ValueError:
                pass
        return None

    def add_start(dt_iso):
        dt = norm_iso(dt_iso)
        if not dt:
            return
        starts_tmp.append({
            "date": dt.date().isoformat(),
            "time": dt.strftime("%H:%M"),
            "datetime_iso": dt.strftime("%Y-%m-%dT%H:%M")
        })

    def add_end(dt_iso):
        dt = norm_iso(dt_iso)
        if dt:
            end_set.add(dt.strftime("%Y-%m-%dT%H:%M"))

    def scan_text(txt):
        if not txt:
            return
        for m in PAT_JSON_START.finditer(txt): add_start(m.group("iso"))
        for m in PAT_URL_BEGIN.finditer(txt): add_start(m.group("iso"))
        for m in PAT_JSON_END.finditer(txt):  add_end(m.group("iso"))
        for m in PAT_URL_END.finditer(txt):   add_end(m.group("iso"))
        for m in PAT_ANY_ISO.finditer(txt):
            span = txt[max(0, m.start()-80): m.end()+80]
            if re.search(r'end|endDate|ending_date_time', span, re.IGNORECASE): continue
            if re.search(r'start|begin', span, re.IGNORECASE): add_start(m.group("iso"))

    for sc in soup.find_all("script"):
        scan_text(sc.string or sc.text or "")
    scan_text(full_text or "")

    uniq = {}
    for s in starts_tmp:
        if s["datetime_iso"] not in end_set:
            uniq[s["datetime_iso"]] = s

    # garde uniquement 2025–2026
    return sorted(
        [s for s in uniq.values() if s["date"].startswith(("2025-", "2026-"))],
        key=lambda x: (x["date"], x["time"])
    )


def get_all_slots():
    soup, final_url, full_text = fetch_first_soup()
    return extract_slots_from_scripts(soup, final_url, full_text)


def detect_new_slots(current_slots, last_slots):
    current_set = {(s["date"], s["time"]) for s in current_slots}
    last_set = {(s["date"], s["time"]) for s in last_slots}
    new_pairs = current_set - last_set
    return [s for s in current_slots if (s["date"], s["time"]) in new_pairs]

def send_whatsapp(message):
    try:
        requests.post(
            "https://n8n.loucam.online/webhook/whatsapp-incoming",
            json={"message": message},
            timeout=10
        )
    except Exception as e:
        print(f"Erreur envoi WhatsApp : {e}")

def run_monitor(interval=10):
    print(f"\nSurveillance active — vérification toutes les {interval}s.")
    last_slots = []

    while True:
        try:
            slots = get_all_slots()
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Erreur de collecte : {e}")
            time.sleep(interval)
            continue

        new_slots = detect_new_slots(slots, last_slots)

        if new_slots:
            print(f"\n=== NOUVEAUX CRÉNEAUX {datetime.now().strftime('%H:%M:%S')} ===")
            for s in new_slots:
                print(f"📅 {s['date']} à {s['time']}")
                send_whatsapp(f"Nouveau créneau PACS : {s['date']} à {s['time']}")
            print("=" * 40)
            last_slots = slots
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Aucun nouveau créneau.")
            send_whatsapp(f"No news sorry")
        time.sleep(interval)


import requests



if __name__ == "__main__":
    run_monitor(interval=10)
