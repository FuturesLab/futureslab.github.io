#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import time
import threading
from typing import Optional, List, Dict
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
    s.headers.update({"User-Agent": "bug-json-grabber/3.5"})
    retry = Retry(
        total=retries, connect=retries, read=retries, status=retries,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        backoff_factor=backoff, raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=200, pool_maxsize=200)
    s.mount("https://", adapter); s.mount("http://", adapter)
    s._default_timeout = timeout
    return s

def http_get(session: requests.Session, url: str, headers=None):
    return session.get(url, timeout=session._default_timeout, headers=headers or {})

# ------------------ Utilities ------------------
def capitalize_project(name: str) -> str:
    if any(c.isupper() for c in name): return name
    if "-" in name or "_" in name:
        return "-".join(seg.capitalize() for seg in name.replace("_", "-").split("-"))
    return name.capitalize()

def try_parse_date(raw: str) -> str:
    if not raw: return ""
    s = raw.strip()
    if s.endswith("Z"): s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).date().isoformat()
    except Exception:
        pass
    fmts = [
        "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
        "%a %b %d, %Y %I:%M %p", "%a %b %d, %Y", "%b %d, %Y",
        "%d %b %Y", "%Y-%m-%d", "%a %b %d %H:%M:%S %Y", "%d %b %Y %H:%M",
    ]
    for fmt in fmts:
        try: return datetime.strptime(s, fmt).date().isoformat()
        except Exception: continue
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
            if parsed: return parsed
    return ""

def collect_all_datetimes_from_html(soup: BeautifulSoup) -> List[str]:
    iso_candidates: List[str] = []
    for tag in soup.find_all(attrs={"datetime": True}):
        d = iso_date(tag.get("datetime", ""))
        if d: iso_candidates.append(d)
    raw_html = soup.decode()
    for m in re.finditer(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+\-]\d{2}:\d{2})\b", raw_html):
        d = iso_date(m.group(0))
        if d: iso_candidates.append(d)
    txt = soup.get_text(" ", strip=True)
    d_text = find_any_date_text(txt)
    if d_text: iso_candidates.append(d_text)
    return iso_candidates

MAIL_SUBJ_BUG = re.compile(r"\[([^\]]+)\]\s+\[Bug\s+(\d+)\]\s*(?:New:\s*)?(.*)", re.IGNORECASE)

# ------------------ Smart display-casing (generalizable) ------------------
_readme_cache: Dict[str, str] = {}
_readme_lock = threading.Lock()
_repo_name_cache: Dict[str, str] = {}
_repo_name_lock = threading.Lock()

def _fetch_readme_text(session: requests.Session, owner: str, repo: str) -> str:
    """Return README text (best-effort). Caches per repo."""
    key = f"{owner.lower()}/{repo.lower()}"
    with _readme_lock:
        if key in _readme_cache:
            return _readme_cache[key]

    # 1) GitHub API 'raw' accept (follows default branch)
    try:
        r = http_get(session, f"https://api.github.com/repos/{owner}/{repo}/readme",
                     headers={"Accept": "application/vnd.github.raw"})
        if r.ok and r.text:
            text = r.text
            with _readme_lock:
                _readme_cache[key] = text
            return text
    except Exception:
        pass

    # 2) Common raw filenames on default branch 'HEAD'
    for fname in ("README.md", "README.rst", "README.txt", "Readme.md", "readme.md"):
        try:
            r = http_get(session, f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{fname}")
            if r.ok and r.text:
                text = r.text
                with _readme_lock:
                    _readme_cache[key] = text
                return text
        except Exception:
            continue

    with _readme_lock:
        _readme_cache[key] = ""
    return ""

def _strip_code_blocks(markdown: str) -> str:
    """Remove fenced and indented code blocks to bias toward prose casing."""
    if not markdown:
        return ""
    # Remove fenced code blocks ```...``` (including language hints)
    md = re.sub(r"```.*?```", "", markdown, flags=re.DOTALL)
    # Remove indented code blocks (lines starting with 4+ spaces or a tab)
    md = re.sub(r"(^|\n)(?:[ \t]{4,}.*(?:\n|$))+", r"\1", md)
    return md

def _most_common_casing(text: str, token: str) -> Optional[str]:
    """
    Pick preferred casing for `token` from README prose (ignoring code).
    Priority order for variants found in prose/headings:
      mixed-case (≥2 uppercase, not all-caps)  >  TitleCase  >  ALL-CAPS  >  lowercase
    - Headings are boosted (×3) since they usually carry brand styling.
    - For short alpha tokens (len <= 4): if the exact TitleCase form exists (e.g., 'Zig'),
      prefer it over ALL-CAPS even if ALL-CAPS is more frequent.
    Returns None if token isn't found at all.
    """
    if not text:
        return None

    # Strip code blocks to avoid lowercase bias from command examples
    def _strip_code_blocks(md: str) -> str:
        if not md:
            return ""
        md = re.sub(r"```.*?```", "", md, flags=re.DOTALL)          # fenced blocks
        md = re.sub(r"(^|\n)(?:[ \t]{4,}.*(?:\n|$))+", r"\1", md)   # indented blocks
        return md

    hay = _strip_code_blocks(text)

    # Separate headings vs prose; headings get a boost
    heading_lines, prose_lines = [], []
    for line in hay.splitlines():
        if re.match(r'^\s{0,3}#{1,6}\s+', line):
            heading_lines.append(line)
        else:
            prose_lines.append(line)
    prose = "\n".join(prose_lines)
    headings = "\n".join(heading_lines)

    # Collect variants
    pattern = re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE)

    def collect_counts(s: str) -> Dict[str, int]:
        c: Dict[str, int] = {}
        for m in pattern.finditer(s):
            v = m.group(0)
            c[v] = c.get(v, 0) + 1
        return c

    counts = collect_counts(prose)
    for v, c in collect_counts(headings).items():
        counts[v] = counts.get(v, 0) + c * 3  # boost headings

    if not counts:
        return None

    # Helpers to classify variants
    def is_allcaps(s: str) -> bool:
        letters = [ch for ch in s if ch.isalpha()]
        return bool(letters) and all(ch.isupper() for ch in letters)

    def is_titlecase(s: str) -> bool:
        letters = "".join(ch for ch in s if ch.isalpha())
        return bool(letters) and letters[0].isupper() and letters[1:].lower() == letters[1:]

    def is_mixedcase(s: str) -> bool:
        # Has at least two uppercase letters but not all-caps (e.g., LibreCAD, CPython)
        uppers = sum(1 for ch in s if ch.isupper())
        return uppers >= 2 and not is_allcaps(s)

    # Special case: short alpha tokens — prefer exact TitleCase if present (e.g., "Zig")
    if token.isalpha() and len(token) <= 4:
        desired = token.lower().capitalize()
        if desired in counts:
            return desired

    # Rank: mixed-case > titlecase > all-caps > lowercase
    def rank(variant: str) -> int:
        if is_mixedcase(variant): return 3
        if is_titlecase(variant): return 2
        if is_allcaps(variant):   return 1
        return 0  # lowercase or other

    best = max(counts.items(), key=lambda kv: (rank(kv[0]), kv[1]))[0]
    return best

def humanize_repo_display_name(session: requests.Session, owner: str, repo_canonical: str) -> str:
    """
    Derive a human-friendly repo display name without a hard-coded map.

    Per token (split on '-' and '_'):
      1) Use _most_common_casing from README prose/headings (handles LibreCAD, CPython, Zig, SWC).
      2) If none:
         - If token has digits:
             • If matches 'letters+digits' and letters <= 4 → UPPERCASE letters, keep digits (hdf5 -> HDF5).
             • Else TitleCase alphabetic segments around digits (go2hx -> Go2Hx).
         - If no digits:
             • Short (len <= 4): keep ALL-CAPS as-is (SWC), else capitalize first letter (zig -> Zig).
             • Long  (> 4): uppercase first letter, preserve the rest as-is (librecad -> Librecad).
      3) Join tokens with '-'.
    """
    tokens = re.split(r"[-_]", repo_canonical)
    readme = _fetch_readme_text(session, owner, repo_canonical)
    out: List[str] = []

    for ct in tokens:
        if not ct:
            continue

        observed = _most_common_casing(readme, ct)
        if observed:
            out.append(observed)
            continue

        if any(ch.isdigit() for ch in ct):
            m = re.match(r"^([a-z]+)(\d+)$", ct)
            if m and len(m.group(1)) <= 4:
                out.append(m.group(1).upper() + m.group(2))  # hdf5 -> HDF5
                continue
            parts = re.split(r"(\d+)", ct)
            segs = [(p.capitalize() if p.isalpha() else p) for p in parts if p]
            out.append("".join(segs))  # go2hx -> Go2Hx
            continue

        # Pure alphabetic fallback
        if len(ct) <= 4:
            out.append(ct if ct.isupper() else (ct[0].upper() + ct[1:]))  # SWC stays SWC; zig -> Zig
        else:
            out.append(ct[0].upper() + ct[1:])  # librecad -> Librecad

    # After building tokens, fix any all-lowercase results by capitalizing the first letter
    fixed_tokens = []
    for t in out:
        if t.islower():
            fixed_tokens.append(t[0].upper() + t[1:])
        else:
            fixed_tokens.append(t)

    return "-".join(fixed_tokens)

def canonical_github_repo_name(session: requests.Session, owner: str, repo: str, issue_soup: Optional[BeautifulSoup] = None) -> str:
    """Return GitHub repo's canonical 'name' using API, with HTML fallbacks."""
    key = f"{owner.lower()}/{repo.lower()}"
    with _repo_name_lock:
        if key in _repo_name_cache:
            return _repo_name_cache[key]

    api = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        r = http_get(session, api, headers={"Accept": "application/vnd.github+json"})
        if r.ok:
            data = r.json()
            name = (data.get("name") or "").strip()
            if name:
                with _repo_name_lock:
                    _repo_name_cache[key] = name
                return name
    except Exception:
        pass

    soups: List[BeautifulSoup] = []
    if issue_soup is not None:
        soups.append(issue_soup)
    try:
        repo_html = http_get(session, f"https://github.com/{owner}/{repo}", headers={"Accept": "text/html"})
        soups.append(BeautifulSoup(repo_html.text, "html.parser"))
    except Exception:
        pass

    for soup in soups:
        if not soup: continue
        meta = soup.find("meta", {"name": "octolytics-dimension-repository_nwo"})
        if meta and meta.get("content"):
            nwo = meta["content"].strip()
            parts = nwo.split("/", 1)
            if len(parts) == 2 and parts[1]:
                with _repo_name_lock:
                    _repo_name_cache[key] = parts[1]
                return parts[1]
        ogt = soup.find("meta", {"property": "og:title"})
        if ogt and ogt.get("content"):
            c = ogt["content"]
            m = re.search(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\s*$", c)
            if m:
                right = m.group(2)
                with _repo_name_lock:
                    _repo_name_cache[key] = right
                return right
        a = soup.select_one('a[data-pjax="#repo-content-pjax-container"]')
        if a and a.get_text(strip=True):
            val = a.get_text(strip=True)
            with _repo_name_lock:
                _repo_name_cache[key] = val
            return val

    with _repo_name_lock:
        _repo_name_cache[key] = repo
    return repo

# ------------------ Source-specific fetchers ------------------
def fetch_github(session, url: str, lead: str):
    parts = urlparse(url)
    seg = parts.path.strip("/").split("/")
    if len(seg) < 4 or seg[2] != "issues":
        raise ValueError("Unrecognized GitHub issue URL: %s" % url)
    owner, repo, number = seg[0], seg[1], seg[3]

    def clean_title(t: str) -> str:
        if not t: return ""
        t = t.split("·", 1)[0].strip()
        return "" if t in {"GitHub", "Page not found", "Sign in to GitHub"} else t

    title, created = "", ""
    api = "https://api.github.com/repos/%s/%s/issues/%s" % (owner, repo, number)
    r = http_get(session, api, headers={"Accept": "application/vnd.github+json"})
    try:
        data = r.json()
    except Exception:
        data = {}

    if isinstance(data, dict) and "message" not in data:
        title = (data.get("title") or "").strip()
        created = iso_date(data.get("created_at", ""))

    issue_soup: Optional[BeautifulSoup] = None
    if not title or not created:
        resp = http_get(session, url, headers={"Accept": "text/html"})
        redirected = urlparse(resp.url).path.strip("/").split("/")
        valid_issue_path = (len(redirected) >= 4 and redirected[2] == "issues")
        issue_soup = BeautifulSoup(resp.text, "html.parser")

        t_el = issue_soup.select_one("span.js-issue-title")
        html_title = clean_title(t_el.get_text(" ", strip=True) if t_el else "")
        if not html_title:
            mt = issue_soup.find("meta", {"property": "og:title"}) or issue_soup.find("meta", {"name": "twitter:title"})
            if mt and mt.get("content"):
                html_title = clean_title(mt["content"])
        if not html_title:
            title_tag = issue_soup.find("title")
            if title_tag:
                html_title = clean_title(title_tag.get_text(" ", strip=True))
        if valid_issue_path and html_title:
            title = title or html_title

        if valid_issue_path:
            all_dates = collect_all_datetimes_from_html(issue_soup)
            if all_dates:
                try: created = min(all_dates)
                except Exception: created = all_dates[0]
            if not created:
                created = find_any_date_text(issue_soup.get_text(" ", strip=True))

    # Canonical then README-informed humanization
    canonical = canonical_github_repo_name(session, owner, repo, issue_soup)
    repo_display = humanize_repo_display_name(session, owner, canonical)

    return {
        "id": "%s #%s" % (repo_display, number),
        "url": url,
        "lead": lead,
        "date": created,
        "desc": title,
    }

def parse_gitlab_path(parts: List[str]):
    if "issues" not in parts: raise ValueError("No 'issues' in GitLab path")
    i = parts.index("issues")
    number = parts[i + 1]
    proj_parts = [p for p in parts[:i] if p != "-"]
    if len(proj_parts) < 2: raise ValueError("Unexpected GitLab project path")
    group = proj_parts[0]
    project = "/".join(proj_parts[1:])
    return group, project, number

def fetch_gitlab(session, url: str, lead: str):
    parts = urlparse(url)
    seg = parts.path.strip("/").split("/")
    group, project, number = parse_gitlab_path(seg)
    proj_path = requests.utils.quote("%s/%s" % (group, project), safe="")
    api = "https://%s/api/v4/projects/%s/issues/%s" % (parts.netloc, proj_path, number)
    data = http_get(session, api).json()
    created = iso_date(data.get("created_at", ""))

    proj_name = project.split("/")[-1]
    try:
        proj_api = "https://%s/api/v4/projects/%s" % (parts.netloc, proj_path)
        p = http_get(session, proj_api).json()
        if isinstance(p, dict) and p.get("name"):
            proj_name = p["name"].strip()
    except Exception:
        pass

    # Apply same generalizable humanization on GitLab
    repo_display = humanize_repo_display_name(session, group, proj_name)

    return {
        "id": "%s #%s" % (repo_display, number),
        "url": url,
        "lead": lead,
        "date": created,
        "desc": (data.get("title") or "").strip(),
    }

def fetch_mailarchive(session, url: str, lead: str):
    html = http_get(session, url).text
    soup = BeautifulSoup(html, "html.parser")

    subj = (soup.find("title").get_text(" ", strip=True) if soup.find("title") else "").strip()
    bug_id = None
    desc = subj
    m = MAIL_SUBJ_BUG.search(subj)
    if m:
        project_raw, bugnum, tail = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        bug_id = "%s #%s" % (capitalize_project(project_raw), bugnum)
        desc = tail if tail else subj

    date_str = ""
    meta = soup.find("meta", {"name": "date"})
    if meta and meta.get("content"):
        date_str = iso_date(meta["content"])
    if not date_str:
        date_str = find_any_date_text(soup.get_text(" ", strip=True))

    if not bug_id:
        bug_id = "MailArchive %s" % os.path.basename(url)

    return {
        "id": bug_id, "url": url, "lead": lead,
        "date": date_str, "desc": desc,
    }

def fetch_qcad(session, url: str, lead: str):
    html = http_get(session, url).text
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.find("h2", class_="topic-title") or soup.find("title")
    title = title_el.get_text(" ", strip=True) if title_el else url
    date_str = ""
    time_el = soup.find("time")
    if time_el and time_el.get("datetime"):
        date_str = iso_date(time_el.get("datetime"))
    if not date_str:
        page_text = soup.get_text(" ", strip=True)
        m = re.search(
            r"Posted:\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}(?:\s+\d{1,2}:\d{2}\s*(?:am|pm))?",
            page_text, flags=re.IGNORECASE,
        )
        if m: date_str = find_any_date_text(m.group(0))
    if not date_str:
        m = re.search(r"»\s+(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})", page_text, flags=re.IGNORECASE)
        if m: date_str = iso_date(m.group(1))
    if not date_str:
        date_str = find_any_date_text(soup.get_text(" ", strip=True))
    tid = parse_qs(urlparse(url).query).get("t", [""])[0] or os.path.basename(url)
    return {"id": "QCAD #%s" % tid, "url": url, "lead": lead, "date": date_str, "desc": title}

# ------------------ Dispatcher ------------------
def process_link(session, url: str, lead: str):
    host = urlparse(url).netloc.lower()
    try:
        if "github.com" in host: return fetch_github(session, url, lead)
        if "gitlab" in host or "invent.kde.org" in host: return fetch_gitlab(session, url, lead)
        if "mail-archive.com" in host: return fetch_mailarchive(session, url, lead)
        if "qcad.org" in host: return fetch_qcad(session, url, lead)
        raise ValueError("Unsupported host: %s" % host)
    except requests.exceptions.RequestException as e:
        raise RuntimeError("%s: %s" % (url, e))

# ------------------ I/O ------------------
def read_url_list(path: str):
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f]
    out, seen = [], set()
    for ln in lines:
        if not ln or ln.startswith("#"): continue
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
        print("Input must be a .txt file", file=sys.stderr); sys.exit(1)

    lead = lead_from_filename(args.txt_file)
    urls = read_url_list(args.txt_file)
    if not urls:
        print("No URLs found in input file.", file=sys.stderr); sys.exit(1)

    session = make_session(args.timeout, args.retries, args.backoff)

    results, warnings = [], 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_link, session, u, lead): u for u in urls}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as e:
                warnings += 1
                print("[WARN] %s" % e, file=sys.stderr)

    results.sort(key=lambda x: x["id"].lower())
    out_path = write_json_for_input(args.txt_file, results)
    print("Wrote %d entries → %s  (warnings: %d, %.2fs)" % (len(results), out_path, warnings, time.time() - t0))

if __name__ == "__main__":
    main()
