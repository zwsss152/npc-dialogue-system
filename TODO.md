# NPC Dialogue System - Development Roadmap

This file tracks the development status and completed features.

---

## Current Status

**All Core Features + Quest System + Voice Synthesis + Multiplayer + NPC-to-NPC Completed!**

- ✅ v1.0.0 - Initial release with core dialogue system
- ✅ v1.1.0 - Relationship tracking system (Issue #2)
- ✅ v1.2.0 - Unity C# Integration (Issue #1)
- ✅ v1.3.0 - Lore/Knowledge Base with RAG (Issue #3)
- ✅ v1.4.0 - Streaming Responses (Issue #4)
- ✅ v1.5.0 - Quest Generation System (Phase 5)
- ✅ v1.6.0 - Voice Synthesis System (Phase 5)
- ✅ v1.7.0 - Multiplayer NPC Synchronization (Phase 5c)
- ✅ v1.8.0 - NPC-to-NPC Conversations (Phase 5d)
- ✅ v1.9.0 - Dungeon Master AI — Event-Driven Narrative Overseer (Phase 5e)
- ✅ v1.10.0 - Eavesdrop System (this fork)          
- ❌ v2.0.0 - Player simulation + novel chronicle — REMOVED in this fork

### Phase 5f: Eavesdrop System ✅ (this fork)

**Status:** COMPLETED

**Implementation:**
- `eavesdrop.py` — the mechanic itself
  - `EavesdropSpot` data model: position, hear radius, notice radius, NPC anchor, fixed topic, intel payload
  - `PlayerPresence`: position + posture + cover, reported by the client
  - Detection scaled by posture (run 2.5x, crouch 0.5x, cover 0.4x further)
  - `check()` returns a machine code *and* a player-facing sentence for every failure
  - Intel ledger, prompt-ready brief, and transcript prefetch/caching
- `eavesdrop_spots.json` — four authored spots forming a three-stage mystery
- `npc_conversation.py` — three hooks added
  - `ConversationTrigger.EAVESDROP`
  - `topic_id` forced through `start_conversation` / `run_full_conversation`
  - `ambient_filter` callback so random chatter yields the pair a player is waiting on
- `api_server.py` — 9 endpoints under `/api/eavesdrop/` plus the `/eavesdrop` map page
- `static/eavesdrop.html` — playable 2D map: move, choose posture, take cover, listen
- `demo_eavesdrop.py`, `test_eavesdrop.py` (29 tests), `EAVESDROP.md`

**Bugs fixed along the way:**
- `run_full_conversation()` never executed a single turn — the loop waited on `ACTIVE` while a
  fresh conversation starts in `STARTING`. NPC-to-NPC conversations had never actually run.
- A missing Ollama backend raised `ConnectionError` on construction, making the whole codebase
  impossible to test without a running model. Now warns, with `NPC_STRICT_BACKEND=1` to opt back in.
- Ollama probe now has a 30s cooldown so building N NPCs does not cost N × 5s of timeouts.
- Offline template responses were subject-blind filler; they are now topic-aware and de-duplicated.

**API Endpoints:**
- GET  /eavesdrop
- GET  /api/eavesdrop/map
- POST /api/eavesdrop/presence
- GET  /api/eavesdrop/check/{spot_id}
- POST /api/eavesdrop/listen
- GET  /api/eavesdrop/session/{player_id}
- GET  /api/eavesdrop/status/{player_id}
- GET  /api/eavesdrop/intel/{player_id}
- POST /api/eavesdrop/prefetch

### Phase 5e: Dungeon Master AI ✅

**Status:** COMPLETED

**Implementation:**
- `dungeon_master.py` - Core DM class (~1100 lines)
  - Event-driven narrative overseer that subscribes to game events
  - Hardcoded fast-path rules for relationship transitions, quest outcomes, faction changes
  - LLM judgment fallback for ambiguous events
  - Observation logging and pattern detection
  - Rule compilation pipeline (patterns → structured rules)
  - Tension tracking with decay and force-trigger thresholds
  - Narrative memory compression (arc summaries + event summaries)
  - Story arc management (create, advance, resolve)
  - Persistence and auto-save
- `dm_rule_engine.py` - Compiled Rule DSL interpreter (~575 lines)
  - Structured JSON rule validation (schema, operators, action whitelist)
  - Condition matching with 11 operators (eq, neq, gt, lt, gte, lte, in, contains, not_contains, exists, regex)
  - Action resolution with event placeholder substitution
  - Rule lifecycle (add, activate, deactivate, delete, eviction)
  - Circular trigger prevention
  - Duplicate detection
  - File-based persistence (active/ and pending/ directories)
- `npc_dialogue.py` - DM directive support
  - `set_dm_directive()` - Apply temporary behavioral directives to NPCs
  - `_get_dm_directive_modifier()` - Auto-expiring prompt modifiers
  - DM directives injected into system prompts
- `api_server.py` - Full DM integration
  - DM initialization in startup, cleanup in shutdown
  - 6 directive consumer callbacks (quest, npc, world, conversation, lore, relationship)
  - 12 API endpoints under `/api/dm/`
- `demo_dungeon_master.py` - Comprehensive demo (5 scenarios)
- `test_dungeon_master.py` - Unit tests (~35 tests)
- `test_dm_rule_engine.py` - Rule engine tests (~40 tests)
- `DUNGEON_MASTER.md` - Complete design documentation

**API Endpoints:**
- GET /api/dm/status - DM status and narrative state summary
- GET /api/dm/arcs - List story arcs
- GET /api/dm/arcs/{id} - Get arc details
- POST /api/dm/arcs - Create a story arc
- GET /api/dm/rules - List active and pending rules
- GET /api/dm/rules/{id} - Get rule details
- POST /api/dm/rules/{id}/approve - Approve a pending rule
- POST /api/dm/rules/{id}/deactivate - Deactivate an active rule
- DELETE /api/dm/rules/{id} - Delete a rule
- POST /api/dm/trigger - Manually trigger a DM evaluation
- POST /api/dm/save - Force-save DM state
- POST /api/dm/reset - Reset DM narrative state

### Phase 5d: NPC-to-NPC Conversations ✅

**Status:** COMPLETED

**Implementation:**
- `npc_conversation.py` - Core conversation system (950+ lines)
  - ConversationTrigger - How conversations start (proximity, scheduled, event, forced)
  - ConversationState - State machine (idle, starting, active, ending, completed)
  - ConversationTopic - 10 built-in topics (gossip, trade, weather, quests, etc.)
  - ConversationExchange - Individual dialogue turns
  - NPCConversation - Full conversation data structure
  - ConversationTopicRegistry - Topic management with cooldowns
  - NPCConversationEngine - LLM-based dialogue generation
  - ConversationManager - Full conversation lifecycle management
- Features:
  - Proximity-based conversations (NPCs near each other)
  - Relationship-aware topic selection
  - Turn-by-turn or full conversation execution
  - Player overhearing support
  - Topic progression and cooldowns
  - Location-based NPC grouping
  - Conversation history and persistence
- `demo_npc_conversation.py` - Comprehensive demo
  - 8 demo scenarios
  - Quick demo mode
  - CLI arguments for specific demos
- API endpoints in `api_server.py`:
  - POST /api/conversations/start - Start NPC conversation
  - POST /api/conversations/{id}/turn - Run single turn
  - POST /api/conversations/{id}/run - Run full conversation
  - POST /api/conversations/{id}/end - End conversation
  - GET /api/conversations/{id} - Get conversation details
  - GET /api/conversations - List active conversations
  - GET /api/conversations/npc/{name} - Get NPC's conversation
  - GET /api/conversations/location/{loc} - Get conversations at location
  - POST /api/conversations/proximity/check - Trigger proximity check
  - POST /api/conversations/location/{npc} - Update NPC location
  - GET /api/conversations/topics - List available topics
  - POST /api/conversations/save - Save history
  - POST /api/conversations/load - Load history

---

## Completed Issues

### Issue #1: Unity C# Integration Wrapper ✅

**Status:** COMPLETED

**Implementation:**
- `api_server.py` - FastAPI REST API server
- `unity_client/` - Complete C# client library
  - `NPCDialogueClient.cs` - Main API client
  - `NPCCharacter.cs` - NPC interaction component
  - `DialogueUI.cs` - Dialogue display system
  - `NPCSceneManager.cs` - Scene manager
  - `RelationshipTracker.cs` - Client-side tracking
  - `Editor/NPCDialogueWindow.cs` - Unity Editor tools
- `UNITY_INTEGRATION.md` - Complete documentation

### Issue #2: Relationship Tracking System ✅

**Status:** COMPLETED

**Implementation:**
- `relationship_tracking.py` - Core relationship system
- 6 relationship levels (Hated → Adored)
- Temperature adjustment based on relationship
- Faction support for group reputation
- Interaction history logging
- Save/load persistence

### Issue #3: Lore/Knowledge Base with RAG ✅

**Status:** COMPLETED

**Implementation:**
- `lore_system.py` - RAG system with ChromaDB
- `lore_templates/` - Example lore files
  - `world_history.json`
  - `locations.json`
  - `characters.json`
  - `factions.json`
- `LORE_SYSTEM.md` - Documentation
- `demo_lore.py` - Working examples
- NPC-specific knowledge filtering

### Issue #4: Streaming Responses ✅

**Status:** COMPLETED

**Implementation:**
- API server streaming endpoint (SSE)
- Unity client streaming support
- `demo_streaming.py` - CLI demo
- Token-by-token display
- Timing statistics

### Phase 5: Quest Generation System ✅

**Status:** COMPLETED

**Implementation:**
- `quest_generator.py` - Core quest system (1168 lines)
  - 6 quest types (kill, fetch, explore, escort, collection, dialogue)
  - Quest, Objective, QuestReward dataclasses
  - QuestGenerator with template-based generation
  - QuestManager for state management
  - Difficulty scaling based on player level
  - NPC archetype-based quest selection
  - Time-limited quests with automatic failure
  - Multi-objective quests with optional goals
- `demo_quest.py` - Comprehensive demo
- Quest API endpoints in `api_server.py`
  - GET /api/quests/npc/{npc_name}
  - POST /api/quests/generate
  - POST /api/quests/{quest_id}/accept
  - POST /api/quests/{quest_id}/abandon
  - GET /api/quests/active
  - POST /api/quests/{quest_id}/progress
  - POST /api/quests/{quest_id}/complete
  - GET /api/quests/summary
- Integration with relationship_tracking and lore_system
- Save/load persistence

### Phase 5b: Voice Synthesis System ✅

**Status:** COMPLETED

**Implementation:**
- `voice_synthesis.py` - Multi-provider TTS system
  - 6 TTS providers (ElevenLabs, OpenAI, Edge TTS, gTTS, pyttsx3, Coqui)
  - VoiceConfig for voice customization (speed, pitch, emotion)
  - VoiceSystem with NPC voice profile management
  - Audio caching for repeated phrases
  - Automatic fallback to available providers
  - Default voice profiles for NPC archetypes
- `demo_voice.py` - Comprehensive demonstration
- `VOICE_SYNTHESIS.md` - Documentation
- Voice API endpoints in `api_server.py`
  - POST /api/voice/synthesize
  - GET /api/voice/synthesize/{npc_name}
  - POST /api/voice/register
  - GET /api/voice/providers
  - GET /api/voice/voices
  - GET /api/voice/profiles
  - GET /api/voice/audio/{filename}

### Phase 5c: Multiplayer NPC Synchronization ✅

**Status:** COMPLETED

**Implementation:**
- `npc_state_manager.py` - Server-side state management
  - PlayerState - Isolated per-player data (dialogue, relationships, active quests)
  - NPCWorldState - Shared NPC visibility/activity state
  - WorldState - Global state (completed quests, faction reputation, world events)
  - Zone management for location-based subscriptions
  - Event emission for state changes
  - Save/load persistence
- `event_system.py` - Real-time event broadcasting
  - EventBroadcaster - WebSocket subscription management
  - Topic-based subscriptions (player, npc, zone, event_type)
  - Event history for reconnection support
  - Ping/pong connection health checks
  - Automatic cleanup of dead connections
  - EventSystem - Integrated state + broadcasting
- WebSocket endpoint in `api_server.py`
  - `/ws/{player_id}` - Real-time multiplayer connection
  - Actions: subscribe, unsubscribe, pong, zone_change, dialogue, quest_accept, quest_complete, relationship_update
- REST API endpoints for multiplayer:
  - GET /api/multiplayer/status
  - GET /api/multiplayer/players
  - GET /api/multiplayer/players/{player_id}
  - GET /api/multiplayer/world
  - GET /api/multiplayer/npcs
  - POST /api/multiplayer/npcs/{npc_id}/register
  - POST /api/multiplayer/save
  - POST /api/multiplayer/load
- `demo_multiplayer.py` - Comprehensive demonstration
  - Simulated multiplayer connections
  - Player-isolated dialogue history
  - Shared quest completion
  - Faction reputation synchronization
  - Zone change handling

---

## Project Structure

```
npc-dialogue-system/
├── README.md                    # Main documentation
├── QUICKSTART.md               # 5-minute setup
├── PROJECT_SUMMARY.md          # Overview
├── UNITY_INTEGRATION.md        # Unity guide
├── LORE_SYSTEM.md              # RAG documentation
├── CHANGELOG.md                # Version history
├── TODO.md                     # This file
│
├── npc_dialogue.py             # Core dialogue engine
├── relationship_tracking.py    # Relationship system
├── lore_system.py              # RAG/lore system
├── quest_generator.py          # Quest generation system
├── voice_synthesis.py          # Voice synthesis system
├── npc_state_manager.py        # Multiplayer state management
├── event_system.py             # Real-time event broadcasting
├── npc_conversation.py         # NPC-to-NPC conversations
├── eavesdrop.py                # Eavesdrop spots, detection, intel (this fork)
├── eavesdrop_spots.json        # Eavesdrop content (this fork)
├── api_server.py               # REST API + WebSocket server
│
├── main.py                     # CLI demo
├── demo.py                     # Interactive demo
├── demo_lore.py                # Lore demo
├── demo_streaming.py           # Streaming demo
├── demo_quest.py               # Quest system demo
├── demo_voice.py               # Voice system demo
├── demo_multiplayer.py         # Multiplayer demo
├── demo_npc_conversation.py    # NPC conversation demo
├── demo_eavesdrop.py           # Eavesdrop demo (this fork)
├── test_eavesdrop.py           # 29 eavesdrop tests (this fork)
│
├── character_cards/            # NPC definitions
│   ├── blacksmith.json
│   ├── merchant.json
│   └── wizard.json
│
├── lore_templates/             # Knowledge base
│   ├── world_history.json
│   ├── locations.json
│   ├── characters.json
│   └── factions.json
│
├── quest_templates/            # Quest templates (optional)
│
├── unity_client/               # Unity C# client
│   ├── NPCDialogueClient.cs
│   ├── NPCCharacter.cs
│   ├── DialogueUI.cs
│   ├── NPCSceneManager.cs
│   ├── RelationshipTracker.cs
│   └── Editor/
│       └── NPCDialogueWindow.cs
│
├── static/
│   └── eavesdrop.html          # Playable eavesdrop map (this fork)
│
├── docs/                       # Documentation
│   ├── QUEST_GENERATION_DESIGN.md
│   └── VOICE_SYNTHESIS.md
│
└── .github/                    # GitHub templates
    ├── workflows/
    └── ISSUE_TEMPLATE/
```

---

## Version History

| Version | Date | Features |
|---------|------|----------|
| v1.10.0 | 2026-09-19 | Eavesdrop system + 4 bug fixes (fork) |
| v1.8.0 | 2026-04-14 | NPC-to-NPC conversations |
| v1.7.0 | 2026-04-13 | Multiplayer NPC synchronization |
| v1.6.0 | 2026-04-13 | Voice synthesis system |
| v1.5.0 | 2026-04-13 | Quest generation system |
| v1.4.0 | 2026-04-08 | Streaming responses |
| v1.3.0 | 2026-04-08 | Lore/KB with RAG |
| v1.2.0 | 2026-04-08 | Unity C# integration |
| v1.1.0 | 2026-04-07 | Relationship tracking |
| v1.0.0 | 2026-04-06 | Initial release |

---

## Future Enhancements

Potential features for future development:

### Phase 5: Advanced Features (Continued)
- [x] NPC-to-NPC conversations ✅ v1.8.0
- [x] Multiplayer NPC synchronization ✅ v1.7.0
- [ ] Character personality fine-tuning (LoRA)

### Phase 6: Production Features
- [ ] Performance optimization (batch requests)
- [ ] Response caching layer
- [ ] Analytics dashboard
- [ ] A/B testing for prompts
- [ ] Multi-language support

### Phase 7: Game Engine Plugins
- [ ] Godot GDScript integration
- [ ] Unreal Engine C++ plugin
- [ ] Web/JavaScript API

---

## Quick Reference

**Repository:** https://github.com/morganpage-tech/npc-dialogue-system

**Running the System:**
```bash
# Start Ollama
ollama serve

# Start API server
python api_server.py

# Run CLI demo
python main.py

# Run lore demo
python demo_lore.py

# Run streaming demo
python demo_streaming.py

# Run quest demo
python demo_quest.py

# Run voice demo
python demo_voice.py

# Run multiplayer demo
python demo_multiplayer.py

# Run eavesdrop demo
python demo_eavesdrop.py

# Run NPC conversation demo
python demo_npc_conversation.py
```

**Testing:**
```bash
python setup_check.py        # System check
python test_structure.py     # Structure tests
```

**Current Version:** v1.8.0

**Status:** Production Ready for Indie Games

---

## Notes

- All core features from the original roadmap are complete
- The system is ready for production use in indie games
- Future enhancements can be added based on user feedback
- Contributions welcome via GitHub Issues and PRs
