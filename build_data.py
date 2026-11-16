#!/usr/bin/env python3
"""
Builds data.js for Reel Rare from Wikidata (free, no API key).

- Film pool: every feature/animated film with >= MIN_SITELINKS Wikipedia language
  editions. Sitelink count is the popularity signal used to rank answers.
- Question pool: director / actor / composer / studio / source-author / series /
  award / release-year prompts, each with its full list of valid answers.

Run:  python3 build_data.py      (takes ~1-3 minutes)
"""
import csv, io, re, json, os, sys, time, hashlib, urllib.error, urllib.parse, urllib.request, datetime
from collections import defaultdict

# QLever (University of Freiburg) serves a full, regularly refreshed Wikidata copy and is
# much faster than query.wikidata.org, which can rate-limit to 1 request/minute.
# Set WD_ENDPOINT=https://query.wikidata.org/sparql to use the official endpoint instead.
ENDPOINT = os.environ.get("WD_ENDPOINT", "https://qlever.dev/api/wikidata")
PREFIXES = """PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX schema: <http://schema.org/>
"""
UA = "ReelRareBuilder/1.0 (film trivia data build)"
MIN_SITELINKS = 6
MIN_ANSWERS = 8
# "Mainstream" gate: a question is only used if at least MIN_MAINSTREAM of its answers are
# well-known films (>= MAINSTREAM_SITELINKS Wikipedia editions), so most players can name one.
MAINSTREAM_SITELINKS = 40
MIN_MAINSTREAM = 10
PACE_SECONDS = float(os.environ.get("WD_PACE", "1"))
# Wikidata production-company data is unreliable for small studios (wrong films get
# attached), so only large, well-curated studios become questions. label -> display name
STUDIOS = {
    "Warner Bros. Entertainment": "Warner Bros.", "Warner Bros.": "Warner Bros.",
    "Paramount Pictures": "Paramount Pictures", "Universal Pictures": "Universal Pictures",
    "Metro-Goldwyn-Mayer": "MGM", "Columbia Pictures": "Columbia Pictures",
    "20th Century Studios": "20th Century Fox / 20th Century Studios",
    "Walt Disney Pictures": "Walt Disney Pictures", "United Artists": "United Artists",
    "RKO Pictures": "RKO Pictures", "Miramax": "Miramax", "Touchstone Pictures": "Touchstone Pictures",
    "New Line Cinema": "New Line Cinema", "TriStar Pictures": "TriStar Pictures",
    "DreamWorks": "DreamWorks Pictures",
}
# film, feature film, animated feature, animated film, anime film, silent film, live-action/animated
FILM_TYPES = "wd:Q11424 wd:Q24869 wd:Q29168811 wd:Q202866 wd:Q20650540 wd:Q226730 wd:Q25110269"


CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wd_cache")
_last_call = [0.0]


def sparql(query, tries=6):
    """Run a query, caching the raw CSV so reruns on the same day don't re-hit Wikidata.
    The public endpoint may rate-limit hard (sometimes 1 request/minute), so calls are paced."""
    query = PREFIXES + query
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = hashlib.sha1((datetime.date.today().isoformat() + query).encode()).hexdigest()
    path = os.path.join(CACHE_DIR, key + ".csv")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    url = ENDPOINT + "?" + urllib.parse.urlencode({"query": query})
    for i in range(tries):
        wait = PACE_SECONDS - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/csv"})
            with urllib.request.urlopen(req, timeout=300) as r:
                text = r.read().decode("utf-8")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            return list(csv.DictReader(io.StringIO(text)))
        except urllib.error.HTTPError as e:
            retry = int(e.headers.get("Retry-After", "0") or 0)
            print(f"  query failed ({e}); retry {i + 1}/{tries}", file=sys.stderr, flush=True)
            time.sleep(max(retry, 30))
        except Exception as e:
            print(f"  query failed ({e}); retry {i + 1}/{tries}", file=sys.stderr, flush=True)
            time.sleep(30)
    raise RuntimeError("Wikidata query failed repeatedly")


def qid(uri):
    return uri.rsplit("/", 1)[-1]


# ---------------------------------------------------------------- film pool
def load_films():
    films = {}
    bands = [(MIN_SITELINKS, 12), (12, 10000)]
    for lo, hi in bands:
        print(f"films with {lo}-{hi - 1} sitelinks...", flush=True)
        rows = sparql(f"""
SELECT ?f ?label ?s ?date WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s.
  FILTER(?s >= {lo} && ?s < {hi})
  OPTIONAL {{ ?f rdfs:label ?en. FILTER(LANG(?en) = "en") }}
  OPTIONAL {{ ?f rdfs:label ?mul. FILTER(LANG(?mul) = "mul") }}
  BIND(COALESCE(?en, ?mul) AS ?label) FILTER(BOUND(?label))
  OPTIONAL {{ ?f wdt:P577 ?date }}
}}""")
        for r in rows:
            k = qid(r["f"])
            f = films.setdefault(k, {"label": r["label"], "s": int(r["s"]), "year": None, "first": None})
            if is_qid(f["label"]) and not is_qid(r["label"]):
                f["label"] = r["label"]  # some items carry a bogus label that is just their ID
            d = r["date"][:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d >= "1880":
                f["first"] = d if f["first"] is None else min(f["first"], d)
    # Year = earliest release/premiere date; films whose first release is still in the future are dropped.
    today = datetime.date.today().isoformat()
    for f in films.values():
        if f["first"] and f["first"] <= today:
            f["year"] = int(f["first"][:4])
    # Drop items that are also typed as something that isn't a feature film:
    # short film, animated short, TV film, TV special, video album, music video, unreleased film project
    print("non-feature items to exclude...", flush=True)
    shorts = {qid(r["f"]) for r in sparql(
        "SELECT DISTINCT ?f WHERE { VALUES ?bad { wd:Q24862 wd:Q17517379 wd:Q506240 wd:Q1261214 "
        "wd:Q10590726 wd:Q193977 wd:Q18011172 } ?f wdt:P31 ?bad; wikibase:sitelinks ?s. FILTER(?s >= %d) }"
        % MIN_SITELINKS)}
    # ...and anything with a recorded running time under 40 minutes (P2047, in minutes)
    shorts |= {qid(r["f"]) for r in sparql(f"""
SELECT DISTINCT ?f WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s; wdt:P2047 ?min. FILTER(?s >= {MIN_SITELINKS} && ?min < 40)
}}""")}
    for v in films.values():
        v["label"] = clean_label(v["label"])
    # Drop films with no release year (usually unreleased / junk items)
    return {k: v for k, v in films.items() if v["year"] and k not in shorts and not is_qid(v["label"])}


def is_qid(label):
    return re.fullmatch(r"Q\d+", label) is not None


def clean_label(label):
    # "The Game (2008 film)" -> "The Game"; the game shows the year separately
    return re.sub(r"\s*\((\d{4} )?([A-Za-z ]+ )?film\)$", "", label).strip()


# ---------------------------------------------------------------- relations
def relation(prop, min_person_sitelinks, extra=""):
    rows = sparql(f"""
SELECT ?f ?p ?plabel ?ps WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?f {prop} ?p. {extra}
  ?p wikibase:sitelinks ?ps. FILTER(?ps >= {min_person_sitelinks})
  OPTIONAL {{ ?p rdfs:label ?en. FILTER(LANG(?en) = "en") }}
  OPTIONAL {{ ?p rdfs:label ?mul. FILTER(LANG(?mul) = "mul") }}
  BIND(COALESCE(?en, ?mul) AS ?plabel) FILTER(BOUND(?plabel))
}}""")
    groups = defaultdict(lambda: {"label": "", "s": 0, "films": set()})
    for r in rows:
        g = groups[qid(r["p"])]
        if not g["label"] or is_qid(g["label"]):
            g["label"] = r["plabel"]
        g["s"] = int(r["ps"])
        g["films"].add(qid(r["f"]))
    return {k: g for k, g in groups.items() if not is_qid(g["label"])}


DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.js")
DAILY_AHEAD = 30   # days of daily puzzles scheduled in advance

# Broad, everyday categories. Every ID here was checked against Wikidata.
# Genres use direct genre tags only: Wikidata's genre hierarchy is too tangled to follow
# (following it made Forrest Gump a "teen movie").
GENRES = [
    ("Q188473", "Name an action movie"), ("Q157443", "Name a comedy"),
    ("Q471839", "Name a sci-fi movie"), ("Q200092", "Name a horror movie"),
    ("Q1054574", "Name a romance movie"), ("Q1535153", "Name a superhero movie"),
    ("Q842256", "Name a musical"), ("Q369747", "Name a war movie"),
    ("Q172980", "Name a Western"), ("Q28026639", "Name a Christmas movie"),
    ("Q2297927", "Name a spy movie"), ("Q496523", "Name a heist movie"),
    ("Q157394", "Name a fantasy movie"), ("Q959790", "Name a crime movie"),
    ("Q2484376", "Name a thriller"), ("Q1361932", "Name a family movie"),
    ("Q1146335", "Name a teen movie"), ("Q645928", "Name a biopic"),
    ("Q1342372", "Name a monster movie"), ("Q846544", "Name a disaster movie"),
    ("Q3072049", "Name a zombie movie"), ("Q853630", "Name a slasher movie"),
    ("Q1341051", "Name a post-apocalyptic movie"), ("Q319221", "Name an adventure movie"),
    ("Q20443008", "Name a dystopian movie"), ("Q102429885", "Name a coming-of-age movie"),
    ("Q2143665", "Name a kids' movie"),
]
ROMCOMS = ("Q118612349", "Q860626")   # two Wikidata items for "romantic comedy"
SETTINGS = [
    ("Q60", "New York City"), ("Q84", "London"), ("Q65", "Los Angeles"), ("Q90", "Paris"),
    ("Q61", "Washington, D.C."), ("Q62", "San Francisco"), ("Q1297", "Chicago"),
    ("Q23768", "Las Vegas"), ("Q1490", "Tokyo"), ("Q220", "Rome"), ("Q8652", "Miami"),
]
SUBJECTS = [
    ("Q182154", "Name a movie about time travel"), ("Q362", "Name a World War II movie"),
    ("Q484188", "Name a movie about a serial killer"),
    ("Q361", "Name a World War I movie"), ("Q8683", "Name a Cold War movie"),
    ("Q45393", "Name a pirate movie"), ("Q44512", "Name a movie about a deadly outbreak"),
    ("Q11416", "Name a gambling movie"),
    ("Q430", "Name a movie with dinosaurs"), ("Q11660", "Name a movie about artificial intelligence"),
    ("Q46952", "Name a mob movie"), ("Q1712140", "Name a revenge movie"),
]
# Several Wikidata topics merged into one clue
MERGED_SUBJECTS = [(("Q2447078", "Q181508"), "Name a movie about aliens", "aliens")]   # alien invasion + extraterrestrial life
ANIMATED_TYPES = "wd:Q29168811 wd:Q202866 wd:Q20650540"

# Clues a casual movie fan might not get, removed on purpose (kept here so it's easy to change).
SKIP_CLUES = {
    "Name a biopic", "Name a teen movie", "Name a family movie", "Name an adventure movie",
    "Name a crime movie", "Name a heist movie", "Name a slasher movie", "Name a monster movie",
    "Name a post-apocalyptic movie", "Name a revenge movie", "Name a movie about artificial intelligence",
    "Name a movie set in Tokyo", "Name a movie set in Rome", "Name a movie set in Miami",
    "Name a movie set in Chicago", "Name a movie set in San Francisco", "Name a movie set in Washington, D.C.",
    "Name a movie starring Sigourney Weaver", "Name a movie starring Gwyneth Paltrow",
    "Name a movie starring Halle Berry", "Name a movie starring Jeremy Renner", "Name a movie starring Cate Blanchett",
    "Name a movie directed by Ridley Scott", "Name a movie directed by Robert Zemeckis",
    "Name a movie starring Stan Lee",          # cameos only
    "Name a Disney Renaissance movie",         # Wikidata lists only 4 of the 10 films
}
# Only Disney is a studio most people can name films for.
STUDIO_CLUES = {"Walt Disney Pictures": "Name a Disney movie", "Pixar": "Name a Pixar movie",
                "Walt Disney Animation Studios": "Name a Disney animated movie"}
# Answers Wikidata has wrong for a clue (title must match exactly)
BAD_ANSWERS = {
    "Name a Pixar movie": {"Care Bears: Journey to Joke-a-lot", "Planes", "Planes: Fire & Rescue",
                           "Buzz Lightyear of Star Command: The Adventure Begins"},  # not Pixar films
    "Name a Toy Story movie": {"Buzz Lightyear of Star Command: The Adventure Begins"},          # TV pilot
}

# Each day: 5 broad clues + 2 about a big franchise or a famous name, in shuffled order.
# Within a slot a category is picked first, then a clue, so year clues (one option in one
# slot) come up roughly once every four days.
DAILY_SLOTS = [["genre"], ["genre"], ["setting", "subject"], ["award", "subject", "setting", "genre"],
               ["genre", "setting", "subject", "year"], ["series", "studio", "author", "award"],
               ["actor", "director"]]


def plan_daily(questions):
    """Fix each day's 7 clues in advance. Days already scheduled in the previous data.js are
    kept exactly as they were (if all their clues still exist), so a nightly data refresh never
    changes a puzzle people are already playing. No clue repeats within 60 scheduled days."""
    import random
    keys = {q["key"] for q in questions}
    old = {}
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, encoding="utf-8") as fh:
                old = json.loads(fh.read().split("=", 1)[1].strip().rstrip(";")).get("daily", {})
        except (ValueError, IndexError):
            old = {}
    today = datetime.date.today()
    daily = {d: ks for d, ks in old.items()
             if d >= (today - datetime.timedelta(days=60)).isoformat()
             and d < today.isoformat()   # past days stay as played; future ones follow the current rules
             and all(k in keys for k in ks)}
    # keep today's schedule too if it was already published
    if today.isoformat() in old and all(k in keys for k in old[today.isoformat()]):
        daily[today.isoformat()] = old[today.isoformat()]
    by_kind = defaultdict(list)
    for q in questions:
        by_kind[q["kind"]].append(q["key"])
    for n in range(-1, DAILY_AHEAD + 1):   # from yesterday (time zones) to a month ahead
        day = (today + datetime.timedelta(days=n)).isoformat()
        if day in daily:
            continue
        recent = {k for d, ks in daily.items() for k in ks}
        rnd = random.Random("reelrare-" + day)
        picks = []
        for slot in DAILY_SLOTS:
            kinds = [kd for kd in slot if any(k not in picks for k in by_kind.get(kd, []))]
            kind = rnd.choice(kinds)
            pool = [k for k in by_kind[kind] if k not in picks]
            fresh = [k for k in pool if k not in recent]
            picks.append(rnd.choice(fresh or pool))
        rnd.shuffle(picks)
        daily[day] = picks
    return dict(sorted(daily.items()))


def pick_oscars(questions, film_rows):
    """Each clue gets exactly one "One in an Oscar" film: chosen (repeatably, from the clue's
    key) among answers below the Award Movie band that are still reasonably well known
    (in 5+ of the big Wikipedias), so it's a real find rather than an impossible guess.
    The tier cut-offs must match CUTS in index.html."""
    import math, random
    for q in questions:
        n = len(q["a"])
        award_end = max(1, min(30, math.ceil(0.60 * n)))
        deep = q["a"][award_end:] or q["a"][-1:]
        if q["kind"] in ("series", "studio"):
            # small franchises: widen to everything below the Studio Film band
            deep = q["a"][max(1, min(10, math.ceil(0.35 * n))):] or deep
        # franchises/studios: the jackpot must be a proper, widely covered entry (not a TV special)
        bar = 10000 if q["kind"] in ("series", "studio") else 5000
        findable = [i for i in deep if film_rows[i][2] >= bar] or [i for i in deep if film_rows[i][2] >= 5000] or deep
        q["o"] = random.Random("oscar-" + q["key"]).choice(findable)


def article(word):
    """'a' or 'an' by sound: an X-Men movie, an MGM movie, a Universal movie."""
    w = word.strip()
    if re.match(r"(?i)[aeio]|u(?!ni|s[aeiou])", w) or re.match(r"[FHLMNRSX](?:[-A-Z]|$)", w):
        return "an"
    return "a"


# Films whose Wikipedia coverage wildly overstates how well known they are.
NOT_FAMOUS = {
    "Q11252497",   # Bad Cat (2016): one article copied into 120+ Wikipedias
}

MAJOR_WIKIS = ["en", "de", "fr", "es", "it", "ja", "ru", "pt", "zh", "pl", "nl", "sv", "uk", "ar",
               "fa", "ko", "he", "tr", "id", "vi"]


def major_links(films):
    """How many of the 20 biggest Wikipedias have an article on each film. Counting every
    language edition is easy to inflate (one fan can create stubs on dozens of tiny wikis);
    the big ones track real fame much better."""
    wikis = " ".join(f"<https://{w}.wikipedia.org/>" for w in MAJOR_WIKIS)
    rows = sparql(f"""
SELECT ?f (COUNT(DISTINCT ?w) AS ?major) WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  VALUES ?w {{ {wikis} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?a schema:about ?f; schema:isPartOf ?w.
}} GROUP BY ?f""")
    return {qid(r["f"]): int(r["major"]) for r in rows}


def main():
    films = load_films()
    print(f"film pool: {len(films)}")
    major = major_links(films)
    for k, f in films.items():
        # rank by big-Wikipedia coverage first, total language editions as the tie-breaker
        f["pop"] = major.get(k, 0) * 1000 + min(f["s"], 999)
        if k in NOT_FAMOUS:
            f["pop"] = min(f["s"], 999) // 20
    # Big films collect many genre tags (The Terminator has 18), so within a genre clue a film
    # with lots of tags ranks lower: still a valid answer, just not a "most people say" pick.
    genre_counts = {qid(r["f"]): int(r["n"]) for r in sparql(f"""
SELECT ?f (COUNT(DISTINCT ?g) AS ?n) WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s; wdt:P136 ?g. FILTER(?s >= {MIN_SITELINKS})
}} GROUP BY ?f""")}
    questions = []

    def add(kind, text, film_ids, key):
        bad = BAD_ANSWERS.get(text, set())
        valid = [f for f in set(film_ids) if f in films and films[f]["label"] not in bad]
        mainstream = sum(1 for f in valid if films[f]["s"] >= MAINSTREAM_SITELINKS)
        # franchises and single studios are small but famous: a lower bar
        need_all, need_main = (4, 4) if kind in ("series", "studio") else (MIN_ANSWERS, MIN_MAINSTREAM)
        if len(valid) >= need_all and mainstream >= need_main:
            questions.append({"kind": kind, "text": text, "ids": valid, "key": key})

    def films_with(pattern):
        return [qid(r["f"]) for r in sparql(f"""
SELECT DISTINCT ?f WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  {pattern}
}}""")]

    # --- broad categories
    print("genres...", flush=True)
    genre_ids = " ".join("wd:" + g for g, _ in GENRES)
    rows = sparql(f"""
SELECT DISTINCT ?f ?g WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  VALUES ?g {{ {genre_ids} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?f wdt:P136 ?g.
}}""")
    by_genre = defaultdict(list)
    for r in rows:
        by_genre[qid(r["g"])].append(qid(r["f"]))
    for g, text in GENRES:
        add("genre", text, by_genre[g], f"genre:{g}")
    add("genre", "Name a romantic comedy",
        films_with("VALUES ?rc { %s } ?f wdt:P136 ?rc." % " ".join("wd:" + x for x in ROMCOMS)), "genre:romcom")
    add("genre", "Name an animated movie",
        films_with("VALUES ?an { %s } ?f wdt:P31 ?an." % ANIMATED_TYPES), "genre:animated")

    print("settings & subjects...", flush=True)
    place_ids = " ".join("wd:" + p for p, _ in SETTINGS)
    rows = sparql(f"""
SELECT ?f ?p (COUNT(DISTINCT ?any) AS ?places) WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  VALUES ?p {{ {place_ids} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?f wdt:P840 ?p. ?f wdt:P840 ?any.
}} GROUP BY ?f ?p""")
    # A film that lists many locations (e.g. a globe-trotting blockbuster with one Tokyo scene)
    # isn't really "set in" each of them.
    rows = [r for r in rows if int(r["places"]) <= 3]
    for p, name in SETTINGS:
        add("setting", f"Name a movie set in {name}", [qid(r["f"]) for r in rows if qid(r["p"]) == p], f"setting:{p}")
    subj_ids = " ".join("wd:" + x for x, _ in SUBJECTS)
    rows = sparql(f"""
SELECT DISTINCT ?f ?x WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  VALUES ?x {{ {subj_ids} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?f wdt:P921 ?x.
}}""")
    for x, text in SUBJECTS:
        add("subject", text, [qid(r["f"]) for r in rows if qid(r["x"]) == x], f"subject:{x}")
    for ids, text, key in MERGED_SUBJECTS:
        vals = " ".join("wd:" + i for i in ids)
        add("subject", text, films_with("VALUES ?x { %s } ?f wdt:P921 ?x." % vals), f"subject:{key}")
    add("subject", "Name a movie based on a comic book",
        films_with("?f wdt:P144 ?src. ?src wdt:P31/wdt:P279* wd:Q1004."), "subject:comics")

    # Year clues: 2010 onward only (decades and single years)
    by_year, by_decade = defaultdict(list), defaultdict(list)
    for k, f in films.items():
        by_year[f["year"]].append(k)
        by_decade[f["year"] // 10 * 10].append(k)
    for d in range(2010, datetime.date.today().year, 10):
        add("year", f"Name a movie from the {d}s", by_decade[d], f"decade:{d}")
    for y in range(2010, datetime.date.today().year):
        add("year", f"Name a movie released in {y}", by_year[y], f"year:{y}")

    awards = [
        ("Q102427", "Name a movie nominated for Best Picture", {"won", "nom"}),
        ("Q102427", "Name a Best Picture winner", {"won"}),
        ("Q106800", "Name a movie nominated for Best Animated Feature", {"won", "nom"}),
    ]
    print("awards...", flush=True)
    rows = sparql("SELECT DISTINCT ?f ?a ?how WHERE { VALUES ?a { wd:Q102427 wd:Q106800 } "
                  "{ ?f wdt:P166 ?a BIND('won' AS ?how) } UNION { ?f wdt:P1411 ?a BIND('nom' AS ?how) } }")
    for aq, text, hows in awards:
        ids = [qid(r["f"]) for r in rows if qid(r["a"]) == aq and r["how"] in hows]
        add("award", text, ids, f"award:{aq}:{'+'.join(sorted(hows))}")

    # --- a smaller set about big franchises and very famous names
    specs = [
        ("director", "wdt:P57", 80, "", "Name a movie directed by {}"),
        ("actor", "wdt:P161", 90, "?p wdt:P31 wd:Q5. FILTER NOT EXISTS {{ ?p wdt:P106 wd:Q82955 }}",
         "Name a movie starring {}"),
        ("studio", "wdt:P272", 40, "", "Name a {} movie"),
        ("series", "wdt:P179", 15, "", "Name a {} movie"),
        ("author", "wdt:P144/wdt:P50", 80, "?p wdt:P31 wd:Q5.", "Name a movie based on a book by {}"),
    ]
    for kind, prop, ms, extra, tmpl in specs:
        print(f"{kind}...", flush=True)
        for k, g in relation(prop, ms, extra).items():
            label = g["label"]
            if kind == "studio":
                if label in STUDIOS and STUDIOS[label] in STUDIO_CLUES.values() or label in STUDIO_CLUES:
                    add(kind, STUDIO_CLUES.get(label, label), g["films"], f"{kind}:{k}")
                continue
            if kind == "series":
                if re.search(r"(?i)\blist\b|greatest|\bbest\b|\btop\b|\bphase\b|\b\d{2,}\b", label):
                    continue  # ranked lists, not franchises
                for suffix in (" film series", " series", " franchise", " (film series)", " in film"):
                    if label.lower().endswith(suffix):
                        label = label[: -len(suffix)]
                if label.lower().startswith("the "):
                    label = label[4:]
                if label.endswith("Universe"):
                    add(kind, f"Name a movie in the {label}", g["films"], f"{kind}:{k}")
                    continue
            text = tmpl.format(label)
            if kind in ("studio", "series"):
                text = text.replace("Name a ", "Name " + article(label) + " ", 1)
            add(kind, text, g["films"], f"{kind}:{k}")
    # Directors who cameo in their own films (Hitchcock, Tarantino) shouldn't get "starring" clues
    director_names = {q["text"].rsplit(" by ", 1)[1] for q in questions if q["kind"] == "director"}
    questions[:] = [q for q in questions
                    if not (q["kind"] == "actor" and q["text"].rsplit("starring ", 1)[1] in director_names)]
    # people/franchise clues need more famous answers than broad ones to count as mainstream
    def famous_enough(q):
        if q["kind"] in ("studio", "series"):
            return sum(1 for f in q["ids"] if films[f]["s"] >= 60) >= 3
        if q["kind"] in ("director", "actor", "author"):
            return sum(1 for f in q["ids"] if films[f]["s"] >= 60) >= 5
        return True
    questions[:] = [q for q in questions if famous_enough(q) and q["text"] not in SKIP_CLUES]

    # ---- alternate titles (so "Star Wars" finds "Star Wars: Episode IV – A New Hope")
    print("alternate titles...", flush=True)
    alias_rows = sparql(f"""
SELECT ?f ?alt WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?f skos:altLabel ?alt. FILTER(LANG(?alt) = "en" || LANG(?alt) = "mul")
}}""")
    aliases = defaultdict(list)
    for r in alias_rows:
        k, alt = qid(r["f"]), clean_label(r["alt"].strip())
        if k in films and len(alt) >= 3 and not alt.isdigit() and alt != films[k]["label"] \
                and alt not in aliases[k] and len(aliases[k]) < 8:
            aliases[k].append(alt)

    # ---- compact output: films as array, answers as indices sorted by popularity
    order = sorted(films, key=lambda k: -films[k]["pop"])
    index = {k: i for i, k in enumerate(order)}
    def genre_score(k):
        return films[k]["pop"] * min(1.0, 4 / max(1, genre_counts.get(k, 1)))

    out = {
        "built": datetime.date.today().isoformat(),
        "source": "Wikidata (CC0). Popularity = coverage across the 20 largest Wikipedias.",
        "films": [[films[k]["label"], films[k]["year"], films[k]["pop"]] for k in order],
        "aliases": {str(index[k]): v for k, v in aliases.items() if k in index},
        "questions": [
            {"kind": q["kind"], "text": q["text"], "key": q["key"],
             "a": sorted((index[i] for i in q["ids"]),
                        key=lambda j: (-genre_score(order[j]) if q["kind"] == "genre" else j, j))}
            for q in sorted(questions, key=lambda q: q["key"])
        ],
    }
    pick_oscars(out["questions"], out["films"])
    out["daily"] = plan_daily(out["questions"])
    out["slots"] = DAILY_SLOTS   # Unlimited mode deals rounds with the same mix
    with open(DATA_PATH, "w", encoding="utf-8") as fh:
        fh.write("window.REEL_DATA = ")
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write(";\n")
    kinds = defaultdict(int)
    for q in questions:
        kinds[q["kind"]] += 1
    print(f"wrote data.js: {len(order)} films, {len(questions)} questions {dict(kinds)}")


if __name__ == "__main__":
    main()
