"""Crop today's box out of the scanned Riverside monthly lunch menu PDF.

The menus are scanned images (no text layer), so instead of reading them we
find the calendar grid lines and cut out the box for the requested date.
"""
import calendar
import datetime as dt
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
DARK = 170          # pixel value below this counts as "ink"
RENDER_DPI = 150


def _groups(values, gap=4):
    out = []
    for v in values:
        if out and v - out[-1][-1] <= gap:
            out[-1].append(v)
        else:
            out.append([v])
    return [int(sum(g) / len(g)) for g in out]


def _max_run(row):
    """Longest run of True in a 1-D bool array -> (length, start, end)."""
    padded = np.concatenate(([0], row.astype(np.int8), [0]))
    d = np.diff(padded)
    starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
    if len(starts) == 0:
        return 0, 0, 0
    lens = ends - starts
    i = int(lens.argmax())
    return int(lens[i]), int(starts[i]), int(ends[i])


def find_grid(img):
    """Return (row_lines, col_lines) in pixel coords for the calendar table."""
    ink = np.array(img.convert("L")) < DARK
    h, w = ink.shape
    m = 8  # ignore scanner smudges along the page edges
    ink[:m, :] = ink[-m:, :] = False
    ink[:, :m] = ink[:, -m:] = False
    # Table left/right edges = median extent of the long horizontal lines.
    runs = [_max_run(ink[y]) for y in range(h)]
    long_runs = [r for r in runs if r[0] > 0.5 * w]
    if not long_runs:
        raise ValueError("no table found")
    x0 = int(np.median([r[1] for r in long_runs]))
    x1 = int(np.median([r[2] for r in long_runs]))
    # Scans are slightly crooked, so thicken the ink a couple of pixels
    # before asking "is this row/column a solid line across the table?"
    def thicken(a, axis):
        out = a.copy()
        for k in (1, 2):
            out |= np.roll(a, k, axis=axis) | np.roll(a, -k, axis=axis)
        return out
    cover = thicken(ink, 0)[:, x0:x1].mean(axis=1)
    hlines = _groups([y for y in range(h) if cover[y] > 0.9])
    if len(hlines) < 3:
        raise ValueError("couldn't find calendar rows")
    band = thicken(ink, 1)[hlines[0]:hlines[-1], :]
    vlines = _groups([x for x in range(max(0, x0 - 5), min(w, x1 + 5))
                      if band[:, x].mean() > 0.9])
    # Clip art can break an outer border; fall back to the table edges.
    if not vlines or vlines[0] > x0 + 20:
        vlines.insert(0, x0)
    if vlines[-1] < x1 - 20:
        vlines.append(x1)
    return hlines, vlines


def _cell_number(img, box):
    """OCR the day number printed in the top-right corner of a cell."""
    if not shutil.which("tesseract"):
        return None
    l, t, r, b = box
    corner = img.crop((r - int((r - l) * 0.3), t + 2, r - 4, t + int((b - t) * 0.22)))
    corner = corner.convert("L").resize((corner.width * 3, corner.height * 3))
    tmp = Path("/tmp/_lunch_corner.png")
    corner.save(tmp)
    try:
        out = subprocess.run(
            ["tesseract", str(tmp), "-", "--psm", "7",
             "-c", "tessedit_char_whitelist=0123456789"],
            capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return None
    digits = re.sub(r"\D", "", out)
    return int(digits) if digits and 1 <= int(digits) <= 31 else None


def month_from_name(pdf_path, today=None):
    """'SP Site December Menu.pdf' -> (year, month), picking the closest year."""
    today = today or dt.date.today()
    name = Path(pdf_path).stem.lower()
    month = next((n for m, n in MONTHS.items() if m in name), None)
    if not month:
        return None
    yr = re.search(r"(20\d\d)", name)
    if yr:
        return int(yr.group(1)), month
    cands = [(abs((dt.date(y, month, 15) - today).days), y)
             for y in (today.year - 1, today.year, today.year + 1)]
    return min(cands)[1], month


def render(pdf_path, out_png):
    subprocess.run(["pdftoppm", "-r", str(RENDER_DPI), "-png", "-singlefile",
                    str(pdf_path), str(out_png.with_suffix(""))], check=True)
    return Image.open(out_png)


class MenuMonth:
    """One rendered monthly menu, with a date -> cell box map."""

    def __init__(self, pdf_path, cache_dir):
        self.pdf = Path(pdf_path)
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        png = cache_dir / (self.pdf.stem.replace(" ", "_") + ".png")
        if not png.exists() or png.stat().st_mtime < self.pdf.stat().st_mtime:
            self.img = render(self.pdf, png)
        else:
            self.img = Image.open(png)
        self.year, self.month = month_from_name(self.pdf) or (None, None)
        self.cells = self._map_cells()

    def _map_cells(self):
        hl, vl = find_grid(self.img)
        cols = list(zip(vl[:-1], vl[1:]))[:5]                # Mon..Fri
        bands = [(a, b) for a, b in zip(hl[:-1], hl[1:]) if b - a > 15]
        # Header band (day names) is much shorter than week rows.
        tallest = max(b - a for a, b in bands)
        weeks = [(a, b) for a, b in bands if b - a > 0.6 * tallest]
        if not weeks or not self.year:
            return {}

        # Anchor: date of the Monday cell in the first week row.
        anchor = None
        first_of_month = dt.date(self.year, self.month, 1)
        for ci, (l, r) in enumerate(cols):
            n = _cell_number(self.img, (l, weeks[0][0], r, weeks[0][1]))
            if n:
                # n is the date in column ci; it's either this month or the
                # tail of the previous month (e.g. Mon 30 before Tue 1).
                if n <= 7:
                    guess = dt.date(self.year, self.month, n)
                else:
                    prev = first_of_month - dt.timedelta(days=1)
                    guess = prev.replace(day=n) if n <= prev.day else None
                if guess and guess.weekday() == ci:
                    anchor = guess - dt.timedelta(days=ci)
                    break
        if anchor is None:
            # Calendar math fallback: first row = week holding the 1st
            # (or the following Monday if the 1st is a weekend).
            wd = first_of_month.weekday()
            anchor = first_of_month - dt.timedelta(days=wd) if wd < 5 \
                else first_of_month + dt.timedelta(days=7 - wd)

        cells = {}
        for wi, (t, b) in enumerate(weeks):
            for ci, (l, r) in enumerate(cols):
                d = anchor + dt.timedelta(days=7 * wi + ci)
                cells[d] = (l + 3, t + 3, r - 2, b - 2)
        return cells

    def crop(self, day):
        box = self.cells.get(day)
        if not box:
            return None
        cell = self.img.crop(box)
        # Blank cell (no school)? Ignore the date number in the top 25%.
        body = np.array(cell.convert("L"))[int(cell.height * 0.25):] < DARK
        if body.mean() < 0.004:
            return None
        return cell


def find_menu_for(day, menu_dir):
    """Newest PDF in menu_dir whose month covers `day` (incl. spillover)."""
    pdfs = sorted(Path(menu_dir).glob("*.pdf"), key=lambda p: p.stat().st_mtime,
                  reverse=True)
    for p in pdfs:
        ym = month_from_name(p, day)
        if not ym:
            continue
        y, m = ym
        start = dt.date(y, m, 1) - dt.timedelta(days=7)
        end = dt.date(y, m, calendar.monthrange(y, m)[1]) + dt.timedelta(days=7)
        if start <= day <= end:
            yield p


def lunch_image(day, menu_dir, cache_dir):
    """Return path to a PNG of the menu box for `day`, or None."""
    cache_dir = Path(cache_dir)
    for pdf in find_menu_for(day, menu_dir):
        out = cache_dir / f"lunch_{day.isoformat()}_{int(pdf.stat().st_mtime)}.png"
        if out.exists():
            return out
        try:
            mm = MenuMonth(pdf, cache_dir)
        except Exception as e:  # unreadable scan: try the next PDF
            print("lunch: failed on", pdf, e)
            continue
        cell = mm.crop(day)
        if cell is not None:
            cell.save(out)
            return out
    return None


if __name__ == "__main__":
    import sys
    pdf, day = sys.argv[1], dt.date.fromisoformat(sys.argv[2])
    mm = MenuMonth(pdf, "/tmp/lunchcache")
    print(f"{mm.year}-{mm.month}: {len(mm.cells)} cells, "
          f"{min(mm.cells)} .. {max(mm.cells)}")
    c = mm.crop(day)
    print("cell:", None if c is None else c.size)
    if c is not None:
        c.save("/tmp/lunch_test.png")
