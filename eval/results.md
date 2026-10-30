# Duly evaluation, 2026-10-30

Fictional sample documents, live calls to Nebius Token Factory. Reproduce with `python scripts/evaluate.py`.

## Reading documents (Nemotron Super, dates found by code)

| Measure | Result |
|---|---|
| Documents read | 12 (3 runs over 4 samples) |
| Deadlines found with the right date and notice period | 18 of 18 |
| Extra items (not in the answer key) | 0 |
| Items flagged "check this" | 0 |
| Median latency per document | 2.2 s |
| Mean cost per document | $0.00063 |

## Answering questions (Nemotron Nano picks the skill, Nemotron Super acts)

| Question | Runs | Right skill | Required tools used | Nudged | Blocked calls | Links removed | Median time | Mean cost |
|---|---|---|---|---|---|---|---|---|
| My laptop screen started flickering. Am I still covered? | 2 | 2/2 | 2/2 | 1 | 0 | 0 | 13 s | $0.0081 |
| I don't want to renew the gym. What do I do, and by when? | 2 | 2/2 | 2/2 | 0 | 0 | 0 | 12 s | $0.0097 |
| What do I need to do in the next 30 days? | 2 | 2/2 | 2/2 | 0 | 0 | 0 | 4 s | $0.0021 |
| How do I renew my car insurance without a gap? | 2 | 2/2 | 2/2 | 0 | 0 | 0 | 14 s | $0.0081 |

Total for this evaluation: 57 model calls, $0.0635.
"Nudged" counts runs where Duly had to ask the model for a step its skill requires before it could answer.
