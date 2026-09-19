# Lore-Alive NPC Dialogue System

A prototype AI-powered NPC dialogue system for games, running locally on M1 MacBook Air.

> **This is a fork.** Upstream: [morganpage-tech/npc-dialogue-system](https://github.com/morganpage-tech/npc-dialogue-system)
> (MIT, © 2026 Morgan Page). The original `LICENSE` is kept verbatim — see below for what changed.
>
> **Changes in this fork**
> - **Added** a full eavesdrop system (`eavesdrop.py`, `EAVESDROP.md`) — designed positions, posture-based
>   detection, fixed topics, and intel that feeds back into NPC dialogue.
> - **Removed** the automated player simulation engine and the novel-style chronicle viewer
>   (`player_simulation.py`, `PLAIYER_CHARACTER.md`, `static/chronicle.html`, and the
>   `/api/simulation/*` + `/api/chronicle/*` endpoints).
> - **Fixed** `run_full_conversation()` never running a single turn (the loop waited for a state the
>   conversation only enters from inside the loop).
> - **Changed** a missing LLM backend from a hard crash into a warning, so the system is testable offline.
> - **Improved** the offline template responses to be topic-aware instead of generic filler.

## Features

- Local LLM inference (no cloud costs or latency)
- Character-specific personalities via system prompts
- Conversation memory (NPCs remember past interactions)
- Swappable character cards (SillyTavern format)
- Simple CLI interface for testing
- Relationship tracking system - NPCs remember player reputation and respond differently based on trust levels
- **Eavesdrop system** - NPC-to-NPC conversations the player has to position themselves to overhear,
  with posture-based detection and intel that unlocks later dialogue

## Requirements

- Python 3.9+
- **Ollama is optional.** Without it the system warns and falls back to offline template
  responses, so `python demo_eavesdrop.py` and the unit tests run with no model installed.
  Set `NPC_STRICT_BACKEND=1` if you would rather it fail fast.
- For real generation: [Ollama](https://ollama.com) and 8GB+ RAM recommended.
  The setup below assumes Apple Silicon; it runs on Linux and Windows too.

## Quick Start

### 1. Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

### 2. Pull a model

For 8GB RAM:
```bash
ollama pull llama3.2:1b
```

For 16GB RAM:
```bash
ollama pull llama3.2:3b
```

### 3. Install Python dependencies

```bash
cd ~/npc-dialogue-system
pip install -r requirements.txt
```

### 4. Run the demo

```bash
python main.py
```

## Project Structure

```
npc-dialogue-system/
├── README.md                 # This file
├── requirements.txt          # Python dependencies
├── main.py                  # Main CLI demo
├── npc_dialogue.py          # Core dialogue system
├── character_cards/         # Character definitions
│   ├── blacksmith.json
│   ├── merchant.json
│   └── wizard.json
└── conversation_history/     # Persistent conversation storage
    └── (auto-created)
```

## Usage Examples

### Basic Conversation

```python
from npc_dialogue import NPCDialogue

# Create an NPC
npc = NPCDialogue(
    character_name="Thorne",
    character_card="character_cards/blacksmith.json",
    model="llama3.2:1b",
    player_id="player_001"
)

# Generate responses
response = npc.generate_response("Can you fix my sword?")
print(response)
```

### Loading Conversation History

```python
# NPC remembers previous conversations
npc.load_history()

# Continue from last interaction
response = npc.generate_response("Remember when we fought the dragon together?")
```

### Relationship Tracking

```python
from npc_dialogue import NPCDialogue
from relationship_tracking import RelationshipTracker

# Create a relationship tracker
tracker = RelationshipTracker(player_id="player_001")

# Create NPC with relationship tracking
npc = NPCDialogue(
    character_name="Thorne",
    character_card="character_cards/blacksmith.json",
    model="llama3.2:1b",
    relationship_tracker=tracker
)

# Complete a quest - relationship improves
npc.update_from_quest("repair_anvil", success=True, reward=15.0)
# Thorne relationship: +15.0

# Give a gift - relationship improves more
npc.update_from_gift("rare_iron_ore", value=10.0)
# Thorne relationship: +10.0

# Check current relationship
print(f"Score: {npc.get_relationship_score()}")  # 25.0
print(f"Level: {npc.get_relationship_level()}")  # LIKED

# NPC will now be friendlier in dialogue
response = npc.generate_response("Can you help me with something?")
# Response will reflect Liked relationship level

# Save relationships to file
tracker.save()
```

**See [RELATIONSHIPS.md](RELATIONSHIPS.md) for comprehensive relationship tracking documentation.**

## Character Card Format

Character cards use a JSON format based on SillyTavern's popular format:

```json
{
  "name": "Character Name",
  "description": "Brief character description",
  "personality": "Personality traits, speaking style",
  "first_mes": "Opening greeting message",
  "mes_example": "Example dialogue exchanges",
  "system_prompt": "Custom system instructions"
}
```

## Performance Tips

- **8GB RAM**: Use 1B models, limit context to 2048 tokens
- **16GB RAM**: Use 3B models, 4096 tokens context
- Close browser tabs while running
- Keep laptop plugged in for max performance
- Use smaller models for ambient NPCs, larger for quest-givers

## Eavesdropping

```bash
python demo_eavesdrop.py                 # offline walkthrough of every rule

python -m uvicorn api_server:app         # then open http://localhost:8000/eavesdrop
```

On the map: **WASD / arrow keys** to move, pick a posture, tick *In cover*, click a spot,
press **Listen**. A spot tells you why you cannot listen yet — *"They noticed you — the
conversation stopped."*, *"You were too loud."*, *"You haven't heard enough to follow this."*

Local models need 10–20 seconds per conversation. Call `POST /api/eavesdrop/prefetch` at
level load so the button is instant when the player presses it.

See [EAVESDROP.md](EAVESDROP.md) for the geometry rules and how to author your own spots.

## Next Steps

- Add lore/knowledge base (RAG with ChromaDB)
- Add quest triggers in dialogue
- Create character personality fine-tuning (LoRA)
- Add voice synthesis integration

## Documentation

- [EAVESDROP.md](EAVESDROP.md) - Eavesdrop system: positions, detection, intel, API
- [RELATIONSHIPS.md](RELATIONSHIPS.md) - Complete relationship tracking guide
- [QUICKSTART.md](QUICKSTART.md) - 5-minute setup guide
- [INTEGRATION_GUIDE.md](INTEGRATION_GUIDE.md) - Game engine integration examples
- [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md) - Technical overview

## License

MIT License - Free for commercial game development
