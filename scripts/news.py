"""
Finds dividend news and drops "Just in:" posts into queue/ for post.py to send
as @DividendTools. Runs right before post.py in the same workflow.

Two tiers (edit news/config.json to change the lists):

  Tier 1  Fan favorites. Every payout gets a post on its ex-dividend day, with
          the change from the last payout and what $10,000 earns from it.
          "Goes ex-dividend today" is only used when the post goes out on the
          ex-date. If Yahoo's data lags (or the post slides past 9pm into the
          next morning) it says "ex-dividend date: Oct 9" instead, plus the
          payable date from Nasdaq when available. Lag limit: ex_date_lag_days
          Source: Yahoo Finance dividend history (stocks and ETFs).

  Tier 2  S&P 500 members plus your watchlist. Only real news: cuts,
          suspensions, raises of 5%+, streak milestones, first-ever or
          reinstated dividends, and specials.
          Sources: PR Newswire + GlobeNewswire dividend RSS feeds and the
          Business Wire dividend newsroom.

Safety rules:
  - Every number must be found in the release (or in Yahoo's history) and the
    math must check out, otherwise the item is skipped, never guessed
  - Tier 2 posts are held 20 minutes so you can Unschedule them in x-queue
  - Daily caps, 30 minute spacing from every other queued post, 8am to 9pm ET
  - One $ticker per post, no links, no em dashes, no periods at line ends

Optional secret: ANTHROPIC_API_KEY. With it, a small Claude model reads each
release (more accurate). Without it, the script falls back to pattern matching.
"""
import csv
import html
import io
import json
import os
import re
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

ET_TZ = ZoneInfo("America/New_York")
NOW = datetime.now(timezone.utc)
TODAY_ET = NOW.astimezone(ET_TZ).date()

CONFIG_PATH = "news/config.json"
STATE_PATH = "news/state.json"
SP500_CACHE = "news/sp500.txt"
QUEUE_DIR = "queue"

UA = {"User-Agent": "Mozilla/5.0 (DividendTools news bot; Business@TopDividendETFs.com)"}

FEEDS = [
    ("prnewswire", "https://www.prnewswire.com/rss/financial-services-latest-news/dividends-list.rss"),
    ("globenewswire", "https://www.globenewswire.com/RssFeed/subjectcode/12-Dividend%20Reports%20and%20Estimates/feedTitle/GlobeNewswire%20-%20Dividend%20Reports%20and%20Estimates"),
]
BUSINESSWIRE_PAGE = "https://www.businesswire.com/newsroom/subject/dividend"
SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

TICKER_RE = re.compile(r"\((?:NYSE American|NYSE Arca|NYSE|NASDAQ|Nasdaq|NasdaqGS|NasdaqGM|Cboe BZX|CBOE)\s*:\s*([A-Z]{1,5}(?:\.[A-Z])?)\)")
SKIP_TITLE_RE = re.compile(r"closed[- ]end|preferred|series [a-z] |notes due|depositary|interval fund|municipal|distribution sources|19\(a\)|section 19", re.I)


# ---------------------------------------------------------------- helpers

def log(msg):
    print(f"[news] {msg}")


def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def get(url, timeout=30):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r


def strip_html(text):
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def money(x):
    # $0.27, $0.311, $1.00
    s = f"{x:.4f}".rstrip("0")
    if len(s.split(".")[1]) < 2:
        s = f"{x:.2f}"
    return f"${s}"


def pct(x):
    s = f"{abs(x):.1f}"
    return s[:-2] if s.endswith(".0") else s


def mdy(iso):
    try:
        d = datetime.strptime(iso[:10], "%Y-%m-%d")
        return f"{d.month}/{d.day}"
    except (TypeError, ValueError):
        return None


def nice_date(iso):
    # 2026-10-09 -> Oct 9
    d = datetime.strptime(iso[:10], "%Y-%m-%d")
    return f"{d.strftime('%b')} {d.day}"


def pay_date(sym, ex_iso):
    """Payable date for the payout with this ex-date, from Nasdaq. None if unknown"""
    hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*"}
    want = datetime.strptime(ex_iso, "%Y-%m-%d").strftime("%m/%d/%Y")
    for asset in ("stocks", "etf"):
        try:
            r = requests.get(f"https://api.nasdaq.com/api/quote/{sym.replace('.', '-')}/dividends?assetclass={asset}",
                             headers=hdrs, timeout=20)
            rows = ((((r.json() or {}).get("data") or {}).get("dividends") or {}).get("rows")) or []
        except Exception:
            continue
        for row in rows:
            if row.get("exOrEffDate") == want:
                try:
                    return datetime.strptime(row.get("paymentDate", ""), "%m/%d/%Y").date().isoformat()
                except ValueError:
                    return None
    return None


def clean_tweet(text):
    # House rules: no em or en dashes, no period at the end of any line
    text = text.replace("\u2014", ",").replace("\u2013", "-")
    lines = [ln.rstrip().rstrip(".") for ln in text.split("\n")]
    return "\n".join(lines).strip()


# ---------------------------------------------------------------- lists

def load_sp500():
    try:
        rows = csv.DictReader(io.StringIO(get(SP500_URL).text))
        syms = sorted({r["Symbol"].strip().upper() for r in rows if r.get("Symbol")})
        if len(syms) > 400:
            os.makedirs("news", exist_ok=True)
            with open(SP500_CACHE, "w") as fh:
                fh.write("\n".join(syms))
            return set(syms)
    except Exception as e:
        log(f"S&P 500 list download failed, using cache: {e}")
    try:
        with open(SP500_CACHE) as fh:
            return {ln.strip() for ln in fh if ln.strip()}
    except FileNotFoundError:
        return set()


# ---------------------------------------------------------------- yahoo

def yahoo(sym):
    """Returns (price, [(ex_date_iso, amount), ...] newest first)."""
    ysym = sym.replace(".", "-")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?range=2y&interval=1d&events=div"
    j = get(url).json()
    res = j["chart"]["result"][0]
    price = res.get("meta", {}).get("regularMarketPrice")
    divs = (res.get("events") or {}).get("dividends") or {}
    rows = sorted(
        ((datetime.fromtimestamp(d["date"], ET_TZ).date().isoformat(), float(d["amount"])) for d in divs.values()),
        reverse=True,
    )
    return price, rows


# ---------------------------------------------------------------- queue

def queued_times():
    times = []
    if os.path.isdir(QUEUE_DIR):
        for name in os.listdir(QUEUE_DIR):
            if name.endswith(".json"):
                try:
                    with open(os.path.join(QUEUE_DIR, name)) as fh:
                        times.append(datetime.fromisoformat(json.load(fh)["post_at"].replace("Z", "+00:00")))
                except Exception:
                    pass
    return times


def next_slot(earliest, cfg, taken):
    start_h, end_h = cfg["post_window_et"]
    gap = timedelta(minutes=cfg["min_gap_minutes"])
    t = earliest
    for _ in range(500):
        local = t.astimezone(ET_TZ)
        if local.hour < start_h:
            t = local.replace(hour=start_h, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            continue
        if local.hour >= end_h:
            nxt = (local + timedelta(days=1)).replace(hour=start_h, minute=0, second=0, microsecond=0)
            t = nxt.astimezone(timezone.utc)
            continue
        clash = [x for x in taken if abs(x - t) < gap]
        if not clash:
            return t
        t = max(clash) + gap
    return t


def enqueue(text, sym, kind, post_at, source):
    os.makedirs(QUEUE_DIR, exist_ok=True)
    base = f"{post_at.strftime('%Y%m%d-%H%M')}-news-{sym.lower().replace('.', '')}-{uuid.uuid4().hex[:6]}"
    record = {
        "id": base,
        "sym": sym,
        "kind": "news",
        "news_kind": kind,
        "images": 0,
        "text": text,
        "post_at": post_at.isoformat(),
        "created_at": NOW.isoformat(),
        "source": source,
    }
    save_json(os.path.join(QUEUE_DIR, base + ".json"), record)
    log(f"QUEUED {base} for {post_at.astimezone(ET_TZ).strftime('%a %I:%M %p ET')}:\n{text}\n")


# ---------------------------------------------------------------- tier 1

def tier1(cfg, state, plans):
    last = state.setdefault("tier1_last", {})
    for sym in cfg["tier1"]:
        sym = sym.upper().lstrip("$")
        try:
            price, rows = yahoo(sym)
        except Exception as e:
            log(f"Yahoo failed for {sym}: {e}")
            continue
        if len(rows) < 2:
            continue
        ex, amt = rows[0]
        if last.get(sym) == ex:
            continue
        if ex > TODAY_ET.isoformat():
            continue
        if ex < (TODAY_ET - timedelta(days=cfg.get("ex_date_lag_days", 3))).isoformat():
            # Too old to be news. Remember it so we never post it
            last[sym] = ex
            continue
        prev = rows[1][1]
        if prev <= 0:
            continue
        change = (amt - prev) / prev * 100
        if abs(change) > 80:
            log(f"{sym}: change of {change:.0f}% looks like a split or data error, skipping")
            last[sym] = ex
            continue
        if abs(change) < 0.05:
            vs = "Same as last payout"
        elif change > 0:
            vs = f"Up {pct(change)}% from {money(prev)} last payout 📈"
        else:
            vs = f"Down {pct(change)}% from {money(prev)} last payout 📉"
        pay = pay_date(sym, ex)
        body = [f"{money(amt)}/share"]
        if pay:
            body.append(f"Payable {nice_date(pay)}")
        body.append(vs)
        if price and price > 0:
            earns = 10000 / price * amt
            body.append(f"$10,000 invested earns about ${earns:,.0f} from this payout" if earns >= 10 else f"$10,000 invested earns about ${earns:,.2f} from this payout")
        # "today" wording is only used if the post actually goes out on the ex-date (decided in main)
        today_text = clean_tweet("\n".join(["Just in:", "", f"${sym} goes ex-dividend today 💰", ""] + body))
        dated_text = clean_tweet("\n".join(["Just in:", "", f"${sym} ex-dividend date: {nice_date(ex)} 💰", ""] + body))
        plans.append({"tier": 1, "sym": sym, "kind": "ex_date", "text": dated_text, "today_text": today_text,
                      "ex_date": ex, "priority": 5, "source": "yahoo", "mark": ("tier1_last", sym, ex)})


# ---------------------------------------------------------------- tier 2 sources

def wire_items():
    items = []
    for name, url in FEEDS:
        try:
            root = ET.fromstring(get(url).content)
        except Exception as e:
            log(f"{name} feed failed: {e}")
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            desc = strip_html(it.findtext("description") or "")
            pub = it.findtext("pubDate")
            try:
                when = datetime.strptime(pub.strip(), "%a, %d %b %Y %H:%M:%S %z") if pub else None
            except ValueError:
                try:
                    when = datetime.strptime(pub.strip(), "%a, %d %b %Y %H:%M %Z").replace(tzinfo=timezone.utc)
                except ValueError:
                    when = None
            cats = " ".join((c.text or "") for c in it.findall("category"))
            items.append({"source": name, "title": title, "link": link, "summary": desc + " " + cats, "when": when})
    try:
        page = get(BUSINESSWIRE_PAGE).text
        for m in re.finditer(r'<a[^>]+href="(/news/home/(\d{8})\d*/en/[^"?#]+)"[^>]*>([\s\S]{0,400}?)</a>', page):
            path, ymd, inner = m.groups()
            title = strip_html(inner)
            if not title:
                continue
            when = datetime.strptime(ymd, "%Y%m%d").replace(tzinfo=ET_TZ)
            items.append({"source": "businesswire", "title": title, "link": "https://www.businesswire.com" + path,
                          "summary": "", "when": when})
    except Exception as e:
        log(f"Business Wire page failed: {e}")
    # de-dupe by link
    seen, out = set(), []
    for it in items:
        if it["link"] and it["link"] not in seen:
            seen.add(it["link"])
            out.append(it)
    return out


def release_text(link):
    try:
        return strip_html(get(link).text)[:12000]
    except Exception as e:
        log(f"Could not open release {link}: {e}")
        return ""


EXTRACT_PROMPT = """You read dividend press releases and return facts as JSON. Use ONLY what the text states. Never guess.

Return a JSON object with these keys:
"ticker": the common stock ticker of the company declaring (string, uppercase) or null
"security": "common", "preferred", "fund", or "other" (what the dividend is paid on)
"kind": "regular", "special", "initial" (first-ever dividend), "reinstated", or "suspended"
"amount": the per-share dividend amount on the common stock as a number, or null
"frequency": "weekly", "monthly", "quarterly", "semiannual", "annual", or null
"prior_amount": the previous per-share amount if the text states it, else null
"pct_change": the percent change if the text states it, else null
"streak_years": consecutive years of dividend increases if the text states it, else null
"ex_date": ex-dividend date as YYYY-MM-DD if stated, else null
"pay_date": payment date as YYYY-MM-DD if stated, else null
If one release covers several funds or securities, return {"ticker": null}.
Reply with the JSON object only."""


def extract_claude(text, key):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": CLAUDE_MODEL, "max_tokens": 400, "system": EXTRACT_PROMPT,
              "messages": [{"role": "user", "content": text[:9000]}]},
        timeout=60,
    )
    r.raise_for_status()
    out = "".join(b.get("text", "") for b in r.json().get("content", []))
    out = re.sub(r"^```(?:json)?|```$", "", out.strip()).strip()
    return json.loads(out)


AMOUNT_RE = re.compile(r"\$\s?(\d+\.\d{2,4})\s*(?:per share|a share|per common share|per unit)", re.I)


def extract_regex(title, text, ticker):
    blob = f"{title}. {text}"
    low = blob.lower()
    amounts = AMOUNT_RE.findall(blob)
    info = {"ticker": ticker, "security": "common", "kind": "regular", "amount": float(amounts[0]) if amounts else None,
            "frequency": None, "prior_amount": None, "pct_change": None, "streak_years": None, "ex_date": None, "pay_date": None}
    for f in ("monthly", "quarterly", "semiannual", "annual"):
        if f in low[:800]:
            info["frequency"] = f
            break
    if "special" in low[:400]:
        info["kind"] = "special"
    if re.search(r"suspend\w*\s+(?:the\s+|its\s+)?(?:quarterly\s+|common\s+)?dividend", low):
        info["kind"] = "suspended"
    if re.search(r"(?:first|initial|inaugural)\s+(?:ever\s+)?(?:quarterly\s+)?(?:cash\s+)?dividend", low[:1200]):
        info["kind"] = "initial"
    m = re.search(r"(\d{2})(?:th|st|nd|rd)\s+consecutive\s+year", low)
    if m:
        info["streak_years"] = int(m.group(1))
    return info


# ---------------------------------------------------------------- tier 2 rules

def tier2(cfg, state, plans, sp500):
    eligible = sp500 | {s.upper() for s in cfg["watchlist"]} | {s.upper() for s in cfg["tier1"]}
    seen = state.setdefault("seen", {})
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    claude_calls = 0
    for it in wire_items():
        if it["link"] in seen:
            continue
        seen[it["link"]] = TODAY_ET.isoformat()
        if it["when"] and NOW - it["when"].astimezone(timezone.utc) > timedelta(hours=24):
            continue
        if SKIP_TITLE_RE.search(it["title"]):
            continue
        m = TICKER_RE.search(it["title"] + " " + it["summary"])
        body = ""
        if not m:
            body = release_text(it["link"])
            m = TICKER_RE.search(body[:3000])
        if not m:
            continue
        sym = m.group(1).upper()
        if sym not in eligible:
            continue
        body = body or release_text(it["link"])
        if not body:
            continue
        info = None
        if key and claude_calls < cfg["max_claude_calls_per_run"]:
            try:
                claude_calls += 1
                info = extract_claude(f"HEADLINE: {it['title']}\n\n{body}", key)
            except Exception as e:
                log(f"Claude read failed, falling back to patterns: {e}")
        if info is None:
            info = extract_regex(it["title"], body, sym)
        if (info.get("ticker") or "").upper() != sym or info.get("security") not in ("common", "fund"):
            continue
        plan = judge(sym, info, body, it, cfg)
        if plan:
            plans.append(plan)


def appears(amount, text):
    if amount is None:
        return False
    variants = {f"{amount:.2f}", f"{amount:.3f}", f"{amount:.4f}", f"{amount:g}"}
    return any(("$" + v) in text or ("$ " + v) in text for v in variants)


def judge(sym, info, body, it, cfg):
    kind = info.get("kind") or "regular"
    amt = info.get("amount")
    freq = info.get("frequency")
    freq_word = f"{freq} " if freq in ("monthly", "quarterly", "semiannual", "annual") else ""
    ex_line = f"Ex-date: {mdy(info['ex_date'])}" if mdy(info.get("ex_date")) else None
    text_blob = it["title"] + " " + body
    base = {"tier": 2, "sym": sym, "source": it["link"]}

    if kind == "suspended":
        if not re.search(r"suspen", text_blob, re.I):
            return None
        return {**base, "kind": "suspended", "priority": 1,
                "text": clean_tweet(f"Just in:\n\n${sym} suspends its dividend 🚨")}

    if amt is None or not appears(float(amt), text_blob):
        log(f"{sym}: amount not found in the release, skipping")
        return None
    amt = float(amt)

    if kind == "special":
        lines = ["Just in:", "", f"${sym} declares a special dividend of {money(amt)}/share 💵"]
        if ex_line:
            lines += ["", ex_line]
        return {**base, "kind": "special", "priority": 3, "text": clean_tweet("\n".join(lines))}

    if kind in ("initial", "reinstated"):
        verb = "declares its first-ever dividend" if kind == "initial" else "reinstates its dividend"
        lines = ["Just in:", "", f"${sym} {verb} 🎉", "", f"{money(amt)}/share {freq.strip() if freq else ''}".strip()]
        if ex_line:
            lines.append(ex_line)
        return {**base, "kind": kind, "priority": 2, "text": clean_tweet("\n".join(lines))}

    # Regular dividend: compare with the previous payout
    prior = info.get("prior_amount")
    prior_from = "release"
    if prior is not None:
        prior = float(prior)
        if not appears(prior, text_blob):
            prior = None
    if prior is None:
        try:
            _, rows = yahoo(sym)
            past = [a for d, a in rows if d < TODAY_ET.isoformat()]
            prior = past[0] if past else None
            prior_from = "yahoo"
        except Exception as e:
            log(f"{sym}: Yahoo history failed: {e}")
    if not prior or prior <= 0:
        return None
    ratio = amt / prior
    if ratio < 0.2 or ratio > 5:
        log(f"{sym}: {money(amt)} vs {money(prior)} looks like a split or different frequency, skipping")
        return None
    change = (ratio - 1) * 100
    stated = info.get("pct_change")
    if stated is not None and prior_from == "release" and abs(abs(float(stated)) - abs(change)) > 1.0:
        log(f"{sym}: stated change {stated}% does not match the math ({change:.1f}%), skipping")
        return None

    streak = info.get("streak_years")
    streak = int(streak) if streak and re.search(rf"\b{int(streak)}(?:th|st|nd|rd)?\b", text_blob) else None
    milestone = streak in set(cfg["streak_milestones"])

    if change <= -0.5:
        lines = ["Just in:", "", f"${sym} cuts its {freq_word}dividend {pct(change)}% to {money(amt)}/share 📉",
                 "", f"Previous payout: {money(prior)}/share"]
        if ex_line:
            lines.append(ex_line)
        return {**base, "kind": "cut", "priority": 1, "text": clean_tweet("\n".join(lines))}

    if change >= cfg["min_raise_pct"] or (change > 0.1 and milestone):
        if milestone:
            emoji = "👑" if streak >= 50 else "🏆"
            lines = ["Just in:", "", f"${sym} raises its dividend for the {streak}th straight year {emoji}",
                     "", f"New {freq_word}payout: {money(amt)}/share, up {pct(change)}%"]
            if streak == 50:
                lines.append("Officially a Dividend King")
        else:
            lines = ["Just in:", "", f"${sym} raises its {freq_word}dividend {pct(change)}% to {money(amt)}/share 📈"]
            if streak:
                lines += ["", f"{streak} straight years of increases"]
        if ex_line:
            lines.append(ex_line)
        return {**base, "kind": "raise", "priority": 2 if milestone else 3, "text": clean_tweet("\n".join(lines))}

    return None  # routine, not news


# ---------------------------------------------------------------- main

def main():
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit("news/config.json is missing")
    if not cfg.get("enabled", True):
        log("News posts are turned off in news/config.json")
        return
    state = load_json(STATE_PATH, {})
    day = TODAY_ET.isoformat()
    counts = state.setdefault("counts", {})
    for d in list(counts):
        if d < (TODAY_ET - timedelta(days=7)).isoformat():
            del counts[d]
    today = counts.setdefault(day, {"tier1": 0, "tier2": 0})
    seen = state.setdefault("seen", {})
    for link, d in list(seen.items()):
        if d < (TODAY_ET - timedelta(days=7)).isoformat():
            del seen[link]
    first_run = not state.get("initialized")

    plans = []
    try:
        tier1(cfg, state, plans)
    except Exception as e:
        log(f"Tier 1 failed: {e}")
    try:
        tier2(cfg, state, plans, load_sp500())
    except Exception as e:
        log(f"Tier 2 failed: {e}")

    if first_run:
        # First run only learns what is already out there so it doesn't post a backlog
        state["initialized"] = True
        for p in plans:
            if p.get("mark"):
                field, sym, val = p["mark"]
                state.setdefault(field, {})[sym] = val
        save_json(STATE_PATH, state)
        log(f"First run: learned {len(plans)} current items, nothing posted. Posting starts next run")
        return

    taken = queued_times()
    plans.sort(key=lambda p: (p["tier"] != 1, p["priority"]))  # ex-date posts first, then news by importance
    for p in plans:
        tier_key = f"tier{p['tier']}"
        cap = cfg["daily_cap_tier1"] if p["tier"] == 1 else cfg["daily_cap_tier2"]
        if today[tier_key] >= cap:
            log(f"Daily cap reached for {tier_key}, skipping {p['sym']}")
        else:
            hold = timedelta(minutes=cfg["review_hold_minutes"] if p["tier"] == 2 else 0)
            slot = next_slot(NOW + hold, cfg, taken)
            text = p["text"]
            if p.get("ex_date") and slot.astimezone(ET_TZ).date().isoformat() == p["ex_date"]:
                text = p["today_text"]
            enqueue(text, p["sym"], p["kind"], slot, p["source"])
            taken.append(slot)
            today[tier_key] += 1
        if p.get("mark"):
            field, sym, val = p["mark"]
            state.setdefault(field, {})[sym] = val

    save_json(STATE_PATH, state)
    log(f"Done. {len(plans)} newsworthy item(s) this run")


if __name__ == "__main__":
    main()
