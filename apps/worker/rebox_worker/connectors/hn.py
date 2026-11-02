"""Hacker News launches (``public/hn``): "Launch HN" stories from the Algolia HN Search API.

fetch
    ``GET https://hn.algolia.com/api/v1/search_by_date?query="Launch HN"&tags=story``, windowed by
    ``created_at_i`` (Algolia stops paginating after 1,000 hits, so each request asks for the
    stories older than the oldest one seen so far). The first run backfills everything
    (~1.2k stories, two requests); later runs ask for ``created_at_i >= cursor - 7 days`` (so
    points of recent launches are refreshed; unchanged stories are skipped by content hash).
    Stories are yielded newest first; the cursor (max ``created_at_i``) is saved at the end of a
    complete run. An interrupted run simply re-reads the window: stories already written are
    skipped by hash. A ``--limit N`` run yields the newest N as a sample and does not move the
    cursor.

extract (pure)
    One RawRecord per story (``external_id = story:<objectID>``). Titles like
    ``Launch HN: Acme (YC W24) – Tagline`` become:

    * launch   identity ``hn_story``; ``name`` = tagline, ``url`` (story URL), ``points``;
               ``valid_from`` = created_at on all launch facts
    * company  ``name``, ``yc_batch``; identity ``domain`` from the story URL (unless it is a
               platform such as github.com or youtube.com) or else from a link in the post text
               whose host matches the company name; ``github`` from a github.com/<owner> link;
               ``launched`` -> launch. Context = [author], so a second launch by the same
               author without a domain still finds the company by name.
    * person   identity ``hn_user`` (the author), context = [company]; ``posted`` -> launch;
               ``founded`` -> company with ``{"basis": "launch_hn_author"}`` (YC requires
               Launch HN posts to come from founders).

    Titles that don't parse (Ask HN, "Lessons from my Launch HN", descriptions without a
    company name, ...) are kept as raw records and produce no claims. ``fetch`` logs how many,
    and a complete run stores the count in the cursor (``last_run.unparsed_titles``).

source_ref: ``hn:story:<id>:<predicate>:<role>`` for story facts, ``hn:user:<login>:<predicate>``
for the author's own facts (the same claim whichever story it is seen in).
"""

from __future__ import annotations

import html
import re
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..models import ClaimInput, CursorUpdate, EntityRef, RawRecord
from .base import Ctx

API = "https://hn.algolia.com/api/v1/search_by_date"
QUERY = '"Launch HN"'
PAGE_SIZE = 1000  # Algolia's maximum hitsPerPage
LOOKBACK_S = 7 * 86400
MIN_INTERVAL_S = 1.0  # between requests (Algolia allows far more; be polite)
MAX_RETRIES = 6
KEEP_FIELDS = (
    "objectID",
    "title",
    "url",
    "author",
    "created_at",
    "created_at_i",
    "points",
    "num_comments",
    "story_text",
    "_tags",
)


class Hn:
    name = "hn"
    layer = "public/hn"
    interval_s = 3600

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        self.transport = transport
        self.sleep = sleep
        self.min_interval_s = min_interval_s

    # ── fetch: the only part that does I/O ──
    def fetch(self, ctx: Ctx, cursor: dict | None) -> Iterator[RawRecord | CursorUpdate]:
        """Stories created at or after ``cursor.created_at_i - LOOKBACK_S`` (everything on the
        first run), newest first. The window is re-read every run so points of recent launches
        are refreshed; unchanged stories are skipped by content hash.

        The cursor moves only at the end of a complete run. A ``--limit`` run yields the newest
        ``limit`` stories as a sample and leaves the cursor where it was, so the next full run
        still backfills everything older.
        """
        since = int((cursor or {}).get("created_at_i") or 0)
        window_start = max(0, since - LOOKBACK_S) if since else 0
        with ctx.http(transport=self.transport) as client:
            stories = self._stories_since(client, ctx, window_start)
        stories.sort(key=lambda s: (-s["created_at_i"], -int(s["objectID"])))  # newest first
        if ctx.limit is not None:
            stories = stories[: ctx.limit]
        unparsed = 0
        for story in stories:
            if parse_title(story.get("title") or "") is None:
                unparsed += 1  # kept as a raw record; extract emits nothing for it
            yield RawRecord.new(
                self.name,
                f"story:{story['objectID']}",
                story,
                url=f"https://news.ycombinator.com/item?id={story['objectID']}",
            )
        ctx.log.info(
            "hn: %d stories from created_at_i>=%d, %d with unparsed titles (no claims)",
            len(stories),
            window_start,
            unparsed,
        )
        if ctx.limit is None:
            newest = max([since, *(s["created_at_i"] for s in stories)])
            last_run = {"stories": len(stories), "unparsed_titles": unparsed}
            yield CursorUpdate(cursor={"created_at_i": newest, "last_run": last_run})

    def _stories_since(self, client: httpx.Client, ctx: Ctx, since: int) -> list[dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        before: int | None = None
        while True:
            filters = [f"created_at_i>={since}"] if since else []
            if before is not None:
                filters.append(f"created_at_i<{before}")
            params = {"query": QUERY, "tags": "story", "hitsPerPage": str(PAGE_SIZE)}
            if filters:
                params["numericFilters"] = ",".join(filters)
            data = get_json(
                client, API, params=params, log=ctx.log, sleep=self.sleep, min_interval_s=self.min_interval_s
            )
            hits = data.get("hits") or []
            for h in hits:
                if h.get("objectID") and isinstance(h.get("created_at_i"), int):
                    out[str(h["objectID"])] = {k: h.get(k) for k in KEEP_FIELDS if h.get(k) is not None}
            if len(hits) < PAGE_SIZE:
                return list(out.values())
            oldest = min(h["created_at_i"] for h in hits if isinstance(h.get("created_at_i"), int))
            # Stories at exactly `oldest` may straddle two pages, so the next window re-includes
            # that second (`< oldest + 1`); duplicates collapse by objectID.
            if before is not None and oldest + 1 >= before:
                raise RuntimeError(f"hn: pagination stuck at created_at_i={oldest}")
            before = oldest + 1

    # ── extract: pure ──
    def extract(self, rec: RawRecord) -> list[ClaimInput]:
        s: dict[str, Any] = rec.payload
        parsed = parse_title(s.get("title") or "")
        author = s.get("author")
        story_id = str(s.get("objectID") or "")
        if parsed is None or not author or not story_id:
            return []
        when = s.get("created_at")
        base = f"{self.name}:story:{story_id}"
        out: list[ClaimInput] = []

        domain, github = company_identity(parsed.name, s.get("url"), s.get("story_text"))
        company_identity_ = {"domain": domain, "github": github}
        author_plain = EntityRef(kind="person", identity={"hn_user": author}, name=author)
        company = EntityRef(
            kind="company", identity=company_identity_, name=parsed.name, context=[author_plain]
        )
        company_plain = EntityRef(kind="company", identity=company_identity_, name=parsed.name)
        person = EntityRef(kind="person", identity={"hn_user": author}, name=author, context=[company_plain])
        launch = EntityRef(kind="launch", identity={"hn_story": story_id}, name=parsed.tagline or parsed.name)

        def add(subject: EntityRef, predicate: str, ref: str, **kw: Any) -> None:
            out.append(ClaimInput(subject=subject, predicate=predicate, source_ref=ref, **kw))

        # launch
        add(launch, "name", f"{base}:name:launch", value=parsed.tagline or parsed.name, valid_from=when)
        add(launch, "hn_story", f"{base}:hn_story:launch", value=story_id, valid_from=when)
        launch_url = _launch_url(s.get("url"))
        if launch_url:
            add(launch, "url", f"{base}:url:launch", value=launch_url, valid_from=when)
        if isinstance(s.get("points"), int):
            add(launch, "points", f"{base}:points:launch", value=s["points"], valid_from=when)
        # company
        add(company, "name", f"{base}:name:company", value=parsed.name)
        if domain:
            add(company, "domain", f"{base}:domain:company", value=domain)
        if github:
            add(company, "github", f"{base}:github:company", value=github)
        if parsed.batch:
            add(company, "yc_batch", f"{base}:yc_batch:company", value=parsed.batch)
        add(company, "launched", f"{base}:launched:launch", object=launch, valid_from=when)
        # author
        user_ref = f"{self.name}:user:{author}"
        add(person, "name", f"{user_ref}:name", value=author)
        add(person, "hn_user", f"{user_ref}:hn_user", value=author)
        add(person, "posted", f"{base}:posted:launch", object=launch, valid_from=when)
        add(person, "founded", f"{base}:founded:company", object=company, value={"basis": "launch_hn_author"})
        return out


# ── title parsing (pure) ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedTitle:
    name: str
    batch: str | None  # "W24"
    tagline: str | None
    qualifier: str | None  # text in parentheses after the name, e.g. "YC W19 Nonprofit"


# "Launch HN:", "Launch  HN:", "(Re)launch HN:", "Re-Launch HN:", "(Pre)Launch HN:", "Launch HN -"
_PREFIX_RE = re.compile(r"^\s*(?:\((?:re|pre)\)\s*|re-)?launch\s+hn\s*(?::|\s[-–—]\s)\s*", re.I)
# "Name (qualifier) rest" -- the qualifier holds the batch: "YC W24", "YC.S18", "YC W19 Nonprofit"
_QUALIFIED_RE = re.compile(r"^(?P<name>[^()]+?)\s*\((?P<q>[^()]*)\)\s*(?P<rest>.*)$")
_BATCH_RE = re.compile(r"\bYC\b[\s.]*(?:nonprofit\s+)?(?P<b>[WSFXP])(?P<y>\d{2})\b", re.I)
_LEADING_SEP_RE = re.compile(r"^(?:[–—:,]|-(?!\w))\s*")
# Separators between a name and a tagline when there is no batch, strongest first.
_SEPARATORS = (
    re.compile(r"\s+[–—]\s*|\s*[–—]\s+|(?<=\w)[–—](?=\w)"),  # en/em dash (spaced or tight)
    re.compile(r"\s+-\s+"),  # spaced hyphen
    re.compile(r":\s+"),
    re.compile(r",\s+"),
    re.compile(r"\.\s+"),
)
_BRACKETED_RE = re.compile(r"^\[(?P<name>[^\[\]]+)\]\s+(?P<rest>.+)$")
_STOP_FIRST_WORDS = {
    "a",
    "an",
    "the",
    "i",
    "i'm",
    "i've",
    "i'll",
    "we",
    "my",
    "our",
    "how",
    "why",
    "what",
    "on",
}
MAX_NAME_WORDS = 5


def parse_title(title: str) -> ParsedTitle | None:
    """``Launch HN: Acme (YC W24) – Tagline`` -> (Acme, W24, Tagline); None if it's not a launch
    of a named company. See tests/connectors/test_hn_titles.py for the shapes handled."""
    title = " ".join(html.unescape(title).split())
    m = _PREFIX_RE.match(title)
    if not m:
        return None
    body = title[m.end() :].strip()
    if not body or body.count("(YC") > 1 or "[name-redacted]" in body:
        return None

    q = _QUALIFIED_RE.match(body)
    if q and not q.group("name").startswith("["):
        name, qualifier, rest = q.group("name").strip(), q.group("q").strip(), q.group("rest").strip()
        b = _BATCH_RE.search(qualifier)
        batch = f"{b.group('b').upper()}{b.group('y')}" if b else None
        tagline = _LEADING_SEP_RE.sub("", rest).strip() or None
        if batch is not None or (tagline is not None and _LEADING_SEP_RE.match(rest)):
            # "Acme (YC W24) – x", "Acme (YC W24): x", "Acme (YC W24) x", "Acme (Not YC Yet) – x"
            return _checked(name, batch, tagline, qualifier)
        # Otherwise the parentheses belong to a description ("Synbio School (Synthetic Biology)",
        # "Calenvy.com – scheduling powered by email (like I Want Sandy)"): try separators.

    br = _BRACKETED_RE.match(body)
    if br:
        return _checked(br.group("name").strip(), None, br.group("rest").strip(), None)

    for i, sep in enumerate(_SEPARATORS):
        parts = sep.split(body, maxsplit=1)
        if len(parts) != 2:
            continue
        name, tagline = parts[0].strip(), parts[1].strip()
        if "(" in name:
            return None
        if i >= 3:  # comma / period: only "Short name, a longer tagline" with no further list
            if len(name.split()) > 3 or len(tagline.split()) < 3 or "," in tagline:
                return None
            if i == 4 and len(name.split()) > 2:
                return None
        return _checked(name, None, tagline, None)
    if re.fullmatch(r"[A-Za-z][\w.-]*[A-Za-z0-9]", body) and (
        any(c.isupper() for c in body[1:]) or "." in body
    ):
        return _checked(body, None, None, None)  # "Launch HN: FairFunders": one CamelCase/dotted token
    return None  # "Prompt Engineering as a Service" is a description, not a name


def _checked(name: str, batch: str | None, tagline: str | None, qualifier: str | None) -> ParsedTitle | None:
    name = name.strip(" -–—:,.")
    words = name.split()
    if not words or len(words) > MAX_NAME_WORDS:
        return None
    if words[0].lower() in _STOP_FIRST_WORDS and not (batch and words[0].lower() == "the" and len(words) > 1):
        return None  # "A directory for ..." / "I built ..." -- but "The Juggernaut (YC W19)" is a name
    if not any(ch.isalnum() for ch in name) or name.startswith(("(", "[")):
        return None
    if tagline is not None:
        tagline = tagline.strip() or None
    return ParsedTitle(name=name, batch=batch, tagline=tagline, qualifier=qualifier)


# ── company domain / GitHub owner (pure) ──────────────────────────────────

# Hosts that are platforms, not a company's own site: a link there says nothing about the
# company's domain (github.com/acme/x is Acme's repo, not Acme's website).
PLATFORM_HOSTS = frozenset(
    {
        "github.com",
        "gitlab.com",
        "bitbucket.org",
        "github.io",
        "gitlab.io",
        "huggingface.co",
        "youtube.com",
        "youtu.be",
        "vimeo.com",
        "loom.com",
        "twitch.tv",
        "medium.com",
        "substack.com",
        "dev.to",
        "hashnode.dev",
        "notion.so",
        "notion.site",
        "producthunt.com",
        "ycombinator.com",
        "news.ycombinator.com",
        "bookface.ycombinator.com",
        "twitter.com",
        "x.com",
        "t.co",
        "linkedin.com",
        "facebook.com",
        "instagram.com",
        "tiktok.com",
        "reddit.com",
        "discord.gg",
        "discord.com",
        "slack.com",
        "t.me",
        "apps.apple.com",
        "itunes.apple.com",
        "play.google.com",
        "chrome.google.com",
        "chromewebstore.google.com",
        "addons.mozilla.org",
        "marketplace.visualstudio.com",
        "docs.google.com",
        "drive.google.com",
        "forms.gle",
        "sites.google.com",
        "goo.gl",
        "bit.ly",
        "tinyurl.com",
        "calendly.com",
        "cal.com",
        "typeform.com",
        "airtable.com",
        "imgur.com",
        "i.imgur.com",
        "gist.github.com",
        "raw.githubusercontent.com",
        "npmjs.com",
        "pypi.org",
        "crates.io",
        "arxiv.org",
        "wikipedia.org",
        "en.wikipedia.org",
        "kickstarter.com",
        "indiegogo.com",
        "gumroad.com",
        "wordpress.com",
        "blogspot.com",
        "herokuapp.com",
        "vercel.app",
        "netlify.app",
        "replit.com",
        "repl.co",
        "glitch.me",
        "techcrunch.com",
        "venturebeat.com",
        "wired.com",
        "nytimes.com",
        "bloomberg.com",
        "forbes.com",
        "businessinsider.com",
        "theverge.com",
        "wsj.com",
        "cnbc.com",
        "docsend.com",
        "dropbox.com",
        "figma.com",
        "canva.com",
        "webflow.io",
        "framer.website",
        "framer.app",
        "carrd.co",
        "wixsite.com",
        "squarespace.com",
        "shopify.com",
        "myshopify.com",
        "stripe.com",
        "buy.stripe.com",
        "openai.com",
        "chatgpt.com",
        "anthropic.com",
        "claude.ai",
    }
)
# Hosts of platforms that hand out subdomains to customers ("acme.github.io").
PLATFORM_SUFFIXES = (
    ".github.io",
    ".gitlab.io",
    ".substack.com",
    ".medium.com",
    ".notion.site",
    ".herokuapp.com",
    ".vercel.app",
    ".netlify.app",
    ".webflow.io",
    ".framer.website",
    ".framer.app",
    ".wixsite.com",
    ".myshopify.com",
    ".blogspot.com",
    ".wordpress.com",
    ".repl.co",
    ".glitch.me",
    ".carrd.co",
    ".streamlit.app",
    ".pages.dev",
    ".fly.dev",
    ".onrender.com",
)
# Leading labels that name a part of a site, not a different company: app.acme.com -> acme.com
SITE_SUBDOMAINS = frozenset(
    {
        "www",
        "app",
        "docs",
        "blog",
        "about",
        "beta",
        "dashboard",
        "console",
        "web",
        "home",
        "en",
        "us",
        "landing",
        "cloud",
        "get",
        "try",
        "go",
        "my",
        "portal",
        "platform",
        "studio",
        "demo",
        "signup",
        "login",
        "auth",
        "api",
        "developers",
        "dev",
        "help",
        "support",
        "status",
    }
)
_TWO_LEVEL_SUFFIXES = frozenset({"co.uk", "com.au", "co.in", "com.br", "co.jp", "co.nz", "com.sg", "co.za"})
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_BARE_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_NAME_SUFFIXES = (
    "labs",
    "lab",
    "ai",
    "hq",
    "app",
    "inc",
    "io",
    "dev",
    "technologies",
    "technology",
    "tech",
    "security",
    "health",
    "data",
    "robotics",
    "bio",
    "co",
    "team",
    "cloud",
    "api",
    "software",
    "systems",
    "studio",
    "finance",
    "markets",
    "network",
    "platform",
    "solutions",
)
_HOST_PREFIXES = ("get", "try", "use", "join", "with", "go", "hello", "meet", "the", "my", "run", "hey", "on")


def host_of(url: object) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    s = url.strip()
    try:
        parts = urlsplit(s if re.match(r"^[a-z][a-z0-9+.-]*://", s, re.I) else f"https://{s}")
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        return None
    if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return None
    return host


def site_domain(url: object, *, allow_platform_root: bool = False) -> str | None:
    """The company domain a URL points at, or None when it points into a platform.

    ``https://app.acme.ai/login`` -> ``acme.ai``; ``https://github.com/acme/x`` -> None.
    With ``allow_platform_root`` a bare platform root (``https://github.com/``) is kept: on
    Wikidata that is the platform company's own website.
    Shared by the Wikidata and GitHub connectors.
    """
    host = host_of(url)
    if host is None:
        return None
    labels = host.split(".")
    while (
        len(labels) > 2 and labels[0] in SITE_SUBDOMAINS and ".".join(labels[-2:]) not in _TWO_LEVEL_SUFFIXES
    ):
        labels = labels[1:]
    host = ".".join(labels)
    platform = (
        host in PLATFORM_HOSTS
        or host.endswith(PLATFORM_SUFFIXES)
        or any(host.endswith("." + p) for p in PLATFORM_HOSTS)  # foo.slack.com, blog.medium.com
    )
    if platform:
        path = urlsplit(url if "://" in str(url) else f"https://{url}").path.strip("/")  # type: ignore[arg-type]
        if not (allow_platform_root and not path and host in PLATFORM_HOSTS):
            return None
    return host


def _host_label(host: str) -> str:
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_LEVEL_SUFFIXES:
        return labels[-3]
    return labels[-2] if len(labels) >= 2 else labels[0]


def _key(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(ch for ch in s if not unicodedata.combining(ch)).lower())


def name_matches_host(name: str, host: str) -> bool:
    """Does ``host`` look like the company's own site? ``Nao Labs`` ~ ``getnao.io``,
    ``Buy Me a Coffee`` ~ ``buymeacoffee.com``; ``Xix.ai`` !~ ``x.com``."""
    label = _key(_host_label(host))
    full = _key(name)
    if len(label) < 3 or len(full) < 2:
        return False
    bases = {
        full,
        _key(name.split()[0]) if len(name.split()) > 1 and len(_key(name.split()[0])) >= 4 else full,
    }
    for base in list(bases):
        for suf in _NAME_SUFFIXES:
            if base.endswith(suf) and len(base) - len(suf) >= 3:
                bases.add(base[: -len(suf)])
        if "." in name:  # "Fly.io" -> "fly", "Builder.io" -> "builder"
            bases.add(_key(name.split(".")[0]))
    bases = {b for b in bases if len(b) >= 3}
    for b in bases:
        if label == b or label in {p + b for p in _HOST_PREFIXES} or label in {b + s for s in _NAME_SUFFIXES}:
            return True
        if label == full or (label.startswith(b) and len(b) >= 5 and len(label) - len(b) <= 4):
            return True
    return False


def github_owner(url: object) -> str | None:
    """``https://github.com/acme/repo`` -> ``acme`` (None for non-repo pages)."""
    if not isinstance(url, str):
        return None
    m = re.match(
        r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))(?:[/?#]|$)", url.strip()
    )
    if not m or m.group(1).lower() in {
        "orgs",
        "topics",
        "features",
        "marketplace",
        "sponsors",
        "apps",
        "settings",
        "about",
        "login",
        "explore",
    }:
        return None
    return m.group(1)


def post_links(text: object) -> list[str]:
    """Links in an HN post body (HTML), in order, de-duplicated."""
    if not isinstance(text, str) or not text:
        return []
    raw = _HREF_RE.findall(text) + _BARE_URL_RE.findall(html.unescape(text))
    seen: dict[str, None] = {}
    for u in raw:
        u = html.unescape(u).strip().rstrip(".,;:")
        if u:
            seen.setdefault(u, None)
    return list(seen)


def company_identity(name: str, story_url: object, story_text: object) -> tuple[str | None, str | None]:
    """(domain, github owner) for the launching company.

    domain: the story URL's site unless it's a platform; else the first link in the post whose
    host matches the company name. github: the owner of a github.com story URL, or of a
    github.com link in the post whose owner matches the company name.
    """
    domain = site_domain(story_url)
    links = post_links(story_text)
    if domain is None:
        for link in links:
            d = site_domain(link)
            if d and name_matches_host(name, d):
                domain = d
                break
    github = github_owner(story_url)
    if github is None:
        for link in links:
            owner = github_owner(link)
            if owner and name_matches_host(name, owner + ".x"):
                github = owner
                break
    return domain, github


_TRACKING_PARAM_RE = re.compile(r"^(?:utm_[a-z]+|ref|source|src|via)$", re.I)


def _launch_url(url: object) -> str | None:
    """The story URL as the launch's ``url`` fact, unless the url normalizer (which drops the
    query) would lose what makes it specific, e.g. ``youtube.com/watch?v=...``."""
    if not isinstance(url, str) or host_of(url) is None:
        return None
    parts = urlsplit(url.strip())
    params = [kv.split("=", 1)[0] for kv in parts.query.split("&") if kv]
    if any(not _TRACKING_PARAM_RE.match(k) for k in params):
        return None
    return url.strip()


# ── polite HTTP (shared with the wikidata, github and x connectors) ───────


class HttpGiveUp(RuntimeError):
    pass


def retry_after_s(resp: httpx.Response) -> float | None:
    """Seconds the server asked us to wait (``Retry-After``, or ``x-ratelimit-reset`` /
    ``x-rate-limit-reset`` epoch seconds on 403/429). Not capped: see ``MAX_WAIT_S``."""
    v = resp.headers.get("Retry-After")
    if v is not None:
        try:
            return max(0.0, float(v))
        except ValueError:
            return None
    reset = resp.headers.get("x-ratelimit-reset") or resp.headers.get("x-rate-limit-reset")
    if reset and resp.status_code in (403, 429):
        try:
            return max(0.0, float(reset) - time.time()) + 1.0
        except ValueError:
            return None
    return None


MAX_WAIT_S = 300.0  # a longer requested wait ends the run; the job queue retries it later


def get_json(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    log: Any,
    sleep: Callable[[float], None] = time.sleep,
    min_interval_s: float = 1.0,
    max_retries: int = MAX_RETRIES,
    retry_statuses: tuple[int, ...] = (429,),
    max_wait_s: float = MAX_WAIT_S,
) -> Any:
    """GET (or form POST when ``data`` is given) -> JSON.

    Sleeps ``min_interval_s`` before every request. Retries 429 / ``retry_statuses``, 5xx and
    network errors, waiting as long as ``Retry-After`` / rate-limit-reset asks, else with
    exponential backoff (2s, 4s, ..., 60s). Never retries earlier than the server asked: a
    requested wait longer than ``max_wait_s`` raises ``HttpGiveUp`` (the job is retried later
    by the queue's backoff). Other 4xx raise ``httpx.HTTPStatusError`` immediately.
    """
    error = ""
    for attempt in range(max_retries + 1):
        sleep(min_interval_s)
        try:
            if data is not None:
                resp = client.post(url, params=params, data=data, headers=headers)
            else:
                resp = client.get(url, params=params, headers=headers)
        except httpx.TransportError as e:
            error, wait = repr(e), None
        else:
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code < 500 and resp.status_code not in retry_statuses:
                resp.raise_for_status()
            error, wait = f"HTTP {resp.status_code}", retry_after_s(resp)
            if wait is not None and wait > max_wait_s:
                raise HttpGiveUp(f"{url}: {error}, server asks to wait {wait:.0f}s (> {max_wait_s:.0f}s)")
        if attempt == max_retries:
            break
        delay = wait if wait is not None else min(60.0, 2.0 * 2**attempt)
        log.warning("%s: %s; retry %d/%d in %.0fs", url, error, attempt + 1, max_retries, delay)
        sleep(delay)
    raise HttpGiveUp(f"{url}: giving up after {max_retries + 1} attempts: {error}")
