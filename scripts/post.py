"""
Posts queued DividendProjection tweets to X.

Runs every 15 minutes from GitHub Actions. For each file in queue/ whose
post_at time has passed, it uploads the image, creates the post, and moves
the record to posted/ (or failed/ after 3 tries). The image is deleted after
posting to keep the repo small.

Secrets needed (repo Settings > Secrets and variables > Actions):
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
"""
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from requests_oauthlib import OAuth1

API = "https://api.x.com/2"
MAX_PER_RUN = 2          # never burst more than this in one run
MISSED_AFTER = timedelta(hours=6)  # skip posts that are this late (e.g. Actions was down)
MAX_ATTEMPTS = 3


def env(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"Missing secret {name}. Add it in repo Settings > Secrets and variables > Actions")
    return v


AUTH = OAuth1(env("X_API_KEY"), env("X_API_SECRET"), env("X_ACCESS_TOKEN"), env("X_ACCESS_TOKEN_SECRET"))


def check(r, what):
    if r.status_code >= 300:
        raise RuntimeError(f"{what} failed ({r.status_code}): {r.text[:500]}")
    return r.json()


def upload_image(path):
    with open(path, "rb") as fh:
        r = requests.post(
            f"{API}/media/upload",
            auth=AUTH,
            files={"media": (os.path.basename(path), fh, "image/png")},
            data={"media_category": "tweet_image"},
            timeout=90,
        )
    return check(r, "Image upload")["data"]["id"]


def create_post(text, media_ids):
    body = {"text": text}
    if media_ids:
        body["media"] = {"media_ids": media_ids}
    r = requests.post(f"{API}/tweets", auth=AUTH, json=body, timeout=60)
    return check(r, "Create post")["data"]["id"]


def test_connection():
    r = requests.get(f"{API}/users/me", auth=AUTH, timeout=30)
    me = check(r, "Connection test")["data"]
    print(f"Connected as @{me['username']} ({me['name']}). Nothing was posted")


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def images_for(src_json):
    # post.png, then post-2.png, post-3.png, post-4.png (X allows up to 4)
    base = src_json[:-5]
    found = [base + ".png"] + [f"{base}-{n}.png" for n in range(2, 5)]
    return [f for f in found if os.path.exists(f)]


def move(item, src_json, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src_json))
    with open(dest, "w") as fh:
        json.dump(item, fh, indent=2, ensure_ascii=False)
    os.remove(src_json)
    for img in images_for(src_json):
        os.remove(img)


def main():
    if os.environ.get("TEST_ONLY") == "true":
        test_connection()
        return

    now = datetime.now(timezone.utc)
    posted = 0
    files = sorted(glob.glob("queue/*.json"))
    for path in files:
        with open(path) as fh:
            item = json.load(fh)
        due = parse_time(item["post_at"])
        if due > now:
            continue
        if now - due > MISSED_AFTER:
            item["failed_at"] = now.isoformat()
            item["error"] = f"Missed its time by more than {MISSED_AFTER}, so it was not posted"
            move(item, path, "failed")
            print(f"SKIPPED (too late): {os.path.basename(path)}")
            continue
        if posted >= MAX_PER_RUN:
            print("Hit the per-run limit, the rest will post next run")
            break
        try:
            media_ids = [upload_image(img) for img in images_for(path)]
            tweet_id = create_post(item["text"], media_ids)
            item["tweet_id"] = tweet_id
            item["url"] = f"https://x.com/i/web/status/{tweet_id}"
            item["posted_at"] = datetime.now(timezone.utc).isoformat()
            move(item, path, "posted")
            posted += 1
            print(f"POSTED {os.path.basename(path)} -> {item['url']}")
        except Exception as e:  # keep going with the next item
            item["attempts"] = item.get("attempts", 0) + 1
            item["error"] = str(e)
            print(f"ERROR on {os.path.basename(path)} (try {item['attempts']}): {e}")
            if item["attempts"] >= MAX_ATTEMPTS:
                item["failed_at"] = now.isoformat()
                move(item, path, "failed")
            else:
                with open(path, "w") as fh:
                    json.dump(item, fh, indent=2, ensure_ascii=False)
    print(f"Done. Posted {posted}")


if __name__ == "__main__":
    main()
