#!/usr/bin/env python3
"""The mount families of warcraftmounts.com, for contact_sheet.py --families.

    python3 mount_families.py --out families.tsv [--page gallery.html]

Reads the site's gallery page (one request; --page takes a saved copy instead) and writes one
mount per row, tab-separated: family, the site's mount id, name, thumbnail file name. The names
are retail's, so they match the census by name.
"""
import argparse
import html
import re
import urllib.request

GALLERY = "https://www.warcraftmounts.com/gallery.php"
ENTRY = re.compile(r"<h5[^>]*>(.*?)</h5>|mount\.php\?mountid=(\d+)' class='thumbimagelink'>"
                   r"<img class='thumbimage' src='/images/thumbs/([^']+)' alt='([^']*)'", re.S)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--page")
    args = ap.parse_args()
    if args.page:
        text = open(args.page, encoding="utf-8", errors="replace").read()
    else:
        req = urllib.request.Request(GALLERY, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            text = r.read().decode("utf-8", errors="replace")
    family, rows, seen = None, [], set()
    for m in ENTRY.finditer(text, max(text.find("Mounts by Family"), 0)):
        if m.group(1) is not None:
            family = html.unescape(re.sub("<[^>]+>", "", m.group(1))).strip()
        elif m.group(2) not in seen:
            seen.add(m.group(2))
            rows.append((family, m.group(2), html.unescape(m.group(4)), m.group(3)))
    if not rows:
        raise SystemExit("no mounts found: the gallery page's markup has changed")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("family\tsite_id\tname\tthumb\n")
        for row in rows:
            f.write("\t".join(row) + "\n")
    print("%d mounts in %d families" % (len(rows), len({r[0] for r in rows})))


if __name__ == "__main__":
    main()
