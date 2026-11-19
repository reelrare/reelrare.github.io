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


def sparql(query, tries=10):
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
        except Exception as e:      # network hiccup: wait longer each time (up to ~15 minutes in all)
            print(f"  query failed ({e}); retry {i + 1}/{tries}", file=sys.stderr, flush=True)
            time.sleep(30 + 15 * i)
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
        # its premiere year and the year after both count (festival premiere, then wide release),
        # but not re-releases years later (Interstellar's IMAX re-release isn't "a 2025 movie")
        f["years"] = sorted({int(d[:4]) for d in f.get("dates", ())
                             if d <= today and f["year"] and int(d[:4]) - f["year"] <= 1})
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
VIEWS_MIN_MAJOR = 12    # films in at least this many big Wikipedias get page views looked up (the rest are estimated)
# Most points a film can earn, by its fame rank among all films: (tier, films in that group).
# 1 = Blockbuster, 2 = Studio Film, 3 = Award Movie; everything else can reach Indie. Must match index.html.
FAME_CAPS = [(1, 500), (2, 2000), (3, 6000)]

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
# Titles that settle a topic clue by themselves
SUBJECT_TITLE_WORDS = {
    "Q430": r"jurassic|dinosaur|land before time",
    "Q45393": r"\bpirates?\b",
    "Q182154": r"back to the future|time machine|\bterminator\b",
}
ROMCOMS = ("Q118612349", "Q860626")   # two Wikidata items for "romantic comedy"
SETTINGS = [
    ("Q60", "New York City"), ("Q84", "London"), ("Q65", "Los Angeles"), ("Q90", "Paris"),
    ("Q61", "Washington, D.C."), ("Q62", "San Francisco"), ("Q1297", "Chicago"),
    ("Q23768", "Las Vegas"), ("Q1490", "Tokyo"), ("Q220", "Rome"), ("Q8652", "Miami"),
    ("Q100", "Boston"), ("Q4169", "space"),
]
# Second source for city clues: English Wikipedia's "Films set in ..." categories (with sub-categories
# like "Films set in Manhattan"), which are far more complete than Wikidata's "narrative location".
WIKI_CITY_CATEGORIES = {
    "Q60": "Films set in New York City", "Q84": "Films set in London", "Q65": "Films set in Los Angeles",
    "Q90": "Films set in Paris", "Q23768": "Films set in the Las Vegas Valley",
    "Q61": "Films set in Washington, D.C.", "Q62": "Films set in San Francisco", "Q1297": "Films set in Chicago",
    "Q1490": "Films set in Tokyo", "Q220": "Films set in Rome", "Q8652": "Films set in Miami",
    "Q100": "Films set in Boston", "Q4169": "Films set in outer space",
}

# The films everyone thinks of first for each city. They always rank at the top (so they score as
# Blockbusters); data alone can't tell "a Vegas movie" from "a movie with one Vegas scene".
ICONIC_CITY_FILMS = {
    "Q23768": ["The Hangover", "Ocean's Eleven", "Casino", "Fear and Loathing in Las Vegas", "Rain Man", "Leaving Las Vegas"],
    "Q60": ["Ghostbusters", "Home Alone 2: Lost in New York", "Taxi Driver", "Elf", "The Devil Wears Prada", "Spider-Man"],
    "Q65": ["La La Land", "Pulp Fiction", "Once Upon a Time in Hollywood", "The Big Lebowski", "Heat", "Clueless"],
    "Q84": ["Notting Hill", "Love Actually", "Paddington", "Mary Poppins", "Bridget Jones's Diary", "Skyfall"],
    "Q90": ["Ratatouille", "Amélie", "Midnight in Paris", "Moulin Rouge!", "Before Sunset", "An American in Paris"],
    "Q1297": ["Ferris Bueller's Day Off", "Home Alone", "The Dark Knight", "The Blues Brothers", "The Untouchables", "Chicago"],
    "Q62": ["Mrs. Doubtfire", "Vertigo", "Inside Out", "Dirty Harry", "The Pursuit of Happyness", "Rise of the Planet of the Apes"],
    "Q61": ["All the President's Men", "Mr. Smith Goes to Washington", "The Exorcist", "National Treasure", "Captain America: The Winter Soldier", "Wonder Woman 1984"],
    "Q1490": ["Lost in Translation", "The Fast and the Furious: Tokyo Drift", "Kill Bill: Volume 1", "Your Name", "Akira", "Shoplifters"],
    "Q220": ["Roman Holiday", "Gladiator", "Angels & Demons", "Eat Pray Love", "La Dolce Vita", "When in Rome"],
    "Q8652": ["Scarface", "Bad Boys", "Moonlight", "Miami Vice", "There's Something About Mary", "The Birdcage"],
    "Q100": ["Good Will Hunting", "The Departed", "Ted", "Mystic River", "The Town", "Spotlight"],
    "Q4169": ["Gravity", "Interstellar", "2001: A Space Odyssey", "Alien", "Apollo 13", "WALL-E"],
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
    "Q100": r"\bboston\b",
    "Q4169": r"\bin space\b|\bouter space\b",
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
    "Name a movie starring Sigourney Weaver", "Name a movie starring Gwyneth Paltrow",
    "Name a movie starring Halle Berry", "Name a movie starring Jeremy Renner", "Name a movie starring Cate Blanchett",
    "Name a movie starring Stan Lee",          # cameos only
    "Name a Disney Renaissance movie",         # Wikidata lists only 4 of the 10 films
    "Name a Dollars movie",                    # the "Dollars Trilogy" isn't a name most people know
    "Name a movie based on a book by Charles Perrault", "Name a movie based on a book by Mario Puzo",
}
# Only Disney is a studio most people can name films for.
STUDIO_CLUES = {"Walt Disney Pictures": "Name a Disney movie", "Pixar": "Name a Pixar movie",
                "Walt Disney Animation Studios": "Name a Disney animated movie",
                "DreamWorks Animation": "Name a DreamWorks animated movie", "Studio Ghibli": "Name a Studio Ghibli movie"}
# Answers Wikidata has wrong for a clue (title must match exactly)
BAD_ANSWERS = {
    "Name a Pixar movie": {"Care Bears: Journey to Joke-a-lot", "Planes", "Planes: Fire & Rescue",
                           "Buzz Lightyear of Star Command: The Adventure Begins"},  # not Pixar films
    "Name a Toy Story movie": {"Buzz Lightyear of Star Command: The Adventure Begins"},          # TV pilot
    "Name a movie with a U.S. president as a character": {"Milk"},   # archival footage only
    "Name a movie based on a TV show": {"Backrooms"},                 # based on a web series
}

# Directors: only household names (current) and all-time legends. Label as in Wikidata.
DIRECTORS = {
    "Steven Spielberg", "Christopher Nolan", "Quentin Tarantino", "Martin Scorsese", "James Cameron",
    "Tim Burton", "Peter Jackson", "Ridley Scott", "Denis Villeneuve", "Wes Anderson", "Greta Gerwig",
    "Jordan Peele", "Guillermo del Toro", "David Fincher",
    "Alfred Hitchcock", "Stanley Kubrick",
    "Clint Eastwood", "Ron Howard", "Michael Bay", "Zack Snyder", "M. Night Shyamalan", "Spike Lee",
    "Guy Ritchie", "Taika Waititi", "Sam Raimi", "J. J. Abrams", "James Gunn", "Hayao Miyazaki",
    "Francis Ford Coppola", "George Lucas",
    "Robert Zemeckis", "John Hughes", "Rob Reiner", "Chris Columbus", "Jon Favreau", "Roland Emmerich",
    "John Carpenter", "Wes Craven", "Ang Lee", "Oliver Stone", "Sergio Leone", "Akira Kurosawa",
    "Billy Wilder", "Kathryn Bigelow", "Brad Bird", "Pete Docter",
}
# Directors who are also big stars: their "starring" clue stays too
ACTOR_DIRECTORS = {"Clint Eastwood"}

# Actors in 70-89 Wikipedias (below the usual bar of 90) who are household names anyway
MORE_ACTORS = {
    "Ewan McGregor", "Emma Thompson", "Cillian Murphy", "Mike Myers", "Tom Hardy", "Mark Ruffalo",
    "Helena Bonham Carter", "Bill Murray", "Willem Dafoe", "Idris Elba", "Elijah Wood", "Colin Firth",
    "Zoe Saldaña", "Elizabeth Olsen", "Ralph Fiennes", "Michael Keaton", "Tom Hiddleston", "Owen Wilson",
    "Kurt Russell", "Jeff Goldblum", "Christopher Walken", "Steve Buscemi", "Mark Hamill", "Joe Pesci",
    "Joseph Gordon-Levitt", "Lucy Liu", "Lupita Nyong'o", "Laurence Fishburne",
}

# Clues about the title itself (computed from English titles, so always accurate)
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWYZ"
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

# "Name a movie with the word "Love" in the title" (whole words, so Spider-Man counts for "Man")
TITLE_WORDS = ["Love", "Man", "Night", "Day", "Star", "Dead", "King", "Girl", "Boy", "War", "Life",
               "Home", "Big", "Little", "Last", "Dark", "World", "Lost", "Story", "Time",
               "Death", "Heart", "Monster", "Ghost", "Dragon", "City", "Island", "Planet", "Secret", "Wild",
               "Good", "Bad", "Queen", "Prince", "Princess", "Dream", "Summer", "House", "Fire", "Sun",
               "Moon", "Blood", "Kid", "Lady", "Return", "Rise", "Christmas", "Mission", "Game", "Baby",
               "Street", "Road", "River", "Sea", "Ocean", "Snow", "Ice", "Storm", "Water", "Money", "Doctor",
               "Captain", "Miss", "Kill", "Murder", "Killer", "Fear", "Evil", "Hell", "Heaven", "Angel",
               "Devil", "God", "Hotel", "School", "Club", "Party", "Wedding", "Family", "Father", "Mother",
               "Brother", "Sister", "Son", "Daughter", "Friend", "Woman", "Men", "Never", "Forever", "Tomorrow",
               "Legend", "Escape", "Hunt", "Rock", "Gun", "Space", "Earth", "Sky", "Paradise", "Shadow", "Light"]

# Genre + decade clues ("Name a 90s horror movie"): genre clue text -> how it reads with a decade
ERA_GENRES = {
    "Name an action movie": "action movie", "Name a comedy": "comedy", "Name a horror movie": "horror movie",
    "Name a sci-fi movie": "sci-fi movie", "Name an animated movie": "animated movie",
    "Name a romantic comedy": "romantic comedy", "Name a superhero movie": "superhero movie",
    "Name a thriller": "thriller", "Name a fantasy movie": "fantasy movie", "Name a Christmas movie": "Christmas movie",
    "Name a war movie": "war movie", "Name a musical": "musical",
    "Name a kids' movie": "kids' movie", "Name a spy movie": "spy movie", "Name a disaster movie": "disaster movie",
    "Name a Western": "Western", "Name a romance movie": "romance movie", "Name a heist movie": "heist movie",
}
# City + decade ("Name a 90s movie set in New York City"). (Genre + city was tried: Wikidata's genre
# tags are too loose for it, e.g. Mary Poppins as an "action movie set in London".)
ERA_CITIES = ["Q60", "Q65", "Q84", "Q90", "Q1297", "Q62", "Q23768", "Q61", "Q100", "Q8652", "Q1490", "Q220"]
ERA_DECADES = [(1980, "an 80s"), (1990, "a 90s"), (2000, "a 2000s"), (2010, "a 2010s"), (2020, "a 2020s")]

# The films everyone thinks of first for a genre. They rank at the top (so they score as
# Blockbusters) and always count; page views alone can be skewed by a season or a news story.
ICONIC_GENRE_FILMS = {
    "Name an action movie": ["Die Hard", "The Matrix", "Mad Max: Fury Road", "John Wick", "Terminator 2: Judgment Day", "Top Gun", "Lethal Weapon", "Speed", "Rambo", "Gladiator"],
    "Name a comedy": ["Home Alone", "The Hangover", "Superbad", "Mean Girls", "Dumb and Dumber", "Groundhog Day", "Step Brothers", "Anchorman: The Legend of Ron Burgundy", "Bridesmaids", "Ferris Bueller's Day Off"],
    "Name a Western": ["The Good, the Bad and the Ugly", "Django Unchained", "Unforgiven", "True Grit", "Tombstone", "Butch Cassidy and the Sundance Kid", "Once Upon a Time in the West", "The Magnificent Seven"],
    "Name a horror movie": ["The Shining", "Halloween", "The Exorcist", "Scream", "It", "Get Out", "A Nightmare on Elm Street", "The Conjuring", "Psycho", "Saw"],
    "Name a sci-fi movie": ["Star Wars: Episode IV – A New Hope", "The Matrix", "Alien", "Blade Runner", "Interstellar", "Back to the Future", "E.T. the Extra-Terrestrial", "The Terminator", "Inception", "Avatar"],
    "Name a romance movie": ["Titanic", "The Notebook", "Pride & Prejudice", "Casablanca", "La La Land", "Dirty Dancing", "Pretty Woman", "Romeo + Juliet", "A Walk to Remember", "Ghost"],
    "Name a superhero movie": ["The Avengers", "The Dark Knight", "Spider-Man", "Iron Man", "Black Panther", "Avengers: Endgame", "Wonder Woman", "Guardians of the Galaxy", "Superman", "Deadpool"],
    "Name a musical": ["The Sound of Music", "Grease", "The Greatest Showman", "Mamma Mia!", "Les Misérables", "West Side Story", "Chicago", "Singin' in the Rain", "Hairspray", "The Wizard of Oz"],
    "Name a war movie": ["Saving Private Ryan", "Apocalypse Now", "Full Metal Jacket", "Dunkirk", "1917", "Platoon", "Hacksaw Ridge", "Black Hawk Down", "Inglourious Basterds", "Pearl Harbor"],
    "Name a Christmas movie": ["Home Alone", "Elf", "The Polar Express", "It's a Wonderful Life", "Love Actually", "Dr. Seuss' How the Grinch Stole Christmas", "A Christmas Story", "The Nightmare Before Christmas", "The Santa Clause", "Miracle on 34th Street"],
    "Name a spy movie": ["Casino Royale", "Skyfall", "Mission: Impossible", "The Bourne Identity", "Goldfinger", "Kingsman: The Secret Service", "Tinker Tailor Soldier Spy", "Spy"],
    "Name a fantasy movie": ["The Lord of the Rings: The Fellowship of the Ring", "Harry Potter and the Philosopher's Stone", "The Wizard of Oz", "The Princess Bride", "Pan's Labyrinth", "The Chronicles of Narnia: The Lion, the Witch and the Wardrobe", "Willow", "Labyrinth"],
    "Name a thriller": ["The Silence of the Lambs", "Seven", "Gone Girl", "Shutter Island", "Psycho", "Jaws", "Prisoners", "The Sixth Sense", "Zodiac", "Rear Window"],
    "Name a zombie movie": ["Shaun of the Dead", "28 Days Later", "World War Z", "Zombieland", "Dawn of the Dead", "Night of the Living Dead", "Train to Busan", "I Am Legend"],
    "Name a disaster movie": ["Titanic", "Twister", "The Day After Tomorrow", "2012", "Armageddon", "Deep Impact", "San Andreas", "Independence Day", "Volcano", "Dante's Peak"],
    "Name an animated movie": ["Toy Story", "The Lion King", "Frozen", "Shrek", "Finding Nemo", "Spirited Away", "Up", "Moana", "Aladdin", "Coco"],
    "Name a romantic comedy": ["When Harry Met Sally", "Notting Hill", "Pretty Woman", "10 Things I Hate About You", "Crazy Rich Asians", "How to Lose a Guy in 10 Days", "Love Actually", "The Proposal", "Sleepless in Seattle", "Clueless"],
    "Name a dystopian movie": ["The Hunger Games", "Blade Runner", "The Matrix", "V for Vendetta", "Children of Men", "Divergent", "Mad Max: Fury Road", "The Maze Runner", "Snowpiercer"],
    "Name a coming-of-age movie": ["Stand by Me", "The Breakfast Club", "Lady Bird", "The Perks of Being a Wallflower", "Boyhood", "Superbad", "Dazed and Confused", "Eighth Grade", "Juno"],
    "Name a kids' movie": ["Toy Story", "Finding Nemo", "Shrek", "Frozen", "The Lion King", "Matilda", "Home Alone", "Paddington", "Despicable Me", "The Incredibles"],
    "Name a heist movie": ["Ocean's Eleven", "The Italian Job", "Inception", "Heat", "Baby Driver", "The Town", "Reservoir Dogs", "Now You See Me"],
}

# Topic clues: (text, key, Wikidata main-subject label regex, Wikidata genre label regex).
# Both are matched against English labels, so "boxing film" and a film about "boxing" both count.
TOPIC_CLUES = [
    ("Name a sports movie", "sports", r"^(boxing|baseball|basketball|association football|american football|golf|horse racing|ice hockey|tennis|surfing|wrestling|mixed martial arts)$", r"sports? film|boxing film|football film|basketball film|baseball film"),
    ("Name a boxing movie", "boxing", r"^boxing$", r"boxing film"),
    ("Name a vampire movie", "vampire", r"^vampires?$", r"vampire"),
    ("Name a ghost movie", "ghost", r"^ghosts?$|haunted house", r"ghost film"),
    ("Name a werewolf movie", "werewolf", r"^werewol", r"werewolf"),
    ("Name a witch movie", "witch", r"^witch|witchcraft", r"\bwitch"),
    ("Name a martial arts movie", "martialarts", r"martial arts|kung fu|karate", r"martial arts|kung fu|ninja film"),
    ("Name a buddy cop movie", "buddycop", None, r"buddy cop"),
    ("Name a road trip movie", "roadtrip", r"road trip", r"road movie"),
    ("Name a prison movie", "prison", r"^prison|imprisonment|prison escape", r"prison film"),
    ("Name a courtroom movie", "courtroom", r"^trial$|courtroom", r"trial film|legal drama|courtroom"),
    ("Name a detective movie", "detective", r"^detective|private investigator", r"detective film|whodunit"),
    ("Name a survival movie", "survival", r"^survival$", r"survival film"),
    ("Name a giant monster movie", "kaiju", r"kaiju|giant monster", r"kaiju|giant monster"),
    ("Name a parody movie", "parody", None, r"parody|spoof"),
    ("Name a movie with a time loop", "timeloop", r"time loop", r"time loop"),
    ("Name a Vietnam War movie", "vietnam", r"vietnam war", r"vietnam war"),
    ("Name a movie about the Holocaust", "holocaust", r"the holocaust", r"holocaust"),
    ("Name a shark movie", "shark", r"^sharks?$|great white shark", r"\bshark"),
    ("Name a body-swap movie", "bodyswap", r"body swap", r"body swap"),
    ("Name a car movie", "cars", r"^cars?$|auto racing|street racing|motor racing|^automobile", r"car chase|auto racing|racing film"),
    ("Name a movie with a robot", "robot", r"^robots?$|^android$", r"\brobot|mecha"),
    ("Name a movie about a dog", "dog", r"^dogs?$", r"\bdog film"),
    ("Name a movie about a cat", "cat", r"^cats?$", None),
    ("Name a movie about a horse", "horse", r"^horses?$|horse racing", None),
    ("Name a movie set in ancient Rome or Greece", "ancient", r"ancient rome|ancient greece|greek mythology|roman empire|roman republic", r"sword-and-sandal|peplum"),
    ("Name a medieval movie", "medieval", r"middle ages|medieval|crusades|^knights?$", r"medieval"),
    ("Name a dance movie", "dance", r"^danc", r"dance film"),
    ("Name a movie about a musician", "musician", r"^musician|^singer|^rock band|musical group", r"music-themed"),
    ("Name a high school movie", "highschool", r"high school", r"high school"),
    ("Name a summer camp movie", "camp", r"summer camp", r"summer camp"),
]
ICONIC_TOPIC_FILMS = {
    "Name a sports movie": ["Rocky", "Space Jam", "Remember the Titans", "The Blind Side", "Moneyball", "Creed", "Million Dollar Baby", "Field of Dreams", "Air", "Raging Bull"],
    "Name a boxing movie": ["Rocky", "Creed", "Raging Bull", "Million Dollar Baby", "The Fighter", "Cinderella Man", "Southpaw", "Real Steel"],
    "Name a vampire movie": ["Twilight", "Interview with the Vampire", "Bram Stoker's Dracula", "Blade", "Hotel Transylvania", "Nosferatu", "What We Do in the Shadows", "Let the Right One In", "Sinners"],
    "Name a ghost movie": ["Ghostbusters", "The Sixth Sense", "Poltergeist", "Ghost", "Beetlejuice", "The Others", "Casper", "The Conjuring"],
    "Name a werewolf movie": ["An American Werewolf in London", "The Twilight Saga: New Moon", "Teen Wolf", "The Wolfman", "The Howling", "Underworld"],
    "Name a witch movie": ["Hocus Pocus", "The Witches", "Practical Magic", "The Craft", "The Witch", "Kiki's Delivery Service", "Wicked", "The Wizard of Oz"],
    "Name a martial arts movie": ["Enter the Dragon", "Kung Fu Panda", "The Karate Kid", "Crouching Tiger, Hidden Dragon", "Kill Bill: Volume 1", "Ip Man", "Shang-Chi and the Legend of the Ten Rings", "Rush Hour", "Kung Fu Hustle"],
    "Name a buddy cop movie": ["Lethal Weapon", "Rush Hour", "Bad Boys", "21 Jump Street", "The Nice Guys", "Hot Fuzz", "Men in Black", "The Other Guys", "Beverly Hills Cop"],
    "Name a road trip movie": ["Thelma & Louise", "Little Miss Sunshine", "Dumb and Dumber", "Planes, Trains and Automobiles", "National Lampoon's Vacation", "Due Date", "Easy Rider", "Green Book", "Zombieland"],
    "Name a prison movie": ["The Shawshank Redemption", "The Green Mile", "Escape from Alcatraz", "Con Air", "The Great Escape", "Cool Hand Luke", "Papillon", "Escape Plan"],
    "Name a courtroom movie": ["A Few Good Men", "12 Angry Men", "To Kill a Mockingbird", "Legally Blonde", "My Cousin Vinny", "Philadelphia", "Primal Fear", "Kramer vs. Kramer", "Anatomy of a Fall"],
    "Name a detective movie": ["Knives Out", "Sherlock Holmes", "Glass Onion: A Knives Out Mystery", "Murder on the Orient Express", "Chinatown", "The Nice Guys", "Seven"],
    "Name a survival movie": ["Cast Away", "The Revenant", "127 Hours", "The Martian", "Life of Pi", "Alive", "The Grey", "Gravity"],
    "Name a giant monster movie": ["Godzilla", "King Kong", "Pacific Rim", "Kong: Skull Island", "Cloverfield", "Godzilla Minus One", "Rampage", "Godzilla vs. Kong"],
    "Name a parody movie": ["Airplane!", "Scary Movie", "Spaceballs", "The Naked Gun: From the Files of Police Squad!", "Hot Shots!", "Austin Powers: International Man of Mystery", "Young Frankenstein", "Shrek"],
    "Name a movie with a time loop": ["Groundhog Day", "Edge of Tomorrow", "Happy Death Day", "Palm Springs", "Source Code", "Before I Fall", "Boss Level"],
    "Name a Vietnam War movie": ["Apocalypse Now", "Platoon", "Full Metal Jacket", "Forrest Gump", "The Deer Hunter", "Good Morning, Vietnam", "We Were Soldiers", "Rambo: First Blood Part II"],
    "Name a movie about the Holocaust": ["Schindler's List", "Life Is Beautiful", "The Pianist", "The Boy in the Striped Pajamas", "Jojo Rabbit", "The Zone of Interest"],
    "Name a shark movie": ["Jaws", "The Meg", "Deep Blue Sea", "The Shallows", "47 Meters Down", "Jaws 2", "Open Water"],
    "Name a body-swap movie": ["Freaky Friday", "Face/Off", "The Hot Chick", "The Change-Up", "Freaky", "Your Name", "13 Going on 30"],
    "Name a car movie": ["Cars", "The Fast and the Furious", "Rush", "Ford v Ferrari", "Days of Thunder", "Talladega Nights: The Ballad of Ricky Bobby", "Baby Driver", "Gone in 60 Seconds", "Mad Max: Fury Road", "F1"],
    "Name a movie with a robot": ["The Terminator", "WALL-E", "Big Hero 6", "I, Robot", "RoboCop", "Transformers", "Ex Machina", "The Iron Giant", "Real Steel", "The Wild Robot"],
    "Name a movie about a dog": ["Marley & Me", "Beethoven", "101 Dalmatians", "Bolt", "Hachi: A Dog's Tale", "A Dog's Purpose", "Lady and the Tramp", "Isle of Dogs", "Turner & Hooch", "Air Bud"],
    "Name a movie about a cat": ["Garfield: The Movie", "Puss in Boots", "The Aristocats", "Flow", "Cats", "That Darn Cat!"],
    "Name a movie about a horse": ["War Horse", "Seabiscuit", "Black Beauty", "Spirit: Stallion of the Cimarron", "Secretariat", "The Black Stallion", "National Velvet", "Dreamer"],
    "Name a movie set in ancient Rome or Greece": ["Gladiator", "300", "Troy", "Ben-Hur", "Spartacus", "Hercules", "Clash of the Titans", "Gladiator II", "Cleopatra"],
    "Name a medieval movie": ["Braveheart", "A Knight's Tale", "Monty Python and the Holy Grail", "Kingdom of Heaven", "Robin Hood", "The Green Knight", "The Last Duel", "Shrek"],
    "Name a dance movie": ["Dirty Dancing", "Step Up", "Footloose", "Save the Last Dance", "Billy Elliot", "Black Swan", "Saturday Night Fever", "Flashdance", "Magic Mike", "Stomp the Yard"],
    "Name a movie about a musician": ["Bohemian Rhapsody", "Rocketman", "Walk the Line", "Ray", "Elvis", "A Star Is Born", "Whiplash", "School of Rock", "Almost Famous", "A Complete Unknown"],
    "Name a high school movie": ["Mean Girls", "Clueless", "The Breakfast Club", "Superbad", "Grease", "10 Things I Hate About You", "Ferris Bueller's Day Off", "Easy A", "Booksmart", "Napoleon Dynamite"],
    "Name a summer camp movie": ["The Parent Trap", "Friday the 13th", "Wet Hot American Summer", "Meatballs", "Moonrise Kingdom", "Heavyweights", "Friday the 13th Part 2"],
    "Name an anime movie": ["Spirited Away", "Your Name", "Akira", "My Neighbor Totoro", "Princess Mononoke", "Howl's Moving Castle", "Demon Slayer: Kimetsu no Yaiba the Movie: Mugen Train", "Suzume", "Pokémon: The First Movie", "Ghost in the Shell"],
}

# Clues on one theme never share a game ("set in Las Vegas" + "about gambling", "Pixar" + "Toy Story").
# Matched against clue text; clues whose best-known answers overlap a lot also never share a game.
THEMES = {
    "vegas": r"las vegas|gambling|casino",
    "war": r"\bwar\b|world war|cold war|\bspy\b|james bond|ian fleming|mission: impossible|holocaust",
    "hero": r"superhero|comic|marvel|\bdc\b|batman|spider-man|x-men|avengers|superman|stan lee|justice league",
    "horror": r"horror|zombie|serial killer|outbreak|slasher|stephen king|monster|vampire|ghost|werewolf|witch|shark",
    "sport": r"sport|boxing|car movie|horse",
    "crimeplus": r"prison|courtroom|detective|buddy cop|heist",
    "animal": r"\bdog\b|\bcat\b|horse|shark|animal",
    "ancient": r"ancient|medieval|gladiator",
    "school": r"high school|summer camp|coming-of-age|teen",
    "road": r"road trip|car movie",
    "scifi": r"sci-fi|alien|time travel|time loop|dystopian|star wars|star trek|terminator|artificial|matrix|transformers|planet of the apes|set in space|robot|giant monster|body-swap",
    "kids": r"kids|animated|ghibli|animation|pixar|disney|toy story|ice age|shrek|dreamworks|despicable|madagascar|miyazaki|kung fu panda|how to train|fairy tale|based on a toy|roald dahl|dr\. seuss",
    "romance": r"romance|romantic|nicholas sparks|jane austen",
    "comedy": r"comedy|comedies",
    "fantasy": r"fantasy|lord of the rings|harry potter|rowling|tolkien|hobbit|wizarding|narnia|middle-earth",
    "pirate": r"pirate|caribbean",
    "crime": r"\bmob\b|crime|heist|gangster|godfather|scarface",
    "dino": r"dinosaur|jurassic",
    "xmas": r"christmas",
    "oscar": r"best picture|oscar|academy award|animated feature",
    "music": r"musical|musician|dance movie",
    "west": r"western",
    "japan": r"japan|tokyo|miyazaki|anime|godzilla|ghibli|giant monster|martial arts",
    "asia": r"korea|chin(a|ese)|hong kong",
    "spanish": r"spanish|mexic",
    "france": r"paris|french",
    "italy": r"\brome\b|italian",
    "uk": r"london|british|james bond|harry potter",
    "space": r"space|star wars|star trek|alien",
    "nyc": r"new york", "la": r"los angeles|hollywood", "chicago": r"chicago", "sf": r"san francisco",
    "dc": r"washington|president", "boston": r"boston", "miami": r"miami",
}

# Films from a country (country of origin)
COUNTRIES = [("Q17", "Name a movie from Japan"), ("Q142", "Name a French movie"),
             ("Q145", "Name a British movie"), ("Q38", "Name an Italian movie"),
             ("Q668", "Name a movie from India"),
             ("Q29", "Name a Spanish movie"), ("Q183", "Name a German movie")]
# Wikidata rarely gives Korean and Chinese films a country, so these go by original language
LANGUAGE_CLUES = [(("Q9176",), "Name a Korean movie", "korean"),
                  (("Q7850", "Q9192", "Q9186"), "Name a Chinese movie", "chinese")]
# Co-productions list many countries (Ratatouille isn't Japanese), so for these the film must also be
# in one of the country's languages.
COUNTRY_LANGUAGES = {
    "Q17": ["Q5287"],                       # Japanese
    "Q142": ["Q150"],                       # French
    "Q38": ["Q652"],                        # Italian
    "Q29": ["Q1321", "Q7026", "Q8752", "Q9307"],   # Spanish, Catalan, Basque, Galician
    "Q183": ["Q188"],                       # German
    "Q668": ["Q1568", "Q5885", "Q8097", "Q36236", "Q33673", "Q9610", "Q1571", "Q58635"],  # Indian languages
}

# Each day: 5 broad clues + 2 about a big franchise or a famous name, in shuffled order.
# Within a slot a category is picked first, then a clue, so year clues (one option in one
# slot) come up roughly once every four days.
DAILY_SLOTS = [["genre", "subject", "era"], ["title", "letter", "record"], ["setting", "country", "subject"],
               ["award", "record", "genre", "people", "era"],
               ["genre", "setting", "subject", "year", "title", "letter", "country", "era"],
               ["series", "studio", "author"], ["actor", "director", "people"]]


# At most this many clues of one kind in a game (everything else: one each)
KIND_CAP = {"genre": 2, "era": 2, "actor": 2, "subject": 2, "title": 2}
GAMES_VERSION = 4   # bump to rebuild every Unlimited game with new rules
DAILY_VERSION = 2   # bump to re-plan every future daily puzzle with new rules
# Every clue is used once, ever: in one daily puzzle or in one Unlimited game, never both.
# Each clue's lane is fixed by its key (so it never moves between nightly builds); this share
# of clues goes to Unlimited, the rest to the daily puzzles.
UNLIMITED_SHARE = 30   # percent
LOW_DAILY_WARNING = 30   # warn when fewer than this many days of fresh daily clues are left


def lane(key):
    import hashlib
    return "unlimited" if int(hashlib.sha1(key.encode()).hexdigest(), 16) % 100 < UNLIMITED_SHARE else "daily"


def clue_clashes(questions):
    """Pairs of clues that shouldn't share a game: the same theme (Las Vegas + gambling, a horror
    movie + a zombie movie), the same decade (a 2010s comedy + a movie from 2014), or several of
    the same best-known answers (Tom Cruise + Mission: Impossible)."""
    themes = {}
    for q in questions:
        t = {name for name, rx in THEMES.items() if re.search(rx, q["text"], re.I)}
        m = re.search(r"(?:decade|year|era:[^:]+):(\d{4})$", q["key"])
        if m:
            t.add(f"decade{int(m.group(1)) // 10 * 10}")
        themes[q["key"]] = t
    top = {q["key"]: set(q["a"][:8]) for q in questions}
    keys = list(themes)
    clash = defaultdict(set)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if themes[a] & themes[b] or len(top[a] & top[b]) >= 3:
                clash[a].add(b)
                clash[b].add(a)
    return clash


def deal(rnd, by_kind, fresh, clash, relax=True):
    """Pick 7 fresh clues (never used before): one per slot, at most KIND_CAP of a kind, no two on
    the same theme. Kinds with more fresh clues left come up more often, so every kind runs out at
    about the same time. Returns None when 7 can't be found. With relax, the theme and kind rules
    are dropped (in that order) rather than fail."""
    for strict in ((True, False) if relax else (True,)):      # theme rule first
        picks, used = [], defaultdict(int)
        def ok(k):
            return k in fresh and k not in picks and not (strict and clash.get(k, set()) & set(picks))
        for slot in DAILY_SLOTS:
            def open_kinds(kinds, cap=True):
                return [kd for kd in kinds if (not cap or used[kd] < KIND_CAP.get(kd, 1))
                        and any(ok(k) for k in by_kind.get(kd, []))]
            kinds = (open_kinds(slot) or open_kinds(sorted(by_kind))
                     or (open_kinds(sorted(by_kind), cap=False) if relax else []))
            if not kinds:
                break
            kind = rnd.choices(kinds, weights=[sum(1 for k in by_kind[kd] if ok(k)) for kd in kinds])[0]
            picks.append(rnd.choice(sorted(k for k in by_kind[kind] if ok(k))))
            used[kind] += 1
        if len(picks) == DAILY_SIZE:
            rnd.shuffle(picks)
            return picks
    return None


def previous_output():
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, encoding="utf-8") as fh:
                return json.loads(fh.read().split("=", 1)[1].strip().rstrip(";"))
        except (ValueError, IndexError):
            pass
    return {}


def plan_all(questions):
    """Daily puzzles and Unlimited games, with no clue ever used twice anywhere.
    Kept as published: every past day and today, every Unlimited game already numbered, and future
    days already planned under the same rules. Only what's new is dealt, from clues never used."""
    import random
    all_keys = {q["key"] for q in questions}
    active = {q["key"] for q in questions if not q.get("r")}
    clash = clue_clashes([q for q in questions if q["key"] in active])
    prev = previous_output()
    today = datetime.date.today().isoformat()
    old_daily = prev.get("daily", {})
    same_rules = prev.get("daily_version") == DAILY_VERSION
    # published days are never changed (even ones from before this rule, with repeats)
    daily = {d: ks for d, ks in old_daily.items() if d <= today and all(k in all_keys for k in ks)}
    used = {k for ks in daily.values() for k in ks}
    games = []
    if prev.get("games_version") == GAMES_VERSION:
        for g in prev.get("games", []):
            if all(k in active and k not in used for k in g):
                games.append(g)
                used.update(g)
            else:
                break          # numbering must stay stable: stop at the first game that can't be kept
    lanes = {k: lane(k) for k in active}
    by_kind = defaultdict(list)
    for q in questions:
        if q["key"] in active:
            by_kind[q["kind"]].append(q["key"])

    # daily: from yesterday (time zones) to a month ahead
    base = datetime.date.today()
    fresh_daily = {k for k in active if lanes[k] == "daily"} - used
    reused = []
    for n in range(-1, DAILY_AHEAD + 1):
        day = (base + datetime.timedelta(days=n)).isoformat()
        if day in daily:
            continue
        planned = old_daily.get(day)
        rnd = random.Random("reelrare-v2-" + day)
        if same_rules and planned and all(k in fresh_daily for k in planned):
            pick = planned
        else:
            pick = deal(rnd, by_kind, fresh_daily, clash)
        if pick is None:
            # out of fresh daily clues: borrow unused Unlimited ones, then (last resort) the daily
            # clues used longest ago. The build warns long before this happens.
            spare = {k for k in active if lanes[k] == "unlimited"} - used
            pick = deal(rnd, by_kind, fresh_daily | spare, clash)
            if pick is None:
                last = {}
                for d in sorted(daily):
                    for k in daily[d]:
                        last[k] = d
                oldest = sorted((k for k in active if lanes[k] == "daily"), key=lambda k: last.get(k, ""))
                pick = deal(rnd, by_kind, set(oldest[: max(60, len(oldest) // 3)]), clash)
                reused.append(day)
        daily[day] = pick
        used.update(pick)
        fresh_daily -= set(pick)
    if reused:
        print(f"WARNING: no fresh clues left; {len(reused)} daily puzzles reuse old clues ({reused[0]} on)", flush=True)
    left_days = len(fresh_daily) // DAILY_SIZE
    print(f"daily: {len(fresh_daily)} fresh clues left after {max(daily)} = about {left_days} more days", flush=True)
    if left_days < LOW_DAILY_WARNING:
        print(f"WARNING: daily clues run out in about {left_days} days after {max(daily)}: add clues", flush=True)

    # Unlimited: new games from Unlimited clues never used anywhere, until fewer than 7 are left
    fresh_unl = {k for k in active if lanes[k] == "unlimited"} - used
    n = len(games)
    while True:
        n += 1
        pick = deal(random.Random(f"reelrare-unlimited-v{GAMES_VERSION}-{n}"), by_kind, fresh_unl, clash, relax=False)
        if pick is None:
            break
        games.append(pick)
        fresh_unl -= set(pick)
    print(f"unlimited: {len(games)} games, {len(fresh_unl)} clues waiting for a full game", flush=True)
    return dict(sorted(daily.items())), games


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


def fetch_pageviews(films, major):
    """English Wikipedia page views (last 60 days) for every reasonably known film, 50 titles per
    request at a gentle pace. One sample is saved per month in wiki_cache/pageviews.json and the
    last six are averaged, which evens out seasonal spikes (horror in October, Christmas films in
    December). If Wikipedia can't be reached, the saved samples are used."""
    path = os.path.join(WIKI_CACHE_DIR, "pageviews.json")
    store = {"samples": {}}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            store = json.load(fh)
    month = datetime.date.today().strftime("%Y-%m")
    titles = {}
    for r in sparql(f"""
SELECT ?f ?title WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?a schema:about ?f; schema:isPartOf <https://en.wikipedia.org/>; schema:name ?title.
}}"""):
        k = qid(r["f"])
        if k in films and major.get(k, 0) >= VIEWS_MIN_MAJOR:
            titles[k] = r["title"]
    sample = store["samples"].setdefault(month, {})
    todo = [] if os.environ.get("NO_VIEW_FETCH") else [k for k in titles if k not in sample]
    print(f"page views: {len(titles)} films, {len(todo)} to fetch for {month}", flush=True)
    by_title = {}
    for k in todo:
        by_title.setdefault(titles[k], []).append(k)
    names = list(by_title)
    try:
        for b in range(0, len(names), 50):
            batch = names[b:b + 50]
            cont = {}
            while True:
                params = {"action": "query", "prop": "pageviews", "pvipdays": "60", "titles": "|".join(batch),
                          "format": "json", "formatversion": "2", **cont}
                url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
                for attempt in range(6):
                    try:
                        req = urllib.request.Request(url, headers={"User-Agent": UA})
                        with urllib.request.urlopen(req, timeout=60) as r:
                            data = json.load(r)
                        break
                    except urllib.error.HTTPError as e:
                        if e.code != 429 or attempt == 5:
                            raise
                        time.sleep(int(e.headers.get("Retry-After") or 0) or 15 * (attempt + 1))
                renamed = {n["to"]: n["from"] for n in data["query"].get("normalized", [])}
                for page in data["query"]["pages"]:
                    t = renamed.get(page["title"], page["title"])
                    if "pageviews" in page:
                        views = sum(v or 0 for v in page["pageviews"].values())
                        for k in by_title.get(t, []):
                            sample[k] = views
                if "continue" not in data:
                    break
                cont = data["continue"]
                time.sleep(1)
            time.sleep(1)
            if b % 2500 == 0:
                print(f"  {b}/{len(names)}", flush=True)
    except Exception as e:
        print(f"  page views unavailable ({e}); using saved samples", flush=True)
    keep = sorted(store["samples"])[-6:]
    store["samples"] = {m: store["samples"][m] for m in keep}
    os.makedirs(WIKI_CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(store, fh, separators=(",", ":"))
    avg = {}
    for k in titles:
        vals = [store["samples"][m][k] for m in keep if k in store["samples"][m]]
        if vals:
            avg[k] = sum(vals) / len(vals)
    return avg


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
        # the jackpot is never a famous film (cap 4 = outside the global top 6,000), but still one a
        # film fan could know (franchises: a proper, well-read entry)
        bar = 17000 if q["kind"] in ("series", "studio") else 15000
        deep_ok = [i for i in deep if film_rows[i][3] == 4] or deep
        findable = ([i for i in deep_ok if film_rows[i][2] >= bar] or [i for i in deep_ok if film_rows[i][2] >= 12000]
                    or deep_ok)
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
    views = fetch_pageviews(films, major)
    import math
    # Films with no page-view reading (not looked up, or a run that was cut short) get the average
    # for films covered by the same number of big Wikipedias, so a missing reading never makes a
    # famous film look obscure.
    by_major = defaultdict(list)
    for k, v in views.items():
        by_major[major.get(k, 0)].append(math.log10(v + 10))
    est = {m: sum(x) / len(x) for m, x in by_major.items() if len(x) >= 20}
    this_year = datetime.date.today().year
    def log_views(k):
        if k in views:
            lv = math.log10(views[k] + 10)
            # brand-new releases get a burst of attention that doesn't mean people know them yet
            age = this_year - films[k]["year"]
            return lv - (1.0 if age <= 0 else 0.5 if age == 1 else 0)
        m = major.get(k, 0)
        near = [est[x] for x in sorted(est, key=lambda x: abs(x - m))[:1]]
        return min(near[0], 1 + 0.2 * m) if near and m < VIEWS_MIN_MAJOR else (near[0] if near else 1 + 0.2 * m)
    for k, f in films.items():
        # how well known a film is: English Wikipedia page views (log scale), blended with how many
        # of the 20 biggest Wikipedias cover it. ~5,000 = barely known, ~24,000 = everyone knows it.
        f["pop"] = round(1000 * (2.5 * log_views(k) + 0.5 * major.get(k, 0))) + min(f["s"], 999) // 100
        if k in NOT_FAMOUS:
            f["pop"] = min(f["s"], 999) // 20
    # Points cap by fame across ALL films, whatever the clue: the best-known films can never be
    # a deep cut just because a clue has thousands of answers (Home Alone is never "Indie").
    ranked = sorted(films, key=lambda k: -films[k]["pop"])
    for n, k in enumerate(ranked):
        films[k]["cap"] = next((tier for tier, limit in FAME_CAPS if n < limit), 4)
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
    missing = sorted({t for d in (EXTRA_ANSWERS, ICONIC_GENRE_FILMS, ICONIC_TOPIC_FILMS) for ts in d.values() for t in ts
                      if t not in extra_ids})
    if missing:
        print("EXTRA_ANSWERS titles not in the film list:", missing, flush=True)

    def add(kind, text, film_ids, key):
        film_ids = list(film_ids) + [extra_ids[t] for t in EXTRA_ANSWERS.get(text, []) + ICONIC_GENRE_FILMS.get(text, [])
                                     + ICONIC_TOPIC_FILMS.get(text, []) if t in extra_ids]
        bad = BAD_ANSWERS.get(text, set())
        valid = [f for f in set(film_ids) if f in films and films[f]["label"] not in bad]
        mainstream = sum(1 for f in valid if films[f]["s"] >= MAINSTREAM_SITELINKS)
        # franchises and single studios are small but famous: a lower bar
        # small but famous sets (franchises, a hand-picked director, record-breakers) get a lower bar
        need_all, need_main = ((3, 1) if kind == "director" else      # hand-picked famous names
                               (3, 3) if kind == "series" else
                               (4, 3) if kind in ("series", "studio", "record", "people", "author")
                               else (8, 5) if key.startswith("title:word:")
                               else (8, 6) if key.startswith("country:lang:")
                               # topic tags are patchy: a clue needs plenty of answers, or right ones get rejected
                               else (30, 6) if text in ICONIC_TOPIC_FILMS
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
    # genre + decade ("Name a 90s horror movie"), by first release year
    era_base, era_city, city_sets = {}, {}, {}
    genre_qs = {q["text"]: q for q in questions if q["kind"] == "genre"}
    for gtext, phrase in ERA_GENRES.items():
        if gtext not in genre_qs:
            continue
        for d, words_ in ERA_DECADES:
            key = f"era:{genre_qs[gtext]['key'].split(':', 1)[1]}:{d}"
            add("era", f"Name {words_} {phrase}", [k for k in genre_qs[gtext]["ids"] if d <= films[k]["year"] < d + 10], key)
            era_base[key] = gtext

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
        set_in |= {extra_ids[t] for t in ICONIC_CITY_FILMS.get(p, []) if t in extra_ids}   # pinned films always count
        shot_in = {qid(r["f"]) for r in filmed if qid(r["p"]) == p}
        rx = re.compile(CITY_TITLE_WORDS[p], re.I) if p in CITY_TITLE_WORDS else None
        titled = {k for k, f in films.items() if rx and rx.search(f["label"])}
        for k in (shot_in - set_in - titled):
            weak_place.add(k + "|" + p)
        add("setting", f"Name a movie set in {name}", set_in | shot_in | titled, f"setting:{p}")
        city_sets[p] = (name, {k for k in set_in | titled if k in films})      # combined clues use only "set in" (not "shot in")
    for p in ERA_CITIES:
        name, ids = city_sets[p]
        for d, words_ in ERA_DECADES:
            add("era", f"Name {words_} movie set in {name}", [k for k in ids if d <= films[k]["year"] < d + 10],
                f"era:setting-{p}:{d}")
            era_city[f"era:setting-{p}:{d}"] = p
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
        if x in SUBJECT_TITLE_WORDS:     # Jurassic World: Fallen Kingdom is plainly a dinosaur movie
            rx = re.compile(SUBJECT_TITLE_WORDS[x], re.I)
            extra += [k for k, f in films.items() if rx.search(f["label"])]
        add("subject", text, [qid(r["f"]) for r in rows if qid(r["x"]) == x] + extra, f"subject:{x}")
    for ids, text, key in MERGED_SUBJECTS:
        vals = " ".join("wd:" + i for i in ids)
        add("subject", text, films_with("VALUES ?x { %s } ?f wdt:P921 ?x." % vals)
            + by_genre_words(r"alien|extraterrestrial"), f"subject:{key}")
    add("subject", "Name a movie based on a comic book",
        films_with("?f wdt:P144 ?src. ?src wdt:P31/wdt:P279* wd:Q1004.") + by_genre_words(r"superhero|comic"),
        "subject:comics")
    topic_rows = sparql(f"""
SELECT DISTINCT ?f ?xl WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?s. FILTER(?s >= {MIN_SITELINKS})
  ?f wdt:P921 ?x. ?x rdfs:label ?xl. FILTER(LANG(?xl) = "en")
}}""")
    for text, key, topic_rx, genre_rx in TOPIC_CLUES:
        ids = by_genre_words(genre_rx) if genre_rx else []
        if topic_rx:
            rx = re.compile(topic_rx, re.I)
            ids += [qid(r["f"]) for r in topic_rows if rx.search(r["xl"])]
        add("subject", text, ids, f"subject:{key}")
    add("genre", "Name an anime movie", films_with("?f wdt:P31 wd:Q20650540.") + by_genre_words(r"\banime\b"), "genre:anime")
    add("subject", "Name a movie based on a video game",
        films_with("VALUES ?vg { wd:Q7889 wd:Q7058673 } ?f wdt:P144 ?src. ?src wdt:P31/wdt:P279* ?vg.")
        + by_genre_words(r"video game"), "subject:videogame")
    add("subject", "Name a movie based on a TV show",
        films_with("?f wdt:P144 ?src. ?src wdt:P31/wdt:P279* wd:Q5398426."), "subject:tv")

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
    for lang_ids, text, key in LANGUAGE_CLUES:
        # not English-language films with a few lines in it (2012, The Interview)
        add("country", text, [k for k, ls in langs.items() if ls & set(lang_ids) and "Q1860" not in ls],
            f"country:lang:{key}")
    add("country", "Name a foreign-language movie",
        films_with("?f wdt:P364 ?lang. FILTER(?lang != wd:Q1860) FILTER NOT EXISTS { ?f wdt:P364 wd:Q1860 }"),
        "country:foreign")
    add("record", "Name a billion-dollar movie",   # worldwide box office of $1 billion or more
        films_with("?f p:P2142/psv:P2142 ?v. ?v wikibase:quantityAmount ?amt; wikibase:quantityUnit wd:Q4917. "
                   "FILTER(?amt >= 1000000000)"), "record:billion")
    add("record", "Name a movie over three hours long", films_with("?f wdt:P2047 ?m. FILTER(?m >= 180)"),
        "record:3h")
    add("record", "Name a movie under 90 minutes long", films_with("?f wdt:P2047 ?m. FILTER(?m >= 60 && ?m < 90)"),
        "record:under90")
    add("record", "Name a movie over two and a half hours long", films_with("?f wdt:P2047 ?m. FILTER(?m >= 150)"),
        "record:150")
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
    for w in TITLE_WORDS:
        # plurals count too ("Mean Girls" for "Girl")
        add("title", f'Name a movie with the word "{w}" in the title', title_has([w.lower(), w.lower() + "s"]),
            f"title:word:{w.lower()}")
    for L in LETTERS:
        # counts whether or not you include a leading "The", "A" or "An"
        ids = [k for k, f in films.items()
               if f["label"][:1].upper() == L or re.sub(r"(?i)^(the|a|an)\s+", "", f["label"])[:1].upper() == L]
        add("letter", f"Name a movie starting with the letter {L}", ids, f"letter:{L}")

    # --- a smaller set about big franchises and very famous names
    specs = [
        ("director", "wdt:P57", 30, "", "Name a movie directed by {}"),
        ("actor", "wdt:P161|wdt:P725", 70, "?p wdt:P31 wd:Q5. FILTER NOT EXISTS {{ ?p wdt:P106 wd:Q82955 }}",
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
            if kind == "actor" and g["s"] < 90 and label not in MORE_ACTORS:
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
                    if not (q["kind"] == "actor" and q["text"].rsplit("starring ", 1)[1] in director_names - ACTOR_DIRECTORS)]
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

    # ---- numbered sequels: films in a franchise numbered by release order, so "Harry Potter 3"
    # finds Prisoner of Azkaban and "Alien 2" finds Aliens. Franchises come from Wikidata series and
    # from shared title prefixes ("Pirates of the Caribbean: ..."). Never shadows a real title.
    print("numbered sequels...", flush=True)
    real_titles = {re.sub(r"[^a-z0-9]", "", f["label"].lower()) for f in films.values()}
    groups = defaultdict(set)
    for r in sparql(f"""
SELECT ?f ?s ?sl WHERE {{
  VALUES ?t {{ {FILM_TYPES} }}
  ?f wdt:P31 ?t; wikibase:sitelinks ?n. FILTER(?n >= {MIN_SITELINKS})
  ?f wdt:P179 ?s. ?s rdfs:label ?sl. FILTER(LANG(?sl) = "en")
}}"""):
        name = re.sub(r"(?i)\s*(film series|series|franchise|\(film series\)|in film|trilogy)$", "", r["sl"]).strip()
        name = re.sub(r"(?i)^the\s+", "", name)
        if qid(r["f"]) in films and 3 <= len(name) <= 40 and not re.search(r"(?i)universe|\blist\b|phase", name):
            groups[name].add(qid(r["f"]))
    for k, f in films.items():
        m = re.match(r"^(?:The\s+)?(.{3,40}?)\s*[:–—]\s+", f["label"])
        if m:
            groups[m.group(1).strip()].add(k)
    for name, members in groups.items():
        ordered = sorted((k for k in members if films[k]["first"]), key=lambda k: films[k]["first"])
        if not 2 <= len(ordered) <= 12:
            continue
        for n, k in enumerate(ordered, start=1):
            alias = f"{name} {n}"
            if n == 1 or re.sub(r"[^a-z0-9]", "", alias.lower()) in real_titles:
                continue
            if alias not in aliases[k] and alias != films[k]["label"]:
                aliases[k].append(alias)

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
        score = films[k]["pop"] - 400 * max(0, place_counts.get(k, 1) - 3)
        return score - (1500 if f"{k}|{city}" in weak_place else 0)

    iconic_genre = {text: {extra_ids[t]: n for n, t in enumerate(ts) if t in extra_ids}
                    for text, ts in {**ICONIC_GENRE_FILMS, **ICONIC_TOPIC_FILMS}.items()}

    def genre_score(k, text=""):
        if k in iconic_genre.get(text, {}):
            return 10 ** 9 - iconic_genre[text][k]          # pinned to the top, in the listed order
        # small fixed deduction per extra genre tag (a percentage swamps the popularity scale)
        return films[k]["pop"] - 400 * max(0, genre_counts.get(k, 1) - 4)

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
        # a minor voice part counts like a film about ten times less read
        return films[k]["pop"] - (2500 if (actor, k) in voice_only else 0)

    def rank_key(q, j):
        k = order[j]
        if q["kind"] == "genre" or q["text"] in iconic_genre:
            return (-genre_score(k, q["text"]), j)
        if q["key"] in era_city:
            return (-place_score(k, era_city[q["key"]]), j)
        if q["kind"] == "era":
            return (-genre_score(k, era_base.get(q["key"], "")), j)
        if q["kind"] == "setting":
            return (-place_score(k, q["key"].split(":", 1)[1]), j)
        if q["kind"] == "actor":
            return (-role_score(q["key"].split(":", 1)[1], k), j)
        return (j, j)

    out = {
        "built": datetime.date.today().isoformat(),
        "source": "Wikidata (CC0). Popularity = English Wikipedia page views blended with coverage across the 20 largest Wikipedias.",
        "films": [[films[k]["label"], films[k]["year"], films[k]["pop"], films[k]["cap"]] for k in order],
        "aliases": {str(index[k]): v for k, v in aliases.items() if k in index and v},
        "questions": [
            {"kind": q["kind"], "text": q["text"], "key": q["key"], **({"r": 1} if q.get("retired") else {}),
             "a": sorted((index[i] for i in q["ids"]),
                        key=lambda j: rank_key(q, j))}
            for q in sorted(questions, key=lambda q: q["key"])
        ],
    }
    pick_oscars(out["questions"], out["films"])
    out["daily"], out["games"] = plan_all(out["questions"])
    out["slots"] = DAILY_SLOTS
    out["games_version"] = GAMES_VERSION
    out["daily_version"] = DAILY_VERSION
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
