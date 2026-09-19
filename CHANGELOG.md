# Changelog

All notable changes to the NPC Dialogue System will be documented in this file.

## [1.10.0] - 2026-09-19 — fork

### Added — Eavesdrop System

Deliberate listening, modelled on the Spycraft pillar in *007: First Light*. NPC-to-NPC
conversations stop being pure ambience and become something the player positions
themselves to overhear.

- `eavesdrop.py`
  - Spots with two radii: a hear radius and a notice radius, so there is a band that is
    close enough to hear and far enough to be ignored
  - Detection scaled by posture — running 2.5x, crouching 0.5x, cover a further 0.4x
  - Every rejection returns a player-facing sentence, not just a boolean
  - Intel ledger that renders straight into an NPC system prompt
  - Transcript prefetch, because local models need 10-20s and the button cannot wait
- `eavesdrop_spots.json` — four authored spots forming a three-stage mystery; the finale is
  gated on intel from the other three
- `static/eavesdrop.html` — playable 2D map
- `demo_eavesdrop.py`, `test_eavesdrop.py` (29 tests), `EAVESDROP.md`
- `api_server.py` — 9 endpoints under `/api/eavesdrop/`

### Added — hooks in `npc_conversation.py`

- `ConversationTrigger.EAVESDROP`
- `topic_id` may be forced through `start_conversation` / `run_full_conversation`
- `ambient_chance` is now configurable, and `ambient_filter` lets the eavesdrop system
  suppress pairs the player is currently waiting on

### Removed

- `player_simulation.py`, `PLAIYER_CHARACTER.md`, `static/chronicle.html`
- `/api/simulation/*`, `/api/chronicle/*`, `/ws/chronicle`, `/chronicle`
- All related imports, globals, startup/shutdown wiring and documentation references

### Fixed

- **`run_full_conversation()` never ran a single turn.** The loop waited on
  `ConversationState.ACTIVE`, but a fresh conversation starts in `STARTING` and only flips
  inside the loop body. Every NPC-to-NPC conversation — including the proximity trigger and
  `POST /api/conversations/{id}/run` — had been silently producing nothing.
- A missing LLM backend raised `ConnectionError` from `NPCDialogue.__init__`, which made the
  entire codebase impossible to import or test without a running Ollama. It now warns;
  `NPC_STRICT_BACKEND=1` restores fail-fast.
- The Ollama availability probe now has a 30-second cooldown. Creating N NPCs used to cost
  N × 5s of dead timeout.
- Offline fallback responses were subject-blind filler that repeated itself; they are now
  topic-aware and skip lines already spoken in the conversation.

### Changed

- README: Ollama documented as optional; requirements no longer assume Apple Silicon.

## [1.1.0] - 2026-04-07

### Added - Relationship Tracking System

**Core Features:**
- Full relationship tracking with scores (-100 to +100)
- Six relationship levels: Hated, Disliked, Neutral, Liked, Loved, Adored
- Dynamic NPC personality based on relationship score
- Temperature adjustment for dialogue consistency
- Speaking style modifiers per relationship level

**Relationship Updates:**
- Quest completion relationship rewards
- Gift giving mechanics with diminishing returns
- Dialogue choice impact on relationships
- Faction support for group-wide relationships
- Time-based relationship decay (optional)

**Persistence:**
- Full save/load persistence for relationships
- Query system for relationship conditions
- Relationship summary and statistics

### New Files

| File | Lines | Description |
|------|-------|-------------|
| `relationship_tracking.py` | 500+ | Complete relationship tracking system |
| `test_relationships.py` | 600+ | 50 unit tests (100% passing) |
| `RELATIONSHIPS.md` | 450+ | Comprehensive documentation |
| `demo_relationships.py` | 300+ | Feature demonstration script |
| `test_demo_relationships.py` | 200+ | Simplified demo |

### Updated Files

- `npc_dialogue.py` - Integrated relationship tracking
- `README.md` - Added relationship examples
- `character_cards/*.json` - Added relationship fields

### Integration

- `RelationshipTracker` class for standalone use
- `NPCDialogue` integration via `relationship_tracker` parameter
- `NPCManager` support for shared relationship tracking
- Character cards support `gift_preferences` and `relationships` fields

### Tests

- 50 unit tests covering all relationship features
- Test categories: State, Levels, Scores, Temperature, Quests, Gifts, Dialogue, Factions, Persistence, Time Decay, Queries, Summary

---

## [1.0.0] - 2026-04-07

### Added
- Initial release of AI-powered NPC dialogue system
- Core dialogue engine with Ollama integration
- 3 example characters (Thorne - Blacksmith, Elara - Merchant, Zephyr - Wizard)
- Interactive CLI demo for testing conversations
- Conversation memory and persistence
- Character card system (SillyTavern JSON format)
- Complete documentation set
- Setup and testing scripts
- MIT License for commercial use

### Features
- 100% local LLM inference (no cloud costs)
- NPCs remember past conversations
- Multiple character support with seamless switching
- Game state context in dialogue
- Easy-to-use Python API
- Works on M1 MacBook Air (8GB/16GB RAM)

### Documentation
- README.md - Full system documentation
- QUICKSTART.md - 5-minute setup guide
- PROJECT_SUMMARY.md - Overview and roadmap
- INTEGRATION_GUIDE.md - Unity/Godot/Web integration
- 1,480 lines of comprehensive documentation
- Step-by-step setup instructions
- Game engine integration examples
- Performance optimization tips
- Troubleshooting guide

### Files
- npc_dialogue.py (327 lines) - Core dialogue engine
- main.py (228 lines) - CLI demo
- setup_check.py (145 lines) - Setup verification
- test_structure.py (113 lines) - System tests
- 4 documentation files
- 3 character cards
- Setup and configuration files

**Total: 12 files, 2,129 lines**
