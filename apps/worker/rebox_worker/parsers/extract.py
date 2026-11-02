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
"""

from __future__ import annotations

import re
from typing import Any

from ..models import ClaimInput, EntityRef
from ..normalize import normalize_name
from .places import canonical_city
from .prompt import InputDoc
from .schema import Acquisition, Hire, Location, Milestone, Role, parse_facts

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\w$€£])\+?\(?\d{1,4}\)?(?:[\s.-]?\(?\d{2,4}\)?){2,5}(?![\w%])")
MOVE_RE = re.compile(r"(\S[^→>]*?)\s*(?:→|->|=>|>|\bto\b)\s*([A-Za-z][A-Za-z .]+)$")
INVESTING_TITLE = re.compile(r"(?i)investing|investor|angel|backing")
FOUNDER_PREFIX = re.compile(r"(?i)^(co-?\s?founder|cofounder|founder)\s*(&|and|/|,)\s*")
FOUNDER_TITLE = re.compile(r"(?i)(co-?\s?)?founder|founding (ceo|team)|cofounder")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{1,39}$")


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
            return ref
        if not name or name.casefold() not in self.lower:
            return self._drop("org name not in text", quote) if name else None
        if self.is_own_name(name):
            return self.subject if self.subject is not None and self.subject.kind == "company" else None
        ids = self.graph.get(f"name:{normalize_name(name)}") or []
        if len(ids) == 1:
            return EntityRef(id=ids[0])
        ctx = [self.subject] if self.subject is not None else None
        if self.doc.kind == "article" and self.doc.meta.get("host"):
            from ..connectors.hn import name_matches_host

            host = str(self.doc.meta["host"])
            if name_matches_host(name, host):
                ref = EntityRef(kind="company", identity={"domain": host}, name=name)
                self._name(ref, name, quote, key=f"domain={host}")
                return ref
        if ctx is None and self.doc.kind != "article":
            return self._drop("unknown company without context", quote)
        # Unknown name-only company: minted from this record (the resolver keys identity-less
        # mints by record + name), so one article mentioning it twice gives one entity.
        ref = EntityRef(kind="company", name=name, context=ctx)
        self._name(ref, name, quote, key=f"name={normalize_name(name)}")
        return ref

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
        if ref.name:
            self._name(ref, name, quote, key=f"wikidata={qid}")
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
        valid_to = self.date if past else None
        if not f.founder or title or past:
            self.add(self.subject, "worked_at", key, f.quote, object=company, value=value, valid_to=valid_to)
        if f.founder:
            self.add(self.subject, "founded", key, f.quote, object=company)

    def _location(self, f: Location) -> None:
        if self.subject is None or self.doc.kind in ("x_post", "article"):
            return
        move = MOVE_RE.search(f.quote)
        if move and canonical_city(f.place) != canonical_city(move.group(2).strip(" .,")):
            return self._drop("origin of a move", f.quote)  # "NYC → SF": only the destination
        place = self.place(f.place, f.quote)
        if place is not None:
            self.add(self.subject, "based_in", _ref_key(place), f.quote, object=place)

    def _milestone(self, f: Milestone) -> None:
        if not self.date:
            return
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
