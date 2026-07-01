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
        "%a %d %b %Y %I:%M:%S %p %Z", "%a %d %b %Y %H:%M:%S %Z",
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
    Ranking: MixedCase > TitleCase > ALL-CAPS > lowercase.
    For short words (<=4 letters): if TitleCase appears at all, prefer it over ALL-CAPS.
    """
    if not text:
        return None

    cleaned = _strip_code_blocks(text)
    cleaned = re.sub(r"`[^`\n]+`", "", cleaned)  # drop inline `code`

    heading_lines = []
    prose_lines = []
    for line in cleaned.splitlines():
        if re.match(r'^\s{0,3}#{1,6}\s+', line):
            heading_lines.append(line)
        else:
            prose_lines.append(line)
    prose, headings = "\n".join(prose_lines), "\n".join(heading_lines)

    pattern = re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE)

    def collect_counts(s: str) -> Dict[str, int]:
        c: Dict[str, int] = {}
        for m in pattern.finditer(s):
            v = m.group(0)
            c[v] = c.get(v, 0) + 1
        return c

    counts = collect_counts(prose)
    for v, c in collect_counts(headings).items():
        counts[v] = counts.get(v, 0) + c * 3

    if not counts:
        return None

    def is_allcaps(s: str) -> bool:
        letters = [ch for ch in s if ch.isalpha()]
        return bool(letters) and all(ch.isupper() for ch in letters)

    def is_titlecase(s: str) -> bool:
        return s.isalpha() and len(s) >= 2 and s[0].isupper() and s[1:].islower()

    def is_mixedcase(s: str) -> bool:
        uppers = sum(1 for ch in s if ch.isupper())
        return uppers >= 2 and not is_allcaps(s)

    # Short-token preference: if TitleCase exists for short alpha tokens, take it immediately.
    if token.isalpha() and len(token) <= 4:
        for v in counts:
            if is_titlecase(v):
                return v

    def rank(variant: str) -> int:
        if is_mixedcase(variant): return 3
        if is_titlecase(variant): return 2
        if is_allcaps(variant):   return 1
        return 0

    return max(counts.items(), key=lambda kv: (rank(kv[0]), kv[1]))[0]


def _build_casing_dict_from_readme(text: str) -> Dict[str, str]:
    """
    From README prose/headings, build a dict mapping lowercase tokens (incl. digits) → best-cased variant.
    We collect A/Z/0-9 strings length>=2 that have at least one uppercase (i.e., not all-lowercase),
    and keep the variant with the most uppercase letters; tiebreaker by length, then frequency.
    """
    if not text:
        return {}
    # Reuse the code-block stripper from your _most_common_casing or inline a simple one:
    def _strip_code_blocks(md: str) -> str:
        if not md:
            return ""
        md = re.sub(r"```.*?```", "", md, flags=re.DOTALL)
        md = re.sub(r"(^|\n)(?:[ \t]{4,}.*(?:\n|$))+", r"\1", md)
        return md

    prose = _strip_code_blocks(text)
    # Whole tokens like JSON5, OpenCL, CPython; allow digits inside
    candidates = re.findall(r"[A-Za-z0-9]{2,}", prose)

    stats: Dict[str, Dict[str, int]] = {}  # lower -> { 'upper': count, 'len': len, 'freq': freq, 'best': variant }
    for v in candidates:
        if v.islower():
            continue  # ignore plain lowercase
        key = v.lower()
        upper_cnt = sum(1 for ch in v if ch.isupper())
        d = stats.get(key)
        if not d:
            stats[key] = {'upper': upper_cnt, 'len': len(v), 'freq': 1, 'best': v}
        else:
            d['freq'] += 1
            # prefer more uppercase, then longer, then more frequent
            cur = d['best']
            cur_upper = d['upper']
            cur_len = d['len']
            if (upper_cnt, len(v), d['freq']) > (cur_upper, cur_len, d['freq'] - 1):
                d['upper'] = upper_cnt
                d['len'] = len(v)
                d['best'] = v

    return {k: v['best'] for k, v in stats.items()}

def _apply_subtoken_casing(token: str, casing_dict: Dict[str, str]) -> str:
    """
    Replace substrings in `token` using `casing_dict` (longest-first, case-insensitive).

    Safeguards:
      - Do NOT replace a full-token short TitleCase (e.g., 'Zig') with an ALL-CAPS mapping (e.g., 'ZIG').
      - Only upgrade substrings that are "substantial":
          • length >= 3, OR
          • contain any digit, OR
          • map to ALL-CAPS with at least 2 letters (e.g., 'PDF', 'CPU').
    """
    if not token or not casing_dict:
        return token

    def is_titlecase_word(s: str) -> bool:
        return s.isalpha() and len(s) >= 2 and s[0].isupper() and s[1:].islower()

    def is_allcaps_word(s: str) -> bool:
        letters = [ch for ch in s if ch.isalpha()]
        return bool(letters) and all(ch.isupper() for ch in letters)

    # Guard: don't turn short TitleCase full token into ALL-CAPS
    repl_full = casing_dict.get(token.lower())
    if (
        repl_full
        and len(token) <= 4
        and is_titlecase_word(token)
        and is_allcaps_word(repl_full)
    ):
        return token

    def substantial(k: str) -> bool:
        if any(ch.isdigit() for ch in k):
            return True
        if len(k) >= 3:
            return True
        # allow short ALL-CAPS like 'PDF', 'CPU' (>=2 letters)
        mapped = casing_dict[k]
        return is_allcaps_word(mapped) and sum(1 for ch in mapped if ch.isalpha()) >= 2

    # Build candidate keys, excluding the forbidden full-token Zig->ZIG case
    keys = [
        k for k in casing_dict.keys()
        if substantial(k) and not (
            len(token) <= 4 and token.lower() == k and is_titlecase_word(token) and is_allcaps_word(casing_dict[k])
        )
    ]
    if not keys:
        return token
    keys.sort(key=len, reverse=True)

    i, n = 0, len(token)
    low = token.lower()
    out: List[str] = []

    while i < n:
        replaced = False
        for k in keys:
            L = len(k)
            if i + L <= n and low[i:i+L] == k:
                out.append(casing_dict[k])
                i += L
                replaced = True
                break
        if not replaced:
            out.append(token[i])
            i += 1

    return "".join(out)


def humanize_repo_display_name(session: requests.Session, owner: str, repo_canonical: str) -> str:
    """
    Derive a human-friendly repo display name without a hard-coded map.

    Per token (split on '-' and '_'):
      1) Use _most_common_casing from README prose/headings if available (handles LibreCAD, CPython, Zig, SWC).
      2) If none:
         - If token has digits:
             • If matches 'letters+digits' and letters <= 4 → UPPERCASE letters, keep digits (hdf5 -> HDF5).
             • If matches 'letters+digits+letters' → Capitalize both letter segments (c4go -> C4Go).
             • Else split by digit runs and capitalize alpha segments (go2hx -> Go2Hx, c4go2java -> C4Go2Java).
         - If no digits:
             • Short (len <= 4): keep ALL-CAPS as-is (SWC), else capitalize first letter (zig -> Zig).
             • Long  (> 4): uppercase first letter, preserve the rest as-is (librecad -> Librecad).
      3) After assembly, if any token is still completely lowercase, capitalize its first letter.
      4) Apply subtoken casing upgrades from README (e.g., 'json5' -> 'JSON5' inside 'pyjson5' -> 'PyJSON5').
      5) Join tokens with '-'.
    """
    tokens = re.split(r"[-_]", repo_canonical)
    readme = _fetch_readme_text(session, owner, repo_canonical)
    casing_dict = _build_casing_dict_from_readme(readme)
    out: List[str] = []

    for ct in tokens:
        if not ct:
            continue

        observed = _most_common_casing(readme, ct)
        token_display = observed if observed else ct

        if any(ch.isdigit() for ch in token_display):
            # hdf5-style
            m_end = re.match(r"^([A-Za-z]+)(\d+)$", token_display)
            if m_end and len(m_end.group(1)) <= 4:
                token_display = m_end.group(1).upper() + m_end.group(2)
            else:
                # c4go or more complex
                m_mid = re.match(r"^([A-Za-z]+)(\d+)([A-Za-z]+)$", token_display)
                if m_mid:
                    left, digits, right = m_mid.group(1), m_mid.group(2), m_mid.group(3)
                    token_display = left[0].upper() + left[1:] + digits + right[0].upper() + right[1:]
                else:
                    parts = re.split(r"(\d+)", token_display)
                    segs = [(p[0].upper() + p[1:] if p and p.isalpha() else p) for p in parts if p]
                    token_display = "".join(segs)
        else:
            if not observed:
                if len(token_display) <= 4:
                    token_display = token_display if token_display.isupper() else (token_display[0].upper() + token_display[1:])
                else:
                    token_display = token_display[0].upper() + token_display[1:]

        # Final safeguard: capitalize first letter if all lowercase
        if token_display.islower():
            token_display = token_display[0].upper() + token_display[1:]

        # Subtoken casing upgrade from README (e.g., json5 -> JSON5)
        token_display = _apply_subtoken_casing(token_display, casing_dict)

        out.append(token_display)

    return "-".join(out)

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
    if len(seg) < 4 or seg[2] not in ("issues", "pull"):
        raise ValueError("Unrecognized GitHub issue/PR URL: %s" % url)
    owner, repo, number = seg[0], seg[1], seg[3]
    is_pr = (seg[2] == "pull")

    def clean_title(t: str) -> str:
        if not t: return ""
        # GitHub PR page titles are "TITLE by AUTHOR · Pull Request #N · owner/repo".
        if is_pr and "· Pull Request" in t:
            t = t.split("·", 1)[0].strip()
            t = re.sub(r"\s+by\s+\S+$", "", t).strip()
        else:
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
        valid_issue_path = (len(redirected) >= 4 and redirected[2] in ("issues", "pull"))
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
    # GitLab issues appear as ".../issues/<n>" or as work items ".../work_items/<n>";
    # both map to the same issue via the REST issues API.
    kind = None
    for k in ("issues", "work_items"):
        if k in parts:
            kind = k
            break
    if kind is None:
        raise ValueError("No 'issues' or 'work_items' in GitLab path")
    i = parts.index(kind)
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

def fetch_xnview_forum(session, url: str, lead: str):
    """
    XnView (phpBB) forum thread:
      - ID: XnView #<topic id>
      - Title: page <title> or h2/h3 topic title
      - Date: from <time datetime="...">, meta tags, or '» Mon Sep 16, 2024 10:20 am' style text
    """
    html = http_get(session, url).text
    soup = BeautifulSoup(html, "html.parser")

    # Title
    title_el = soup.find("title") or soup.find("h2") or soup.find("h3")
    title = title_el.get_text(" ", strip=True) if title_el else url

    # Date candidates
    date_str = ""
    # a) <time datetime="...">
    t_el = soup.find("time")
    if t_el and t_el.get("datetime"):
        date_str = iso_date(t_el["datetime"])

    # b) meta[name=date] or meta[property=article:published_time]
    if not date_str:
        meta = soup.find("meta", {"name": "date"}) or soup.find("meta", {"property": "article:published_time"})
        if meta and meta.get("content"):
            date_str = iso_date(meta["content"])

    # c) visible phpBB "» Mon Sep 16, 2024 10:20 am" style
    if not date_str:
        txt = soup.get_text(" ", strip=True)
        m = re.search(r"»\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}(?:\s+\d{1,2}:\d{2}\s*(?:am|pm))?", txt, flags=re.IGNORECASE)
        if m:
            date_str = find_any_date_text(m.group(0))
    if not date_str:
        # Any date-like text anywhere
        date_str = find_any_date_text(soup.get_text(" ", strip=True))

    # Topic id from query param t=49598
    tid = parse_qs(urlparse(url).query).get("t", [""])[0] or os.path.basename(url)

    return {
        "id": f"XnView #{tid}",
        "url": url,
        "lead": lead,
        "date": date_str,
        "desc": title,
    }

def fetch_savannah(session, url: str, lead: str):
    """
    Savannah (Savane) bug tracker, e.g. https://savannah.gnu.org/bugs/?68391
      - ID: <Project> #<bug id>  (project from ?group= links, e.g. gettext -> Gettext)
      - Title: full item title from the <h1> ("bug #NNNNN: ...") minus the prefix
      - Date: "Submitted:" field ("Fri 22 May 2026 06:10:10 PM UTC")
    """
    html = http_get(session, url).text
    soup = BeautifulSoup(html, "html.parser")

    # Bug id: query string is the number (?68391), fall back to item_id.
    q = urlparse(url).query
    bug_id = ""
    m = re.match(r"^(\d+)$", q.strip())
    if m:
        bug_id = m.group(1)
    if not bug_id:
        bug_id = parse_qs(q).get("item_id", [""])[0]

    # Full title from an <h1> containing "bug #<id>: <title>"
    title = ""
    for h1 in soup.find_all("h1"):
        txt = h1.get_text(" ", strip=True)
        mt = re.search(r"bug\s+#\d+\s*:\s*(.+)$", txt, flags=re.IGNORECASE)
        if mt:
            title = mt.group(1).strip()
            break
    if not title:
        # Fall back to <title> (may be truncated with "...")
        t_tag = soup.find("title")
        if t_tag:
            raw = t_tag.get_text(" ", strip=True)
            mt = re.search(r"bug\s+#\d+\s*,\s*(.+?)\s*\[Savannah\]", raw, flags=re.IGNORECASE)
            if mt:
                title = mt.group(1).strip()

    # Project/group name: pick the most frequent ?group=<name> across the page,
    # ignoring the generic "administration" support link in the site menu.
    groups: Dict[str, int] = {}
    for g in re.findall(r"[?&]group=([A-Za-z0-9_.\-]+)", html):
        if g.lower() == "administration":
            continue
        groups[g] = groups.get(g, 0) + 1
    group = max(groups.items(), key=lambda kv: kv[1])[0] if groups else ""
    project_display = capitalize_project(group) if group else "Savannah"

    # Submitted date
    date_str = ""
    for td in soup.find_all("td", class_="preinput"):
        if "Submitted" in td.get_text(" ", strip=True):
            sib = td.find_next_sibling("td")
            if sib:
                date_str = iso_date(sib.get_text(" ", strip=True))
            break
    if not date_str:
        date_str = find_any_date_text(soup.get_text(" ", strip=True))

    return {
        "id": "%s #%s" % (project_display, bug_id) if bug_id else "%s" % project_display,
        "url": url,
        "lead": lead,
        "date": date_str,
        "desc": title,
    }

def fetch_forgejo(session, url: str, lead: str):
    """
    Forgejo/Gitea instances (e.g. https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/23299).
    Uses the /api/v1 REST API for issues and pull requests.
      - Path: /<owner>/<repo>/(issues|pulls)/<number>
      - ID: <RepoDisplay> #<number>
    """
    parts = urlparse(url)
    seg = parts.path.strip("/").split("/")
    if len(seg) < 4 or seg[2] not in ("issues", "pulls"):
        raise ValueError("Unrecognized Forgejo issue/PR URL: %s" % url)
    owner, repo, kind, number = seg[0], seg[1], seg[2], seg[3]

    api = "https://%s/api/v1/repos/%s/%s/%s/%s" % (parts.netloc, owner, repo, kind, number)
    data = {}
    try:
        data = http_get(session, api, headers={"Accept": "application/json"}).json()
    except Exception:
        data = {}

    title = (data.get("title") or "").strip() if isinstance(data, dict) else ""
    created = iso_date(data.get("created_at", "")) if isinstance(data, dict) else ""

    # Canonical repo name from API (base.repo.name), fall back to path segment
    repo_canonical = repo
    if isinstance(data, dict):
        base_name = (data.get("base") or {}).get("repo", {}).get("name")
        if base_name:
            repo_canonical = base_name

    # Fallbacks from HTML if API failed
    if not title or not created:
        soup = BeautifulSoup(http_get(session, url).text, "html.parser")
        if not title:
            t_tag = soup.find("title")
            if t_tag:
                raw = t_tag.get_text(" ", strip=True)
                mt = re.match(r"#\d+\s+-\s+(.+?)\s+-\s+", raw)
                title = mt.group(1).strip() if mt else raw
        if not created:
            all_dates = collect_all_datetimes_from_html(soup)
            if all_dates:
                created = min(all_dates)

    # Repo name from the API is already the canonical display name (e.g. "FFmpeg");
    # use it directly rather than doing a cross-host GitHub README lookup.
    return {
        "id": "%s #%s" % (repo_canonical, number),
        "url": url,
        "lead": lead,
        "date": created,
        "desc": title,
    }

def _parse_mantis_view(html: str, url: str, lead: str, bug_id: str):
    """Parse a MantisBT view.php page into a result dict (shared by live + Wayback)."""
    soup = BeautifulSoup(html, "html.parser")

    def td_text(cls: str) -> str:
        el = soup.find("td", class_=cls)
        return el.get_text(" ", strip=True) if el else ""

    # Summary: "0000768: <title>" -> strip the zero-padded id prefix
    summary = td_text("bug-summary")
    if not summary:
        t_tag = soup.find("title")
        if t_tag:
            summary = t_tag.get_text(" ", strip=True).rsplit("-", 1)[0].strip()
    desc = re.sub(r"^0*\d+\s*:\s*", "", summary).strip()

    # Project name -> display (e.g. "file" -> "File")
    project = td_text("bug-project")
    project_display = capitalize_project(project) if project else "Astron"

    # Date submitted
    date_str = iso_date(td_text("bug-date-submitted"))
    if not date_str:
        date_str = find_any_date_text(soup.get_text(" ", strip=True))

    return {
        "id": "%s #%s" % (project_display, bug_id),
        "url": url,
        "lead": lead,
        "date": date_str,
        "desc": desc,
    }

def _looks_like_mantis_login(html: str) -> bool:
    low = html.lower()
    if "bug-summary" in low:
        return False
    return "login_page.php" in low or "<title>mantisbt</title>" in low or "login_anon" in low

def fetch_astron(session, url: str, lead: str):
    """
    MantisBT tracker (bugs.astron.com), e.g. https://bugs.astron.com/view.php?id=768
      - ID: <Project> #<id>   (project from the 'bug-project' cell, e.g. file -> File)
      - Title: 'bug-summary' cell, minus the zero-padded id prefix
      - Date: 'bug-date-submitted' cell

    bugs.astron.com is login-walled for some items; when the live page redirects to
    the login screen we fall back to the most recent public Wayback Machine snapshot.
    """
    bug_id = parse_qs(urlparse(url).query).get("id", [""])[0] or os.path.basename(url)

    resp = http_get(session, url, headers={"Accept": "text/html"})
    html = resp.text
    if not _looks_like_mantis_login(html):
        return _parse_mantis_view(html, url, lead, bug_id)

    # Login-walled: try the latest public Wayback Machine capture.
    try:
        cdx = http_get(
            session,
            "http://web.archive.org/cdx/search/cdx?url=%s&output=json&filter=statuscode:200&limit=-1"
            % requests.utils.quote(url, safe=""),
        ).json()
        rows = [r for r in cdx[1:]] if isinstance(cdx, list) and len(cdx) > 1 else []
        if rows:
            ts = rows[-1][1]
            snap = http_get(session, "https://web.archive.org/web/%sid_/%s" % (ts, url))
            snap_html = snap.text
            if not _looks_like_mantis_login(snap_html):
                return _parse_mantis_view(snap_html, url, lead, bug_id)
    except Exception:
        pass

    raise ValueError("Astron bug is login-walled and no public snapshot exists: %s" % url)

# ------------------ Dispatcher ------------------
def process_link(session, url: str, lead: str):
    host = urlparse(url).netloc.lower()
    try:
        if "github.com" in host:
            return fetch_github(session, url, lead)
        if "gitlab" in host or "invent.kde.org" in host:
            return fetch_gitlab(session, url, lead)
        if "savannah.gnu.org" in host or "savannah.nongnu.org" in host:
            return fetch_savannah(session, url, lead)
        if "bugs.astron.com" in host:
            return fetch_astron(session, url, lead)
        if "code.ffmpeg.org" in host:
            return fetch_forgejo(session, url, lead)
        if "mail-archive.com" in host:
            return fetch_mailarchive(session, url, lead)
        if "qcad.org" in host:
            return fetch_qcad(session, url, lead)
        if "xnview.com" in host:
            return fetch_xnview_forum(session, url, lead)
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
