#!/usr/bin/env node
// Thin helper over the installed pokemon-showdown package (pinned in package.json).
// Usage:
//   node ps_tool.js pack            < export-or-json-team   -> packed team on stdout (JSON string)
//   node ps_tool.js json            < any-format-team       -> JSON team on stdout
//   node ps_tool.js validate FORMAT < any-format-team       -> {"ok":bool,"problems":[...]}
//   node ps_tool.js dex             -> JSON dump of gen9 species, moves, type chart, package version
//   node ps_tool.js version         -> package version
const fs = require("fs");
const PS = require("pokemon-showdown");
const { Teams, Dex, TeamValidator } = PS;
const cmd = process.argv[2];
function readStdin() { return fs.readFileSync(0, "utf8"); }
function out(x) { process.stdout.write(JSON.stringify(x) + "\n"); }

if (cmd === "version") {
  out(require("pokemon-showdown/package.json").version);
} else if (cmd === "pack") {
  const team = Teams.import(readStdin());
  if (!team) { console.error("could not parse team"); process.exit(1); }
  out(Teams.pack(team));
} else if (cmd === "json") {
  const team = Teams.import(readStdin());
  if (!team) { console.error("could not parse team"); process.exit(1); }
  out(team);
} else if (cmd === "validate") {
  const format = process.argv[3] || "gen9customgame";
  const team = Teams.import(readStdin());
  const v = new TeamValidator(format);
  const problems = v.validateTeam(team);
  out({ ok: !problems, problems: problems || [] });
} else if (cmd === "dex") {
  const dex = Dex.forGen(9);
  const species = {};
  for (const s of dex.species.all()) {
    if (s.num <= 0) continue;
    species[s.id] = {
      name: s.name, num: s.num, types: s.types, baseStats: s.baseStats, abilities: s.abilities,
      nonstandard: s.isNonstandard || null, nfe: !!s.nfe, weightkg: s.weightkg, baseSpecies: s.baseSpecies,
      forme: s.forme || "", tier: s.tier || "", heightm: s.heightm,
    };
  }
  const moves = {};
  for (const m of dex.moves.all()) {
    if (m.isNonstandard && m.isNonstandard !== "Past") continue;
    moves[m.id] = {
      name: m.name, type: m.type, basePower: m.basePower, category: m.category, accuracy: m.accuracy,
      priority: m.priority, pp: m.pp, target: m.target, flags: m.flags, nonstandard: m.isNonstandard || null,
      shortDesc: m.shortDesc, multihit: m.multihit || null, drain: m.drain || null, recoil: m.recoil || null,
      selfdestruct: m.selfdestruct || null, ohko: m.ohko || null, heal: m.heal || null,
      secondary: m.secondary ? { chance: m.secondary.chance, status: m.secondary.status || null, volatileStatus: m.secondary.volatileStatus || null, boosts: m.secondary.boosts || null } : null,
      boosts: m.boosts || null, status: m.status || null, volatileStatus: m.volatileStatus || null,
      self: m.self ? { boosts: m.self.boosts || null } : null, sideCondition: m.sideCondition || null, weather: m.weather || null, terrain: m.terrain || null,
    };
  }
  const types = {};
  for (const t of dex.types.all()) types[t.id] = { name: t.name, damageTaken: t.damageTaken };
  const items = {};
  for (const it of dex.items.all()) { if (!it.isNonstandard) items[it.id] = { name: it.name }; }
  const abilities = {};
  for (const ab of dex.abilities.all()) { if (!ab.isNonstandard) abilities[ab.id] = { name: ab.name, shortDesc: ab.shortDesc }; }
  const revivalLearners = Object.entries(dex.data.Learnsets).filter(([k, v]) => v.learnset && v.learnset.revivalblessing).map(([k]) => k);
  out({ package_version: require("pokemon-showdown/package.json").version, gen: 9, species, moves, types, items, abilities, revival_moves: ["revivalblessing"], revival_learners: revivalLearners });
} else {
  console.error("unknown command"); process.exit(2);
}
