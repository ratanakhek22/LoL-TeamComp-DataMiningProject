# CompSense

**Learning team composition identity in League of Legends with machine learning.**

Most draft tools rate a team by adding up champion win rates and lane matchups. This project takes a different angle: it tries to learn whether a team composition is *coherent*, meaning whether the five champions look like they were drafted to work together, and then tests whether coherence actually relates to winning.

For example, Kalista + Renata is a well-known bot lane pairing and should score as coherent. A Vayne top in an otherwise full-engage, tank-heavy comp should stand out as the pick that doesn't fit.

The project grew out of my data mining coursework. It applies ideas from those classes (co-occurrence analysis, clustering, and representation learning) to a domain I know well as a player.

> Status: early research. This is a personal portfolio project, not a public product.

## Approach

1. **Coherence model (self-supervised).** Champions are represented as learned embeddings. The model is trained on a "fill in the blank" task: hide one champion from a real team and predict it from the other four plus the hidden role. No hand labels are needed; every real team is its own training example. At inference time, each champion's predicted probability (adjusted for overall popularity) measures how well it fits the team, and the team average gives a coherence score.
2. **Comp archetypes.** Team embeddings are clustered into interpretable identities (engage, poke, protect-the-carry, split push, etc.).
3. **Solo queue vs. pro.** The model is pretrained on high-elo ranked games and fine-tuned on professional games, then used to compare where pro and solo queue ideas of "coherent" disagree.
4. **Matchup model.** A win-probability model compares two drafts using both teams' embeddings, coherence scores, and lane matchup features, and is evaluated against simple win-rate baselines.

## Data

| Source | Used for | Notes |
|---|---|---|
| Riot Games API (League-V4, Match-V5) | Ranked solo queue matches, Master+ | Only draft-level fields are stored: champions, roles, patch, outcome. Collected offline and rate-limited to personal-key limits. |
| Oracle's Elixir | Professional match data | Downloadable CSVs. |
| Leaguepedia (Cargo API) | Pro draft order, gaps in other data | CC-BY-SA, attributed where used. |
| Data Dragon | Champion metadata (names, IDs, class tags) | Updated per patch. |

No API calls are made by any public-facing component. Any demo will use trained models and precomputed data only.

## Evaluation

- **Coherence model:** top-k accuracy at predicting the hidden champion on held-out games; a hand-made test set of known good and bad combinations; correlation between coherence and win rate.
- **Matchup model:** log loss and calibration, compared against a champion-win-rate baseline. Draft alone is expected to explain only a small part of match outcomes, and results will be reported honestly with that in mind.

## Roadmap

- [ ] Explore pro data from Oracle's Elixir (games per patch, champion pair counts)
- [ ] PMI co-occurrence baseline for champion synergy
- [ ] Solo queue crawler (Riot API, Master+, current patch)
- [ ] Coherence model v1: averaged embeddings + linear layer + softmax
- [ ] Popularity-adjusted coherence scores and sanity checks on known combos
- [ ] Archetype clustering of team embeddings
- [ ] Fine-tune on pro games; analyze pro vs. solo queue differences
- [ ] Matchup / win-probability model with ablations
- [ ] Demo app (offline, precomputed)
- [ ] Write-up

## Setup

API keys are read from a local `.env` file, which is git-ignored and never committed.

```
RIOT_API_KEY=your-key-here
```

## Disclaimer

CompSense isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games and all associated properties are trademarks or registered trademarks of Riot Games, Inc.
