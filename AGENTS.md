# AGENTS.md

Instructions for AI coding agents (Codex, Claude Code, Cursor, Gemini CLI, Kimi, …)
and for humans who want the short version.

---

## 1. What this repo is

A local LLM backend that gives a game NPC-to-NPC conversations and a player-facing
**eavesdrop** mechanic. It is **not** a game engine — it does not render, handle input,
or do collision. It exposes HTTP endpoints (`api_server.py`) and a CLI (`main.py`);
a game (Unity client included) calls in and gets dialogue back.

**It is a fork.** Upstream: [morganpage-tech/npc-dialogue-system](https://github.com/morganpage-tech/npc-dialogue-system)
(MIT, © 2026 Morgan Page). This fork adds the eavesdrop system and removes the player
simulation / chronicle feature. See `EAVESDROP.md` and `CHANGELOG.md`.

> **Provenance rule — do not break this.** `LICENSE` must stay byte-identical to upstream.
> Do not rebrand, do not remove the "this is a fork" notice in `README.md`, and do not
> present this repository as original work. Additions should be described as additions.

## 2. Quick start — no model required

Nothing below needs Ollama, an API key, or `pip install` beyond `requests`:

```bash
python demo_eavesdrop.py          # full eavesdrop walkthrough, offline
python -m unittest test_eavesdrop # 29 tests
python main.py                    # CLI chat (asks for a model; degrades gracefully)
```

If no LLM backend is reachable the system **warns and falls back to template responses**
instead of crashing. Set `NPC_STRICT_BACKEND=1` to restore fail-fast behaviour.
Do not reintroduce a hard crash on a missing backend — offline testability is a feature.

Full stack (API server, Unity, RAG) needs `pip install -r requirements.txt`.
`chromadb` + `sentence-transformers` are optional; the lore system silently degrades
when they are absent (`HAS_CHROMADB=False`).

## 3. Layout

| Path | What |
|---|---|
| `eavesdrop.py` | **The mechanic.** Spots, presence, detection, intel ledger |
| `eavesdrop_spots.json` | Authored content: 4 spots forming a 3-stage mystery |
| `npc_conversation.py` | NPC-to-NPC conversations; the eavesdrop hooks live here |
| `npc_dialogue.py` | Single-NPC dialogue: character card → system prompt → LLM |
| `api_server.py` | FastAPI app, ~80 endpoints, `/eavesdrop` map page |
| `dungeon_master.py` + `dm_rule_engine.py` | Narrative overseer; compiles repeated LLM judgements into permanent JSON rules |
| `relationship_tracking.py` | Player↔NPC relationship score; also drives sampling temperature |
| `static/eavesdrop.html` | Playable 2D map (move, posture, take cover, listen) |
| `unity_client/` | C# REST client + Unity Editor window |
| `EAVESDROP.md`, `DUNGEON_MASTER.md`, `RELATIONSHIPS.md`, `INTEGRATION_GUIDE.md` | Design docs — read these before changing behaviour |

## 4. Invariants you must not break

**Eavesdrop geometry.** Each spot has two radii: `radius` (the player can hear) and
`alert_radius` (the NPC notices). The mechanic only exists if those two circles overlap
in a band — close enough to hear, far enough to be ignored. `TestSpotGeometry` in
`test_eavesdrop.py` asserts both directions. If you add a spot and those tests fail,
the spot is unplayable; fix the coordinates, do not relax the test.

**Spot anchors beat global positions.** A spot declares `npc_anchor` — where the pair
actually stands for *that* conversation. An NPC appears in several spots and in
`npc_positions`, so detection must not fall back to global coordinates when an anchor
is present.

**No random topics in eavesdrop content.** Topics are pinned per spot. Random selection
pulled in filler like weather chatter and made the mechanic feel worthless.
Ambient NPC chatter may stay random, but it must yield to a player waiting at a spot
(`ambient_filter`).

**Offline fallback responses must be topic-aware** and must not repeat a line already
spoken in the same conversation.

## 5. Working conventions

- Python 3.9+. Standard library first; keep new runtime dependencies out of the
  eavesdrop path so it stays testable without a virtualenv.
- Tests are `unittest`, one `test_*.py` per module: `python -m unittest test_<module>`.
  Run `demo_eavesdrop.py` when you touch detection, geometry or intel.
- Keep player-facing rejection messages human-readable. Every `check()` failure returns
  a machine code *and* a sentence the player will read ("They noticed you — the
  conversation stopped.").
- Comments explain **why**, not what. Several existing comments record a real bug.

## 6. Git

Remote is SSH (`git@github.com:zwsss152/npc-dialogue-system.git`). Push with plain
`git push`; no token needed. Port 22 is blocked on this machine — `~/.ssh/config`
routes `github.com` to `ssh.github.com:443`. If you clone elsewhere and get a timeout,
that routing is the fix, not a broken key.
