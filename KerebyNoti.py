from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from pathlib import Path
from datetime import datetime, timezone
import csv
import re
import requests

BASE_URL = "https://kerebyudlejning.dk"
URL = "https://kerebyudlejning.dk/"
OUT_HTML = Path("kereby_output.html")
OUT_CSV = Path("kereby_rentals.csv")   # seneste snapshot
KNOWN_URLS_FILE = Path("known_urls.txt")
LOG_CSV = Path("kereby_log.csv")       # historisk log med dato og tid


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


def load_known_urls():
    if not KNOWN_URLS_FILE.exists():
        return set()
    lines = KNOWN_URLS_FILE.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip()}


def save_new_urls(urls):
    if not urls:
        return
    with KNOWN_URLS_FILE.open("a", encoding="utf-8") as f:
        for url in urls:
            f.write(url + "\n")


def find_new_listings(listings):
    known = load_known_urls()
    new = [lst for lst in listings if lst["url"] not in known]
    new_urls = [lst["url"] for lst in new]
    save_new_urls(new_urls)
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
    """
    Format for hver lejlighed:

    København S
    Værelser: 3
    Pris: 13895 kr/md
    Adresse: Drogdensgade 5, 1. tv 2300
    Link: ...

    tom linje mellem lejligheder
    """
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
        line5 = f"Link: {url}"

        parts.append("\n".join([line1, line2, line3, line4, line5]))

    return "\n\n".join(parts)


def send_ntfy(body: str):
    """
    Sender en notifikation til ntfy emnet 'kereby-anders'.
    Du har appen til at lytte på dette emne.
    """
    try:
        resp = requests.post(
            "https://ntfy.sh/kereby-anders",
            data=body.encode("utf-8"),
            timeout=10,
        )
        if resp.status_code != 200:
            print("Fejl ved ntfy:", resp.status_code, resp.text[:200])
        else:
            print("ntfy besked sendt.")
    except Exception as e:
        print("Undtagelse ved ntfy:", e)


def append_log(listings, timestamp_utc: str):
    """
    Appender alle aktuelle listings til en logfil
    med en kolonne for tidspunkt. Bruges til mønsteranalyse.
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


def main():
    # tidspunkt i utc for denne kørsel
    timestamp_utc = datetime.now(timezone.utc).isoformat()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle")
        html = page.content()
        browser.close()

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

    # log alle aktuelle listings med tidspunkt til historisk analyse
    append_log(listings, timestamp_utc)
    print(f"Appender {len(listings)} rækker til logfilen {LOG_CSV.resolve()} med timestamp {timestamp_utc}")

    new_listings = find_new_listings(listings)

    if not new_listings:
        print("Ingen nye lejligheder siden sidste kørsel, sender ingen notifikation.")
        return

    selected = new_listings
    body = build_message_body(selected)

    print("Notifikation indhold:")
    print(body)

    send_ntfy(body)


if __name__ == "__main__":
    main()
