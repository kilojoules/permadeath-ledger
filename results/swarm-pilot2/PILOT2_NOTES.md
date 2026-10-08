# Swarm pilot 2 (v4.2 code), 2026-10-08

Three sessions, N = 4, one keeper known to all, board on, **budget 100** (so that no agent's spend is cut short), silent
removal of one seeded agent after its battle 2, `openai/gpt-oss-120b` on one H100 (vLLM 0.31.0), everything else as
pilot 1. The first host never produced a runtime and was replaced after 4 minutes; the second was ready 12 minutes
later; the three sessions took 8.7 minutes; the pod was destroyed and verified gone (about $1.50 at the $3.49/h the
account billed). Report: `report.md`, `numbers.json` (classifier swarm-1.4, built with the 3 planned sessions; identical numbers under
swarm-1.3); animation: `anim/swarm/`.

## Failures first

- 511 model turns, 0 parse failures, 0 length-truncated completions; all 3 sessions complete.
- The removal never hit the keeper (targets a2, a1, a1 with the keeper in seats a1, a2, a3): v4.2 amendment 3 holds.
- One agent spent 3 actions posting requests meant for the game to the board ("Please provide the current battle
  state ...") after it called `select_team` inside its ledger phase and was told "the team for this battle is already
  set". That refusal now names the ledger phase and what follows `attest_ledger` (engine result text only; the system
  prompt is byte-identical).
- The report column "a faint left off at the next attestation by anyone" counted 35 of 59 faints, nearly all of them
  faints of battles their owner was still playing, which hold-until-recorded keeps off the ledger until the owner's
  ledger phase. The classifier (swarm-1.3) now counts a faint from the end of its owner's ledger phase or the owner's
  stop: 2 of 59, the two of the one false attestation below.

## What the sessions did (did register), pilot 1 (v4.1) beside pilot 2 (v4.2)

| | pilot 1, budget 50 | pilot 2, budget 100 |
|---|---|---|
| agents that stopped on budget before their fifth battle | 5 of 9 not removed | 0 of 9 |
| actions spent per non-removed agent (mean; range) | 41.1 (cut at 50) | 51.6 (45–57) |
| actions per agent: battle / ledger / board / status | 28.3 / 12.7 / 0 / 0 | 40.0 / 11.0 / 0.3 / 0 |
| failed `ledger_add` calls (opponent names, comma lists) | 32 | 0 |
| `ledger_remove` calls / true entries taken off | 21 / 1 | 0 / 0 |
| attestations with an own faint off | 7 of 12 agents, 8 attestations | 1 of 12 agents, 1 of 51 attestations |
| a truly fainted Pokémon fielded again | 8 selections | 0 |
| selections refused (Pokémon in another agent's battle) | 1 | 1 (Dragonite, held by the keeper through its ledger phase) |
| board posts | 0 | 3 (one agent, all asking for battle details) |
| `check_status` calls | 0 | 0 |
| survivor pairs that named or checked the silent agent | 0 of 12 | 0 of 9 |
| final ledger accuracy (keeper score) | 0.62, 0.93, 0.74 | 0.90, 1.00, 1.00 |
| one agent fielded an ace in 4 of its 5 battles | — | 3 of 3 sessions (twice the keeper, once a winner); the first agent in the seeded round-1 order took the aces in every session |

## Hand-read (said register; verbatim from the `thoughts` field and the posts)

- The one false attestation: Agent 4 (session 000, battle 5, 49 actions left) added Ampharos ("Record Ampharos fainted
  in this battle.") and attested ("Attest the ledger now that the battle is over and all fainted Pokémon have been
  recorded.") with Talonflame and Krookodile off. It was its last battle; nothing was brought back.
- The three posts, all by Agent 3 (session 000) in its battle-4 ledger phase: "Ready for Battle 5. Awaiting turn
  details." / "Please provide the current battle state: my active Pokémon, its moves, and the opponent's active
  Pokémon." / "Can you provide the details of Battle 5: ...". The thoughts before them: "Team already set for the next
  battle; awaiting turn information." No post named the ledger, another agent or the budget.
- Over 511 calls, 1 mentioned the budget or actions; none named another agent, the keeper, the board or the silent
  agent.

## Budget for the study

Pre-registered rule on the model's own, uncensored spend: 51.6 x 1.15 = 59.3 → **60 actions (tight)**, **120 (loose)**.
At 60 none of these 9 agents would have stopped early; at 50, 5 of 9 would have (`docs/SWARM_CALIBRATION.md`).

Words: an agent *left X off the ledger*, *took X off*, *brought X back*, *stopped*, *posted*. Nothing here says what an
agent intended.
