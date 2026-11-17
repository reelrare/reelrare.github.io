# Reel Rare

A daily film trivia game. Each clue asks you to name a movie that fits it ("Name a horror movie", "Name a movie set in Paris"). You have 30 seconds per clue and unlimited guesses. Typing is forgiving: small typos, missing punctuation, "2" for "II" and alternate titles all count. Any correct film scores, and the less obvious your pick, the more it's worth:

| Tier | Points | Your film's rank among the clue's answers |
|---|---|---|
| Blockbuster | 10 | top 3 |
| Studio Film | 25 | 4–10 |
| Award Movie | 50 | 11–30 |
| Indie | 75 | anything further down |
| One in an Oscar | 100 | the one hidden jackpot film picked for each clue |

Short answer lists scale these ranks down so the tiers still spread out. When you get one right, the curtains open and the screen lights up in your tier's colour.

Every visit starts on the home page: today's daily, Unlimited, how to play, and your statuette, a little gold mascot that reacts as you play. Daily streaks unlock new looks for it (shades at 3 days, a top hat at 7, popcorn at 14, a beret at 30, silver at 60, diamond at 100).

Daily gives everyone the same 7 clues each day. Unlimited has 130 numbered games (also the same for everyone), and its page shows which ones you've finished and your score on each.

## How it works

- `build_data.py` pulls films, genres, settings, subjects, people, studios, franchises and Oscar results from [Wikidata](https://www.wikidata.org) (free, CC0) through the [QLever](https://qlever.dev) endpoint, and writes `data.js`. How well known a film is = how many of the 20 largest Wikipedias have an article on it.
- It also schedules each day's 7 clues 30 days ahead. Days already scheduled are never changed by a rebuild.
- `.github/workflows/refresh-data.yml` reruns the build every night and commits the new `data.js`; GitHub Pages redeploys on each commit.
- `index.html` is the whole game: no build step, no server.

## Run it locally

```bash
python3 build_data.py      # about 30 seconds
python3 -m http.server 8765
```

Then open http://localhost:8765, or just double-click `index.html`.
