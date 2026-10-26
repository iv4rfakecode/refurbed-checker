#!/usr/bin/env python3
"""Refurbed-MacBook-Checker: sucht gute MacBook-Pro-Angebote und schickt sie per Telegram."""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://www.refurbed.de"
HERE = Path(__file__).parent
CONFIG = json.loads((HERE / "config.json").read_text())
STATE_FILE = HERE / "seen.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
MAX_PAGES_PER_PRODUCT = 60
QUICK_PAGES_PER_PRODUCT = 4  # Schnell-Check: nur die vermutlich günstigsten Varianten


def fetch(path):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": UA, "Accept-Language": "de-DE"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def options(page, slug, base_price):
    """Alle Varianten-Links mit Auswahl-Labels (z. B. '18.0 GB', 'DE (QWERTZ)') und geschätztem Preis."""
    found = {}
    pat = rf'<option value="(/p/{re.escape(slug)}/[^"?]+/\?offer=\d+)"([^>]*)>\s*([^<]*?)\s*</option>'
    for url, attrs, label in re.findall(pat, page):
        delta = re.search(r'data-price="(more|less),([+-]?[\d.]+,\d+) €"', attrs)
        est = base_price
        if delta:
            est += float(delta.group(2).replace(".", "").replace(",", "."))
        entry = found.setdefault(url, [set(), est])
        entry[0].add(html.unescape(label))
        entry[1] = min(entry[1], est)
    return found


def worth_visiting(labels):
    for l in labels:
        m = re.fullmatch(r"(\d+)\.0 GB", l)
        if m and int(m.group(1)) < CONFIG["min_ram_gb"]:
            return False  # zu wenig RAM
        if re.fullmatch(r"[A-Z]{2}.*\((QWERT[YZ]|AZERTY|JCUKEN)\)", l) and not l.startswith(CONFIG["layout"]):
            return False  # falsches Tastaturlayout
    return True


def parse_offer(page, url):
    title = re.search(r"<title>([^<]*)", page)
    title = html.unescape(title.group(1)).replace(" – refurbed", "").strip() if title else ""
    offer_id = url.split("offer=")[-1]
    m = re.search(rf'offer={offer_id}","price":([\d.]+)', page) or re.search(r'"price":([\d.]+)', page)
    if not m or "InStock" not in page:
        return None
    chip = re.search(r"Apple (M\d(?: Pro| Max)?) \d+ Core", title)
    ram = re.search(r"(\d+)\.0 GB", title)
    storage = re.search(r"GB (\d+) GB", title)
    akku = re.search(r'data-price="more,\+([\d.,]+) €"\s*>\s*Neu\s*<', page)
    return {
        "id": offer_id,
        "title": title,
        "chip": chip.group(1) if chip else "?",
        "ram": int(ram.group(1)) if ram else 0,
        "storage": int(storage.group(1)) if storage else 0,
        "layout_ok": f"{CONFIG['layout']} (" in title,
        "price": float(m.group(1)),
        "akku_neu": akku.group(1) if akku else None,
        "url": BASE + url,
    }


def first_price(page):
    m = re.search(r'"price":([\d.]+)', page)
    return float(m.group(1)) if m else 0.0


def crawl(slug, max_pages):
    """Prüft die Varianten eines Modells, die vermutlich günstigsten zuerst."""
    start = fetch(f"/p/{slug}/")
    queue = options(start, slug, first_price(start))
    # Die Startseite zeigt das günstigste Angebot des Modells: als Erstes prüfen
    sel = re.search(rf'<option value="(/p/{re.escape(slug)}/[^"?]+/\?offer=\d+)"[^>]*selected', start)
    if sel:
        queue[sel.group(1)] = [set(), 0.0]
    seen, offers = set(), []
    while queue and len(seen) < max_pages:
        url = min(queue, key=lambda u: queue[u][1])
        labels, _ = queue.pop(url)
        variant = url.split("?")[0]
        if variant in seen or not worth_visiting(labels):
            continue
        seen.add(variant)
        time.sleep(0.7)
        try:
            page = fetch(url)
        except Exception as e:
            print(f"  Fehler bei {url}: {e}", file=sys.stderr)
            continue
        o = parse_offer(page, url)
        if o:
            offers.append(o)
        # Nur von passenden Varianten (DE, genug RAM) aus weitersuchen
        if not o or not o["layout_ok"] or o["ram"] < CONFIG["min_ram_gb"]:
            continue
        for u, (l, est) in options(page, slug, o["price"]).items():
            if u.split("?")[0] in seen:
                continue
            entry = queue.setdefault(u, [set(), est])
            entry[0].update(l)
            entry[1] = min(entry[1], est)
    return offers


def is_deal(o):
    limit = CONFIG["max_price"].get(o["chip"])
    return (
        limit is not None
        and o["price"] <= limit
        and o["ram"] >= CONFIG["min_ram_gb"]
        and o["storage"] >= CONFIG["min_storage_gb"]
        and o["layout_ok"]
    )


def send(text):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        print("TG_TOKEN/TG_CHAT_ID fehlen, Nachricht nur ausgegeben:\n" + text)
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30) as r:
        print("Telegram:", r.status)


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    deals = []
    max_pages = QUICK_PAGES_PER_PRODUCT if "--quick" in sys.argv else MAX_PAGES_PER_PRODUCT
    for slug in CONFIG["products"]:
        print("Prüfe", slug)
        try:
            offers = crawl(slug, max_pages)
        except Exception as e:
            print(f"  Fehler: {e}", file=sys.stderr)
            continue
        for o in offers:
            print(f"  {o['price']:>8.2f} €  {o['title']}")
            if is_deal(o):
                deals.append(o)

    # Nur melden, was neu ist oder billiger geworden ist
    fresh = [d for d in deals if d["id"] not in state or d["price"] < state[d["id"]]]
    for d in deals:
        state[d["id"]] = d["price"]
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))

    if "--test" in sys.argv:
        fresh = fresh or sorted(deals, key=lambda d: d["price"])[:1]
        send("✅ Refurbed-Checker läuft." + ("" if fresh else " Gerade kein Deal unter deinen Grenzen."))
    if not fresh:
        print("Keine neuen Deals.")
        return
    fresh.sort(key=lambda d: d["price"])
    lines = ["💻 Refurbed-Deal gefunden!"]
    for d in fresh[:5]:
        akku = f" (neuer Akku +{d['akku_neu']} €)" if d["akku_neu"] else ""
        lines.append(f"\n{d['chip']} · {d['ram']} GB · {d['storage']} GB · {d['price']:.0f} €{akku}\n{d['url']}")
    send("\n".join(lines))


if __name__ == "__main__":
    main()
