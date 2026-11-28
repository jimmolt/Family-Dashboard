"""Build the static dashboard site into _site/ (run by GitHub Actions).

Public (plain) files:   index.html, data/meta.json, data/school.json,
                        data/lunch.json, lunch/<date>.png
Private (encrypted):    data/private.bin  (family events + photo list)
                        photos/<id>.bin   (each photo)

Secrets (GitHub repo settings -> Secrets -> Actions):
  DASH_PASSPHRASE    passphrase the page asks for
  DROPBOX_LINK       shared link to the "Kitchen Display" folder (optional)
  FAMILY_CALENDARS   JSON list: [{"name":"Jim","color":"#4f8cff","url":"<secret iCal>"}]
"""
import base64
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).parent))
import lunch  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text())
TZ = ZoneInfo(CFG.get("timezone", "America/Chicago"))
SITE = ROOT / "_site"
WORK = ROOT / "_work"
PBKDF2_ITER = 200_000
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
MAX_SIDE = 1920


def now():
    return dt.datetime.now(TZ)


def today():
    return now().date()


# ------------------------------------------------------------- calendars
def fetch_ics(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return icalendar.Calendar.from_ical(r.content)


def events_between(cal, start, end):
    """Plain dicts with 'YYYY-MM-DD' dates and local 'HH:MM' times, so the
    page never converts from UTC (that bug pushed all-day events a day early)."""
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        s = ev.get("DTSTART").dt
        e = ev.get("DTEND").dt if ev.get("DTEND") else s
        if not isinstance(s, dt.datetime):          # all-day
            last = e - dt.timedelta(days=1) if e > s else s
            item = {"date": s.isoformat(), "end": last.isoformat(), "time": None}
        else:
            s, e = s.astimezone(TZ), e.astimezone(TZ)
            item = {"date": s.date().isoformat(), "end": e.date().isoformat(),
                    "time": s.strftime("%H:%M")}
        item["title"] = re.sub(r"\s+", " ", str(ev.get("SUMMARY", ""))).strip()
        item["where"] = str(ev.get("LOCATION", "")).strip()
        out.append(item)
    out.sort(key=lambda e: (e["date"], e["time"] or "00:00"))
    return out


def window(days):
    s = dt.datetime.combine(today(), dt.time.min, TZ)
    return s, s + dt.timedelta(days=days)


def build_school():
    sc = CFG["school"]
    evs = events_between(fetch_ics(sc["ics_url"]), *window(sc.get("days", 21)))
    sport = re.compile(sc["sports_pattern"], re.I)
    tidy = re.compile(r"^\W*(\*+\s*)?(time change|site change|update)\W*:?\s*", re.I)
    sports, school = [], []
    for e in evs:
        e["title"] = re.sub(r"\s+", " ", tidy.sub("", e["title"])).strip("* ")
        e["where"] = re.sub(r"(?i)^site change to\s+", "", e["where"])
        (sports if sport.search(e["title"]) else school).append(e)
    return {"sports": sports, "school": school}


def build_family():
    feeds = json.loads(os.environ.get("FAMILY_CALENDARS") or "[]")
    merged, errors = {}, []
    for feed in feeds:
        try:
            for e in events_between(fetch_ics(feed["url"]),
                                    *window(CFG.get("calendar_days", 8))):
                k = (e["date"], e["time"], e["title"].lower())
                color = feed.get("color", "#888")
                if k in merged:
                    merged[k]["colors"].append(color)
                else:
                    merged[k] = {**e, "colors": [color]}
        except Exception as ex:
            errors.append(f"{feed.get('name', '?')}: {type(ex).__name__}")
    events = sorted(merged.values(), key=lambda e: (e["date"], e["time"] or "00:00"))
    return events, errors


# ------------------------------------------------------------- dropbox
def fetch_dropbox():
    """Download the shared folder as a zip. Returns (photos, pdfs) paths."""
    link = (os.environ.get("DROPBOX_LINK") or "").strip()
    dest = WORK / "dropbox"
    dest.mkdir(parents=True, exist_ok=True)
    if not link:
        return [], []
    link = re.sub(r"([?&])dl=0", r"\1dl=1", link)
    if "dl=1" not in link:
        link += ("&" if "?" in link else "?") + "dl=1"
    r = requests.get(link, timeout=300)
    r.raise_for_status()
    zipfile.ZipFile(io.BytesIO(r.content)).extractall(dest)
    photos, menus = [], []
    for p in dest.rglob("*"):
        if not p.is_file() or p.name.startswith("."):
            continue
        rel = [x.lower() for x in p.relative_to(dest).parts[:-1]]
        ext = p.suffix.lower()
        # Lunch menus: any PDF, or a picture with "menu" in its name or in a
        # Lunch folder. Everything else that's a picture is a slideshow photo.
        if ext == ".pdf" or (ext in lunch.MENU_EXT and
                             ("menu" in p.stem.lower() or "lunch" in rel)):
            menus.append(p)
        elif ext in IMG_EXT and "lunch" not in rel:
            photos.append(p)
    print('dropbox menus:', ', '.join(sorted(m.name for m in menus)) or 'none')
    return sorted(photos), menus


# ------------------------------------------------------------- lunch
def build_lunch(menus):
    """One set of daily menu crops per school site (Spalding, Cedar Rapids)."""
    out_dir = SITE / "lunch"
    out_dir.mkdir(parents=True, exist_ok=True)
    sources = list(menus) + [p for p in (ROOT / "menus").iterdir()
                             if p.suffix.lower() in lunch.MENU_EXT]
    sites = []
    for site in CFG["lunch_sites"]:
        pat = re.compile(site["match"], re.I)
        menu_dir = WORK / "menus" / site["key"]
        menu_dir.mkdir(parents=True, exist_ok=True)
        for p in sources:
            if pat.search(p.stem):
                shutil.copy2(p, menu_dir / p.name)
        days, d = {}, today()
        for _ in range(21):
            if d.weekday() < 5:
                img = lunch.lunch_image(d, menu_dir, WORK / "lunchcache" / site["key"])
                if img:
                    name = f"{site['key']}-{d.isoformat()}.png"
                    shutil.copy2(img, out_dir / name)
                    days[d.isoformat()] = f"lunch/{name}"
            d += dt.timedelta(days=1)
        print(f"lunch {site['name']}: {len(days)} days")
        sites.append({"key": site["key"], "name": site["name"], "days": days})
    return {"sites": sites}


# ------------------------------------------------------------- crypto
def make_key(passphrase, salt):
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, PBKDF2_ITER, 32)


def encrypt(key, data: bytes) -> bytes:
    iv = os.urandom(12)
    return iv + AESGCM(key).encrypt(iv, data, None)


def shrink(path):
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82, optimize=True)
    return buf.getvalue()


# ------------------------------------------------------------- main
def main():
    shutil.rmtree(SITE, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)
    (SITE / "data").mkdir(parents=True)
    (SITE / "photos").mkdir()
    shutil.copy2(ROOT / "web" / "index.html", SITE / "index.html")
    for f in (ROOT / "web").glob("*.png"):
        shutil.copy2(f, SITE / f.name)
    shutil.copy2(ROOT / "pi" / "setup-kiosk.sh", SITE / "setup-kiosk.sh")
    (SITE / ".nojekyll").write_text("")

    problems = []

    def step(name, fn, default):
        try:
            return fn()
        except Exception as e:
            print(f"!! {name} failed: {e}")
            problems.append(name)
            return default

    school = step("school calendar", build_school, {"sports": [], "school": []})
    (SITE / "data" / "school.json").write_text(json.dumps(school))

    photos, menus = step("dropbox", fetch_dropbox, ([], []))
    lunches = step("lunch", lambda: build_lunch(menus), {"sites": []})
    (SITE / "data" / "lunch.json").write_text(json.dumps(lunches))

    passphrase = os.environ.get("DASH_PASSPHRASE", "")
    # Same salt every build -> same key every build, so a page that's been
    # open for hours can still unlock photos from the newest build.
    salt = hashlib.sha256(b"molt-dashboard-salt:" + passphrase.encode()).digest()[:16]
    meta = {"built": now().isoformat(timespec="minutes"), "title": CFG["title"],
            "weather": CFG["weather"], "photo_seconds": CFG["photo_seconds"],
            "lunch_cutoff": CFG["lunch_cutoff"], "timezone": str(TZ),
            "salt": base64.b64encode(salt).decode(), "iter": PBKDF2_ITER,
            "private": bool(passphrase), "problems": problems}

    if passphrase:
        key = make_key(passphrase, salt)
        names = []
        for p in photos:
            try:
                jpg = shrink(p)
            except Exception as e:
                print("skip photo", p.name, e)
                continue
            name = hashlib.sha256(jpg).hexdigest()[:16] + ".bin"
            (SITE / "photos" / name).write_bytes(encrypt(key, jpg))
            names.append(name)
        print(f"photos: {len(names)}")
        events, errors = step("family calendars", build_family, ([], ["all feeds"]))
        private = {"events": events, "errors": errors, "photos": names}
        (SITE / "data" / "private.bin").write_bytes(
            encrypt(key, json.dumps(private).encode()))
    else:
        print("No DASH_PASSPHRASE set: skipping photos and family calendars.")

    (SITE / "data" / "meta.json").write_text(json.dumps(meta))
    shutil.rmtree(WORK, ignore_errors=True)
    print("built", meta["built"], "problems:", problems or "none")


if __name__ == "__main__":
    main()
