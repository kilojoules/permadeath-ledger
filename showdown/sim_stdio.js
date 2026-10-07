#!/usr/bin/env node
// Same contract as `pokemon-showdown simulate-battle` (see node_modules/pokemon-showdown/sim/SIMULATOR.md),
// without the CLI wrapper's `node build` check and first-run config chatter. Input: `>` lines on stdin.
// Output: messages separated by blank lines ("update", "sideupdate", "end" blocks).
const { BattleTextStream } = require("pokemon-showdown/dist/sim/battle-stream.js");
const Streams = require("pokemon-showdown/dist/lib/streams");
const stdin = Streams.stdin();
const stdout = Streams.stdout();
const battleStream = new BattleTextStream({ noCatch: true, debug: false });
stdin.pipeTo(battleStream);
battleStream.pipeTo(stdout);
