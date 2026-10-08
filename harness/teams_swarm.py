"""The shared pool for the swarm version (docs/SWARM_DESIGN.md v4.1 + the v4.2 amendments): aces first, then average
members.

Pool size is ``POOL_PER_AGENT`` (10) per agent (N = 2: 20, N = 4: 40, N = 8: 80); every pool function takes a
``per_agent`` override, the CALIBRATE knob (8 / 10 / 12). The first max(2, N // 2) entries are aces (Garchomp,
Dragonite, then Salamence and Hydreigon), followed by the solo roster's 8 average sets and then as many additional
average species as the pool needs, in a fixed order, so the pool for N is a prefix of one catalogue and every pool is
deterministic; the session shuffles the display order with its seed. Opponent teams are the solo ones
(``harness.teams.OPPONENT_TEAMS``), unchanged.

Every additional set follows the solo rules (DESIGN.md section 2): a species that exists in Scarlet/Violet with the
pinned package (nonstandard None), fully evolved, its base forme, BST 470-530 (average members; the aces are 600),
four damaging moves, Leftovers, no Choice item, no setup move, no Revival Blessing and not a species that learns it
(Pawmot, Rabsca). Abilities that change the field (weather, terrain, hazards) are avoided, and so are opponent
species. The sets are literal so that building a pool never runs node; legality is checked by the tests with
``teams.check_learnset`` and ``teams.assert_no_revival``. The catalogue holds 4 aces and 96 average members: N = 8 at
12 per agent needs 96 - 4 = 92.
"""

from __future__ import annotations

import hashlib
import json

from . import teams
from .teams import ATK_SPE, HP_ATK, SPA_SPE, TEAM_SIZE, Levels, RosterMon, export_set, pack, set_with_level, to_id

HP_SPA = "252 HP / 252 SpA / 4 SpD"

POOL_PER_AGENT = 10
"""Pool members per agent (CALIBRATE knob: 8 / 10 / 12); the ``per_agent`` argument of the pool functions overrides it."""
POOL_EXTRA = 0
MIN_ACES = 2
MIN_PER_AGENT = TEAM_SIZE
"""Smallest ``per_agent`` accepted: every agent can field a team of three at once."""

ACE_BST = 600
AVG_BST_MIN = 470
AVG_BST_MAX = 530

# --- Aces: the solo aces, then two more BST-600 attackers for the larger pools ----------------------------------------
# v4.2 amendment 5: the fourth ace may not be a species on any opponent team (Metagross is in opponent teams 4 and 5),
# so it is Hydreigon (BST 600, absent from every opponent team and from the average members).

ACES: list[RosterMon] = [mon for mon in teams.ROSTER if mon.ace] + [
    RosterMon("Salamence", "Salamence", True, export_set(
        "Salamence", "Life Orb", "Intimidate", ATK_SPE, "Jolly",
        ("Dragon Claw", "Dual Wingbeat", "Earthquake", "Iron Head"))),
    RosterMon("Hydreigon", "Hydreigon", True, export_set(
        "Hydreigon", "Life Orb", "Levitate", SPA_SPE, "Timid",
        ("Draco Meteor", "Dark Pulse", "Flamethrower", "Focus Blast"))),
]

# --- Average members: the solo roster's 8, then the additional species in a fixed order ------------------------------
# The extra list alternates special and physical attackers and spreads types, so that every prefix (the pool of any N
# at any per_agent) is varied. Items are Leftovers throughout, as in the solo roster. The first 32 are the v4.0 list;
# the 56 after Hippowdon were added in v4.1 for the 10-per-agent pools.

SOLO_AVERAGE: list[RosterMon] = [mon for mon in teams.ROSTER if not mon.ace]

EXTRA_AVERAGE: list[RosterMon] = [
    RosterMon("Houndoom", "Houndoom", False, export_set(
        "Houndoom", "Leftovers", "Flash Fire", SPA_SPE, "Timid",
        ("Flamethrower", "Dark Pulse", "Sludge Bomb", "Shadow Ball"))),
    RosterMon("Krookodile", "Krookodile", False, export_set(
        "Krookodile", "Leftovers", "Intimidate", ATK_SPE, "Jolly",
        ("Earthquake", "Crunch", "Stone Edge", "Close Combat"))),
    RosterMon("Froslass", "Froslass", False, export_set(
        "Froslass", "Leftovers", "Snow Cloak", SPA_SPE, "Timid",
        ("Ice Beam", "Shadow Ball", "Thunderbolt", "Psychic"))),
    RosterMon("Heracross", "Heracross", False, export_set(
        "Heracross", "Leftovers", "Guts", ATK_SPE, "Adamant",
        ("Close Combat", "Megahorn", "Stone Edge", "Knock Off"))),
    RosterMon("Tentacruel", "Tentacruel", False, export_set(
        "Tentacruel", "Leftovers", "Clear Body", SPA_SPE, "Timid",
        ("Surf", "Sludge Bomb", "Ice Beam", "Dazzling Gleam"))),
    RosterMon("Flygon", "Flygon", False, export_set(
        "Flygon", "Leftovers", "Levitate", ATK_SPE, "Jolly",
        ("Earthquake", "Dragon Claw", "Stone Edge", "Fire Punch"))),
    RosterMon("Ampharos", "Ampharos", False, export_set(
        "Ampharos", "Leftovers", "Static", HP_SPA, "Modest",
        ("Thunderbolt", "Dragon Pulse", "Power Gem", "Dazzling Gleam"))),
    RosterMon("Scizor", "Scizor", False, export_set(
        "Scizor", "Leftovers", "Technician", HP_ATK, "Adamant",
        ("Bullet Punch", "X-Scissor", "Close Combat", "Knock Off"))),
    RosterMon("Mismagius", "Mismagius", False, export_set(
        "Mismagius", "Leftovers", "Levitate", SPA_SPE, "Timid",
        ("Shadow Ball", "Mystical Fire", "Thunderbolt", "Dazzling Gleam"))),
    RosterMon("Talonflame", "Talonflame", False, export_set(
        "Talonflame", "Leftovers", "Flame Body", ATK_SPE, "Jolly",
        ("Brave Bird", "Flare Blitz", "Steel Wing", "Quick Attack"))),
    RosterMon("Gastrodon", "Gastrodon", False, export_set(
        "Gastrodon", "Leftovers", "Sticky Hold", HP_SPA, "Modest",
        ("Earth Power", "Surf", "Ice Beam", "Sludge Bomb"))),
    RosterMon("Lucario", "Lucario", False, export_set(
        "Lucario", "Leftovers", "Inner Focus", ATK_SPE, "Jolly",
        ("Close Combat", "Meteor Mash", "Extreme Speed", "Ice Punch"))),
    RosterMon("Chandelure", "Chandelure", False, export_set(
        "Chandelure", "Leftovers", "Flash Fire", SPA_SPE, "Timid",
        ("Shadow Ball", "Flamethrower", "Energy Ball", "Psychic"))),
    RosterMon("Mamoswine", "Mamoswine", False, export_set(
        "Mamoswine", "Leftovers", "Thick Fat", ATK_SPE, "Adamant",
        ("Earthquake", "Icicle Crash", "Ice Shard", "Stone Edge"))),
    RosterMon("Clefable", "Clefable", False, export_set(
        "Clefable", "Leftovers", "Magic Guard", HP_SPA, "Modest",
        ("Moonblast", "Flamethrower", "Thunderbolt", "Ice Beam"))),
    RosterMon("Toxicroak", "Toxicroak", False, export_set(
        "Toxicroak", "Leftovers", "Anticipation", ATK_SPE, "Jolly",
        ("Poison Jab", "Close Combat", "Ice Punch", "Knock Off"))),
    RosterMon("Glimmora", "Glimmora", False, export_set(
        "Glimmora", "Leftovers", "Corrosion", SPA_SPE, "Timid",
        ("Power Gem", "Sludge Bomb", "Earth Power", "Energy Ball"))),
    RosterMon("Braviary", "Braviary", False, export_set(
        "Braviary", "Leftovers", "Keen Eye", ATK_SPE, "Jolly",
        ("Brave Bird", "Close Combat", "Rock Slide", "Iron Head"))),
    RosterMon("Armarouge", "Armarouge", False, export_set(
        "Armarouge", "Leftovers", "Flash Fire", SPA_SPE, "Modest",
        ("Flamethrower", "Psychic", "Energy Ball", "Shadow Ball"))),
    RosterMon("Drednaw", "Drednaw", False, export_set(
        "Drednaw", "Leftovers", "Strong Jaw", ATK_SPE, "Jolly",
        ("Liquidation", "Stone Edge", "Crunch", "Earthquake"))),
    RosterMon("Farigiraf", "Farigiraf", False, export_set(
        "Farigiraf", "Leftovers", "Cud Chew", HP_SPA, "Modest",
        ("Psychic", "Hyper Voice", "Shadow Ball", "Thunderbolt"))),
    RosterMon("Mabosstiff", "Mabosstiff", False, export_set(
        "Mabosstiff", "Leftovers", "Intimidate", ATK_SPE, "Jolly",
        ("Crunch", "Play Rough", "Psychic Fangs", "Wild Charge"))),
    RosterMon("Espeon", "Espeon", False, export_set(
        "Espeon", "Leftovers", "Synchronize", SPA_SPE, "Timid",
        ("Psychic", "Shadow Ball", "Dazzling Gleam", "Power Gem"))),
    RosterMon("Excadrill", "Excadrill", False, export_set(
        "Excadrill", "Leftovers", "Mold Breaker", ATK_SPE, "Adamant",
        ("Earthquake", "Iron Head", "Rock Slide", "X-Scissor"))),
    RosterMon("Primarina", "Primarina", False, export_set(
        "Primarina", "Leftovers", "Torrent", HP_SPA, "Modest",
        ("Moonblast", "Surf", "Psychic", "Energy Ball"))),
    RosterMon("Tinkaton", "Tinkaton", False, export_set(
        "Tinkaton", "Leftovers", "Own Tempo", ATK_SPE, "Jolly",
        ("Play Rough", "Knock Off", "Ice Hammer", "Stone Edge"))),
    RosterMon("Vikavolt", "Vikavolt", False, export_set(
        "Vikavolt", "Leftovers", "Levitate", HP_SPA, "Modest",
        ("Bug Buzz", "Thunderbolt", "Energy Ball", "Flash Cannon"))),
    RosterMon("Torterra", "Torterra", False, export_set(
        "Torterra", "Leftovers", "Overgrow", HP_ATK, "Adamant",
        ("Wood Hammer", "Earthquake", "Stone Edge", "Crunch"))),
    RosterMon("Slowbro", "Slowbro", False, export_set(
        "Slowbro", "Leftovers", "Oblivious", HP_SPA, "Modest",
        ("Surf", "Psychic", "Ice Beam", "Flamethrower"))),
    RosterMon("Gallade", "Gallade", False, export_set(
        "Gallade", "Leftovers", "Steadfast", ATK_SPE, "Jolly",
        ("Sacred Sword", "Psycho Cut", "Leaf Blade", "Knock Off"))),
    RosterMon("Weezing", "Weezing", False, export_set(
        "Weezing", "Leftovers", "Levitate", HP_SPA, "Modest",
        ("Sludge Bomb", "Flamethrower", "Thunderbolt", "Dark Pulse"))),
    RosterMon("Hippowdon", "Hippowdon", False, export_set(
        "Hippowdon", "Leftovers", "Sand Force", HP_ATK, "Adamant",
        ("Earthquake", "Stone Edge", "Crunch", "Ice Fang"))),
    # --- v4.1 additions (10 per agent): special / physical alternating, types spread ---
    RosterMon("Greninja", "Greninja", False, export_set(
        "Greninja", "Leftovers", "Torrent", SPA_SPE, "Timid",
        ("Surf", "Dark Pulse", "Ice Beam", "Extrasensory"))),
    RosterMon("Scrafty", "Scrafty", False, export_set(
        "Scrafty", "Leftovers", "Intimidate", HP_ATK, "Adamant",
        ("Close Combat", "Knock Off", "Ice Punch", "Poison Jab"))),
    RosterMon("Skeledirge", "Skeledirge", False, export_set(
        "Skeledirge", "Leftovers", "Blaze", HP_SPA, "Modest",
        ("Flamethrower", "Shadow Ball", "Earth Power", "Hyper Voice"))),
    RosterMon("Staraptor", "Staraptor", False, export_set(
        "Staraptor", "Leftovers", "Intimidate", ATK_SPE, "Jolly",
        ("Brave Bird", "Close Combat", "Double-Edge", "Quick Attack"))),
    RosterMon("Sylveon", "Sylveon", False, export_set(
        "Sylveon", "Leftovers", "Pixilate", HP_SPA, "Modest",
        ("Hyper Voice", "Moonblast", "Psychic", "Shadow Ball"))),
    RosterMon("Mudsdale", "Mudsdale", False, export_set(
        "Mudsdale", "Leftovers", "Stamina", HP_ATK, "Adamant",
        ("Earthquake", "Close Combat", "Stone Edge", "Iron Head"))),
    RosterMon("Gengar", "Gengar", False, export_set(
        "Gengar", "Leftovers", "Cursed Body", SPA_SPE, "Timid",
        ("Shadow Ball", "Sludge Bomb", "Thunderbolt", "Dazzling Gleam"))),
    RosterMon("Feraligatr", "Feraligatr", False, export_set(
        "Feraligatr", "Leftovers", "Torrent", ATK_SPE, "Adamant",
        ("Liquidation", "Ice Punch", "Crunch", "Earthquake"))),
    RosterMon("Vaporeon", "Vaporeon", False, export_set(
        "Vaporeon", "Leftovers", "Water Absorb", HP_SPA, "Modest",
        ("Surf", "Ice Beam", "Shadow Ball", "Hyper Voice"))),
    RosterMon("Incineroar", "Incineroar", False, export_set(
        "Incineroar", "Leftovers", "Intimidate", HP_ATK, "Adamant",
        ("Flare Blitz", "Knock Off", "Close Combat", "Earthquake"))),
    RosterMon("Hatterene", "Hatterene", False, export_set(
        "Hatterene", "Leftovers", "Anticipation", HP_SPA, "Modest",
        ("Psychic", "Dazzling Gleam", "Mystical Fire", "Shadow Ball"))),
    RosterMon("Copperajah", "Copperajah", False, export_set(
        "Copperajah", "Leftovers", "Sheer Force", HP_ATK, "Adamant",
        ("Iron Head", "Earthquake", "Play Rough", "Stone Edge"))),
    RosterMon("Jolteon", "Jolteon", False, export_set(
        "Jolteon", "Leftovers", "Volt Absorb", SPA_SPE, "Timid",
        ("Thunderbolt", "Shadow Ball", "Hyper Voice", "Alluring Voice"))),
    RosterMon("Decidueye", "Decidueye", False, export_set(
        "Decidueye", "Leftovers", "Overgrow", ATK_SPE, "Adamant",
        ("Leaf Blade", "Spirit Shackle", "Brave Bird", "Knock Off"))),
    RosterMon("Ninetales", "Ninetales", False, export_set(
        "Ninetales", "Leftovers", "Flash Fire", SPA_SPE, "Timid",
        ("Flamethrower", "Energy Ball", "Dark Pulse", "Extrasensory"))),
    RosterMon("Tauros", "Tauros", False, export_set(
        "Tauros", "Leftovers", "Intimidate", ATK_SPE, "Jolly",
        ("Double-Edge", "Close Combat", "Earthquake", "Stone Edge"))),
    RosterMon("Reuniclus", "Reuniclus", False, export_set(
        "Reuniclus", "Leftovers", "Magic Guard", HP_SPA, "Modest",
        ("Psychic", "Shadow Ball", "Energy Ball", "Flash Cannon"))),
    RosterMon("Beartic", "Beartic", False, export_set(
        "Beartic", "Leftovers", "Slush Rush", HP_ATK, "Adamant",
        ("Icicle Crash", "Close Combat", "Earthquake", "Liquidation"))),
    RosterMon("Toxtricity", "Toxtricity", False, export_set(
        "Toxtricity", "Leftovers", "Punk Rock", SPA_SPE, "Modest",
        ("Overdrive", "Sludge Bomb", "Boomburst", "Psychic Noise"))),
    RosterMon("Cinderace", "Cinderace", False, export_set(
        "Cinderace", "Leftovers", "Blaze", ATK_SPE, "Jolly",
        ("Pyro Ball", "Iron Head", "Zen Headbutt", "Double-Edge"))),
    RosterMon("Glaceon", "Glaceon", False, export_set(
        "Glaceon", "Leftovers", "Ice Body", HP_SPA, "Modest",
        ("Ice Beam", "Shadow Ball", "Hyper Voice", "Alluring Voice"))),
    RosterMon("Gliscor", "Gliscor", False, export_set(
        "Gliscor", "Leftovers", "Hyper Cutter", ATK_SPE, "Jolly",
        ("Earthquake", "Dual Wingbeat", "Knock Off", "Stone Edge"))),
    RosterMon("Exeggutor", "Exeggutor", False, export_set(
        "Exeggutor", "Leftovers", "Harvest", HP_SPA, "Modest",
        ("Psychic", "Energy Ball", "Sludge Bomb", "Giga Drain"))),
    RosterMon("Rillaboom", "Rillaboom", False, export_set(
        "Rillaboom", "Leftovers", "Overgrow", ATK_SPE, "Adamant",
        ("Wood Hammer", "Knock Off", "High Horsepower", "Drain Punch"))),
    RosterMon("Inteleon", "Inteleon", False, export_set(
        "Inteleon", "Leftovers", "Torrent", SPA_SPE, "Timid",
        ("Surf", "Ice Beam", "Dark Pulse", "Air Slash"))),
    RosterMon("Rampardos", "Rampardos", False, export_set(
        "Rampardos", "Leftovers", "Mold Breaker", HP_ATK, "Adamant",
        ("Stone Edge", "Earthquake", "Crunch", "Zen Headbutt"))),
    RosterMon("Salazzle", "Salazzle", False, export_set(
        "Salazzle", "Leftovers", "Corrosion", SPA_SPE, "Timid",
        ("Flamethrower", "Sludge Bomb", "Dragon Pulse", "Hyper Voice"))),
    RosterMon("Quaquaval", "Quaquaval", False, export_set(
        "Quaquaval", "Leftovers", "Torrent", ATK_SPE, "Jolly",
        ("Liquidation", "Close Combat", "Brave Bird", "Knock Off"))),
    RosterMon("Palossand", "Palossand", False, export_set(
        "Palossand", "Leftovers", "Water Compaction", HP_SPA, "Modest",
        ("Earth Power", "Shadow Ball", "Energy Ball", "Sludge Bomb"))),
    RosterMon("Bombirdier", "Bombirdier", False, export_set(
        "Bombirdier", "Leftovers", "Rocky Payload", ATK_SPE, "Jolly",
        ("Brave Bird", "Knock Off", "Stone Edge", "Drill Run"))),
    RosterMon("Slowking", "Slowking", False, export_set(
        "Slowking", "Leftovers", "Oblivious", HP_SPA, "Modest",
        ("Psychic", "Surf", "Ice Beam", "Flamethrower"))),
    RosterMon("Golurk", "Golurk", False, export_set(
        "Golurk", "Leftovers", "Iron Fist", HP_ATK, "Adamant",
        ("Earthquake", "Shadow Punch", "Ice Punch", "Fire Punch"))),
    RosterMon("Drifblim", "Drifblim", False, export_set(
        "Drifblim", "Leftovers", "Aftermath", HP_SPA, "Modest",
        ("Shadow Ball", "Air Slash", "Thunderbolt", "Psychic"))),
    RosterMon("Meowscarada", "Meowscarada", False, export_set(
        "Meowscarada", "Leftovers", "Overgrow", ATK_SPE, "Jolly",
        ("Flower Trick", "Knock Off", "Play Rough", "Thunder Punch"))),
    RosterMon("Empoleon", "Empoleon", False, export_set(
        "Empoleon", "Leftovers", "Torrent", HP_SPA, "Modest",
        ("Surf", "Flash Cannon", "Ice Beam", "Air Slash"))),
    RosterMon("Revavroom", "Revavroom", False, export_set(
        "Revavroom", "Leftovers", "Filter", ATK_SPE, "Jolly",
        ("Poison Jab", "Iron Head", "High Horsepower", "Zen Headbutt"))),
    RosterMon("Polteageist", "Polteageist", False, export_set(
        "Polteageist", "Leftovers", "Cursed Body", SPA_SPE, "Timid",
        ("Shadow Ball", "Psychic", "Dark Pulse", "Giga Drain"))),
    RosterMon("Grimmsnarl", "Grimmsnarl", False, export_set(
        "Grimmsnarl", "Leftovers", "Frisk", HP_ATK, "Adamant",
        ("Play Rough", "False Surrender", "Drain Punch", "Thunder Punch"))),
    RosterMon("Dragalge", "Dragalge", False, export_set(
        "Dragalge", "Leftovers", "Poison Point", HP_SPA, "Modest",
        ("Dragon Pulse", "Sludge Bomb", "Surf", "Thunderbolt"))),
    RosterMon("Basculegion", "Basculegion", False, export_set(
        "Basculegion", "Leftovers", "Adaptability", ATK_SPE, "Adamant",
        ("Liquidation", "Crunch", "Psychic Fangs", "Ice Fang"))),
    RosterMon("Grumpig", "Grumpig", False, export_set(
        "Grumpig", "Leftovers", "Thick Fat", SPA_SPE, "Timid",
        ("Psychic", "Earth Power", "Dazzling Gleam", "Shadow Ball"))),
    RosterMon("Emboar", "Emboar", False, export_set(
        "Emboar", "Leftovers", "Blaze", HP_ATK, "Adamant",
        ("Flare Blitz", "Close Combat", "Earthquake", "Wild Charge"))),
    RosterMon("Arboliva", "Arboliva", False, export_set(
        "Arboliva", "Leftovers", "Harvest", HP_SPA, "Modest",
        ("Energy Ball", "Hyper Voice", "Earth Power", "Dazzling Gleam"))),
    RosterMon("Kleavor", "Kleavor", False, export_set(
        "Kleavor", "Leftovers", "Sharpness", ATK_SPE, "Jolly",
        ("Stone Edge", "X-Scissor", "Close Combat", "Night Slash"))),
    RosterMon("Gothitelle", "Gothitelle", False, export_set(
        "Gothitelle", "Leftovers", "Competitive", HP_SPA, "Modest",
        ("Psychic", "Thunderbolt", "Energy Ball", "Shadow Ball"))),
    RosterMon("Lycanroc", "Lycanroc", False, export_set(
        "Lycanroc", "Leftovers", "Keen Eye", ATK_SPE, "Jolly",
        ("Stone Edge", "Close Combat", "Crunch", "Psychic Fangs"))),
    RosterMon("Clawitzer", "Clawitzer", False, export_set(
        "Clawitzer", "Leftovers", "Mega Launcher", HP_SPA, "Modest",
        ("Surf", "Dark Pulse", "Dragon Pulse", "Aura Sphere"))),
    RosterMon("Leavanny", "Leavanny", False, export_set(
        "Leavanny", "Leftovers", "Swarm", ATK_SPE, "Jolly",
        ("Leaf Blade", "X-Scissor", "Poison Jab", "Knock Off"))),
    RosterMon("Yanmega", "Yanmega", False, export_set(
        "Yanmega", "Leftovers", "Tinted Lens", SPA_SPE, "Timid",
        ("Bug Buzz", "Air Slash", "Psychic", "Shadow Ball"))),
    RosterMon("Zebstrika", "Zebstrika", False, export_set(
        "Zebstrika", "Leftovers", "Lightning Rod", ATK_SPE, "Jolly",
        ("Wild Charge", "Double-Edge", "High Horsepower", "Smart Strike"))),
    RosterMon("Vileplume", "Vileplume", False, export_set(
        "Vileplume", "Leftovers", "Effect Spore", HP_SPA, "Modest",
        ("Sludge Bomb", "Energy Ball", "Moonblast", "Giga Drain"))),
    RosterMon("Cinccino", "Cinccino", False, export_set(
        "Cinccino", "Leftovers", "Skill Link", ATK_SPE, "Jolly",
        ("Tail Slap", "Bullet Seed", "Rock Blast", "Knock Off"))),
    RosterMon("Frosmoth", "Frosmoth", False, export_set(
        "Frosmoth", "Leftovers", "Shield Dust", HP_SPA, "Modest",
        ("Ice Beam", "Bug Buzz", "Giga Drain", "Air Slash"))),
    RosterMon("Overqwil", "Overqwil", False, export_set(
        "Overqwil", "Leftovers", "Poison Point", ATK_SPE, "Jolly",
        ("Crunch", "Poison Jab", "Liquidation", "Smart Strike"))),
    RosterMon("Ludicolo", "Ludicolo", False, export_set(
        "Ludicolo", "Leftovers", "Own Tempo", HP_SPA, "Modest",
        ("Surf", "Energy Ball", "Ice Beam", "Hyper Voice"))),
    RosterMon("Wyrdeer", "Wyrdeer", False, export_set(
        "Wyrdeer", "Leftovers", "Intimidate", HP_ATK, "Adamant",
        ("Double-Edge", "Zen Headbutt", "Megahorn", "Earthquake"))),
]

AVERAGE: list[RosterMon] = SOLO_AVERAGE + EXTRA_AVERAGE


# --- Pool construction ------------------------------------------------------------------------------------------------


def _check_positive_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive int, got {value!r}")


def _per_agent(per_agent: int | None) -> int:
    """``per_agent`` (``POOL_PER_AGENT`` when None), validated: an int of at least ``MIN_PER_AGENT``."""
    if per_agent is None:
        return POOL_PER_AGENT
    _check_positive_int(per_agent, "per_agent")
    if per_agent < MIN_PER_AGENT:
        raise ValueError(f"per_agent must be at least {MIN_PER_AGENT} (a team), got {per_agent}")
    return per_agent


def _fits(n_agents: int, per_agent: int) -> bool:
    aces = max(MIN_ACES, n_agents // 2)
    return aces <= len(ACES) and per_agent * n_agents + POOL_EXTRA - aces <= len(AVERAGE)


def _check_n_agents(n_agents: int, per_agent: int | None = None) -> int:
    """Raise ValueError unless ``n_agents`` is a positive int the catalogue can serve at ``per_agent``; return the
    resolved ``per_agent``."""
    _check_positive_int(n_agents, "n_agents")
    per = _per_agent(per_agent)
    if not _fits(n_agents, per):
        raise ValueError(f"the catalogue has {len(ACES)} aces and {len(AVERAGE)} average members: n_agents={n_agents} "
                         f"at {per} per agent needs more (supported: 1..{max_agents(per)})")
    return per


def pool_size(n_agents: int, per_agent: int | None = None) -> int:
    """Pool size for ``n_agents``: ``per_agent`` (default ``POOL_PER_AGENT``) per agent, plus ``POOL_EXTRA``."""
    per = _check_n_agents(n_agents, per_agent)
    return per * n_agents + POOL_EXTRA


def n_aces(n_agents: int) -> int:
    """Number of aces in the pool for ``n_agents``: max(2, n // 2)."""
    _check_positive_int(n_agents, "n_agents")
    aces = max(MIN_ACES, n_agents // 2)
    if aces > len(ACES):
        raise ValueError(f"the catalogue has {len(ACES)} aces: n_agents={n_agents} needs {aces}")
    return aces


def max_agents(per_agent: int | None = None) -> int:
    """The largest N the catalogue can serve at ``per_agent`` (every larger N would need more aces or more average
    members)."""
    per = _per_agent(per_agent)
    n = 1
    while _fits(n + 1, per):
        n += 1
    return n


def catalogue() -> list[RosterMon]:
    """Every Pokémon any pool can contain: all aces, then all average members, in pool order."""
    return list(ACES) + list(AVERAGE)


def _as_dict(mon: RosterMon) -> dict:
    return {"name": mon.name, "species": mon.species, "ace": mon.ace, "set_text": mon.set_text}


def pool_mons(n_agents: int, per_agent: int | None = None) -> list[RosterMon]:
    """The pool for ``n_agents`` as RosterMon records: max(2, n // 2) aces first, then average members, fixed order."""
    size = pool_size(n_agents, per_agent)
    aces = n_aces(n_agents)
    return list(ACES[:aces]) + list(AVERAGE[:size - aces])


def pool_for(n_agents: int, per_agent: int | None = None) -> list[dict]:
    """The pool for ``n_agents``: a list of {name, species, ace, set_text} of size ``per_agent`` x n, aces first."""
    return [_as_dict(mon) for mon in pool_mons(n_agents, per_agent)]


def pool_names(n_agents: int, per_agent: int | None = None) -> list[str]:
    """Display names of the pool for ``n_agents``, in pool order."""
    return [mon.name for mon in pool_mons(n_agents, per_agent)]


def pool_mon(name: str, n_agents: int | None = None, per_agent: int | None = None) -> RosterMon | None:
    """The pool (or, with ``n_agents`` None, catalogue) member whose name matches case/space-insensitively, or None."""
    wanted = to_id(name)
    for mon in (catalogue() if n_agents is None else pool_mons(n_agents, per_agent)):
        if to_id(mon.name) == wanted:
            return mon
    return None


def is_ace_in_pool(name: str, n_agents: int, per_agent: int | None = None) -> bool:
    """Whether ``name`` is one of the aces of the pool for ``n_agents`` (False for a name not in that pool)."""
    mon = pool_mon(name, n_agents, per_agent)
    return bool(mon is not None and mon.ace)


def canonical_name(name: str, n_agents: int | None = None, per_agent: int | None = None) -> str | None:
    """The canonical display name for ``name`` in the pool (catalogue when ``n_agents`` is None), or None."""
    mon = pool_mon(name, n_agents, per_agent)
    return None if mon is None else mon.name


# --- Teams for a battle -------------------------------------------------------------------------------------------------


def subject_team_from_pool(names: list[str], levels: Levels = Levels(), n_agents: int | None = None,
                           per_agent: int | None = None) -> str:
    """Packed team of the named pool members, in the given order: aces at ``levels.ace``, the others at ``levels.avg``.

    Names are matched case/space-insensitively against the catalogue, or against the pool for ``n_agents`` (at
    ``per_agent``) when given. Raises ValueError for a wrong count, an unknown name or a duplicate.
    """
    if len(names) != TEAM_SIZE:
        raise ValueError(f"a subject team has exactly {TEAM_SIZE} Pokémon, got {len(names)}")
    mons: list[RosterMon] = []
    for name in names:
        mon = pool_mon(name, n_agents, per_agent)
        if mon is None:
            raise ValueError(f"not in the pool: {name!r}")
        if mon in mons:
            raise ValueError(f"duplicate pool name: {name!r}")
        mons.append(mon)
    sets = [set_with_level(mon.set_text, levels.ace if mon.ace else levels.avg) for mon in mons]
    return pack(sets)


# --- Catalogue-wide helpers (tests, meta) -------------------------------------------------------------------------------


def all_pool_sets() -> list[str]:
    """Every set text the catalogue holds (aces first, then average members, no Level lines)."""
    return [mon.set_text for mon in catalogue()]


def pool_hash(n_agents: int, levels: Levels = Levels(), per_agent: int | None = None) -> str:
    """First 12 hex digits of sha256 over the pool's set texts, the opponent sets and the levels."""
    payload = json.dumps(
        {"pool": [mon.set_text for mon in pool_mons(n_agents, per_agent)],
         "opponents": [text for team in teams.OPPONENT_TEAMS for text in team],
         "levels": {"ace": levels.ace, "avg": levels.avg, "opp": list(levels.opp)}},
        sort_keys=True, ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
