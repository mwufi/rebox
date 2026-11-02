"""Model output -> evidence-backed ``ClaimInput``s. Pure: no network, no DB.

Everything ``extract`` needs is in the parser's raw record: the input exactly as the model saw
it (``input``), the model's answer (``output``) and what the graph already knew about the
entities it names (``graph``, looked up at fetch time). Replays therefore give the same claims
without calling the model again.

Guards, applied to every fact before it becomes a claim:

- **evidence**: ``quote`` must be an exact substring of the input text, else the fact is dropped
  (hallucination guard). Every claim carries ``evidence = [{url, author, quote, observed_at}]``.
- **privacy**: a fact whose quote, title or names contain an email address or a phone number is
  dropped; people other than the account owner only appear when written as an ``@handle`` that is
  in the text.
- **grounding**: organization names must appear in the text (case-insensitive) and ``@handles``
  must be written in the text; a role title that is not in the text is dropped (the role is kept).
- **relevance** (articles, S18): a milestone is kept only when the company is named in the
  article title or lead and its quote (or the title) names it (``relevance.about``); else it is
  dropped as ``irrelevant article``. One article never yields the same (kind, normalized title,
  date) milestone twice (``duplicate milestone``).

Entities:

- account owner: ``x_id``/``x_handle`` (X) or ``github`` (GitHub). Its kind is the kind the
  graph already has for that identity; only an account the graph does not know yet uses the
  model's ``account`` guess (``other`` -> no facts about the owner).
- organization by handle: company with identity ``x_handle`` (X) or ``github`` (GitHub bios,
  where ``@org`` means a GitHub organization); by name: the one company of that exact name in the
  graph if unique, else a name-only company ref with the account owner as context.
- city: place with identity ``wikidata`` from ``places.CITIES`` (others are skipped).

An entity the graph did not know at fetch time gets an explicit ``name`` claim (with evidence),
so the resolver never has to add an evidence-less name claim for it; known entities get none.
One referenced by ``@handle`` also gets its handle as an identity claim (``x_handle``, or
``github`` for a GitHub org; never the profile's own handle), with the same evidence: the
resolver mints it from that key, and without the claim it would look name-only forever (S18).
The same holds for a company minted from an article's ``domain`` (unless a same-named company in
the graph already has that domain) and a place minted from its ``wikidata`` QID (unless the graph
had it at fetch time): the key is written as an identity claim (S19).
"""

from __future__ import annotations

import re
from typing import Any

from ..models import ClaimInput, EntityRef
from ..normalize import normalize_name
from . import relevance
from .places import canonical_city
from .prompt import InputDoc
from .schema import Acquisition, Hire, Location, Milestone, Role, parse_facts

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\w$€£])\+?\(?\d{1,4}\)?(?:[\s.-]?\(?\d{2,4}\)?){2,5}(?![\w%])")
URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|ai|io|co|dev|app|so|xyz|org|net|tech|inc|hq|health|bio)\b[^\s)\]]*",
    re.I,
)
# "NYC → SF", "LA to SF": two short place-like phrases around an arrow or "to"
MOVE_RE = re.compile(r"([A-Za-z][A-Za-z ]{0,24}?)\s*(?:→|->|=>|>|\bto\b)\s*([A-Za-z][A-Za-z ]{0,24})")
INVESTING_TITLE = re.compile(r"(?i)investing|investor|angel|backing")
FOUNDER_PREFIX = re.compile(r"(?i)^(co-?\s?founder|cofounder|founder)\s*(&|and|/|,)\s*")
# Past-tense markers in a role quote ("ex-", "prev", "formerly", "founded"), not inside handles.
PAST_MARKER = re.compile(
    r"(?i)(?<![\w-])(ex|prev|previously|formerly|former|fmr|past|alum|used to|was)(?![\w])"
    r"|(?<![\w-])ex-|\bfounded\b"
)
FOUNDER_TITLE = re.compile(r"(?i)(co-?\s?)?founder|founding (ceo|team)|cofounder")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{1,39}$")
# ``Extraction.dropped`` reasons that ``exa_parse`` counts in its run stats.
IRRELEVANT = "irrelevant article"
DUPLICATE_MILESTONE = "duplicate milestone"


def has_private_data(*texts: str) -> bool:
    for t in texts:
        if not t:
            continue
        if EMAIL_RE.search(t):
            return True
        for m in PHONE_RE.finditer(t):
            digits = re.sub(r"\D", "", m.group(0))
            if len(digits) >= 9 and not re.fullmatch(r"(19|20)\d{2}(19|20)\d{2}", digits):
                return True
    return False


def handle_in_text(h: str, text: str) -> bool:
    return bool(re.search(rf"(?<![\w@])@{re.escape(h)}(?![\w-])", text, re.I))


class Extraction:
    """Builds the claims for one parsed input."""

    def __init__(self, source: str, payload: dict[str, Any]) -> None:
        inp = payload["input"]
        self.source = source
        self.doc = InputDoc(
            ref=payload["input_ref"], kind=inp["kind"], fields=inp["fields"], meta=inp["meta"]
        )
        self.text = self.doc.text()
        self.lower = self.text.casefold()
        self.output: dict[str, Any] | None = payload.get("output")
        self.graph: dict[str, Any] = payload.get("graph") or {}
        self.claims: list[ClaimInput] = []
        self.dropped: list[tuple[str, str]] = []  # (reason, quote) — for tests and eval
        self._named: set[str] = set()
        self._milestones: set[tuple[str, str, str]] = set()  # (kind, normalized title, date), articles
        m = self.doc.meta
        self.url = str(m.get("url") or "")
        self.author = m.get("author")
        self.date = m.get("date") or m.get("observed")
        self.handle_platform = "github" if self.doc.kind == "github_bio" else "x_handle"
        self.subject = self._subject()

    # ── entities ──

    def _subject(self) -> EntityRef | None:
        m, out = self.doc.meta, self.output or {}
        if self.doc.kind == "article":
            return None
        if self.doc.kind == "github_bio":
            known = self.graph.get(f"github:{str(m['github']).lower()}")
            ref = EntityRef(
                kind="person",
                identity={"github": m["github"], "x_handle": m.get("x_handle")},
                name=None if known else (self.doc.fields.get("name") or m["github"]),
            )
            self._subject_known = bool(known)
            return ref
        kinds: list[str] = []
        for key in (f"x_id:{m.get('x_id')}", f"x_handle:{m.get('handle')}"):
            kinds = [k for k in self.graph.get(key) or [] if k in ("person", "company")]
            if kinds:
                break
        kinds = kinds or [k for k in m.get("graph_kinds") or [] if k in ("person", "company")]
        guess = out.get("account")
        if len(kinds) == 1:
            kind = kinds[0]
        elif kinds:
            kind = guess if guess in kinds else "person"
        elif guess in ("person", "company"):
            kind = guess
        else:
            return None
        if not m.get("handle") and not m.get("x_id"):
            return None
        self._subject_known = bool(kinds)
        name = self.doc.fields.get("name") or m.get("author_name") or m.get("handle")
        return EntityRef(
            kind=kind,
            identity={"x_id": m.get("x_id"), "x_handle": m.get("handle")},
            name=None if kinds else name,
        )

    def org(self, name: str, handle: str, *, quote: str, platform: str | None = None) -> EntityRef | None:
        """A company named in the text, by handle (preferred) or by name."""
        platform = platform or self.handle_platform
        handle = handle.strip().lstrip("@")
        name = " ".join(name.split())
        if not handle and name.lstrip("@") and handle_in_text(name.lstrip("@"), self.text):
            handle = name.lstrip("@")  # the model put "@acme" (or "acme" written as @acme) in org
            name = ""
        own = str(self.doc.meta.get("handle") or "").lower()
        if handle and not handle_in_text(handle, self.text) and not (handle.lower() == own and name):
            if not name:
                return self._drop("org handle not in text", quote)
            handle = ""  # an invented handle: fall back to the written name
        if handle:
            if not _HANDLE_RE.match(handle):
                return self._drop("org handle malformed", quote)
            if handle.lower() == str(self.doc.meta.get("handle") or "").lower() and platform == "x_handle":
                # the account itself: a company names itself in its own posts
                return self.subject if self.subject is not None and self.subject.kind == "company" else None
            known = self.graph.get(f"{platform}:{handle.lower()}")
            if known and "company" not in known:
                return None  # that handle is a person (or something else) in the graph
            ref = EntityRef(
                kind="company", identity={platform: handle}, name=None if known else (name or handle)
            )
            if not known:
                self._name(ref, name or handle, quote, key=f"{platform}={handle.lower()}")
                self._handle_identity(ref, platform, handle, quote)
            return ref
        if not name or name.casefold() not in self.lower:
            return self._drop("org name not in text", quote) if name else None
        if self.is_own_name(name):
            return self.subject if self.subject is not None and self.subject.kind == "company" else None
        cands = [c if isinstance(c, dict) else {"id": c, "domains": []}
                 for c in self.graph.get(f"name:{normalize_name(name)}") or []]  # fmt: skip
        domain = self._own_domain(name)
        if domain is not None:
            hit = [c for c in cands if domain in c["domains"]]
            if len(hit) == 1:
                return EntityRef(id=hit[0]["id"])
            ref = EntityRef(kind="company", identity={"domain": domain}, name=name)
            self._name(ref, name, quote, key=f"domain={domain}")
            if not hit and not self.graph.get(f"domain:{domain}"):
                self._identity(ref, "domain", domain, quote)  # minted from a domain the graph lacks
            return ref
        if len(cands) == 1:
            return EntityRef(id=cands[0]["id"])
        if len(cands) > 1:
            return self._drop("ambiguous company name", quote)  # several same-named companies
        ctx = [self.subject] if self.subject is not None else None
        # A company new to the graph, known only by name: minted from this record (the resolver
        # keys identity-less mints by record + name). Articles have no account to scope it, so
        # the same new name in two articles is two entities until a later source gives it a key.
        ref = EntityRef(kind="company", name=name, context=ctx)
        self._name(ref, name, quote, key=f"name={normalize_name(name)}")
        return ref

    def _own_domain(self, name: str) -> str | None:
        """A domain in the article that is the company's own site: the article's host or a link
        in its text ("Acme (acme.ai)", "https://getacme.com"), matched by name."""
        if self.doc.kind != "article":
            return None
        from ..connectors.hn import name_matches_host, site_domain

        hosts = [str(self.doc.meta.get("host") or "")]
        hosts += [d for u in URL_RE.findall(self.text) if (d := site_domain(u))]
        for h in hosts:
            if h and name_matches_host(name, h):
                return h
        return None

    def is_own_name(self, name: str) -> bool:
        """``name`` is the account itself: its display name, or its handle ("Quillworks" for
        @quillworks_ai)."""
        if self.subject is None:
            return False
        key = re.sub(r"[^a-z0-9]", "", normalize_name(name))
        own_name = self.doc.fields.get("name") or self.doc.meta.get("author_name") or ""
        if own_name and key == re.sub(r"[^a-z0-9]", "", normalize_name(str(own_name))):
            return True
        h = re.sub(r"[^a-z0-9]", "", str(self.doc.meta.get("handle") or "").lower())
        return bool(h) and len(key) >= 4 and (h == key or (h.startswith(key) and len(h) - len(key) <= 4))

    def place(self, city: str, quote: str) -> EntityRef | None:
        hit = canonical_city(city)
        if hit is None:
            return self._drop(f"unknown city {city!r}", quote)
        name, qid = hit
        ref = EntityRef(
            kind="place", identity={"wikidata": qid}, name=None if self.graph.get(f"wikidata:{qid}") else name
        )
        if ref.name:  # a place new to the graph: minted from its QID, which it now carries
            self._name(ref, name, quote, key=f"wikidata={qid}")
            self._identity(ref, "wikidata", qid, quote)
        return ref

    def person_by_handle(self, handle: str, quote: str) -> EntityRef | None:
        handle = handle.strip().lstrip("@")
        own = str(self.doc.meta.get("handle") or "")
        if handle.lower() == own.lower() and self.subject is not None and self.subject.kind == "person":
            return self.subject
        if not _HANDLE_RE.match(handle) or not handle_in_text(handle, self.text):
            return self._drop("person handle not in text", quote)
        known = self.graph.get(f"x_handle:{handle.lower()}")
        if known and "person" not in known:
            return None
        ref = EntityRef(kind="person", identity={"x_handle": handle}, name=None if known else handle)
        if not known:
            self._name(ref, handle, quote, key=f"x_handle={handle.lower()}")
            self._handle_identity(ref, "x_handle", handle, quote)
        return ref

    # ── claims ──

    def evidence(self, quote: str) -> list[dict[str, Any]]:
        ev: dict[str, Any] = {"url": self.url, "quote": quote}
        if self.author:
            ev["author"] = self.author
        if self.date:
            ev["observed_at"] = self.date
        return [ev]

    def add(self, subject: EntityRef, predicate: str, key: str, quote: str, **kw: Any) -> None:
        ref = f"{self.source}:{self.doc.ref}:{predicate}:{key}"
        if any(c.source_ref == ref and c.subject == subject for c in self.claims):
            return  # the same fact from two model facts (e.g. a hire and a role): keep the first
        self.claims.append(
            ClaimInput(
                subject=subject,
                predicate=predicate,
                source_ref=f"{self.source}:{self.doc.ref}:{predicate}:{key}",
                evidence=self.evidence(quote),
                **kw,
            )
        )

    def _name(self, ref: EntityRef, name: str, quote: str, *, key: str) -> None:
        if key in self._named:
            return
        self._named.add(key)
        self.add(ref, "name", key, quote, value=name)

    def _handle_identity(self, ref: EntityRef, platform: str, handle: str, quote: str) -> None:
        """An entity new to the graph, referenced by ``@handle``: also write the handle as its
        identity claim (``x_handle``, or ``github`` for a GitHub org), with the referencing
        quote as evidence. Without it the resolver mints the entity from the handle but nothing
        records the key, so it looks name-only forever (S18). Never for the profile's own handle
        (the owner's identity is ``_own_identity``'s)."""
        m = self.doc.meta
        own = {str(m.get("handle") or "").lower(), str(m.get("github") or "").lower()} - {""}
        if handle.lower() in own:
            return
        self._identity(ref, platform, handle, quote)

    def _identity(self, ref: EntityRef, predicate: str, value: str, quote: str) -> None:
        """Write the key a new entity is minted from as its identity claim (once per key), with
        the referencing quote as evidence: ``x_handle``/``github`` (@handles), ``domain`` (an
        article's company site) and ``wikidata`` (a city's QID). The resolver normalizes it."""
        key = f"{predicate}={value if predicate == 'wikidata' else value.lower()}"
        if f"id:{key}" in self._named:
            return
        self._named.add(f"id:{key}")
        self.add(ref, predicate, key, quote, value=value)

    def _strip_label(self, quote: str) -> str:
        """Models sometimes quote the rendered field label ("location: SF"); the value is the quote."""
        for label in self.doc.fields:
            prefix = f"{label}: "
            if quote.startswith(prefix) and quote not in self.text:
                return quote[len(prefix) :]
        return quote

    def _drop(self, reason: str, quote: str) -> None:
        self.dropped.append((reason, quote))
        return None

    def run(self) -> list[ClaimInput]:
        if not self.output:
            return []
        facts, bad = parse_facts(self.output.get("facts"))
        self.dropped.extend(("invalid fact", "") for _ in range(bad))
        before = len(self.claims)
        for f in facts:
            f.quote = self._strip_label(f.quote)
            if f.quote not in self.text:
                self._drop("quote not in input", f.quote)
                continue
            texts = [f.quote] + [v for k, v in f.model_dump().items() if isinstance(v, str) and k != "type"]
            if has_private_data(*texts):
                self._drop("private data", f.quote)
                continue
            if isinstance(f, Role):
                self._role(f)
            elif isinstance(f, Location):
                self._location(f)
            elif isinstance(f, Milestone):
                self._milestone(f)
            elif isinstance(f, Acquisition):
                self._acquisition(f)
            elif isinstance(f, Hire):
                self._hire(f)
        facts_written = len(self.claims) > before
        if facts_written and self.subject is not None and not getattr(self, "_subject_known", True):
            self._own_identity()
        return self.claims

    def _own_identity(self) -> None:
        """An account the graph did not know: also write its identity and name (with evidence)."""
        s, m = self.subject, self.doc.meta
        assert s is not None
        f = self.doc.fields
        # The account's own name (or, in a post, the first sentence) is the evidence of its
        # existence; never the whole bio, which may hold contact details.
        ev_quote = f.get("name") or ""
        if not ev_quote or has_private_data(ev_quote):
            first = re.split(r"(?<=[.!?])\s", f.get("text") or f.get("bio") or "")[0][:120]
            ev_quote = "" if has_private_data(first) else first
        if self.doc.kind == "github_bio":
            self.add(s, "github", "github", ev_quote, value=m["github"])
        else:
            if m.get("x_id"):
                self.add(s, "x_id", "x_id", ev_quote, value=str(m["x_id"]))
            if m.get("handle"):
                self.add(s, "x_handle", "x_handle", ev_quote, value=m["handle"])
        if s.name:
            self.add(s, "name", "subject", ev_quote, value=s.name)

    # ── per fact type ──

    def _role(self, f: Role) -> None:
        if self.subject is None or self.subject.kind != "person" or self.doc.kind == "article":
            return
        if self.doc.kind == "x_post":
            # "I'm joining @x as Partner" in a post: a dated hire of the author (current roles only)
            if f.status == "current" and not f.founder:
                self._hire(
                    Hire(type="hire", person_handle=str(self.doc.meta.get("handle") or ""), org=f.org,
                         org_handle=f.org_handle, title=f.title, quote=f.quote)
                )  # fmt: skip
            return
        company = self.org(f.org, f.org_handle, quote=f.quote)
        if company is None:
            return
        title = f.title if f.title and f.title.casefold() in self.lower else ""
        if FOUNDER_TITLE.fullmatch(title) or INVESTING_TITLE.fullmatch(title):
            title = ""  # "co-founder" is the founded edge; "investing" is no title
        title = FOUNDER_PREFIX.sub("", title)  # "co-founder & CEO" -> "CEO"
        if f.investor and not title:
            title = "Investor"
        key = _ref_key(company)
        value = {"title": title} if title else None
        past = f.status == "past"
        if past and f.founder and not PAST_MARKER.search(f.quote):
            past = False  # "co-founder @x" with no ex-/prev/former: the model guessed the tense
        valid_to = self.date if past else None
        if not f.founder or title or past:
            self.add(self.subject, "worked_at", key, f.quote, object=company, value=value, valid_to=valid_to)
        if f.founder:
            self.add(self.subject, "founded", key, f.quote, object=company)

    def _location(self, f: Location) -> None:
        if self.subject is None or self.doc.kind in ("x_post", "article"):
            return
        if self._move_origin(f.place):
            return self._drop("origin of a move", f.quote)  # "NYC → SF": only the destination
        place = self.place(f.place, f.quote)
        if place is not None:
            self.add(self.subject, "based_in", _ref_key(place), f.quote, object=place)

    def _move_origin(self, place: str) -> bool:
        """``place`` is where the account moved from ("NYC → SF", "LA to SF") in its text."""
        city = canonical_city(place)
        for line in self.text.splitlines():
            for m in MOVE_RE.finditer(line):
                src = _city_suffix(m.group(1))
                dst = _city_prefix(m.group(2))
                if src and dst and src == city and dst != city:
                    return True
        return False

    def _milestone(self, f: Milestone) -> None:
        if not self.date:
            return
        if self.doc.kind == "article":
            # Relevance (S18): only articles *about* the company, quoted naming it.
            company_name = f.org or f.org_handle
            fields = self.doc.fields
            if company_name and not relevance.about(
                company_name, title=fields.get("title") or "", text=fields.get("text") or "", quote=f.quote
            ):
                return self._drop(IRRELEVANT, f.quote)
            # The same milestone twice from one article (e.g. once per spelling of the company): one.
            key = (f.kind, _norm_title(f.title), str(self.date))
            if key in self._milestones:
                return self._drop(DUPLICATE_MILESTONE, f.quote)
            self._milestones.add(key)
        value = {"title": f.title, "kind": f.kind, "url": self.url}
        subjects: list[EntityRef] = []
        company = self.org(f.org, f.org_handle, quote=f.quote) if (f.org or f.org_handle) else None
        if company is not None:
            subjects.append(company)
        elif self.subject is not None and self.subject.kind == "company":
            subjects.append(self.subject)
        if (
            f.first_person
            and self.subject is not None
            and self.subject.kind == "person"
            and self.doc.kind != "article"
        ):
            subjects.append(self.subject)
        for s in subjects:
            role = "author" if s is self.subject else _ref_key(s)
            self.add(s, "milestone", f"{f.kind}:{role}", f.quote, value=value, valid_from=self.date)
        if f.kind == "hire" and self.doc.kind == "x_post":
            self._hire_from_milestone(f)

    def _hire_from_milestone(self, f: Milestone) -> None:
        """A hire milestone implies the worked_at edge even when the model gave no ``hire`` fact:
        the author joining (first person), or the one @handle welcomed in the quote."""
        if any(c.predicate == "worked_at" for c in self.claims):
            return  # a hire or role fact already made it
        own = str(self.doc.meta.get("handle") or "").lower()
        if f.first_person and self.subject is not None and self.subject.kind == "person":
            person_handle = own
        else:
            others = {h.lower() for h in re.findall(r"@([A-Za-z0-9_]{1,15})", f.quote)} - {own}
            others -= {f.org_handle.lower().lstrip("@")}
            if len(others) != 1 or not re.search(r"(?i)\bwelcom", f.quote):
                return
            person_handle = others.pop()
        self._hire(
            Hire(type="hire", person_handle=person_handle, org=f.org, org_handle=f.org_handle, quote=f.quote)
        )

    def _acquisition(self, f: Acquisition) -> None:
        acquirer = self._company_or_self(f.acquirer, f.acquirer_handle, f.quote)
        target = self._company_or_self(f.target, f.target_handle, f.quote)
        if acquirer is None or target is None or _ref_key(acquirer) == _ref_key(target):
            return
        self.add(acquirer, "acquired", _ref_key(target), f.quote, object=target, valid_from=self.date)

    def _hire(self, f: Hire) -> None:
        if self.doc.kind == "github_bio":
            return
        person = self.person_by_handle(f.person_handle, f.quote)
        if person is None:
            return
        company = self._company_or_self(f.org, f.org_handle, f.quote)
        if company is None:
            return
        title = f.title if f.title and f.title.casefold() in self.lower else ""
        self.add(
            person,
            "worked_at",
            _ref_key(company),
            f.quote,
            object=company,
            value={"title": title} if title else None,
            valid_from=self.date,
        )

    def _company_or_self(self, name: str, handle: str, quote: str) -> EntityRef | None:
        if name or handle:
            return self.org(name, handle, quote=quote)
        if self.subject is not None and self.subject.kind == "company":
            return self.subject
        return None


def _city_suffix(s: str) -> tuple[str, str] | None:
    """The city at the end of ``s`` ("ex-Lyft NYC" -> NYC), trying the last 1-3 words."""
    words = s.split()
    for n in (3, 2, 1):
        if len(words) >= n and (hit := canonical_city(" ".join(words[-n:]))):
            return hit
    return None


def _city_prefix(s: str) -> tuple[str, str] | None:
    words = s.split()
    for n in (3, 2, 1):
        if len(words) >= n and (hit := canonical_city(" ".join(words[:n]))):
            return hit
    return None


def _norm_title(title: str) -> str:
    """Lowercase, punctuation to spaces (amounts keep their dot: "$9.7M" -> "9.7m"), collapsed."""
    return " ".join(re.sub(r"[^\w.]+|(?<!\d)\.|\.(?!\d)", " ", title.casefold()).split())


def _ref_key(ref: EntityRef) -> str:
    if ref.id is not None:
        return ref.id
    for k in ("wikidata", "domain", "github", "x_handle", "x_id"):
        v = ref.identity.get(k)
        if v:
            return f"{k}={str(v).lower()}"
    return f"name={normalize_name(ref.name or '')}"


def extract_claims(source: str, payload: dict[str, Any]) -> list[ClaimInput]:
    return Extraction(source, payload).run()


def extract_with_drops(
    source: str, payload: dict[str, Any]
) -> tuple[list[ClaimInput], list[tuple[str, str]]]:
    """The claims and the dropped facts ``(reason, quote)``, for run stats."""
    ex = Extraction(source, payload)
    claims = ex.run()
    return claims, ex.dropped
