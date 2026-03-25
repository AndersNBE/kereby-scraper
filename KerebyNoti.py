from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from pathlib import Path
from datetime import datetime, timezone
import csv
import re
import time
import requests

BASE_URL = "https://kerebyudlejning.dk"
URL = "https://kerebyudlejning.dk/"
OUT_HTML = Path("kereby_output.html")
OUT_CSV = Path("kereby_rentals.csv")   # seneste snapshot
LOG_CSV = Path("kereby_log.csv")       # historisk log med dato og tid
METRICS_CSV = Path("kereby_metrics.csv")  # run-metrics til flaskehalssøgning


def parse_int_from(text: str):
    """
    Finder det første tal i en tekst som for eksempel
    '13.889 kr./md.' eller '3 værelser' eller '67 m2'
    og returnerer det som int. Hvis det ikke lykkes returneres None.
    """
    if not text:
        return None
    match = re.search(r"(\d[\d\.]*)", text)
    if not match:
        return None
    num_str = match.group(1).replace(".", "")
    try:
        return int(num_str)
    except ValueError:
        return None


def extract_listings(html: str):
    soup = BeautifulSoup(html, "html.parser")

    cards = soup.select("a.rental-card")

    listings = []
    seen_urls = set()

    for card in cards:
        def safe_text(selector: str) -> str:
            el = card.select_one(selector)
            if not el:
                return ""
            return el.get_text(" ", strip=True)

        href = (card.get("href", "") or "").strip()

        if href.startswith("/"):
            url = BASE_URL + href
        elif href.startswith("http"):
            url = href
        elif href:
            url = BASE_URL.rstrip("/") + "/" + href.lstrip("/")
        else:
            url = ""

        if not url:
            continue

        if url in seen_urls:
            continue
        seen_urls.add(url)

        location = safe_text(".location")
        headline = safe_text(".headline")
        rent_raw = safe_text(".monthly-rent")
        rooms_raw = safe_text(".rooms")
        sqm_raw = safe_text(".square-meters")

        status = ""
        inactive = card.select_one(".inactive, .inactive-message")
        if inactive:
            status = inactive.get_text(" ", strip=True)

        rent = parse_int_from(rent_raw)
        rooms = parse_int_from(rooms_raw)
        sqm = parse_int_from(sqm_raw)

        listings.append(
            {
                "url": url,
                "headline": headline,
                "location": location,
                "status": status,
                "rent_kr_per_month": rent,
                "rooms": rooms,
                "sqm": sqm,
            }
        )

    return listings


def get_logged_urls():
    """
    Henter alle URLs der tidligere er logget i kereby_log.csv.
    Bruges til at afgøre hvad der er nyt.
    """
    if not LOG_CSV.exists():
        print("Logfil findes ikke endnu, 0 logged urls")
        return set()

    urls = set()
    with LOG_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = (row.get("url") or "").strip()
            if url:
                urls.add(url)

    print(f"Loggen indeholder {len(urls)} urls")
    return urls

def find_new_listings(listings):
    """
    Finder nye lejligheder i forhold til kereby_log.csv.
    Alt der IKKE findes i logfilens URL-kolonne betragtes som nyt.
    """
    logged = get_logged_urls()

    logged_sample = list(sorted(logged))[:10]
    print("Eksempel på logged urls (maks 10):")
    for u in logged_sample:
        print(" -", u)

    new = []
    for lst in listings:
        url = (lst.get("url") or "").strip()
        if not url:
            continue
        if url in logged:
            continue
        new.append(lst)

    print(f"Fundet {len(new)} nye lejligheder i dette run")
    if new:
        print("Nye urls (maks 10):")
        for u in [x["url"] for x in new[:10]]:
            print(" -", u)

    return new


def split_city_and_address(location: str):
    """
    Forsøger at dele en streng som
    'Drogdensgade 5, 1. tv 2300 København S'
    i
    adresse: 'Drogdensgade 5, 1. tv 2300'
    by: 'København S'
    """
    if not location:
        return "Ukendt by", "Ukendt adresse"

    location = location.strip()

    match = re.match(r"^(.*\b)(\d{4})\s+(.+)$", location)
    if match:
        adresse = (match.group(1) + match.group(2)).strip()
        by = match.group(3).strip()
        return by, adresse

    parts = [p.strip() for p in location.rsplit(",", 1)]
    if len(parts) == 2:
        adresse, by = parts
        return by, adresse

    return location, location


def build_message_body(new_listings):
    parts = []
    for listing in new_listings:
        location = listing.get("location") or ""
        city, address = split_city_and_address(location)

        rooms = listing.get("rooms")
        price = listing.get("rent_kr_per_month")
        url = listing.get("url")

        line1 = f"{city}"
        line2 = f"Værelser: {rooms if rooms is not None else '?'}"
        line3 = f"Pris: {price} kr/md" if price is not None else "Pris: ukendt"
        line4 = f"Adresse: {address if address else 'Ukendt adresse'}"
        line5 = url  # ← gør linket klikbart (står alene)

        parts.append("\n".join([line1, line2, line3, line4, line5]))

    return "\n\n".join(parts)


def send_ntfy(body: str):
    first_link = None
    for line in body.splitlines():
        if line.startswith("http"):
            first_link = line.strip()
            break

    headers = {
        "Title": "Ny Kereby lejlighed",
        "Priority": "high",
        "User-Agent": "kereby-scraper/1.0 (github-actions)",
    }
    if first_link:
        headers["Click"] = first_link

    resp = requests.post(
        "https://ntfy.sh/kereby-anders",
        headers=headers,
        data=body.encode("utf-8"),
        timeout=20,
    )

    print("ntfy status:", resp.status_code)
    if resp.status_code != 200:
        print("ntfy response (første 300 tegn):", (resp.text or "")[:300])
        raise RuntimeError(f"ntfy returnerede {resp.status_code}")

    print("ntfy besked sendt.")


def append_log(listings, timestamp_utc: str):
    """
    Appender listings til en logfil
    med en kolonne for tidspunkt.
    Bruges til mønsteranalyse og til at huske hvad vi har set.
    """
    if not listings:
        return

    fieldnames = [
        "timestamp_utc",
        "url",
        "headline",
        "location",
        "status",
        "rent_kr_per_month",
        "rooms",
        "sqm",
    ]

    file_exists = LOG_CSV.exists()

    with LOG_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        for lst in listings:
            row = {
                "timestamp_utc": timestamp_utc,
                "url": lst.get("url"),
                "headline": lst.get("headline"),
                "location": lst.get("location"),
                "status": lst.get("status"),
                "rent_kr_per_month": lst.get("rent_kr_per_month"),
                "rooms": lst.get("rooms"),
                "sqm": lst.get("sqm"),
            }
            writer.writerow(row)


def append_metrics(row: dict):
    file_exists = METRICS_CSV.exists()
    fieldnames = [
        "run_start_utc",
        "scrape_duration_s",
        "total_listings_on_site",
        "new_listings",
        "new_already_reserved",
        "new_available",
        "notification_sent",
        "notification_timestamp_utc",
        "total_duration_s",
    ]
    with METRICS_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def main():
    run_start = time.monotonic()
    timestamp_utc = datetime.now(timezone.utc).isoformat()

    # ── scrape ────────────────────────────────────────────────────────────────
    scrape_start = time.monotonic()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle")
        html = page.content()
        browser.close()
    scrape_duration = round(time.monotonic() - scrape_start, 2)

    OUT_HTML.write_text(html, encoding="utf-8")

    listings = extract_listings(html)

    fieldnames = [
        "url",
        "headline",
        "location",
        "status",
        "rent_kr_per_month",
        "rooms",
        "sqm",
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(listings)

    print(f"Skrev {len(listings)} unikke rækker til {OUT_CSV.resolve()}")

    # ── nye lejligheder ───────────────────────────────────────────────────────
    new_listings = find_new_listings(listings)

    new_reserved = [l for l in new_listings if (l.get("status") or "").strip()]
    new_available = [l for l in new_listings if not (l.get("status") or "").strip()]

    metrics = {
        "run_start_utc": timestamp_utc,
        "scrape_duration_s": scrape_duration,
        "total_listings_on_site": len(listings),
        "new_listings": len(new_listings),
        "new_already_reserved": len(new_reserved),
        "new_available": len(new_available),
        "notification_sent": False,
        "notification_timestamp_utc": "",
        "total_duration_s": "",
    }

    if not new_listings:
        print("Ingen nye lejligheder siden sidste kørsel, sender ingen notifikation.")
        metrics["total_duration_s"] = round(time.monotonic() - run_start, 2)
        append_metrics(metrics)
        return

    # ── log nye ───────────────────────────────────────────────────────────────
    append_log(new_listings, timestamp_utc)
    print(
        f"Appender {len(new_listings)} nye rækker til logfilen "
        f"({len(new_reserved)} allerede reserverede, {len(new_available)} ledige)"
    )

    body = build_message_body(new_listings)

    print("Notifikation indhold:")
    print(body)

    send_ntfy(body)

    metrics["notification_sent"] = True
    metrics["notification_timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    metrics["total_duration_s"] = round(time.monotonic() - run_start, 2)
    append_metrics(metrics)


if __name__ == "__main__":
    main()
