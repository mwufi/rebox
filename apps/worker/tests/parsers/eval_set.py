# ruff: noqa: E501  (hand-written test data: one case per line)
"""The parser eval: 40 hand-labelled inputs, all synthetic (invented people, handles and companies,
written in the style of real SF bios, posts and launch articles; no real person's text).

Each case gives the upstream raw payload and the facts ``extract`` must produce, as compact
tuples (see ``fact_tuples`` in ``eval_parser.py``):

    ("worked_at", <org>, <title or "">, "current" | "past")
    ("founded", <org>)
    ("based_in", <city>)
    ("milestone", <subject>, <kind>)
    ("acquired", <acquirer>, <target>)
    ("hire", <@person>, <org>)

``<org>`` / ``<subject>`` is ``@handle`` for handle refs (lowercase), the name otherwise
(lowercase), or ``self`` for the account owner. Investor roles carry their written title or
"investor". Labels follow docs/PARSING.md (what to extract and what never to extract).
"""

from __future__ import annotations

import hashlib
from typing import Any


def _num(s: str, digits: int = 12) -> str:
    """A stable fake numeric id (``hash()`` is salted per process)."""
    return str(int(hashlib.sha256(s.encode()).hexdigest(), 16) % 10**digits)


def bio(
    handle: str, name: str, description: str, location: str = "", uid: str | None = None
) -> dict[str, Any]:
    return {
        "type": "x_user",
        "user": {
            "id": uid or _num(handle),
            "username": handle,
            "name": name,
            "description": description,
            "location": location,
        },
    }


def post(
    handle: str, text: str, created: str = "2026-10-20T16:00:00.000Z", tid: str | None = None
) -> dict[str, Any]:
    uid = _num(handle)
    return {
        "type": "x_post",
        "post": {"id": tid or _num(text, 15), "text": text, "created_at": created, "author_id": uid},
        "author": {"id": uid, "username": handle},
    }


def gh(login: str, name: str, bio_text: str, company: str = "", location: str = "") -> dict[str, Any]:
    return {
        "user": {
            "login": login,
            "type": "User",
            "name": name,
            "bio": bio_text,
            "company": company,
            "location": location,
        },
        "orgs": {},
    }


def article(url: str, title: str, text: str, date: str = "2026-10-15T00:00:00.000Z") -> dict[str, Any]:
    return {
        "type": "exa_result",
        "url": url,
        "title": title,
        "published_date": date,
        "homepage": False,
        "text": text,
    }


# (case id, upstream source, account kind in the graph or None, payload, expected facts)
CASES: list[tuple[str, str, str | None, dict[str, Any], set[tuple[str, ...]]]] = [
    # ── X bios ──
    (
        "bio-01",
        "x_spider",
        None,
        bio(
            "mara_quill_ex",
            "Mara Quill",
            "cofounder & CEO @quillworks_ex. ex-Stripe, prev eng @plaidtech_ex",
            "SF",
        ),
        {
            ("worked_at", "@quillworks_ex", "CEO", "current"),
            ("founded", "@quillworks_ex"),
            ("worked_at", "stripe", "", "past"),
            ("worked_at", "@plaidtech_ex", "eng", "past"),
            ("based_in", "San Francisco"),
        },
    ),
    (
        "bio-02",
        "x_spider",
        None,
        bio(
            "devonk_ex",
            "Devon Kale",
            "building agents at @loomline_ex | previously ML at Northwind Labs | Bay Area",
        ),
        {
            ("worked_at", "@loomline_ex", "", "current"),
            ("worked_at", "northwind labs", "ML", "past"),
            ("based_in", "San Francisco"),
        },
    ),
    (
        "bio-03",
        "x_spider",
        None,
        bio(
            "priya_inv_ex",
            "Priya Raman",
            "Partner @cobaltvc_ex. investing in dev tools + infra. formerly product @figtree_ex",
            "San Francisco, CA",
        ),
        {
            ("worked_at", "@cobaltvc_ex", "Partner", "current"),
            ("worked_at", "@figtree_ex", "product", "past"),
            ("based_in", "San Francisco"),
        },
    ),
    (
        "bio-04",
        "x_spider",
        None,
        bio("sam_builds_ex", "Sam Ortiz", "dad, runner, coffee snob. opinions my own ☕️", "Earth"),
        set(),
    ),
    (
        "bio-05",
        "x_spider",
        "company",
        bio(
            "quillworks_ex",
            "Quillworks",
            "Quillworks builds AI agents for finance teams. Backed by @cobaltvc_ex. We're hiring!",
            "San Francisco",
        ),
        {("based_in", "San Francisco")},
    ),
    (
        "bio-06",
        "x_spider",
        None,
        bio("lena_ops_ex", "Lena Park", "Head of Ops at Brightwave. Ex-Uber, ex-Lyft. NYC → SF"),
        {
            ("worked_at", "brightwave", "Head of Ops", "current"),
            ("worked_at", "uber", "", "past"),
            ("worked_at", "lyft", "", "past"),
            ("based_in", "San Francisco"),
        },
    ),
    (
        "bio-07",
        "x_spider",
        None,
        bio(
            "tomas_eng_ex",
            "Tomás Reyes",
            "founding engineer @driftlabs_ex. Stanford CS '19. email me: tomas@driftlabs.example",
            "Palo Alto",
        ),
        {("worked_at", "@driftlabs_ex", "founding engineer", "current"), ("based_in", "Palo Alto")},
    ),
    (
        "bio-08",
        "x_spider",
        None,
        bio(
            "angel_jo_ex",
            "Jo Lin",
            "angel investor. 2x founder (sold to Acme). writing at jolin.example",
            "Oakland, CA",
        ),
        {("based_in", "Oakland")},
    ),
    (
        "bio-09",
        "x_spider",
        None,
        bio(
            "ceo_nadia_ex",
            "Nadia Brook",
            "CEO and co-founder of @parcel_ai_ex (YC W25). Previously co-founded Tinyfox (acq. by Dropbox).",
            "SF / NYC",
        ),
        {
            ("worked_at", "@parcel_ai_ex", "CEO", "current"),
            ("founded", "@parcel_ai_ex"),
            ("founded", "tinyfox"),
            ("worked_at", "tinyfox", "", "past"),
            ("based_in", "San Francisco"),
            ("based_in", "New York City"),
        },
    ),
    (
        "bio-10",
        "x_spider",
        None,
        bio(
            "ravi_gp_ex",
            "Ravi Shah",
            "GP at Lattice Ventures. Seed + Series A in AI infra. DMs open",
            "Menlo Park",
        ),
        {("worked_at", "lattice ventures", "GP", "current"), ("based_in", "Menlo Park")},
    ),
    (
        "bio-11",
        "x_spider",
        None,
        bio(
            "kai_research_ex",
            "Kai Moreno",
            "research scientist @opalai_ex | PhD @ Berkeley | prev @deepforge_ex intern",
            "berkeley",
        ),
        {
            ("worked_at", "@opalai_ex", "research scientist", "current"),
            ("worked_at", "@deepforge_ex", "intern", "past"),
            ("based_in", "Berkeley"),
        },
    ),
    (
        "bio-12",
        "x_spider",
        None,
        bio(
            "ella_design_ex",
            "Ella Grant",
            "design @figtree_ex 🎨 call me at +1 (415) 555-0134 for freelance",
            "sf bay area",
        ),
        {("worked_at", "@figtree_ex", "design", "current"), ("based_in", "San Francisco")},
    ),
    (
        "bio-13",
        "x_spider",
        None,
        bio(
            "ben_sre_ex",
            "Ben Holt",
            "SRE. formerly @cloudnine_ex, @stackpath_ex. now: stealth",
            "Seattle, WA",
        ),
        {
            ("worked_at", "@cloudnine_ex", "", "past"),
            ("worked_at", "@stackpath_ex", "", "past"),
            ("based_in", "Seattle"),
        },
    ),
    (
        "bio-14",
        "x_spider",
        None,
        bio(
            "parody_ceo_ex",
            "Not A Real CEO",
            "parody account. I fire people for fun. not affiliated with anyone",
            "the internet",
        ),
        set(),
    ),
    (
        "bio-15",
        "x_spider",
        None,
        bio(
            "amy_pm_ex",
            "Amy Cho",
            "PM at Notionly. ex-Google. I write about product at amycho.example",
            "San Francisco",
        ),
        {
            ("worked_at", "notionly", "PM", "current"),
            ("worked_at", "google", "", "past"),
            ("based_in", "San Francisco"),
        },
    ),
    (
        "bio-16",
        "x_spider",
        None,
        bio(
            "li_founder_ex",
            "Li Wen",
            "Founder, Mosaic Health. Building the OS for clinics. Previously @ Verily",
            "LA",
        ),
        {("founded", "mosaic health"), ("worked_at", "verily", "", "past"), ("based_in", "Los Angeles")},
    ),
    # ── GitHub bios ──
    (
        "gh-01",
        "github",
        None,
        gh(
            "octo-ex-1",
            "Rosa Diaz",
            "Staff engineer. Previously at Heroku and Segment.",
            "Vercel",
            "San Francisco",
        ),
        {
            ("worked_at", "vercel", "", "current"),
            ("worked_at", "heroku", "", "past"),
            ("worked_at", "segment", "", "past"),
            ("based_in", "San Francisco"),
        },
    ),
    (
        "gh-02",
        "github",
        None,
        gh("octo-ex-2", "Omar F", "co-founder @tinyrocket-ex · rust · wasm", "", "Berlin"),
        {("founded", "@tinyrocket-ex"), ("based_in", "Berlin")},
    ),
    ("gh-03", "github", None, gh("octo-ex-3", "", "I like compilers.", "", "Mars"), set()),
    (
        "gh-04",
        "github",
        None,
        gh("octo-ex-4", "Grace H", "CTO at Fernhill. ex-Stripe infra.", "Fernhill", "SF"),
        {
            ("worked_at", "fernhill", "CTO", "current"),
            ("worked_at", "stripe", "", "past"),
            ("based_in", "San Francisco"),
        },
    ),
    # ── X posts ──
    (
        "post-01",
        "x_spider",
        "company",
        post(
            "quillworks_ex",
            "Today we're launching Quillworks Agents: AI that closes your books in hours, not weeks. Try it free →",
        ),
        {("milestone", "self", "launch")},
    ),
    (
        "post-02",
        "x_spider",
        "company",
        post(
            "quillworks_ex",
            "We raised a $14M Series A led by @cobaltvc_ex to bring agents to every finance team. Thank you to our customers 🙏",
        ),
        {("milestone", "self", "funding")},
    ),
    (
        "post-03",
        "x_spider",
        "company",
        post(
            "loomline_ex", "Welcome @devonk_ex to @loomline_ex as our new Head of AI! Thrilled to have you."
        ),
        {("hire", "@devonk_ex", "self"), ("milestone", "self", "hire")},
    ),
    (
        "post-04",
        "x_spider",
        "person",
        post("mara_quill_ex", "hot take: tabs > spaces and I will die on this hill"),
        set(),
    ),
    (
        "post-05",
        "x_spider",
        "company",
        post(
            "parcel_ai_ex", "We're hiring! Looking for senior backend engineers in SF. Apply at the link 👇"
        ),
        set(),
    ),
    (
        "post-06",
        "x_spider",
        "company",
        post(
            "figtree_ex",
            "Big news: @figtree_ex has acquired @sketchpad_ex. The Sketchpad team joins us to build the future of design.",
        ),
        {("acquired", "self", "@sketchpad_ex"), ("milestone", "self", "acquisition")},
    ),
    (
        "post-07",
        "x_spider",
        "person",
        post(
            "priya_inv_ex",
            "Thrilled to announce I'm joining @cobaltvc_ex as a Partner! Excited to back founders building infra.",
        ),
        {
            ("hire", "@priya_inv_ex", "@cobaltvc_ex"),
            ("milestone", "self", "hire"),
            ("milestone", "@cobaltvc_ex", "hire"),
        },
    ),
    (
        "post-08",
        "x_spider",
        "company",
        post(
            "opalai_ex",
            "Opal 3 is out today: our fastest model yet, now available in the API and on our app.",
        ),
        {("milestone", "self", "product")},
    ),
    (
        "post-09",
        "x_spider",
        "person",
        post("ravi_gp_ex", "Founders: stop raising on vibes. Know your metrics. A thread 🧵"),
        set(),
    ),
    (
        "post-10",
        "x_spider",
        "company",
        post(
            "driftlabs_ex",
            "Drift Labs won Best Developer Tool at DevAwards 2026 🏆 thank you to the whole team!",
        ),
        {("milestone", "self", "award")},
    ),
    (
        "post-11",
        "x_spider",
        "person",
        post(
            "nadia_ceo_ex",
            "Personal news: after 4 amazing years I've left Stripe. Next chapter: starting something new.",
        ),
        set(),
    ),
    (
        "post-12",
        "x_spider",
        "company",
        post("cobaltvc_ex", "Proud to lead the $6M seed for @tinyrocket_ex. Welcome to the Cobalt family!"),
        {("milestone", "@tinyrocket_ex", "funding")},
    ),
    (
        "post-13",
        "x_spider",
        "company",
        post("brightwave_ex", "Giveaway time! RT and follow to win a Brightwave hoodie. Ends Friday."),
        set(),
    ),
    (
        "post-14",
        "x_spider",
        "person",
        post("lena_ops_ex", "Excited to share that I've joined @brightwave_ex as Head of Ops 🎉"),
        {
            ("hire", "@lena_ops_ex", "@brightwave_ex"),
            ("milestone", "self", "hire"),
            ("milestone", "@brightwave_ex", "hire"),
        },
    ),
    # ── Exa articles ──
    (
        "art-01",
        "exa",
        None,
        article(
            "https://techwire.example/2026/10/14/quillworks-series-a",
            "Quillworks raises $14M Series A to automate accounting",
            "San Francisco startup Quillworks has raised a $14 million Series A led by Cobalt Ventures. "
            "The company, founded in 2024 by Mara Quill, builds AI agents for finance teams. "
            "The new funding will be used to expand its engineering team, the company said.",
        ),
        {("milestone", "quillworks", "funding")},
    ),
    (
        "art-02",
        "exa",
        None,
        article(
            "https://techwire.example/2026/10/12/figtree-buys-sketchpad",
            "Figtree acquires Sketchpad in push into AI design",
            "Design platform Figtree has acquired Sketchpad, a two-year-old startup building AI sketching tools, "
            "for an undisclosed sum. Sketchpad's 12 employees will join Figtree.",
        ),
        {("acquired", "figtree", "sketchpad"), ("milestone", "figtree", "acquisition")},
    ),
    (
        "art-03",
        "exa",
        None,
        article(
            "https://blog.loomline.example/introducing-loom-2",
            "Introducing Loom 2",
            "Today Loomline is releasing Loom 2, our second-generation agent runtime. Loom 2 is 3x faster and "
            "supports long-running tasks out of the box. Contact sales at sales@loomline.example.",
        ),
        {("milestone", "loomline", "product")},
    ),
    (
        "art-04",
        "exa",
        None,
        article(
            "https://opinions.example/2026/10/why-ai-agents-are-overhyped",
            "Why AI agents are overhyped",
            "Everyone is talking about agents. In this essay I argue that most of them are demos dressed up as "
            "products, and that the real work is still in data plumbing.",
        ),
        set(),
    ),
    (
        "art-05",
        "exa",
        None,
        article(
            "https://techwire.example/2026/10/09/parcel-launch",
            "Parcel launches AI dispatch for local couriers",
            "Parcel, a Y Combinator-backed startup from San Francisco, launched its AI dispatch product on Tuesday. "
            "CEO Nadia Brook said the company has signed 40 courier networks.",
        ),
        {("milestone", "parcel", "launch")},
    ),
    (
        "art-06",
        "exa",
        None,
        article(
            "https://techwire.example/2026/10/02/mosaic-health-hires-cto",
            "Mosaic Health hires former Verily exec as CTO",
            "Mosaic Health has hired Dana Whitfield, previously a director at Verily, as its chief technology officer. "
            "Whitfield will lead a team of 30 engineers.",
        ),
        {("milestone", "mosaic health", "hire")},
    ),
]

assert len(CASES) == 40, len(CASES)
