"""
pdfstamp - stamp PDFs as RECEIVED and APPROVED on your own computer.

Folders (created next to this script on first run):
    1_inbox/       put downloaded PDFs here
    2_received/    PDFs stamped "RECEIVED yyyy-mm-dd" on every page
    3_approved/    received PDFs with the approval seal added
    originals/     untouched originals, kept after "receive"
    seal/approved_seal.png   your seal image (transparent PNG)
    stamp_log.csv  audit log of every stamp

Usage:
    python pdfstamp.py                      interactive menu
    python pdfstamp.py receive              stamp every PDF in 1_inbox
    python pdfstamp.py receive --date 2026-10-01
    python pdfstamp.py approve              approve every PDF in 2_received
    python pdfstamp.py approve FILE.pdf     approve one file (name or path)
    python pdfstamp.py approve --pages all  seal on every page (default: last)
    python pdfstamp.py recipients           CSV of file name + Attn email (from 2_received)
    python pdfstamp.py recipients --folder 1_inbox --out next_recipients.csv
"""
import argparse
import csv
import datetime as dt
import getpass
import hashlib
import io
import re
import shutil
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")  # hide harmless library deprecation notices

from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

BASE = Path(__file__).resolve().parent
INBOX = BASE / "1_inbox"
RECEIVED = BASE / "2_received"
APPROVED = BASE / "3_approved"
ORIGINALS = BASE / "originals"
SEAL = BASE / "seal" / "approved_seal.png"
LOG = BASE / "stamp_log.csv"

RED = "#C00000"
BLUE = "#1F4E9A"
MARGIN = 24  # points from the page edge (72 points = 1 inch)
AUTO_PLACE = True  # put the seal in the emptiest spot (needs pypdfium2); False = always bottom-right

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
ATTN_RE = re.compile(r"\b(?:attn|attention)\b\s*[:.\-]?", re.IGNORECASE)


# ---------------------------------------------------------------- helpers
def ensure_folders():
    for d in (INBOX, RECEIVED, APPROVED, ORIGINALS, SEAL.parent):
        d.mkdir(parents=True, exist_ok=True)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def log(action, src, dst, user, detail=""):
    new = not LOG.exists()
    with LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "action", "source_file", "output_file",
                        "user", "output_sha256", "detail"])
        w.writerow([dt.datetime.now().isoformat(timespec="seconds"), action,
                    Path(src).name, Path(dst).name if dst else "", user,
                    sha256(dst) if dst else "", detail])


def open_pdf(path):
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise ValueError
        except Exception:
            raise RuntimeError("password-protected PDF; ask the sender for an unlocked copy")
    return reader


def overlay_page(width, height, draw):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    draw(c, width, height)
    c.save()
    buf.seek(0)
    return PdfReader(buf).pages[0]


def stamp_pages(src, dst, draw, which="all"):
    """Apply draw(canvas, w, h) on top of the chosen pages and save to dst."""
    reader = open_pdf(src)
    writer = PdfWriter()
    n = len(reader.pages)
    targets = {"all": set(range(n)), "first": {0}, "last": {n - 1}}[which]
    for i, page in enumerate(reader.pages):
        if i in targets:
            # Bake any /Rotate into the content so "top-right" is top-right as viewed.
            if page.rotation:
                page.transfer_rotation_to_content()
            box = page.mediabox
            w, h = float(box.width), float(box.height)
            page.merge_transformed_page(
                overlay_page(w, h, draw),
                [1, 0, 0, 1, float(box.left), float(box.bottom)])
        writer.add_page(page)
    tmp = Path(dst).with_suffix(".tmp")
    with tmp.open("wb") as f:
        writer.write(f)
    tmp.replace(dst)  # never leave a half-written PDF behind
    return n


def boxed_text(c, text, x, y, color, size=14):
    c.setFont("Helvetica-Bold", size)
    c.setFillColor(color)
    c.setStrokeColor(color)
    c.setLineWidth(1.5)
    tw = c.stringWidth(text, "Helvetica-Bold", size)
    bw, bh = tw + 16, size + 12
    c.roundRect(x, y, bw, bh, 4, stroke=1, fill=0)
    c.drawString(x + 8, y + 8, text)
    return bw, bh


def find_blank_spot(pdf_path, page_index, block_w, block_h, dpi=36):
    """Find the emptiest place on a page for a block of block_w x block_h points.

    Renders the page (as viewed, rotation applied) to a small greyscale image,
    slides the block over a grid and measures how much ink is under it.
    Prefers the bottom-right when spots are about equally clear.
    Returns (x, y, ink_pct) in points from the bottom-left, or None if the page
    can't be rendered (the caller then uses the default bottom-right corner).
    """
    try:
        import pypdfium2 as pdfium
        from PIL import Image
    except ImportError:
        if not getattr(find_blank_spot, "warned", False):
            print("  NOTE  pypdfium2 is not installed, so seals go bottom-right. "
                  "Run: pip install pypdfium2")
            find_blank_spot.warned = True
        return None
    try:
        doc = pdfium.PdfDocument(str(pdf_path))
        img = doc[page_index].render(scale=dpi / 72).to_pil().convert("L")
        doc.close()
    except Exception:
        return None

    cell = 4  # pixels per grid cell (8 points at 36 dpi)
    gw, gh = img.width // cell, img.height // cell
    if gw < 2 or gh < 2:
        return None
    # "Ink" = pixels clearly darker than the paper around them, so grey scans and
    # coloured backgrounds still count as blank; very dark areas always count as ink.
    from PIL import ImageChops, ImageFilter
    paper = img.filter(ImageFilter.MaxFilter(9))          # local background brightness
    contrast = ImageChops.subtract(paper, img).point(lambda v: 255 if v > 35 else 0)
    solid = img.point(lambda v: 255 if v < 110 else 0)    # dark bars, logos, filled boxes
    mask = ImageChops.lighter(contrast, solid)
    vals = list(mask.resize((gw, gh), resample=Image.BOX).getdata())
    # Integral image for fast rectangle sums.
    integ = [[0] * (gw + 1) for _ in range(gh + 1)]
    for r in range(gh):
        row_sum = 0
        for c in range(gw):
            row_sum += vals[r * gw + c]
            integ[r + 1][c + 1] = integ[r][c + 1] + row_sum

    pt_per_cell = cell * 72 / dpi
    bw = max(1, int(-(-block_w // pt_per_cell)))  # block size in cells, rounded up
    bh = max(1, int(-(-block_h // pt_per_cell)))
    m = max(1, int(-(-MARGIN // pt_per_cell)))    # keep clear of the page edge
    if bw + 2 * m > gw or bh + 2 * m > gh:
        return None

    best = None
    for top in range(m, gh - bh - m + 1):
        for left in range(m, gw - bw - m + 1):
            s = (integ[top + bh][left + bw] - integ[top][left + bw]
                 - integ[top + bh][left] + integ[top][left])
            ink_pct = s / (255 * bw * bh) * 100
            # Tie-breaker: small penalty for distance from the bottom-right corner.
            dist = (gw - m - (left + bw)) / gw + (gh - m - (top + bh)) / gh
            score = ink_pct + dist * 0.5
            if best is None or score < best[0]:
                best = (score, left, top, ink_pct)
    _, left, top, ink_pct = best
    x = left * pt_per_cell
    y = (gh - (top + bh)) * pt_per_cell  # flip: image rows run top-down
    return x, y, ink_pct


def list_pdfs(folder):
    return sorted(p for p in folder.iterdir() if p.suffix.lower() == ".pdf")


def parse_date(s):
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        sys.exit(f"Date must look like 2026-10-01, got: {s}")


# ---------------------------------------------------------------- actions
def receive(date=None, user=None):
    ensure_folders()
    date = date or dt.date.today()
    user = user or getpass.getuser()
    text = f"RECEIVED {date.isoformat()}"
    files = list_pdfs(INBOX)
    if not files:
        print(f"No PDFs in {INBOX}")
        return 0
    done = 0
    for src in files:
        dst = RECEIVED / src.name
        if dst.exists():
            print(f"  SKIP  {src.name}: already in 2_received")
            continue

        def draw(c, w, h):
            size = 14
            tw = c.stringWidth(text, "Helvetica-Bold", size) + 16
            boxed_text(c, text, w - MARGIN - tw, h - MARGIN - (size + 12), RED, size)

        try:
            pages = stamp_pages(src, dst, draw, "all")
        except Exception as e:
            print(f"  FAIL  {src.name}: {e}")
            log("receive-failed", src, None, user, str(e))
            continue
        shutil.move(str(src), str(ORIGINALS / src.name))
        log("received", src, dst, user, f"{text}; {pages} pages")
        print(f"  OK    {src.name}: {text} on {pages} page(s)")
        done += 1
    print(f"Done: {done} file(s) stamped into {RECEIVED}")
    return done


def approve(names=None, pages="last", user=None, date=None):
    ensure_folders()
    if not SEAL.exists():
        sys.exit(f"Seal image not found: {SEAL}\nSave your seal as a transparent PNG there.")
    user = user or getpass.getuser()
    date = date or dt.date.today()
    if names:
        files = []
        for n in names:
            p = Path(n)
            p = p if p.exists() else RECEIVED / p.name
            if not p.exists():
                print(f"  FAIL  {n}: not found (look in 2_received)")
                continue
            files.append(p)
    else:
        files = list_pdfs(RECEIVED)
    if not files:
        print(f"No PDFs to approve in {RECEIVED}")
        return 0

    seal = ImageReader(str(SEAL))
    iw, ih = seal.getSize()
    caption = f"APPROVED {date.isoformat()} by {user}"
    done = 0
    for src in files:
        dst = APPROVED / src.name
        if dst.exists():
            print(f"  SKIP  {src.name}: already approved")
            continue

        try:
            total = len(open_pdf(src).pages)
        except Exception as e:
            print(f"  FAIL  {src.name}: {e}")
            log("approve-failed", src, None, user, str(e))
            continue
        # Pages that get the seal, in the order stamp_pages draws them.
        page_order = iter({"all": range(total), "first": [0], "last": [total - 1]}[pages])
        placements = []

        def draw(c, w, h):
            idx = next(page_order)
            sw = min(130.0, w * 0.22)  # seal width in points
            sh = sw * ih / iw
            cap_w = c.stringWidth(caption, "Helvetica-Bold", 8)
            block_w, block_h = max(sw, cap_w), sh + 20  # seal + caption underneath
            spot = find_blank_spot(src, idx, block_w, block_h) if AUTO_PLACE else None
            if spot:
                bx, by, ink = spot
                placements.append(f"p{idx + 1}: {ink:.1f}% ink under seal")
            else:  # default: bottom-right corner
                bx, by = w - MARGIN - block_w, MARGIN - 2
                placements.append(f"p{idx + 1}: bottom-right")
            right = bx + block_w
            c.drawImage(seal, right - sw, by + 20, sw, sh, mask="auto")
            c.setFont("Helvetica-Bold", 8)
            c.setFillColor(BLUE)
            c.drawRightString(right, by + 6, caption)

        try:
            n = stamp_pages(src, dst, draw, pages)
        except Exception as e:
            print(f"  FAIL  {src.name}: {e}")
            log("approve-failed", src, None, user, str(e))
            continue
        where = "; ".join(placements)
        log("approved", src, dst, user, f"seal on {pages} page(s) of {n}; {where}")
        print(f"  OK    {src.name}: seal added ({where})")
        done += 1
    print(f"Done: {done} file(s) approved into {APPROVED}")
    return done


def find_recipient(pdf):
    """Return the first email after 'Attn:' / 'ATTN' / 'Attention:' in the first 2 pages, or ''."""
    reader = open_pdf(pdf)
    text = "\n".join((p.extract_text() or "") for p in reader.pages[:2])
    for m in ATTN_RE.finditer(text):
        e = EMAIL_RE.search(text, m.end(), m.end() + 120)  # email within ~2 lines of the label
        if e:
            return e.group(0).strip(".").lower()
    return ""


def export_recipients(folder=None, out_csv=None):
    """Write a CSV of file name -> next recipient email for every PDF in the folder."""
    ensure_folders()
    folder = Path(folder) if folder else RECEIVED
    if not folder.is_absolute() and not folder.exists():
        folder = BASE / folder
    out_csv = Path(out_csv) if out_csv else BASE / f"recipients_{dt.date.today().isoformat()}.csv"
    if not folder.exists():
        print(f"Folder not found: {folder}")
        return None
    files = list_pdfs(folder)
    if not files:
        print(f"No PDFs in {folder}")
        return None
    found = 0
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig opens cleanly in Excel
        w = csv.writer(f)
        w.writerow(["file_name", "next_recipient_email"])
        for pdf in files:
            try:
                email = find_recipient(pdf)
            except Exception as e:
                email = ""
                print(f"  FAIL  {pdf.name}: {e}")
            else:
                if email:
                    found += 1
                    print(f"  OK    {pdf.name}: {email}")
                else:
                    print(f"  ----  {pdf.name}: no Attn email found")
            w.writerow([pdf.name, email])
    print(f"Done: {found} of {len(files)} recipient(s) found -> {out_csv}")
    return out_csv


# ---------------------------------------------------------------- UI
def menu():
    ensure_folders()
    while True:
        print("\n=== PDF Stamp ===")
        print(f"  Inbox: {len(list_pdfs(INBOX))} PDF(s)   "
              f"Received: {len(list_pdfs(RECEIVED))}   Approved: {len(list_pdfs(APPROVED))}")
        print("  1) Stamp RECEIVED on all PDFs in 1_inbox")
        print("  2) Approve PDFs in 2_received")
        print("  3) Make recipients CSV (file name + Attn email)")
        print("  4) Open the PDF Stamp folder")
        print("  5) Exit")
        choice = input("Choose 1-5: ").strip()
        if choice == "1":
            d = input("Received date (Enter = today, or yyyy-mm-dd): ").strip()
            try:
                date = dt.date.fromisoformat(d) if d else None
            except ValueError:
                print("Date must look like 2026-10-01. Nothing was stamped.")
                continue
            receive(date)
        elif choice == "2":
            files = list_pdfs(RECEIVED)
            pending = [f for f in files if not (APPROVED / f.name).exists()]
            if not pending:
                print("Nothing waiting for approval.")
                continue
            for i, f in enumerate(pending, 1):
                print(f"  {i}) {f.name}")
            pick = input("Approve which? (numbers like 1,3 or 'all'; Enter = cancel): ").strip()
            if not pick:
                continue
            if pick.lower() == "all":
                chosen = pending
            else:
                try:
                    chosen = [pending[int(x) - 1] for x in pick.replace(" ", "").split(",")]
                except (ValueError, IndexError):
                    print("Didn't understand that selection.")
                    continue
            confirm = input(f"Approve {len(chosen)} file(s) as {getpass.getuser()}? (y/n): ")
            if confirm.strip().lower() == "y":
                approve([str(f) for f in chosen])
        elif choice == "3":
            which = input("Read from (r) 2_received, (i) 1_inbox or (a) 3_approved? [r]: ").strip().lower()
            folder = {"i": INBOX, "a": APPROVED}.get(which, RECEIVED)
            export_recipients(folder)
        elif choice == "4":
            open_folder(BASE)
        elif choice == "5":
            return


def open_folder(path):
    import os, subprocess
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)])
    else:
        subprocess.run(["xdg-open", str(path)])


def main():
    ap = argparse.ArgumentParser(description="Stamp PDFs as RECEIVED and APPROVED.")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("receive", help="stamp every PDF in 1_inbox")
    r.add_argument("--date", help="received date, yyyy-mm-dd (default today)")
    a = sub.add_parser("approve", help="add the seal to PDFs in 2_received")
    a.add_argument("files", nargs="*", help="file names or paths (default: all)")
    a.add_argument("--pages", choices=["last", "first", "all"], default="last")
    c = sub.add_parser("recipients", help="CSV of file name + Attn email")
    c.add_argument("--folder", help="folder to read (default: 2_received)")
    c.add_argument("--out", help="CSV file to write (default: recipients_<date>.csv)")
    args = ap.parse_args()
    if args.cmd == "receive":
        receive(parse_date(args.date) if args.date else None)
    elif args.cmd == "approve":
        approve(args.files, args.pages)
    elif args.cmd == "recipients":
        export_recipients(args.folder, args.out)
    else:
        menu()


if __name__ == "__main__":
    main()