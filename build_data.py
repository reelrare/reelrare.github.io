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
PREFIX p: <http://www.wikidata.org/prop/>
PREFIX psv: <http://www.wikidata.org/prop/statement/value/>
PREFIX ps: <http://www.wikidata.org/prop/statement/>
PREFIX pq: <http://www.wikidata.org/prop/qualifier/>
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
WIKI_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki_cache")
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
            f = films.setdefault(k, {"label": r["label"], "s": int(r["s"]), "year": None, "first": None, "dates": set()})
            if is_qid(f["label"]) and not is_qid(r["label"]):
                f["label"] = r["label"]  # some items carry a bogus label that is just their ID
            d = r["date"][:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d >= "1880":
                f["first"] = d if f["first"] is None else min(f["first"], d)
                f["dates"].add(d)
    # Year = earliest release/premiere date; films whose first release is still in the future are dropped.
    today = datetime.date.today().isoformat()
    for f in films.values():
        if f["first"] and f["first"] <= today:
            f["year"] = int(f["first"][:4])
        # every year it was released in (festival premiere, wide release...) counts for year clues
        f["years"] = sorted({int(d[:4]) for d in f.get("dates", ()) if d <= today})
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
DAILY_SIZE = 7
MIN_SMALL_ANSWERS = 5   # franchises and directors need at least this many films to be scheduled
GAMES_PER_CLUE = 5  # Unlimited games: about 5 passes through every clue

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
# Extra genre names that also count for a genre clue (matched against Wikidata genre names)
GENRE_WORDS = {
    "Q188473": r"\baction", "Q157443": r"comedy", "Q471839": r"science fiction|sci-fi|space opera|cyberpunk",
    "Q200092": r"horror|slasher", "Q1054574": r"romance|romantic", "Q1535153": r"superhero",
    "Q842256": r"musical", "Q369747": r"\bwar film|war drama|anti-war", "Q172980": r"western",
    "Q28026639": r"christmas", "Q2297927": r"\bspy|espionage", "Q157394": r"fantasy",
    "Q2484376": r"thriller", "Q3072049": r"zombie", "Q846544": r"disaster",
    "Q20443008": r"dystopi", "Q102429885": r"coming-of-age|coming of age",
    "Q2143665": r"children|family|animat|anime",   # kids' movie: family and animated films count
}
# Genres that also count for a topic clue
SUBJECT_WORDS = {
    "Q362": r"world war ii", "Q361": r"world war i(?!i)", "Q8683": r"cold war", "Q45393": r"pirate|swashbuckler",
    "Q46952": r"gangster|mafia|\bmob\b|yakuza", "Q182154": r"time travel", "Q484188": r"serial killer|slasher",
    "Q44512": r"pandemic|epidemic|outbreak|zombie", "Q11416": r"gambling|casino|poker", "Q430": r"dinosaur",
}
# Well-known films that clearly fit a clue but that Wikidata doesn't tag that way.
# Added on top of the Wikidata answers (never removes anything). Titles as in Wikidata; if several
# films share a title, the best-known one is used. Titles not found are reported by the build.
EXTRA_ANSWERS = {
    "Name a movie about time travel": [
        "Looper", "Groundhog Day", "Avengers: Endgame", "Edge of Tomorrow", "Tenet", "Interstellar",
        "Hot Tub Time Machine", "About Time", "Source Code", "Palm Springs", "Happy Death Day",
        "Men in Black 3", "X-Men: Days of Future Past", "Midnight in Paris", "12 Monkeys",
        "Back to the Future Part II", "Back to the Future Part III", "Bill & Ted's Excellent Adventure"],
    "Name a World War II movie": [
        "Dunkirk", "Fury", "Darkest Hour", "The Imitation Game", "Jojo Rabbit", "Pearl Harbor", "The Pianist",
        "Life Is Beautiful", "The Great Escape", "Letters from Iwo Jima", "Flags of Our Fathers", "Valkyrie",
        "Unbroken", "Hacksaw Ridge", "Grave of the Fireflies", "The Boy in the Striped Pajamas",
        "Captain America: The First Avenger", "Oppenheimer", "Inglourious Basterds", "Saving Private Ryan"],
    "Name a movie with a U.S. president as a character": [
        "Air Force One", "Independence Day", "Olympus Has Fallen", "White House Down", "Dave",
        "The American President", "Don't Look Up", "Lincoln", "Frost/Nixon", "JFK", "Nixon", "Jackie",
        "Thirteen Days", "W.", "Abraham Lincoln: Vampire Hunter", "Forrest Gump"],
    "Name a movie about aliens": [
        "Close Encounters of the Third Kind", "Arrival", "District 9", "Predator", "Nope", "Lilo & Stitch",
        "E.T. the Extra-Terrestrial", "Signs", "Men in Black", "Independence Day", "War of the Worlds",
        "Alien", "Aliens", "Contact", "Super 8", "Paul", "Edge of Tomorrow", "A Quiet Place"],
    "Name a movie with dinosaurs": ["The Good Dinosaur", "The Land Before Time", "Jurassic World",
                                    "Jurassic World Dominion", "Jurassic World Rebirth"],
    "Name a pirate movie": ["Treasure Planet", "Peter Pan", "Muppet Treasure Island", "Captain Phillips",
                            "The Pirates! In an Adventure with Scientists!"],
    "Name a movie about a serial killer": ["Zodiac", "Seven", "Prisoners", "Scream", "Halloween",
                                           "No Country for Old Men", "The Lovely Bones"],
    "Name a movie about a deadly outbreak": ["I Am Legend", "Contagion", "Outbreak", "28 Days Later",
                                             "World War Z", "Train to Busan", "12 Monkeys", "Carriers"],
    "Name a movie based on a comic book": ["Joker", "Joker: Folie à Deux", "Road to Perdition",
                                           "A History of Violence", "Persepolis", "Snowpiercer", "Oldboy"],
    "Name a coming-of-age movie": ["Lady Bird", "Eighth Grade", "Stand by Me", "Boyhood", "Superbad",
                                   "The Perks of Being a Wallflower", "Booksmart", "Moonlight", "Call Me by Your Name"],
    "Name a gambling movie": ["Uncut Gems", "Rounders", "Molly's Game", "Casino", "Ocean's Eleven", "Maverick"],
    "Name a Cold War movie": ["Tinker Tailor Soldier Spy", "The Hunt for Red October", "Atomic Blonde",
                              "Bridge of Spies", "Thirteen Days", "Dr. Strangelove", "The Man from U.N.C.L.E.",
                              "X-Men: First Class", "Rocky IV", "Red Sparrow"],
    "Name a thriller": ["Prisoners", "Gone Girl", "Shutter Island", "Nightcrawler", "Knives Out", "Seven"],
    "Name a movie set in London": ["Peter Pan"],
    "Name a mob movie": ["Goodfellas", "The Irishman", "Donnie Brasco", "Road to Perdition", "Mean Streets",
                         "The Untouchables", "American Gangster", "Black Mass"],
}
ROMCOMS = ("Q118612349", "Q860626")   # two Wikidata items for "romantic comedy"
SETTINGS = [
    ("Q60", "New York City"), ("Q84", "London"), ("Q65", "Los Angeles"), ("Q90", "Paris"),
    ("Q61", "Washington, D.C."), ("Q62", "San Francisco"), ("Q1297", "Chicago"),
    ("Q23768", "Las Vegas"), ("Q1490", "Tokyo"), ("Q220", "Rome"), ("Q8652", "Miami"),
]
# Second source for city clues: English Wikipedia's "Films set in ..." categories (with sub-categories
# like "Films set in Manhattan"), which are far more complete than Wikidata's "narrative location".
WIKI_CITY_CATEGORIES = {
    "Q60": "Films set in New York City", "Q84": "Films set in London", "Q65": "Films set in Los Angeles",
    "Q90": "Films set in Paris", "Q23768": "Films set in the Las Vegas Valley",
    "Q61": "Films set in Washington, D.C.", "Q62": "Films set in San Francisco", "Q1297": "Films set in Chicago",
    "Q1490": "Films set in Tokyo", "Q220": "Films set in Rome", "Q8652": "Films set in Miami",
}

# The films everyone thinks of first for each city. They always rank at the top (so they score as
# Blockbusters); data alone can't tell "a Vegas movie" from "a movie with one Vegas scene".
ICONIC_CITY_FILMS = {
    "Q23768": ["The Hangover", "Ocean's Eleven", "Casino", "Fear and Loathing in Las Vegas", "Rain Man", "Leaving Las Vegas"],
    "Q60": ["Ghostbusters", "Home Alone 2: Lost in New York", "Taxi Driver", "Elf", "The Devil Wears Prada", "Spider-Man"],
    "Q65": ["La La Land", "Pulp Fiction", "Once Upon a Time in Hollywood", "The Big Lebowski", "Heat", "Clueless"],
    "Q84": ["Notting Hill", "Love Actually", "Paddington", "Mary Poppins", "Bridget Jones's Diary", "Skyfall"],
    "Q90": ["Ratatouille", "Amélie", "Midnight in Paris", "Moulin Rouge!", "Before Sunset", "An American in Paris"],
}

# City names in a title also count ("...Pickpocket in New York", "Once Upon a Time in Hollywood")
CITY_TITLE_WORDS = {
    "Q60": r"new york|\bnyc\b|manhattan|brooklyn|harlem|the bronx",
    "Q84": r"\blondon\b",
    "Q65": r"los angeles|hollywood|\bl\.a\.",
    "Q90": r"\bparis\b(?!,? texas)",
    "Q23768": r"\bvegas\b",
    "Q61": r"washington,? d\.?c\.?",
    "Q62": r"san francisco",
    "Q1297": r"\bchicago\b",
    "Q1490": r"\btokyo\b",
    "Q220": r"\brome\b|\broman\b",
    "Q8652": r"\bmiami\b",
}
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
    "Name a movie directed by Robert Zemeckis",
    "Name a movie starring Stan Lee",          # cameos only
    "Name a Disney Renaissance movie",         # Wikidata lists only 4 of the 10 films
    "Name a Dollars movie",                    # the "Dollars Trilogy" isn't a name most people know
}
# Only Disney is a studio most people can name films for.
STUDIO_CLUES = {"Walt Disney Pictures": "Name a Disney movie", "Pixar": "Name a Pixar movie",
                "Walt Disney Animation Studios": "Name a Disney animated movie"}
# Answers Wikidata has wrong for a clue (title must match exactly)
BAD_ANSWERS = {
    "Name a Pixar movie": {"Care Bears: Journey to Joke-a-lot", "Planes", "Planes: Fire & Rescue",
                           "Buzz Lightyear of Star Command: The Adventure Begins"},  # not Pixar films
    "Name a Toy Story movie": {"Buzz Lightyear of Star Command: The Adventure Begins"},          # TV pilot
    "Name a movie with a U.S. president as a character": {"Milk"},   # archival footage only
}

# Directors: only household names (current) and all-time legends. Label as in Wikidata.
DIRECTORS = {
    "Steven Spielberg", "Christopher Nolan", "Quentin Tarantino", "Martin Scorsese", "James Cameron",
    "Tim Burton", "Peter Jackson", "Ridley Scott", "Denis Villeneuve", "Wes Anderson", "Greta Gerwig",
    "Jordan Peele", "Guillermo del Toro", "David Fincher",
    "Alfred Hitchcock", "Stanley Kubrick",
}

# Clues about the title itself (computed from English titles, so always accurate)
LETTERS = "BCDFGHJKLMNPRSTW"
NUMBER_WORDS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
                "twelve", "thirteen", "twenty", "hundred", "thousand", "million", "first", "second", "third"]
COLOR_WORDS = ["red", "blue", "green", "black", "white", "yellow", "orange", "purple", "pink", "gold",
               "golden", "silver", "grey", "gray", "brown", "scarlet", "crimson"]
ANIMAL_WORDS = ["dog", "dogs", "cat", "cats", "lion", "lions", "tiger", "tigers", "bear", "bears", "wolf",
                "wolves", "shark", "sharks", "bird", "birds", "horse", "horses", "monkey", "monkeys", "ape",
                "apes", "fox", "rabbit", "mouse", "mice", "rat", "rats", "snake", "snakes", "eagle", "eagles",
                "falcon", "hawk", "crow", "raven", "duck", "ducks", "pig", "pigs", "cow", "bull", "goat",
                "sheep", "lamb", "lambs", "deer", "elephant", "elephants", "panther", "jaguar", "leopard",
                "zebra", "whale", "dolphin", "fish", "frog", "turtle", "turtles", "penguin", "penguins", "owl",
                "bee", "ant", "spider", "scorpion", "gorilla", "crocodile", "chicken", "chickens", "parrot",
                "swan", "dove", "butterfly", "wasp", "bat", "bats", "dinosaur", "dinosaurs", "kitten", "puppy",
                "cub", "beetle", "hound", "mustang", "stallion", "raptor"]

# Films from a country (country of origin)
COUNTRIES = [("Q17", "Name a movie from Japan"), ("Q142", "Name a French movie"),
             ("Q145", "Name a British movie"), ("Q38", "Name an Italian movie"),
             ("Q668", "Name a movie from India")]
# Co-productions list many countries (Ratatouille isn't Japanese), so for these the film must also be
# in one of the country's languages.
COUNTRY_LANGUAGES = {
    "Q17": ["Q5287"],                       # Japanese
    "Q142": ["Q150"],                       # French
    "Q38": ["Q652"],                        # Italian
    "Q668": ["Q1568", "Q5885", "Q8097", "Q36236", "Q33673", "Q9610", "Q1571", "Q58635"],  # Indian languages
}

# Each day: 5 broad clues + 2 about a big franchise or a famous name, in shuffled order.
# Within a slot a category is picked first, then a clue, so year clues (one option in one
# slot) come up roughly once every four days.
DAILY_SLOTS = [["genre", "subject"], ["title", "letter", "record"], ["setting", "country", "subject"],
               ["award", "record", "genre", "people"],
               ["genre", "setting", "subject", "year", "title", "letter", "country"],
               ["series", "studio", "author"], ["actor", "director", "people"]]


# At most this many clues of one kind in a game (everything else: one each)
KIND_CAP = {"genre": 2}
GAMES_VERSION = 3   # bump to rebuild every Unlimited game with new rules


def deal(rnd, by_kind, last_used, stamp):
    """Pick 7 clues: one per slot, never more than KIND_CAP of a kind (so every game mixes kinds),
    and within a kind one of the clues used longest ago (so clues rotate). Updates last_used."""
    picks, used = [], defaultdict(int)
    for slot in DAILY_SLOTS:
        def open_kinds(kinds):
            return [kd for kd in kinds if by_kind.get(kd) and used[kd] < KIND_CAP.get(kd, 1)
                    and any(k not in picks for k in by_kind[kd])]
        kinds = open_kinds(slot) or open_kinds(sorted(by_kind))
        # bigger kinds come up more often, so every clue gets used about equally
        kind = rnd.choices(kinds, weights=[len(by_kind[kd]) for kd in kinds])[0]
        pool = sorted((k for k in by_kind[kind] if k not in picks), key=lambda k: (str(last_used.get(k, "")), k))
        oldest = pool[: max(1, len(pool) // 4)]          # the least recently used quarter
        pick = rnd.choice(oldest)
        picks.append(pick)
        used[kind] += 1
        last_used[pick] = stamp
    rnd.shuffle(picks)
    return picks


def previous_output():
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, encoding="utf-8") as fh:
                return json.loads(fh.read().split("=", 1)[1].strip().rstrip(";"))
        except (ValueError, IndexError):
            pass
    return {}


def plan_daily(questions):
    """Fix each day's 7 clues in advance. Past days and today keep the clues already published (if
    they all still exist), so a nightly data refresh never changes a puzzle people are playing."""
    import random
    keys = {q["key"] for q in questions}
    old = previous_output().get("daily", {})
    today = datetime.date.today()
    keep_from = (today - datetime.timedelta(days=60)).isoformat()
    daily = {d: ks for d, ks in old.items()
             if keep_from <= d <= today.isoformat() and all(k in keys for k in ks)}
    by_kind = defaultdict(list)
    for q in questions:
        if not q.get("r"):                       # retired clues are never scheduled
            by_kind[q["kind"]].append(q["key"])
    last_used = {}
    for d in sorted(daily):
        for k in daily[d]:
            last_used[k] = d
    for n in range(-1, DAILY_AHEAD + 1):   # from yesterday (time zones) to a month ahead
        day = (today + datetime.timedelta(days=n)).isoformat()
        if day not in daily:
            daily[day] = deal(random.Random("reelrare-" + day), by_kind, last_used, day)
    return dict(sorted(daily.items()))


def plan_games(questions):
    """Numbered Unlimited games (#1, #2, ...), the same for everyone. Every game mixes 7 different kinds
    of clue; clues rotate (least recently used first). Games already published are kept as they were
    (if all their clues still exist and the rules version matches), so data refreshes don't change them."""
    import random
    keys = {q["key"] for q in questions if not q.get("r")}   # a game with a retired clue is re-dealt
    prev = previous_output()
    old = prev.get("games", []) if prev.get("games_version") == GAMES_VERSION else []
    by_kind = defaultdict(list)
    for q in questions:
        if not q.get("r"):
            by_kind[q["kind"]].append(q["key"])
    target = max(len(old), GAMES_PER_CLUE * (len(questions) // DAILY_SIZE))
    games, last_used = [], {}
    for n in range(1, target + 1):
        prev_game = old[n - 1] if n <= len(old) else None
        if prev_game and all(k in keys for k in prev_game):
            games.append(prev_game)
            for k in prev_game:
                last_used[k] = f"{n:06d}"
            continue
        games.append(deal(random.Random(f"reelrare-unlimited-v{GAMES_VERSION}-{n}"), by_kind, last_used, f"{n:06d}"))
    return games


def wiki_category_titles(category, depth=2):
    """Article titles in an English Wikipedia category and its "Films set in ..." sub-categories.
    Saved in wiki_cache/ (committed with the project) and refreshed once a month; if Wikipedia
    can't be reached, the saved copy is used, so a nightly build never loses answers."""
    month = datetime.date.today().strftime("%Y-%m")
    safe = re.sub(r"[^A-Za-z0-9]+", "_", category)
    path = os.path.join(WIKI_CACHE_DIR, f"{safe}.json")
    saved = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        if saved.get("month") == month:
            return set(saved["titles"])
    titles, seen, queue = set(), set(), [(category, 0)]
    try:
        while queue:
            cat, d = queue.pop(0)
            if cat in seen:
                continue
            seen.add(cat)
            cont = {}
            while True:
                params = {"action": "query", "list": "categorymembers", "cmtitle": "Category:" + cat,
                          "cmlimit": "500", "cmtype": "page|subcat", "format": "json", **cont}
                url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
                for attempt in range(6):          # Wikipedia rate-limits bursts: back off and retry
                    try:
                        req = urllib.request.Request(url, headers={"User-Agent": UA})
                        with urllib.request.urlopen(req, timeout=60) as r:
                            data = json.load(r)
                        break
                    except urllib.error.HTTPError as e:
                        if e.code != 429 or attempt == 5:
                            raise
                        time.sleep(int(e.headers.get("Retry-After") or 0) or 15 * (attempt + 1))
                for m in data["query"]["categorymembers"]:
                    t = m["title"]
                    if t.startswith("Category:"):
                        sub = t[len("Category:"):]
                        if d < depth and sub.startswith("Films set in") and "television" not in sub.lower():
                            queue.append((sub, d + 1))
                    else:
                        titles.add(t)
                if "continue" not in data:
                    break
                cont = data["continue"]
                time.sleep(1)
            time.sleep(1)
    except Exception as e:
        print(f"  Wikipedia category '{category}' unavailable ({e}); "
              + ("using saved copy" if saved else "skipping"), flush=True)
        return set(saved["titles"]) if saved else set()
    os.makedirs(WIKI_CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"month": month, "category": category, "titles": sorted(titles)}, fh, ensure_ascii=False, indent=0)
    return titles


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
    place_counts = {}
    weak_place = set()    # "film|city" counted only because it was shot there
    # title -> best-known film with that title, for EXTRA_ANSWERS
    extra_ids = {}
    for k in sorted(films, key=lambda k: -films[k]["pop"]):
        extra_ids.setdefault(films[k]["label"], k)
    missing = sorted({t for ts in EXTRA_ANSWERS.values() for t in ts if t not in extra_ids})
    if missing:
        print("EXTRA_ANSWERS titles not in the film list:", missing, flush=True)

    def add(kind, text, film_ids, key):
        film_ids = list(film_ids) + [extra_ids[t] for t in EXTRA_ANSWERS.get(text, []) if t in extra_ids]
        bad = BAD_ANSWERS.get(text, set())
        valid = [f for f in set(film_ids) if f in films and films[f]["label"] not in bad]
        mainstream = sum(1 for f in valid if films[f]["s"] >= MAINSTREAM_SITELINKS)
        # franchises and single studios are small but famous: a lower bar
        # small but famous sets (franchises, a hand-picked director, record-breakers) get a lower bar
        need_all, need_main = ((3, 3) if kind in ("director", "series") else
                               (4, 3) if kind in ("series", "studio", "record", "people")
                               else (MIN_ANSWERS, MIN_MAINSTREAM))
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
    # Be forgiving: "supernatural horror film" and "comedy horror" also count as horror, etc.
    tagged = sparql(f"""
SELECT DISTINCT ?f ?gl WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?f wdt:P136 ?g. ?g rdfs:label ?gl. FILTER(LANG(?gl) = "en")
}}""")
    def by_genre_words(pattern):
        rx = re.compile(pattern, re.I)
        return [qid(r["f"]) for r in tagged if rx.search(r["gl"])]
    animated = films_with("VALUES ?an { %s } ?f wdt:P31 ?an." % ANIMATED_TYPES)
    for g, text in GENRES:
        ids = by_genre[g] + (by_genre_words(GENRE_WORDS[g]) if g in GENRE_WORDS else [])
        if g == "Q2143665":
            ids += animated          # every animated film counts as a kids' movie
        add("genre", text, ids, f"genre:{g}")
    add("genre", "Name a romantic comedy",
        films_with("VALUES ?rc { %s } ?f wdt:P136 ?rc." % " ".join("wd:" + x for x in ROMCOMS))
        + by_genre_words(r"romantic comedy|rom-com"), "genre:romcom")
    add("genre", "Name an animated movie",
        films_with("VALUES ?an { %s } ?f wdt:P31 ?an." % ANIMATED_TYPES) + by_genre_words(r"animat|anime"),
        "genre:animated")

    print("settings & subjects...", flush=True)
    place_ids = " ".join("wd:" + p for p, _ in SETTINGS)
    rows = sparql(f"""
SELECT ?f ?p (COUNT(DISTINCT ?any) AS ?places) WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  VALUES ?p {{ {place_ids} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?f wdt:P840 ?loc. ?loc wdt:P131* ?p.    # Manhattan, Brooklyn, Hollywood... count for their city
  ?f wdt:P840 ?any.
}} GROUP BY ?f ?p""")
    # Films listing many locations still count, but rank lower (see place_score)
    for r in rows:
        place_counts[qid(r["f"])] = max(place_counts.get(qid(r["f"]), 0), int(r["places"]))
    # Wikidata's "set in" is often missing (The Hangover lists only Los Angeles), so also count
    # where a film was shot and city names in its title. Those rank a little lower (place_score).
    filmed = sparql(f"""
SELECT DISTINCT ?f ?p WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  VALUES ?p {{ {place_ids} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?f wdt:P915 ?loc. ?loc wdt:P131* ?p.
}}""")
    print("Wikipedia city categories...", flush=True)
    en_titles = {}
    for r in sparql(f"""
SELECT ?f ?title WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?a schema:about ?f; schema:isPartOf <https://en.wikipedia.org/>; schema:name ?title.
}}"""):
        en_titles.setdefault(r["title"], qid(r["f"]))
    for p, name in SETTINGS:
        set_in = {qid(r["f"]) for r in rows if qid(r["p"]) == p}
        if p in WIKI_CITY_CATEGORIES:
            set_in |= {en_titles[t] for t in wiki_category_titles(WIKI_CITY_CATEGORIES[p]) if t in en_titles}
        shot_in = {qid(r["f"]) for r in filmed if qid(r["p"]) == p}
        rx = re.compile(CITY_TITLE_WORDS[p], re.I) if p in CITY_TITLE_WORDS else None
        titled = {k for k, f in films.items() if rx and rx.search(f["label"])}
        for k in (shot_in - set_in - titled):
            weak_place.add(k + "|" + p)
        add("setting", f"Name a movie set in {name}", set_in | shot_in | titled, f"setting:{p}")
    subj_ids = " ".join("wd:" + x for x, _ in SUBJECTS)
    rows = sparql(f"""
SELECT DISTINCT ?f ?x WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  VALUES ?x {{ {subj_ids} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?f wdt:P921 ?x.
}}""")
    for x, text in SUBJECTS:
        extra = by_genre_words(SUBJECT_WORDS[x]) if x in SUBJECT_WORDS else []
        add("subject", text, [qid(r["f"]) for r in rows if qid(r["x"]) == x] + extra, f"subject:{x}")
    for ids, text, key in MERGED_SUBJECTS:
        vals = " ".join("wd:" + i for i in ids)
        add("subject", text, films_with("VALUES ?x { %s } ?f wdt:P921 ?x." % vals)
            + by_genre_words(r"alien|extraterrestrial"), f"subject:{key}")
    add("subject", "Name a movie based on a comic book",
        films_with("?f wdt:P144 ?src. ?src wdt:P31/wdt:P279* wd:Q1004.") + by_genre_words(r"superhero|comic"),
        "subject:comics")

    # Year clues: 2010 onward only (decades and single years)
    by_year, by_decade = defaultdict(list), defaultdict(list)
    for k, f in films.items():
        for y in f.get("years") or [f["year"]]:
            by_year[y].append(k)
            by_decade[y // 10 * 10].append(k)
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

    # --- new kinds of clue
    print("countries, records, people...", flush=True)
    rows = sparql(f"""
SELECT DISTINCT ?f ?c WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  VALUES ?c {{ {" ".join("wd:" + c for c, _ in COUNTRIES)} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?f wdt:P495 ?c.
}}""")
    lang_rows = sparql(f"""
SELECT DISTINCT ?f ?lang WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?f wdt:P364 ?lang.
}}""")
    langs = defaultdict(set)
    for r in lang_rows:
        langs[qid(r["f"])].add(qid(r["lang"]))
    for c, text in COUNTRIES:
        ids = [qid(r["f"]) for r in rows if qid(r["c"]) == c]
        if c in COUNTRY_LANGUAGES:
            ok = set(COUNTRY_LANGUAGES[c])
            ids = [k for k in ids if langs[k] & ok]
        add("country", text, ids, f"country:{c}")
    add("country", "Name a foreign-language movie",
        films_with("?f wdt:P364 ?lang. FILTER(?lang != wd:Q1860) FILTER NOT EXISTS { ?f wdt:P364 wd:Q1860 }"),
        "country:foreign")
    add("record", "Name a billion-dollar movie",   # worldwide box office of $1 billion or more
        films_with("?f p:P2142/psv:P2142 ?v. ?v wikibase:quantityAmount ?amt; wikibase:quantityUnit wd:Q4917. "
                   "FILTER(?amt >= 1000000000)"), "record:billion")
    add("record", "Name a movie over three hours long", films_with("?f wdt:P2047 ?m. FILTER(?m >= 180)"),
        "record:3h")
    add("record", "Name a black-and-white movie", films_with("?f wdt:P462 wd:Q838368."), "record:bw")
    seq_rows = sparql(f"""
SELECT DISTINCT ?f ?prev WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?f wdt:P155 ?prev; wdt:P179 ?series.
}}""")
    # released after the film it follows (Star Wars Episode IV follows Episode III in story order only)
    add("record", "Name a sequel", [qid(r["f"]) for r in seq_rows
                                    if qid(r["prev"]) in films and qid(r["f"]) in films
                                    and films[qid(r["f"])]["year"] >= films[qid(r["prev"])]["year"]
                                    and films[qid(r["f"])]["first"] > films[qid(r["prev"])]["first"]],
        "record:sequel")
    add("award", "Name an Oscar-winning movie",
        films_with("?f wdt:P166 ?a. { ?a wdt:P31 wd:Q19020 } UNION { ?a wdt:P279 wd:Q19020 }"), "award:any-oscar")
    add("people", "Name a movie directed by a woman", films_with("?f wdt:P57 ?d. ?d wdt:P21 wd:Q6581072."),
        "people:woman-director")
    add("people", "Name a movie with a U.S. president as a character",
        films_with("?f wdt:P674 ?c. ?c wdt:P39 wd:Q11696."), "people:president")

    # title clues, from the English titles
    def words(label):
        return re.findall(r"[a-z0-9]+", label.lower().replace("'", ""))
    def title_has(word_list):
        ws = set(word_list)
        return [k for k, f in films.items() if ws & set(words(f["label"]))]
    add("title", "Name a movie with a number in the title",
        [k for k, f in films.items() if re.search(r"\d", f["label"]) or set(NUMBER_WORDS) & set(words(f["label"]))],
        "title:number")
    add("title", "Name a movie with a color in the title", title_has(COLOR_WORDS), "title:color")
    add("title", "Name a movie with an animal in the title", title_has(ANIMAL_WORDS), "title:animal")
    add("title", "Name a movie with a one-word title",
        [k for k, f in films.items() if len(words(f["label"])) == 1 and not f["label"][:1].isdigit()], "title:oneword")
    for L in LETTERS:
        # counts whether or not you include a leading "The", "A" or "An"
        ids = [k for k, f in films.items()
               if f["label"][:1].upper() == L or re.sub(r"(?i)^(the|a|an)\s+", "", f["label"])[:1].upper() == L]
        add("letter", f"Name a movie starting with the letter {L}", ids, f"letter:{L}")

    # --- a smaller set about big franchises and very famous names
    specs = [
        ("director", "wdt:P57", 30, "", "Name a movie directed by {}"),
        ("actor", "wdt:P161|wdt:P725", 90, "?p wdt:P31 wd:Q5. FILTER NOT EXISTS {{ ?p wdt:P106 wd:Q82955 }}",
         "Name a movie starring {}"),
        ("studio", "wdt:P272", 40, "", "Name a {} movie"),
        ("series", "wdt:P179", 15, "", "Name a {} movie"),
        ("author", "wdt:P144/wdt:P50", 80, "?p wdt:P31 wd:Q5.", "Name a movie based on a book by {}"),
    ]
    for kind, prop, ms, extra, tmpl in specs:
        print(f"{kind}...", flush=True)
        rel = relation(prop, ms, extra)
        by_label = {g["label"]: g["films"] for g in rel.values()}
        for k, g in rel.items():
            label = g["label"]
            if kind == "director" and label not in DIRECTORS:
                continue
            if kind == "studio":
                if label in STUDIOS and STUDIOS[label] in STUDIO_CLUES.values() or label in STUDIO_CLUES:
                    ids = set(g["films"])
                    if label == "Walt Disney Pictures":   # people call Frozen and Toy Story Disney movies too
                        ids |= by_label.get("Walt Disney Animation Studios", set()) | by_label.get("Pixar", set())
                    add(kind, STUDIO_CLUES.get(label, label), ids, f"{kind}:{k}")
                continue
            if kind == "series":
                if re.search(r"(?i)\blist\b|greatest|\bbest\b|\btop\b|\bphase\b|\b\d{2,}\b", label):
                    continue  # ranked lists, not franchises
                for suffix in (" film series", " series", " franchise", " (film series)", " in film", " trilogy"):
                    if label.lower().endswith(suffix):
                        label = label[: -len(suffix)]
                if label.lower().startswith("the "):
                    label = label[4:]
                if label.endswith("Universe"):
                    add(kind, f"Name a movie in the {label}", g["films"], f"{kind}:{k}")
                    continue
                # franchise lists in Wikidata have holes: any film with the franchise name in its
                # title counts too (Spider-Man: Across the Spider-Verse, The Lego Batman Movie...)
                if len(label) >= 4:
                    # title is the franchise name, or it followed by a colon/dash/number (Alien 3, Alien: Romulus)
                    rx = re.compile(r"^(the )?" + re.escape(label) + r"(\s*[:\-–—]|\s+\d|\s+(part|chapter)\b|$)", re.I)
                    g = dict(g, films=set(g["films"]) | {fk for fk, f in films.items() if rx.search(f["label"])})
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
        if q["kind"] == "director":
            return True   # hand-picked list
        if q["kind"] in ("actor", "author"):
            return sum(1 for f in q["ids"] if films[f]["s"] >= 60) >= 5
        return True
    questions[:] = [q for q in questions if famous_enough(q) and q["text"] not in SKIP_CLUES]
    # Too few answers to be fun ("Name a Back to the Future movie" has 3): retired. They stay in the
    # data so games people already played still load, but are never scheduled again.
    for q in questions:
        if q["kind"] in ("series", "director") and len(q["ids"]) < MIN_SMALL_ANSWERS:
            q["retired"] = True

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
    iconic = {city: {extra_ids[t]: n for n, t in enumerate(ts) if t in extra_ids}
              for city, ts in ICONIC_CITY_FILMS.items()}
    missing_iconic = [t for ts in ICONIC_CITY_FILMS.values() for t in ts if t not in extra_ids]
    if missing_iconic:
        print("ICONIC_CITY_FILMS titles not in the film list:", missing_iconic, flush=True)

    def place_score(k, city):
        if k in iconic.get(city, {}):
            return 10 ** 9 - iconic[city][k]          # pinned to the top, in the listed order
        score = films[k]["pop"] * min(1.0, 3 / max(1, place_counts.get(k, 1)))
        return score - (4000 if f"{k}|{city}" in weak_place else 0)

    def genre_score(k):
        return films[k]["pop"] * min(1.0, 4 / max(1, genre_counts.get(k, 1)))

    # In "starring" clues a voice-only role (Julie Andrews in Shrek 2) ranks well below an on-screen
    # role (Mary Poppins): people name the films an actor is known for, not every film they're in.
    print("voice roles...", flush=True)
    actor_spec = [x for x in specs if x[0] == "actor"][0]
    onscreen = {(a, f) for a, g in relation("wdt:P161", actor_spec[2], actor_spec[3]).items() for f in g["films"]}
    voiced = {(a, f) for a, g in relation("wdt:P725", actor_spec[2], actor_spec[3]).items() for f in g["films"]}
    # ...unless the voiced character is famous in its own right (Woody, Mushu) or is the title role (Rango)
    lead_voice = set()
    for r in sparql(f"""
SELECT DISTINCT ?f ?a ?cs ?charl WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?f p:P725 ?st. ?st ps:P725 ?a; pq:P453 ?char.
  ?a wikibase:sitelinks ?as. FILTER(?as >= {actor_spec[2]})
  OPTIONAL {{ ?char wikibase:sitelinks ?cs }}
  OPTIONAL {{ ?char rdfs:label ?charl. FILTER(LANG(?charl) = "en") }}
}}"""):
        fk = qid(r["f"])
        if (r["cs"] and int(r["cs"]) >= 5) or (fk in films and r["charl"] and r["charl"] in films[fk]["label"]):
            lead_voice.add((qid(r["a"]), fk))
    voice_only = voiced - onscreen - lead_voice

    def role_score(actor, k):
        # a minor voice part counts like being covered in 6 fewer big Wikipedias
        return films[k]["pop"] - (6000 if (actor, k) in voice_only else 0)

    def rank_key(q, j):
        k = order[j]
        if q["kind"] == "genre":
            return (-genre_score(k), j)
        if q["kind"] == "setting":
            return (-place_score(k, q["key"].split(":", 1)[1]), j)
        if q["kind"] == "actor":
            return (-role_score(q["key"].split(":", 1)[1], k), j)
        return (j, j)

    out = {
        "built": datetime.date.today().isoformat(),
        "source": "Wikidata (CC0). Popularity = coverage across the 20 largest Wikipedias.",
        "films": [[films[k]["label"], films[k]["year"], films[k]["pop"]] for k in order],
        "aliases": {str(index[k]): v for k, v in aliases.items() if k in index},
        "questions": [
            {"kind": q["kind"], "text": q["text"], "key": q["key"], **({"r": 1} if q.get("retired") else {}),
             "a": sorted((index[i] for i in q["ids"]),
                        key=lambda j: rank_key(q, j))}
            for q in sorted(questions, key=lambda q: q["key"])
        ],
    }
    pick_oscars(out["questions"], out["films"])
    out["daily"] = plan_daily(out["questions"])
    out["slots"] = DAILY_SLOTS
    out["games"] = plan_games(out["questions"])
    out["games_version"] = GAMES_VERSION
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
