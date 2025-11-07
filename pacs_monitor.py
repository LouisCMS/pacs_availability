import requests
from bs4 import BeautifulSoup
from datetime import datetime, date
import re
import time


import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate



import os
import time, json

HEARTBEAT_INTERVAL_SEC = 8 * 3600
HEARTBEAT_STATE_PATH = "/var/tmp/pacs_monitor_heartbeat.json"


SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
MAIL_TO   = os.environ.get("MAIL_TO", SMTP_USER)
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER)

missing = [k for k,v in {
    "SMTP_HOST":SMTP_HOST, "SMTP_PORT":SMTP_PORT, "SMTP_USER":SMTP_USER, "SMTP_PASS":SMTP_PASS, "MAIL_TO":MAIL_TO, "MAIL_FROM":MAIL_FROM
}.items() if v in (None, "")]
if missing:
    raise RuntimeError(f"Variables manquantes: {', '.join(missing)}")




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



def _load_heartbeat_state():
    try:
        with open(HEARTBEAT_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_heartbeat_ts": 0}

def _save_heartbeat_state(state):
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_STATE_PATH), exist_ok=True)
        with open(HEARTBEAT_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Erreur sauvegarde état heartbeat: {e}")

def _should_send_heartbeat(now_ts, last_ts):
    return (now_ts - last_ts) >= HEARTBEAT_INTERVAL_SEC



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

    # Heartbeat: init état persistant
    try:
        state = _load_heartbeat_state()
    except Exception:
        state = {"last_heartbeat_ts": 0}
    last_hb = state.get("last_heartbeat_ts", 0)

    # --- Envoi mail test spécifique URGENT 2025 au démarrage ---
    try:
        test_subject = "🚨 [PACS] TEST URGENT 2025 — vérification système"
        test_body = (
            "Ceci est un test automatique du système de notification URGENT 2025.\n"
            "Tout fonctionne correctement si vous recevez ce message.\n\n"
            "Destinataires : Louis + Blandine."
        )
        send_email(test_subject, test_body, extra_to=["blandinedelaperriere@gmail.com"])
        print("[TEST] Email URGENT 2025 envoyé au démarrage.")
    except Exception as e:
        print(f"[ERREUR] Envoi du mail test URGENT 2025 : {e}")

    while True:
        try:
            slots = get_all_slots()
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Erreur de collecte : {e}")
            time.sleep(interval)
            continue

        new_slots = detect_new_slots(slots, last_slots)
        now_ts = time.time()

        if new_slots:
            print(f"\n=== NOUVEAUX CRÉNEAUX {datetime.now().strftime('%H:%M:%S')} ===")
            lines = []
            urgent_2025 = []
            for s in new_slots:
                line = f"{s['date']} {s['time']}"
                print(f"• {line}")
                lines.append(line)
                if s["date"].startswith("2025-"):
                    urgent_2025.append(line)

            # Envoi général
            subject = f"[PACS] {len(new_slots)} nouveau(x) créneau(x)"
            body = "Nouveaux créneaux détectés:\n" + "\n".join(lines)
            send_email(subject, body)

            # Envoi URGENT 2025 si au moins un
            if urgent_2025:
                urgent_subject = "🚨 [PACS] CRÉNEAU 2025 DISPONIBLE 🎯"
                urgent_body = (
                    "⚠️ Créneau 2025 détecté !\n\n" +
                    "\n".join(urgent_2025) +
                    "\n\nVérifiez immédiatement la disponibilité sur le site."
                )
                send_email(urgent_subject, urgent_body, extra_to=["blandinedelaperriere@gmail.com"])
                print("[ALERTE] Email URGENT 2025 envoyé.")

            print("=" * 40)
            last_slots = slots

        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Aucun nouveau créneau.")

        # Heartbeat toutes les 8h
        if _should_send_heartbeat(now_ts, last_hb):
            total_2025 = sum(1 for s in slots if s.get('date', '').startswith('2025-'))
            total_2026 = sum(1 for s in slots if s.get('date', '').startswith('2026-'))
            subject = "[PACS] Heartbeat système actif"
            body = (
                f"Système OK {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Intervalle scan: {interval} s\n"
                f"Total slots vus (2025): {total_2025}\n"
                f"Total slots vus (2026): {total_2026}\n"
            )
            send_email(subject, body)
            last_hb = now_ts
            state["last_heartbeat_ts"] = last_hb
            _save_heartbeat_state(state)

        time.sleep(interval)



def send_email(subject: str, body: str, extra_to=None):
    recipients = [os.environ["MAIL_TO"]]
    if extra_to:
        recipients.extend(extra_to)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg["Date"] = formatdate(localtime=True)
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        print("Email envoyé.")
    except Exception as e:
        print(f"Erreur envoi email : {e}")





if __name__ == "__main__":
    run_monitor(interval=10)
