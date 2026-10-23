#!/usr/bin/env python3
"""Chrome Hearts monitor: watches chromehearts.com and sends an email when

  * the homepage changes (that is where drops are announced),
  * a hidden section such as /hoodie or /hat goes live (today they all
    redirect to the homepage),
  * a new product appears anywhere we look, or a sold-out one comes back.

Only the Python standard library is used (works with macOS /usr/bin/python3).

Usage:
    python3 monitor.py              # one check (what launchd runs)
    python3 monitor.py --test-email # send a test email to verify SMTP settings
    python3 monitor.py --list       # print what is visible right now, no email
"""

import argparse
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

BASE = "https://www.chromehearts.com"
GRID = BASE + "/on/demandware.store/Sites-ChromeHearts-Site/en_US/Search-UpdateGrid"
SITEMAP = BASE + "/sitemap_0.xml"
PAGE_SIZE = 12  # the site ignores larger sz values
MAX_PAGES = 40
MAX_LINKED_PAGES = 30

# Sections that exist on the site but are switched off: each one currently
# redirects to the homepage (unknown paths return 404 instead). When one stops
# redirecting, something went live there. Found by probing on 2026-10-23.
# Add your own (category or product paths) with WATCH_PATHS in .env.
HIDDEN_PATHS = [
    "hoodie", "hoodies", "sweatshirt", "sweatshirts", "sweater", "t-shirt",
    "shirt", "shirts", "jacket", "denim", "pants", "shorts", "hat", "beanie",
    "scarf", "gloves", "bandana", "belt", "bag", "bags", "eyewear",
    "sunglasses", "shoes", "boots", "slides", "blanket", "home", "mens",
    "womens", "kids", "foti", "ppo",
    "on/demandware.store/Sites-ChromeHearts-Site/en_US/Search-Show?cgid=SWEATPANTS",
    "hoodie/1988-hoodie/152701BLKXXX762.html",  # the 2025 online-only hoodie
]

# Links on every page that never lead to products.
IGNORE_LINKS = {"/", "/cart", "/login", "/contact", "/locations.html", "/magazine.html",
                "/terms.html", "/disclosure.html", "/privacy.html", "/general.html"}

PRODUCT_PATH = re.compile(r"^/[\w-]+/[\w-]+/([0-9A-Z]{15})\.html$")

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
ENV_FILE = os.path.join(HERE, ".env")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


DEFAULTS = {
    "SMTP_HOST": "smtp.mail.me.com",
    "SMTP_PORT": "587",
    "NOTIFY_RESTOCK": "1",
}
ENV_KEYS = list(DEFAULTS) + ["SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO",
                             "WATCH_PATHS"]


def load_env(path=ENV_FILE):
    """Settings from defaults, then .env (local runs), then environment
    variables (GitHub Actions secrets). Empty values are ignored. The email
    address is never stored in the code: the repo is public."""
    env = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.strip().strip('"').strip("'")
                if value:
                    env[key.strip()] = value
    env.update({k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)})
    if env.get("SMTP_USER"):
        env.setdefault("EMAIL_FROM", env["SMTP_USER"])
        env.setdefault("EMAIL_TO", env["SMTP_USER"])
    return env


def require_password(env):
    if not env.get("SMTP_USER"):
        sys.exit("SMTP_USER is not set (GitHub secret or .env): your iCloud email address.")
    if not env.get("SMTP_PASSWORD"):
        sys.exit("SMTP_PASSWORD is not set (GitHub secret or .env). "
                 "Create an app-specific password at appleid.apple.com.")


# --------------------------------------------------------------------------- fetching

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_no_redirect_opener = urllib.request.build_opener(_NoRedirect)


def fetch(url, follow=True, retries=2):
    """Returns (status, final_url, location, body). 3xx/4xx are returned, not raised."""
    req = urllib.request.Request(url, headers=HEADERS)
    opener = urllib.request.build_opener() if follow else _no_redirect_opener
    for attempt in range(retries + 1):
        try:
            with opener.open(req, timeout=30) as resp:
                return resp.status, resp.geturl(), "", resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code < 500:
                location = urllib.parse.urljoin(url, e.headers.get("Location", ""))
                return e.code, url, location, ""
            err = e
        except (urllib.error.URLError, OSError) as e:
            err = e
        if attempt == retries:
            raise err
        log("fetch failed (%s), retrying: %s" % (err, url))
        time.sleep(5 * (attempt + 1))


def is_home(url):
    return urllib.parse.urlparse(url).path in ("", "/")


def path_of(url):
    p = urllib.parse.urlparse(urllib.parse.urljoin(BASE, url))
    return p.path + ("?" + p.query if p.query else "")


# --------------------------------------------------------------------------- parsing

TILE_SPLIT = re.compile(r'<div class="product productType-\w+" data-pid="')


def attr(chunk, name):
    m = re.search(r'%s="([^"]*)"' % name, chunk)
    return html.unescape(m.group(1)).strip() if m else ""


def parse_tiles(page):
    """Products from a product grid (category page or grid fragment)."""
    products = {}
    for chunk in TILE_SPLIT.split(page)[1:]:
        pid = chunk.split('"', 1)[0]
        meta_end = chunk.find("</span>")
        meta = chunk[:meta_end] if meta_end > 0 else chunk
        href = re.search(r'href="(/[^"]+\.html)"', chunk)
        img = re.search(r'class="tile-image"[^>]*?\ssrc="([^"]+)"', chunk, re.S)
        products[pid] = {
            "pid": pid,
            "name": attr(meta, "data-name"),
            "price": attr(meta, "data-price"),
            "category": attr(meta, "data-category"),
            "url": BASE + html.unescape(href.group(1)) if href else BASE,
            "image": html.unescape(img.group(1)) if img else "",
            "soldout": bool(re.search(r'class="[^"]*\bsold-?out\b', chunk, re.I)),
        }
    return products


def parse_pdp(url, page):
    """A single product page, via its schema.org JSON-LD block."""
    m = re.search(r'<script type="application/ld\+json">\s*(\{.*?"@type"\s*:\s*"Product".*?\})\s*</script>',
                  page, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return {}
    pid = data.get("sku") or data.get("mpn")
    if not pid:
        return {}
    offers = data.get("offers") or {}
    img = re.search(r'(https://www\.chromehearts\.com/dw/image/[^"\s,]+img_products[^"\s,]+)', page)
    category = path_of(url).strip("/").split("/")[0]
    return {pid: {
        "pid": pid,
        "name": html.unescape(data.get("name") or ""),
        "price": str(offers.get("price") or ""),
        "category": category,
        "url": BASE + path_of(url),
        "image": html.unescape(img.group(1)) if img else "",
        "soldout": "InStock" not in str(offers.get("availability", "")),
    }}


def parse_page(url, page):
    products = parse_tiles(page)
    if PRODUCT_PATH.match(urllib.parse.urlparse(url).path):
        products.update(parse_pdp(url, page))
    return products


def homepage_featured(page):
    """Drop items on the homepage. They are server-rendered as hidden
    .js-homepage-product-wrapper blocks that homePage.js fades in and makes
    clickable (it navigates to the block's first link)."""
    items = []
    chunks = re.split(r'<[^>]+class="[^"]*\bjs-homepage-product-wrapper\b', page)[1:]
    for chunk in chunks:
        chunk = chunk[:6000]
        href = re.search(r'<a\b[^>]*href="([^"]+)"', chunk)
        img = re.search(r'<img\b[^>]*?\s(?:src|data-src)="([^"]+)"', chunk, re.S)
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", chunk[:3000]))).strip()
        items.append({
            "url": BASE + path_of(html.unescape(href.group(1))) if href else BASE + "/",
            "image": urllib.parse.urljoin(BASE, html.unescape(img.group(1))) if img else "",
            "text": text[:200],
        })
    return items


def homepage_snapshot(page):
    """What a visitor sees on the homepage, normalized so that only real content
    changes alter the hash (site deploys rotate asset version tokens)."""
    start = page.find('id="maincontent"')
    start = page.find(">", start) + 1 if start >= 0 else 0
    end = page.find("<footer", start)
    main = page[start:end if end >= 0 else len(page)]
    main = re.sub(r"<script.*?</script>", "", main, flags=re.S)
    main = re.sub(r"/v\d{8,}/", "/v/", main)
    main = re.sub(r"/dw[0-9a-f]{8}/", "/dw/", main)
    main = re.sub(r"\s+", " ", main)

    links = set()
    for href in re.findall(r'href="([^"]+)"', page):
        href = html.unescape(href)
        if href.startswith(("#", "javascript:", "mailto:")) or "demandware.static" in href:
            continue
        absolute = urllib.parse.urljoin(BASE + "/", href)
        if urllib.parse.urlparse(absolute).netloc.endswith("chromehearts.com"):
            links.add(path_of(absolute))

    images = sorted(set(html.unescape(u) for u in
                        re.findall(r'(?:src|srcset|data-src|poster)="([^"\s]+)', main)))
    text = re.sub(r"<[^>]+>", " ", re.sub(r"<style.*?</style>", "", main, flags=re.S))
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    video = re.search(r'id="videoRedirection" data-on="([^"]*)"[^>]*data-url="([^"]*)"', page)
    return {
        "hash": hashlib.sha256((main + "|" + "|".join(sorted(links))).encode()).hexdigest(),
        "featured": homepage_featured(page),
        "links": sorted(links),
        "images": images,
        "text": text[:500],
        "video_redirect": video.group(2) if video and video.group(1) == "true" else "",
    }


# --------------------------------------------------------------------------- scanning

def fetch_grid(cgid):
    products = {}
    for page_no in range(MAX_PAGES):
        url = "%s?cgid=%s&start=%d&sz=%d" % (GRID, cgid, page_no * PAGE_SIZE, PAGE_SIZE)
        _, _, _, body = fetch(url)
        found = parse_tiles(body)
        new = {k: v for k, v in found.items() if k not in products}
        products.update(new)
        if len(found) < PAGE_SIZE or not new:
            break
    return products


def sitemap_paths():
    try:
        _, _, _, xml = fetch(SITEMAP)
    except Exception as e:
        log("sitemap unavailable: %s" % e)
        return []
    return [path_of(html.unescape(loc)) for loc in re.findall(r"<loc>([^<]+)</loc>", xml)
            if "Default-Start" not in loc]


def check_path(path):
    """'closed' if the path redirects to the homepage or 404s, 'open' if it
    shows real content, None if the site errored (state then stays as it was).
    Returns (status, final_url, body)."""
    url = urllib.parse.urljoin(BASE + "/", path)
    try:
        code, _, location, body = fetch(url, follow=False)
        if code in (301, 302, 303, 307, 308):
            if is_home(location):
                return "closed", location, ""
            code, final, _, body = fetch(location)  # e.g. /foti -> /hoodie -> /
            if code == 200 and is_home(final):
                return "closed", final, ""
            url = final
        if code == 404:
            return "closed", url, ""
        if code == 200:
            return "open", url, body
    except Exception as e:
        log("watch %s failed: %s" % (path, e))
    return None, url, ""


def scan(watch_paths):
    """Returns (products, homepage snapshot or None, {path: 'open'|'closed'})."""
    products = fetch_grid("root")
    log("catalog: %d products" % len(products))

    homepage = None
    to_visit = set(sitemap_paths())
    try:
        code, final, _, body = fetch(BASE + "/")
        if code == 200:
            homepage = homepage_snapshot(body)
            products.update({k: v for k, v in parse_tiles(body).items() if k not in products})
            to_visit |= {l for l in homepage["links"] if l not in IGNORE_LINKS}
            to_visit |= {path_of(f["url"]) for f in homepage["featured"]}
            if homepage["video_redirect"]:
                to_visit.add(path_of(homepage["video_redirect"]))
    except Exception as e:
        log("homepage failed: %s" % e)

    paths = {}
    for path in watch_paths:
        status, final, body = check_path(path)
        if status:
            paths[path] = status
        if status == "open":
            for k, v in parse_page(final, body).items():
                products.setdefault(k, v)

    featured = [path_of(f["url"]) for f in homepage["featured"]] if homepage else []
    for path in (featured + sorted(to_visit - set(featured)))[:MAX_LINKED_PAGES]:
        if path in ("/", "") or path.strip("/") in paths:
            continue
        try:
            code, final, _, body = fetch(urllib.parse.urljoin(BASE, path))
        except Exception as e:
            log("page failed %s: %s" % (path, e))
            continue
        if code == 200 and not is_home(final):
            for k, v in parse_page(final, body).items():
                products.setdefault(k, v)

    opened = [p for p, s in paths.items() if s == "open"]
    log("total: %d products, homepage %s, %d/%d hidden sections open%s"
        % (len(products), "ok" if homepage else "FAILED", len(opened), len(watch_paths),
           (": " + ", ".join(opened)) if opened else ""))
    return products, homepage, paths


# --------------------------------------------------------------------------- state

def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    # No timestamp: the file only changes when the site does, so GitHub Actions
    # commits it only then.
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def diff_products(old, current):
    new_items, restocked = [], []
    for pid, p in current.items():
        prev = old.get(pid)
        if prev is None:
            new_items.append(p)
        elif (prev.get("soldout") or prev.get("hidden")) and not p["soldout"]:
            restocked.append(p)
    return new_items, restocked


def homepage_alerts(old, new, products):
    """Specific alert per new drop item on the homepage, plus a generic one for
    any other change, so an unexpected layout still gets reported."""
    alerts = []
    old_urls = {f["url"] for f in old.get("featured", [])}
    by_url = {p["url"]: p for p in products.values()}
    for f in new.get("featured", []):
        if f["url"] in old_urls:
            continue
        p = by_url.get(f["url"].split("?")[0], {})
        lines = [x for x in (p.get("name"), p.get("price"),
                             "SOLD OUT" if p.get("soldout") else "") if x]
        if not lines and f["text"]:
            lines = [f["text"]]
        alerts.append({"title": "ДРОП на главной: %s" % (p.get("name") or "новый товар"),
                       "url": f["url"], "lines": lines,
                       "images": [i for i in (f["image"] or p.get("image"),) if i]})
    if not alerts:
        alerts.append(homepage_change_alert(old, new))
    return alerts


def homepage_change_alert(old, new):
    added = [l for l in new["links"] if l not in old.get("links", [])]
    removed = [l for l in old.get("links", []) if l not in new["links"]]
    images = [i for i in new["images"] if i not in old.get("images", [])]
    lines = ["Содержимое главной страницы изменилось."]
    if new["video_redirect"] and new["video_redirect"] != old.get("video_redirect"):
        lines.append("Главная теперь перебрасывает на: " + BASE + path_of(new["video_redirect"]))
    if new["text"] and new["text"] != old.get("text"):
        lines.append("Текст на главной: " + new["text"])
    return {"title": "Главная страница изменилась", "url": BASE + "/", "lines": lines,
            "links": [BASE + l for l in added], "removed": [BASE + l for l in removed],
            "images": images[:6]}


# --------------------------------------------------------------------------- email

def _e(s):
    return html.escape(str(s))


def product_html(p, label):
    img = ('<img src="%s" width="160" style="display:block;border:0" alt="">' % _e(p["image"])
           if p["image"] else "")
    return """
<tr><td style="padding:12px 0;border-bottom:1px solid #ddd">
  <table cellpadding="0" cellspacing="0"><tr>
    <td style="padding-right:16px;vertical-align:top">%s</td>
    <td style="vertical-align:top;font-family:Helvetica,Arial,sans-serif">
      <div style="font-size:11px;letter-spacing:1px;color:#888">%s · %s</div>
      <div style="font-size:16px;font-weight:bold;margin:4px 0">%s</div>
      <div style="font-size:15px;margin-bottom:10px">%s%s</div>
      <a href="%s" style="background:#000;color:#fff;padding:8px 14px;text-decoration:none;font-size:13px">Open on chromehearts.com</a>
    </td>
  </tr></table>
</td></tr>""" % (img, _e(label), _e(p["category"] or "—"), _e(p["name"]), _e(p["price"]),
                 " · SOLD OUT" if p["soldout"] else "", _e(p["url"]))


def alert_html(a):
    font = "font-family:Helvetica,Arial,sans-serif"
    parts = ['<div style="font-size:16px;font-weight:bold;margin-bottom:6px">%s</div>' % _e(a["title"])]
    parts += ['<div style="font-size:14px;margin:4px 0">%s</div>' % _e(l) for l in a.get("lines", [])]
    if a.get("links"):
        parts.append('<div style="font-size:13px;margin-top:8px">Новые ссылки:</div>')
        parts += ['<div style="font-size:13px"><a href="%s">%s</a></div>' % (_e(l), _e(l))
                  for l in a["links"]]
    if a.get("removed"):
        parts.append('<div style="font-size:12px;color:#888;margin-top:6px">Исчезли: %s</div>'
                     % _e(", ".join(a["removed"])))
    parts += ['<img src="%s" width="280" style="display:block;border:0;margin-top:8px" alt="">'
              % _e(i) for i in a.get("images", []) if i.startswith("http")]
    parts.append('<div style="margin-top:12px"><a href="%s" style="background:#000;color:#fff;'
                 'padding:8px 14px;text-decoration:none;font-size:13px">Открыть</a></div>' % _e(a["url"]))
    return '<tr><td style="padding:12px 0;border-bottom:1px solid #ddd;%s">%s</td></tr>' % (
        font, "".join(parts))


def build_email(alerts, new_items, restocked):
    rows = [alert_html(a) for a in alerts]
    rows += [product_html(p, "NEW") for p in new_items]
    rows += [product_html(p, "BACK IN STOCK") for p in restocked]

    parts = [a["title"] for a in alerts]
    if new_items:
        parts.append("%d new: %s" % (len(new_items), new_items[0]["name"]))
    if restocked:
        parts.append("%d back in stock" % len(restocked))
    subject = "Chrome Hearts: " + " · ".join(parts)

    body_html = ('<html><body><table cellpadding="0" cellspacing="0" width="100%%">%s</table>'
                 '<p style="font-family:Helvetica,Arial,sans-serif;font-size:11px;color:#888">'
                 'Checked at %s</p></body></html>'
                 % ("".join(rows), datetime.now().strftime("%Y-%m-%d %H:%M")))
    text_parts = []
    for a in alerts:
        text_parts.append("\n".join([a["title"]] + a.get("lines", []) + a.get("links", []) + [a["url"]]))
    for label, items in (("NEW", new_items), ("RESTOCK", restocked)):
        text_parts += ["%s: %s %s\n%s" % (label, p["name"], p["price"], p["url"]) for p in items]
    return subject, "\n\n".join(text_parts), body_html


def send_email(env, subject, text, body_html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Chrome Hearts Monitor", env["EMAIL_FROM"]))
    msg["To"] = env["EMAIL_TO"]
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    host = env.get("SMTP_HOST", "smtp.mail.me.com")
    port = int(env.get("SMTP_PORT", "587"))
    ctx = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls(context=ctx)
    try:
        server.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
        server.sendmail(env["EMAIL_FROM"],
                        [a.strip() for a in env["EMAIL_TO"].split(",")],
                        msg.as_string())
    finally:
        server.quit()
    log("email sent: %s" % subject)


# --------------------------------------------------------------------------- main

def watch_paths_from(env):
    extra = [p.strip().strip("/") for p in env.get("WATCH_PATHS", "").split(",") if p.strip()]
    return HIDDEN_PATHS + [p for p in extra if p not in HIDDEN_PATHS]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test-email", action="store_true", help="send a test email and exit")
    ap.add_argument("--list", action="store_true", help="print what is visible now and exit")
    args = ap.parse_args()

    if args.list:
        env = load_env()
        products, homepage, paths = scan(watch_paths_from(env))
        for p in sorted(products.values(), key=lambda p: (p["category"], p["name"])):
            print("%-18s %-40s %-10s %s%s" % (p["category"], p["name"], p["price"], p["url"],
                                               "  [SOLD OUT]" if p["soldout"] else ""))
        if homepage:
            print("\nHomepage links:", " ".join(homepage["links"]))
            print("Homepage text:", homepage["text"] or "(none)")
        print("Hidden sections:", ", ".join("%s=%s" % kv for kv in sorted(paths.items())))
        return

    env = load_env()
    require_password(env)

    if args.test_email:
        alert = {"title": "Тестовое письмо: монитор работает", "url": BASE + "/",
                 "lines": ["Если вы читаете это, почта настроена правильно.",
                           "Следующие письма придут, когда на сайте что-то появится."]}
        send_email(env, *build_email([alert], [], []))
        return

    products, homepage, paths = scan(watch_paths_from(env))
    if not products and not homepage:
        # Site down, blocked or layout changed: keep the old state untouched.
        log("nothing parsed — skipping this run")
        return

    state = load_state() or {}
    first_run = not state
    old_products = state.get("products", {})
    old_home = state.get("homepage")
    old_paths = state.get("paths", {})

    alerts = []
    if homepage and old_home and homepage["hash"] != old_home["hash"]:
        alerts += homepage_alerts(old_home, homepage, products)
    for path, status in paths.items():
        if status == "open" and old_paths.get(path) == "closed":
            alerts.append({"title": "Открылся раздел /%s" % path.split("?")[0],
                           "url": urllib.parse.urljoin(BASE + "/", path),
                           "lines": ["Раздел раньше перенаправлял на главную, теперь на нём есть контент."]})

    new_items, restocked = ([], []) if not old_products else diff_products(old_products, products)
    alerted = {a["url"] for a in alerts}
    new_items = [p for p in new_items if p["url"] not in alerted]  # already shown as a drop
    if env.get("NOTIFY_RESTOCK", "1") != "1":
        restocked = []

    # Keep products that vanished from the listing (Chrome Hearts often hides
    # sold-out items) so a reappearance is reported as a restock, not as new.
    merged = {pid: dict(p, hidden=True) for pid, p in old_products.items()}
    merged.update(products)
    new_state = {
        "products": merged if products else old_products,
        "homepage": homepage or old_home,
        "paths": dict(old_paths, **paths),
    }

    if first_run:
        log("first run: saved baseline, no email sent")
    elif alerts or new_items or restocked:
        for a in alerts:
            log("ALERT: %s %s" % (a["title"], a["url"]))
        for p in new_items:
            log("NEW: %s %s %s" % (p["name"], p["price"], p["url"]))
        for p in restocked:
            log("RESTOCK: %s %s %s" % (p["name"], p["price"], p["url"]))
        send_email(env, *build_email(alerts, new_items, restocked))  # raises -> state not saved, retried next run
    else:
        log("no changes")
    save_state(new_state)


if __name__ == "__main__":
    main()
