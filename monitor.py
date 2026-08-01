#!/usr/bin/env python3
"""
Internship watcher
------------------
Polls the career pages of investment banks / PE firms via their Applicant
Tracking System (ATS) JSON APIs and emails you when NEW roles matching your
criteria (default: 2027 summer or off-cycle internships in London) appear.

- Configure your target firms in firms.yaml
- State is stored in seen.json (committed back by the GitHub Action)
- Runs free on GitHub Actions (see .github/workflows/watcher.yml)
"""

import os
import json
import time
import ssl
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import requests
import yaml

# ---------------------------------------------------------------------------
# WHAT COUNTS AS A MATCH  — edit these lists to taste
# ---------------------------------------------------------------------------
# A role matches if its title/location contains one of INTERN_TERMS
# AND (mentions a year in YEAR_TERMS  OR  is flagged off-cycle).
INTERN_TERMS = [
    "intern", "internship", "summer analyst",
    "off-cycle", "off cycle", "offcycle",
    "industrial placement", "placement",
]
YEAR_TERMS = ["2027"]
OFFCYCLE_TERMS = ["off-cycle", "off cycle", "offcycle"]

# Keep only London-ish roles. Set to [] to disable location filtering.
# (Roles whose feed omits a location are kept, since we can't tell.)
LOCATION_TERMS = ["london", "united kingdom", "uk"]

# On the FIRST run (no seen.json yet) record everything WITHOUT emailing,
# so you only ever get alerts for genuinely new postings afterwards.
NOTIFY_ON_FIRST_RUN = False

# ---------------------------------------------------------------------------
STATE_FILE = Path("seen.json")
REQUEST_TIMEOUT = 20
PAUSE_BETWEEN_FIRMS = 1.0
HEADERS = {"User-Agent": "Mozilla/5.0 (internship-watcher; personal use)"}


def _low(s):
    return (s or "").lower()


# --- ATS fetchers ----------------------------------------------------------
# Each returns a list of {id, title, location, url}.

def fetch_greenhouse(firm):
    token = firm["token"]
    # Greenhouse serves EU-resident boards from a parallel host. We try both so
    # a firm works whichever region it's on. Set `region: eu` to try EU first.
    hosts = ["boards-api.greenhouse.io", "boards-api.eu.greenhouse.io"]
    if firm.get("region") == "eu":
        hosts.reverse()
    last_err = None
    for host in hosts:
        try:
            r = requests.get(f"https://{host}/v1/boards/{token}/jobs",
                             params={"content": "false"}, headers=HEADERS,
                             timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            out = []
            for j in r.json().get("jobs", []):
                out.append({
                    "id": f"gh-{token}-{j.get('id')}",
                    "title": j.get("title", ""),
                    "location": (j.get("location") or {}).get("name", ""),
                    "url": j.get("absolute_url", ""),
                })
            return out
        except Exception as e:
            last_err = e
    raise last_err


def fetch_lever(firm):
    token = firm["token"]
    url = f"https://api.lever.co/v0/postings/{token}"
    r = requests.get(url, params={"mode": "json"}, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        out.append({
            "id": f"lever-{token}-{j.get('id')}",
            "title": j.get("text", ""),
            "location": cats.get("location", ""),
            "url": j.get("hostedUrl", ""),
        })
    return out


def fetch_workday(firm):
    # Workday POSTs to a "CXS" endpoint. Find it via DevTools -> Network -> the
    # 'jobs' XHR. Shape:
    #   https://<tenant>.wd<N>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
    url = firm["url"]
    base = url.split("/wday/")[0]
    out, offset = [], 0
    while True:
        r = requests.post(
            url,
            headers={**HEADERS, "Content-Type": "application/json"},
            json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for j in postings:
            path = j.get("externalPath", "")
            out.append({
                "id": f"wd-{path}",
                "title": j.get("title", ""),
                "location": j.get("locationsText", ""),
                "url": base + path if path else url,
            })
        offset += 20
        if offset >= data.get("total", 0) or offset > 500:
            break
        time.sleep(0.3)
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "workday": fetch_workday,
}


# --- matching + state ------------------------------------------------------

def is_match(job):
    hay = _low(job["title"]) + " " + _low(job["location"])
    if not any(t in hay for t in INTERN_TERMS):
        return False
    if not (any(t in hay for t in YEAR_TERMS) or any(t in hay for t in OFFCYCLE_TERMS)):
        return False
    if LOCATION_TERMS and job["location"].strip():
        if not any(t in _low(job["location"]) for t in LOCATION_TERMS):
            return False
    return True


def load_seen():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            return set()
    return None  # None => first run


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=0))


# --- email -----------------------------------------------------------------

def send_email(new_jobs):
    user = os.environ.get("EMAIL_USER")
    pw = os.environ.get("EMAIL_APP_PASSWORD")
    to = os.environ.get("EMAIL_TO", user)
    if not user or not pw:
        print("!! EMAIL_USER / EMAIL_APP_PASSWORD not set — printing new roles instead:")
        for j in new_jobs:
            print(f"   {j['firm']} | {j['title']} | {j['location']} | {j['url']}")
        return

    firms = ", ".join(sorted({j["firm"] for j in new_jobs}))
    subject = f"🆕 {len(new_jobs)} new internship(s): {firms}"

    rows = "".join(
        f"<tr><td style='padding:6px 12px;border-bottom:1px solid #eee'><b>{j['firm']}</b></td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{j['title']}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{j['location']}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee'><a href='{j['url']}'>Apply</a></td></tr>"
        for j in new_jobs
    )
    html = f"<h3>{len(new_jobs)} new matching role(s)</h3><table>{rows}</table>"
    plain = "\n".join(f"- {j['firm']}: {j['title']} ({j['location']}) {j['url']}" for j in new_jobs)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(user, pw)
        s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())


# --- main ------------------------------------------------------------------

def main():
    firms = yaml.safe_load(Path("firms.yaml").read_text()) or []
    seen = load_seen()
    first_run = seen is None
    if first_run:
        seen = set()

    matches, errors = [], []
    for firm in firms:
        fetch = FETCHERS.get(firm.get("ats"))
        if not fetch:
            errors.append(f"{firm.get('name')}: unknown ats '{firm.get('ats')}'")
            continue
        try:
            jobs = fetch(firm)
        except Exception as e:
            errors.append(f"{firm.get('name')}: {type(e).__name__} {e}")
            continue
        hits = [dict(j, firm=firm["name"]) for j in jobs if is_match(j)]
        print(f"{firm['name']:<28} {len(jobs):>4} roles  {len(hits):>3} matching")
        matches.extend(hits)
        time.sleep(PAUSE_BETWEEN_FIRMS)

    new = [j for j in matches if j["id"] not in seen]
    for j in matches:
        seen.add(j["id"])
    save_seen(seen)

    print(f"\nTotal: {len(matches)} matching, {len(new)} new.")
    if errors:
        print("Errors (check the token/url in firms.yaml):")
        for e in errors:
            print("  -", e)

    if new and (NOTIFY_ON_FIRST_RUN or not first_run):
        send_email(new)
        print(f"Emailed {len(new)} new role(s).")
    elif first_run:
        print("First run — recorded current roles without emailing.")


if __name__ == "__main__":
    main()
