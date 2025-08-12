#!/usr/bin/env python3
# Multithreaded URL -> JSON scraper for mixed sources (GitHub, GitLab, Mail-Archive, QCAD)
# - Reads input .txt (ignores lines starting with #)
# - Writes same-basename .json
# - Lead/author is inferred from input filename (FirstnameLastname.txt -> "Firstname Lastname")
# - Robust GitHub HTML fallback (gets title + creation date even if API rate-limited)
# - Mail-Archive: parses subject to "<Project> #<bug>", capitalizes project if needed
# - QCAD: extracts dates from multiple forum formats
#
# Usage:
#   export GITHUB_TOKEN=...        # optional, avoids GitHub rate limits
#   python3 grab_bug_jsons.py FirstnameLastname.txt --workers 16 --timeout 12 --retries 3
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ------------------ HTTP session with retries ------------------
def make_session(timeout: int, retries: int, backoff: float):
    s = requests.Session()
    gh = os.getenv("GITHUB_TOKEN")
    if gh:
        s.headers.update({"Authorization": f"Bearer {gh}"})
    s.headers.update({"User-Agent": "bug-json-grabber/3.2"})
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        backoff_factor=backoff,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=200, pool_maxsize=200)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s._default_timeout = timeout
    return s

def http_get(session: requests.Session, url: str, headers=None):
    return session.get(url, timeout=session._default_timeout, headers=headers or {})

# ------------------ Utilities ------------------
def titlecase_repo(name: str) -> str:
    """Format repo name for ID like 'MyRepoName' (remove separators, TitleCase)."""
    return name.replace("-", " ").replace("_", " ").title().replace(" ", "")

def capitalize_project(name: str) -> str:
    """Capitalize project name for human display (keeps hyphens, preserves existing caps)."""
    if any(c.isupper() for c in name):
        return name
    if "-" in name or "_" in name:
        return "-".join(seg.capitalize() for seg in name.replace("_", "-").split("-"))
    return name.capitalize()

def try_parse_date(raw: str) -> str:
    if not raw:
        return ""
    s = raw.strip()
    if s.endswith("Z"):
        s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).date().isoformat()
    except Exception:
        pass
    fmts = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a %b %d, %Y %I:%M %p",
        "%a %b %d, %Y",
        "%b %d, %Y",
        "%d %b %Y",
        "%Y-%m-%d",
        "%a %b %d %H:%M:%S %Y",
        "%d %b %Y %H:%M",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            continue
    return ""

def iso_date(raw: str) -> str:
    return try_parse_date(raw) or raw

def find_any_date_text(big_text: str) -> str:
    pats = [
        r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}(?:\s+\d{1,2}:\d{2}\s*(?:am|pm))?",
        r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}(?:\s+\d{1,2}:\d{2})?",
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}(?:\s+\d{2}:\d{2}:\d{2}\s+\S+)?",
    ]
    for pat in pats:
        m = re.search(pat, big_text, flags=re.IGNORECASE)
        if m:
            parsed = try_parse_date(m.group(0))
            if parsed:
                return parsed
    return ""

def collect_all_datetimes_from_html(soup: BeautifulSoup) -> list:
    """Collect likely datetime strings from GitHub HTML and normalize to YYYY-MM-DD."""
    iso_candidates = []
    for tag in soup.find_all(attrs={"datetime": True}):
        d = iso_date(tag.get("datetime", ""))
        if d:
            iso_candidates.append(d)
    # Raw ISO-8601 in HTML
    raw_html = soup.decode()
    for m in re.finditer(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+\-]\d{2}:\d{2})\b", raw_html):
        d = iso_date(m.group(0))
        if d:
            iso_candidates.append(d)
    # Human-readable fallback
    txt = soup.get_text(" ", strip=True)
    d_text = find_any_date_text(txt)
    if d_text:
        iso_candidates.append(d_text)
    return iso_candidates

MAIL_SUBJ_BUG = re.compile(r"\[([^\]]+)\]\s+\[Bug\s+(\d+)\]\s*(?:New:\s*)?(.*)", re.IGNORECASE)

# ------------------ Source-specific fetchers ------------------
def fetch_github(session, url: str, lead: str):
    parts = urlparse(url)
    seg = parts.path.strip("/").split("/")
    if len(seg) < 4 or seg[2] != "issues":
        raise ValueError(f"Unrecognized GitHub issue URL: {url}")
    owner, repo, number = seg[0], seg[1], seg[3]

    def clean_title(t: str) -> str:
        if not t:
            return ""
        t = t.split("·", 1)[0].strip()
        junk = {"GitHub", "Page not found", "Sign in to GitHub"}
        return "" if t in junk else t

    # API first
    title, created = "", ""
    api = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
    r = http_get(session, api, headers={"Accept": "application/vnd.github+json"})
    try:
        data = r.json()
    except Exception:
        data = {}

    if isinstance(data, dict) and "message" not in data:
        title = (data.get("title") or "").strip()
        created = iso_date(data.get("created_at", ""))

    # HTML fallback if missing/limited
    if not title or not created:
        resp = http_get(session, url, headers={"Accept": "text/html"})
        redirected = urlparse(resp.url).path.strip("/").split("/")
        valid_issue_path = (len(redirected) >= 4 and redirected[2] == "issues")
        soup = BeautifulSoup(resp.text, "html.parser")

        t_el = soup.select_one("span.js-issue-title")
        html_title = clean_title(t_el.get_text(" ", strip=True) if t_el else "")
        if not html_title:
            mt = soup.find("meta", {"property": "og:title"}) or soup.find("meta", {"name": "twitter:title"})
            if mt and mt.get("content"):
                html_title = clean_title(mt["content"])
        if not html_title:
            title_tag = soup.find("title")
            if title_tag:
                html_title = clean_title(title_tag.get_text(" ", strip=True))

        if valid_issue_path and html_title:
            title = title or html_title

        if valid_issue_path:
            all_dates = collect_all_datetimes_from_html(soup)
            if all_dates:
                try:
                    created = min(all_dates)
                except Exception:
                    created = all_dates[0]
            if not created:
                created = find_any_date_text(soup.get_text(" ", strip=True))

    repo_id = titlecase_repo(repo)
    return {
        "id": f"{repo_id} #{number}",
        "url": url,
        "lead": lead,
        "date": created,
        "desc": title,
    }

def parse_gitlab_path(parts):
    # group[/subgroup/...]/project/-/issues/<iid>
    if "issues" not in parts:
        raise ValueError("No 'issues' in GitLab path")
    i = parts.index("issues")
    number = parts[i + 1]
    proj_parts = [p for p in parts[:i] if p != "-"]
    if len(proj_parts) < 2:
        raise ValueError("Unexpected GitLab project path")
    group = proj_parts[0]
    project = "/".join(proj_parts[1:])
    return group, project, number

def fetch_gitlab(session, url: str, lead: str):
    parts = urlparse(url)
    seg = parts.path.strip("/").split("/")
    group, project, number = parse_gitlab_path(seg)
    proj_path = requests.utils.quote(f"{group}/{project}", safe="")
    api = f"https://{parts.netloc}/api/v4/projects/{proj_path}/issues/{number}"
    data = http_get(session, api).json()
    created = iso_date(data.get("created_at", ""))
    proj_id = titlecase_repo(project.split("/")[-1])
    return {
        "id": f"{proj_id} #{number}",
        "url": url,
        "lead": lead,
        "date": created,
        "desc": data.get("title", "").strip(),
    }

def fetch_mailarchive(session, url: str, lead: str):
    html = http_get(session, url).text
    soup = BeautifulSoup(html, "html.parser")

    subj = (soup.find("title").get_text(" ", strip=True) if soup.find("title") else "").strip()
    # Extract "<Project> #<BugNum>" and clean desc
    bug_id = None
    desc = subj
    m = MAIL_SUBJ_BUG.search(subj)
    if m:
        project_raw, bugnum, tail = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        bug_id = f"{capitalize_project(project_raw)} #{bugnum}"
        desc = tail if tail else subj

    # Date: meta first, then visible header / any date text
    date_str = ""
    meta = soup.find("meta", {"name": "date"})
    if meta and meta.get("content"):
        date_str = iso_date(meta["content"])
    if not date_str:
        date_str = find_any_date_text(soup.get_text(" ", strip=True))

    if not bug_id:
        bug_id = f"MailArchive {os.path.basename(url)}"

    return {
        "id": bug_id,
        "url": url,
        "lead": lead,
        "date": date_str,
        "desc": desc,
    }

def fetch_qcad(session, url: str, lead: str):
    html = http_get(session, url).text
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.find("h2", class_="topic-title") or soup.find("title")
    title = title_el.get_text(" ", strip=True) if title_el else url

    date_str = ""
    time_el = soup.find("time")
    if time_el and time_el.get("datetime"):
        date_str = iso_date(time_el["datetime"])
    if not date_str:
        page_text = soup.get_text(" ", strip=True)
        m = re.search(
            r"Posted:\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}(?:\s+\d{1,2}:\d{2}\s*(?:am|pm))?",
            page_text,
            flags=re.IGNORECASE,
        )
        if m:
            date_str = find_any_date_text(m.group(0))
    if not date_str:
        m = re.search(r"»\s+(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})", page_text, flags=re.IGNORECASE)
        if m:
            date_str = iso_date(m.group(1))
    if not date_str:
        date_str = find_any_date_text(page_text)

    tid = parse_qs(urlparse(url).query).get("t", [""])[0] or os.path.basename(url)
    return {
        "id": f"QCADForum {tid}",
        "url": url,
        "lead": lead,
        "date": date_str,
        "desc": title,
    }

# ------------------ Dispatcher ------------------
def process_link(session, url: str, lead: str):
    host = urlparse(url).netloc.lower()
    try:
        if "github.com" in host:
            return fetch_github(session, url, lead)
        if "gitlab" in host or "invent.kde.org" in host:
            return fetch_gitlab(session, url, lead)
        if "mail-archive.com" in host:
            return fetch_mailarchive(session, url, lead)
        if "qcad.org" in host:
            return fetch_qcad(session, url, lead)
        raise ValueError(f"Unsupported host: {host}")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"{url}: {e}")

# ------------------ I/O ------------------
def read_url_list(path: str):
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f]
    out, seen = [], set()
    for ln in lines:
        if not ln or ln.startswith("#"):
            continue
        if ln not in seen:
            out.append(ln); seen.add(ln)
    return out

def write_json_for_input(input_path: str, items):
    base, _ = os.path.splitext(input_path)
    out_path = base + ".json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    return out_path

def lead_from_filename(path: str) -> str:
    base = os.path.basename(path)
    name, _ = os.path.splitext(base)
    # Support CamelCase, spaces, and underscores
    name = name.replace("_", " ").strip()
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).strip()
    return " ".join(w.capitalize() for w in name.split())

# ------------------ Main ------------------
def main():
    ap = argparse.ArgumentParser(description="Build JSON from bug/issue URLs (multithreaded).")
    ap.add_argument("txt_file", help="Input .txt file with URLs (lines starting with # are ignored)")
    ap.add_argument("--workers", type=int, default=16, help="Max worker threads")
    ap.add_argument("--timeout", type=int, default=12, help="Per-request timeout (seconds)")
    ap.add_argument("--retries", type=int, default=3, help="HTTP retries per request")
    ap.add_argument("--backoff", type=float, default=0.6, help="Retry backoff factor")
    args = ap.parse_args()

    if not args.txt_file.endswith(".txt"):
        print("Input must be a .txt file", file=sys.stderr)
        sys.exit(1)

    lead = lead_from_filename(args.txt_file)
    urls = read_url_list(args.txt_file)
    if not urls:
        print("No URLs found in input file.", file=sys.stderr)
        sys.exit(1)

    session = make_session(args.timeout, args.retries, args.backoff)

    results, warnings = [], 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_link, session, u, lead): u for u in urls}
        for fut in as_completed(futs):
            u = futs[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                warnings += 1
                print(f"[WARN] {e}", file=sys.stderr)

    results.sort(key=lambda x: x["id"].lower())
    out_path = write_json_for_input(args.txt_file, results)
    print(f"Wrote {len(results)} entries → {out_path}  (warnings: {warnings}, {time.time() - t0:.2f}s)")

if __name__ == "__main__":
    main()
