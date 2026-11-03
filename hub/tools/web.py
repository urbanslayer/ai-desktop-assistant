"""The three built-in hub tools: web_search (SearXNG), fetch_page, get_time.

Only stdlib + requests. fetch_page extracts readable text with a small
html.parser walker (no extra dependency) and refuses private/loopback
addresses unless `allow_private_fetch` is set (SSRF guard, checked on every
redirect hop). Limits: 10 MB and 20 s per page.
"""
import ipaddress
import json
import re
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) riko-hub/1 fetch"
SEARXNG_TIMEOUT = 15.0
FETCH_TIMEOUT = 20.0
MAX_FETCH_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_TEXT_CHARS = 20_000     # extracted text cap (the loop caps again)
MAX_LINKS = 30

# ── schemas (OpenAI function-calling shapes) ───────────────────────────────
WEB_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "the search query"},
        "max_results": {"type": "integer", "description": "results to return (1-10, default 5)"},
    },
    "required": ["query"],
}
FETCH_PAGE_SCHEMA = {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "http(s) page to read"}},
    "required": ["url"],
}
GET_TIME_SCHEMA = {
    "type": "object",
    "properties": {"timezone": {"type": "string",
                                "description": "IANA timezone name, e.g. Asia/Tokyo (optional)"}},
}

# ── web_search ──────────────────────────────────────────────────────────────
def web_search(query: str, max_results: int = 5, searxng_url: str = "") -> list[dict]:
    searxng_url = str(searxng_url or "").rstrip("/")
    if not searxng_url:
        raise RuntimeError("web_search is not configured: set tools.searxng_url "
                           "or start the hub with RIKO_SEARXNG_URL")
    try:
        n = max(1, min(10, int(max_results)))
    except (TypeError, ValueError):
        n = 5
    r = requests.get(f"{searxng_url}/search", params={"q": str(query), "format": "json"},
                     timeout=SEARXNG_TIMEOUT, headers={"User-Agent": USER_AGENT})
    if r.status_code != 200:
        raise RuntimeError(f"SearXNG answered HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError("SearXNG returned non-JSON — is format=json enabled on it?")
    out = []
    for row in (data.get("results") or [])[:n]:
        out.append({"title": str(row.get("title") or "").strip(),
                    "url": str(row.get("url") or "").strip(),
                    "snippet": str(row.get("content") or "").strip()})
    return out


# ── fetch_page ──────────────────────────────────────────────────────────────
class _BadUrl(ValueError):
    pass


def _guard_public(url: str, allow_private: bool) -> None:
    """http(s) only; every address the host resolves to must be public
    unless allow_private (SSRF: the hub runs inside the owner's LAN)."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise _BadUrl(f"only http/https URLs can be fetched, got '{parts.scheme or '?'}'")
    host = parts.hostname or ""
    if not host:
        raise _BadUrl("URL has no host")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as e:
        raise _BadUrl(f"cannot resolve host '{host}' ({e})") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        private = (ip.is_private or ip.is_loopback or ip.is_link_local
                   or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
        if private and not allow_private:
            raise _BadUrl(f"refusing private/loopback address {ip} for '{host}' "
                          "(set tools.allow_private_fetch to allow)")


class _TextExtractor(HTMLParser):
    """Main-text extraction with stdlib html.parser: skip scripts/styles/
    nav/…, keep block structure as newlines, collect links."""

    SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe",
            "nav", "header", "footer", "aside", "form", "button", "select", "figure"}
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
             "tr", "table", "section", "article", "blockquote", "pre", "dt", "dd",
             "main", "hr", "figcaption"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.chunks: list[str] = []
        self.links: list[dict] = []
        self._skip_depth = 0
        self._in_title = False
        self._link: dict | None = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self._link = {"href": href, "text": ""}
        if tag in self.BLOCK:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._link is not None:
            link, self._link = self._link, None
            if link["href"].startswith(("http://", "https://")) and link["text"].strip():
                if len(self.links) < MAX_LINKS:
                    self.links.append({"text": link["text"].strip()[:120],
                                       "url": link["href"]})
        elif tag in self.BLOCK:
            self.chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        if data and data.strip():
            self.chunks.append(data)
            if self._link is not None:
                self._link["text"] += data

    def text(self) -> str:
        joined = "".join(self.chunks)
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        joined = re.sub(r" ?\n ?", "\n", joined)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return joined.strip()[:MAX_TEXT_CHARS]


def _looks_binary(content_type: str) -> bool:
    ct = content_type.lower()
    return not any(t in ct for t in ("html", "xml", "text", "json", ""))


def fetch_page(url: str, allow_private: bool = False) -> dict:
    """Download an http(s) page and return {title, url, text, links,
    truncated, content_type}. Raises _BadUrl on scheme/SSRF problems."""
    current = str(url)
    body = b""
    content_type = ""
    truncated = False
    status = None
    for _ in range(MAX_REDIRECTS + 1):
        _guard_public(current, allow_private)
        with requests.get(current, timeout=FETCH_TIMEOUT, allow_redirects=False,
                          stream=True, headers={"User-Agent": USER_AGENT,
                                                "Accept": "text/html,*/*"}) as r:
            status = r.status_code
            if status in (301, 302, 303, 307, 308):
                loc = r.headers.get("location") or ""
                if not loc:
                    raise RuntimeError(f"HTTP {status} redirect with no Location")
                current = urljoin(current, loc)
                continue
            if status != 200:
                raise RuntimeError(f"HTTP {status} fetching {current}")
            content_type = r.headers.get("content-type", "")
            for chunk in r.iter_content(65536):
                body += chunk
                if len(body) >= MAX_FETCH_BYTES:
                    truncated = True
                    break
            break
    else:
        raise RuntimeError("too many redirects")
    if _looks_binary(content_type):
        return {"title": "", "url": current, "text": "",
                "links": [], "truncated": truncated,
                "note": f"not readable text (content-type: {content_type})",
                "bytes": len(body)}
    parser = _TextExtractor()
    try:
        parser.feed(body.decode("utf-8", errors="replace"))
    except Exception:
        pass  # keep whatever text we got out of a broken page
    return {"title": parser.title.strip()[:200], "url": current,
            "text": parser.text(), "links": parser.links,
            "truncated": truncated, "content_type": content_type.split(";")[0]}


# ── get_time ────────────────────────────────────────────────────────────────
def get_time(timezone_name: str = "") -> dict:
    tz = timezone.utc
    name = "UTC"
    tzname = str(timezone_name or "").strip()
    if tzname:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tzname)
            name = tzname
        except Exception:
            pass  # unknown zone -> UTC, but tell the model
    now = datetime.now(tz)
    out = {"iso": now.isoformat(timespec="seconds"), "weekday": now.strftime("%A"),
           "timezone": name}
    if tzname and name == "UTC" and tzname.upper() != "UTC":
        out["note"] = f"unknown timezone '{tzname}', answered in UTC"
    return out


def jsonable(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
