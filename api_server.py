"""
NPC Dialogue API Server
FastAPI REST API for Unity/game engine integration
"""

import os
import json
import asyncio
import time
from typing import Optional, List, Dict, Any
from pathlib import Path

from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse
from pydantic import BaseModel

from npc_dialogue import NPCDialogue, NPCManager
from relationship_tracking import RelationshipTracker
from quest_generator import QuestGenerator, QuestManager, QuestType, ObjectiveType
from quest_extractor import QuestExtractor
from inventory_validation import validate_inventory_for_input
from voice_synthesis import VoiceSystem, VoiceConfig, VoiceProvider
from npc_state_manager import NPCStateManager, StateEvent, EventType
from event_system import EventSystem, EventBroadcaster
from npc_conversation import (
    ConversationManager, NPCConversationEngine, NPCConversation,
    ConversationTrigger, ConversationState
)
from performance import (
    PerformanceManager, ResponseCache, OllamaConnectionPool,
    BatchProcessor, PerformanceMetrics
)
from dungeon_master import DungeonMaster, DungeonMasterConfig
from dm_rule_engine import DmRuleEngine
from llm_providers import create_provider
from eavesdrop import EavesdropManager, Posture

EAVESDROP_SPOTS_FILE = Path("eavesdrop_spots.json")

# Initialize FastAPI app
app = FastAPI(
    title="NPC Dialogue System API",
    description="REST API for AI-powered NPC dialogue in games",
    version="1.2.0"
)

# Enable CORS for Unity/WebView access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global state
manager: Optional[NPCManager] = None
relationship_tracker: Optional[RelationshipTracker] = None
quest_manager: Optional[QuestManager] = None
quest_extractor: Optional[QuestExtractor] = None
voice_system: Optional[VoiceSystem] = None
event_system: Optional[EventSystem] = None
conversation_manager: Optional[ConversationManager] = None
performance_manager: Optional[PerformanceManager] = None
dungeon_master: Optional[DungeonMaster] = None
dm_rule_engine: Optional[DmRuleEngine] = None
eavesdrop_manager: Optional[EavesdropManager] = None
CHARACTER_CARDS_DIR = Path("character_cards")


# Request/Response Models
class GenerateRequest(BaseModel):
    npc_name: str
    player_input: str
    player_id: str = "player"
    game_state: Optional[Dict[str, Any]] = None
    player_inventory: Optional[Dict[str, int]] = None


class GenerateResponse(BaseModel):
    response: str
    npc_name: str
    tokens: int
    elapsed_time: float
    tokens_per_second: float
    quest_available: Optional[Dict[str, Any]] = None
    quest_accepted: Optional[str] = None
    quest_completed: Optional[Dict[str, Any]] = None


class LoadCharacterRequest(BaseModel):
    character_path: str
    player_id: str = "player"


class UpdateRelationshipRequest(BaseModel):
    npc_name: str
    player_id: str
    change: int
    reason: Optional[str] = None


class SetFactionRequest(BaseModel):
    npc_name: str
    faction: str


class SaveHistoryRequest(BaseModel):
    npc_name: str
    player_id: str
    directory: str = "conversation_history"


class LoadHistoryRequest(BaseModel):
    npc_name: str
    player_id: str
    directory: str = "conversation_history"


class CharacterInfo(BaseModel):
    name: str
    description: str
    personality: str
    speaking_style: str


class RelationshipInfo(BaseModel):
    npc_name: str
    player_id: str
    score: int
    level: str
    history_count: int


class StatusResponse(BaseModel):
    status: str
    version: str
    loaded_npcs: List[str]
    model: str
    backend: str
    backend_connected: bool


# Startup/Shutdown
@app.on_event("startup")
async def startup_event():
    """Initialize the NPC system on server start."""
    global manager, relationship_tracker, quest_manager, quest_extractor, voice_system, event_system, performance_manager, dungeon_master, dm_rule_engine, conversation_manager, eavesdrop_manager
    
    # Initialize performance manager FIRST (for connection pooling)
    performance_manager = PerformanceManager(
        cache_size=2000,
        cache_ttl=7200,  # 2 hours
        pool_size=20,    # Connection pool size
        max_concurrent=10,
        ollama_url="http://localhost:11434"
    )
    print("✅ Performance manager initialized (caching + connection pooling)")
    
    # Try to load cached responses
    try:
        performance_manager.load_state()
        print("✅ Loaded cached responses from disk")
    except Exception:
        pass
    
    # Initialize relationship tracker
    relationship_tracker = RelationshipTracker(player_id="player")
    
    # Determine backend from environment
    backend = os.getenv("LLM_BACKEND", "ollama")
    groq_api_key = os.getenv("GROQ_API_KEY")
    
    if backend == "groq":
        model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    else:
        model = os.getenv("OLLAMA_MODEL", "llama3.2:1b")
    
    # Initialize NPC manager
    manager = NPCManager(
        model=model,
        relationship_tracker=relationship_tracker,
        backend=backend,
        api_key=groq_api_key,
    )
    print(f"✅ NPC manager initialized (backend: {backend}, model: {model})")

    # Load the bundled character cards up front.
    #
    # Without this the NPC pool stays empty, every conversation endpoint answers
    # "NPC 'X' not loaded", and the /eavesdrop map dies at the very last step:
    # you can walk to a spot, be told you may listen, press Listen, and get a 400.
    # Loading here also means the API is usable the moment uvicorn is up, with no
    # setup call first.
    loaded_cards = 0
    if CHARACTER_CARDS_DIR.exists():
        for card_file in sorted(CHARACTER_CARDS_DIR.glob("*.json")):
            try:
                manager.load_character(str(card_file))
                loaded_cards += 1
            except Exception as exc:
                print(f"⚠️  Skipped {card_file.name}: {exc}")
    print(f"✅ Loaded {loaded_cards} character card(s) from '{CHARACTER_CARDS_DIR}'")

    # Initialize quest manager
    quest_generator = QuestGenerator(relationship_tracker=relationship_tracker)
    quest_manager = QuestManager(
        quest_generator=quest_generator,
        relationship_tracker=relationship_tracker,
        save_dir="saves"
    )
    
    # Initialize quest extractor for natural quest detection
    quest_extractor = QuestExtractor(
        model=model,
        backend=backend,
        api_key=groq_api_key,
    )
    print("✅ Quest extractor initialized")
    
    # Initialize voice system
    voice_system = VoiceSystem(
        cache_dir="voice_cache",
        output_dir="voice_output",
        default_provider=VoiceProvider.EDGE_TTS
    )
    
    # Initialize multiplayer event system
    event_system = EventSystem()
    await event_system.start()
    print("✅ Event system started")

    # Initialize NPC-to-NPC conversation manager
    conversation_manager = ConversationManager(
        npc_manager=manager,
        relationship_tracker=relationship_tracker
    )
    print("✅ Conversation manager initialized")

    # Initialize eavesdrop manager (see eavesdrop.py)
    eavesdrop_manager = EavesdropManager(conversation_manager)
    if EAVESDROP_SPOTS_FILE.exists():
        loaded = eavesdrop_manager.load_spots(EAVESDROP_SPOTS_FILE)
        print(f"✅ Eavesdrop manager initialized ({loaded} spots)")
    else:
        print("⚠️  eavesdrop_spots.json not found — no eavesdrop spots registered")

    # Initialize Dungeon Master
    dm_config = DungeonMasterConfig.from_env()
    if dm_config.enabled:
        dm_rule_engine = DmRuleEngine(
            rules_dir=dm_config.rules_dir,
            max_active_rules=dm_config.max_active_rules,
            max_pending_rules=dm_config.max_pending_rules,
            min_confidence_auto_activate=dm_config.min_confidence_auto_activate,
        )
        dm_rule_engine.load_rules()

        dm_provider = create_provider(
            backend=dm_config.backend,
            api_key=dm_config.api_key or groq_api_key,
            ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
        )

        dungeon_master = DungeonMaster(
            state_manager=event_system.state_manager if event_system else None,
            event_callback=event_system.state_manager.event_callback if event_system else None,
            rule_engine=dm_rule_engine,
            config=dm_config,
            llm_provider=dm_provider,
        )
        dungeon_master.load_state()

        event_system.state_manager.on_any_event(dungeon_master.handle_event)

        _register_dm_directive_consumers()

        await dungeon_master.start()
        print(f"✅ Dungeon Master initialized (model: {dm_config.model})")
    else:
        print("ℹ️  Dungeon Master disabled (DM_ENABLED=false)")
    
    # Check backend connection
    if manager._provider.check_connection():
        print(f"✅ Connected to {backend} backend")
    else:
        if backend == "ollama":
            print(f"⚠️  Warning: Could not connect to Ollama. Make sure it's running.")
        else:
            print(f"⚠️  Warning: Could not verify {backend} connection. Check your API key.")


@app.on_event("shutdown")
async def shutdown_event():
    """Save data on server shutdown."""
    global relationship_tracker, event_system, performance_manager, dungeon_master, dm_rule_engine
    
    if relationship_tracker:
        relationship_tracker.save()
        print("💾 Saved relationship data")

    if dungeon_master:
        await dungeon_master.stop()
        print("💾 Saved Dungeon Master state")
    
    if performance_manager:
        performance_manager.save_state()
        print("💾 Saved performance cache")
    
    if event_system:
        event_system.state_manager.save_state()
        await event_system.stop()
        print("💾 Saved state and stopped event system")
    

# Health Check
@app.get("/api/status", response_model=StatusResponse)
async def get_status():
    """Get API server status."""
    backend_connected = False
    try:
        if manager and manager._provider:
            backend_connected = manager._provider.check_connection()
    except Exception:
        pass
    
    return StatusResponse(
        status="running",
        version="1.2.0",
        loaded_npcs=list(manager.npcs.keys()) if manager else [],
        model=manager.model if manager else "unknown",
        backend=manager.backend if manager else "unknown",
        backend_connected=backend_connected
    )


# Dialogue Generation
@app.post("/api/dialogue/generate", response_model=GenerateResponse)
async def generate_dialogue(request: GenerateRequest):
    """Generate an NPC response to player input (with caching + quest detection)."""
    if not manager:
        raise HTTPException(status_code=503, detail="Server not initialized")
    
    import time
    
    # --- Quest acceptance/rejection detection ---
    quest_accepted_id = None
    quest_completed_data = None
    
    if quest_extractor and quest_manager:
        pending = quest_extractor.get_pending_quest(request.npc_name)
        if pending:
            action = quest_extractor.detect_acceptance(
                player_input=request.player_input,
                npc_name=request.npc_name,
                quest=pending,
            )
            if action == "accept":
                accepted = quest_manager.accept_quest(
                    pending.id,
                    player_inventory=request.player_inventory,
                )
                if accepted:
                    quest_accepted_id = pending.id
            elif action == "reject":
                pending.abandon()
                quest_extractor.clear_pending(request.npc_name)
    
    # --- Build game_state with active quest context ---
    game_state = dict(request.game_state) if request.game_state else {}
    
    if request.player_inventory:
        game_state["player_inventory"] = request.player_inventory
    
    if quest_manager:
        active_quests = quest_manager.get_active_quests()
        npc_quests = [q for q in active_quests if q.quest_giver == request.npc_name]
        if npc_quests:
            game_state["active_quests"] = [
                {
                    "name": q.name,
                    "quest_type": q.quest_type.value,
                    "progress": q.progress_percent(),
                    "is_complete": q.is_complete(),
                    "objectives": [o.to_dict() for o in q.objectives],
                }
                for q in npc_quests
            ]
        
        pending = quest_extractor.get_pending_quest(request.npc_name) if quest_extractor else None
        if pending:
            game_state["pending_quest"] = {
                "name": pending.name,
                "description": pending.description,
            }
    
    # Check cache FIRST for instant response
    if performance_manager:
        cached = performance_manager.get_cached_response(
            request.npc_name,
            request.player_input,
            request.game_state
        )
        if cached:
            performance_manager.record_request(0, 0, from_cache=True)
            return GenerateResponse(
                response=cached,
                npc_name=request.npc_name,
                tokens=0,
                elapsed_time=0.001,
                tokens_per_second=0,
                quest_available=None,
                quest_accepted=quest_accepted_id,
                quest_completed=None,
            )
    
    # Load NPC if not already loaded
    if request.npc_name not in manager.npcs:
        char_path = CHARACTER_CARDS_DIR / f"{request.npc_name.lower()}.json"
        if not char_path.exists():
            raise HTTPException(
                status_code=404, 
                detail=f"Character '{request.npc_name}' not found"
            )
        
        try:
            manager.load_character(
                str(char_path),
                player_id=request.player_id
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to load character: {str(e)}"
            )
    
    # Get the NPC
    npc = manager.npcs[request.npc_name]
    
    # --- Inventory validation ---
    player_inventory = game_state.get("player_inventory")
    if player_inventory:
        is_valid, missing = validate_inventory_for_input(
            request.player_input, player_inventory
        )
        if not is_valid:
            items_str = ", ".join(f"'{i}'" for i in missing)
            game_state["_inventory_override"] = (
                f"The player claims to offer {items_str} but they do NOT have "
                f"it in their inventory (their inventory: {list(player_inventory.keys())}). "
                f"Refuse the offer in character and tell them they don't have that item."
            )
    
    # Generate response
    start_time = time.time()
    
    try:
        response = npc.generate_response(
            request.player_input,
            game_state=game_state if game_state else None,
            show_thinking=False
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to generate response: {str(e)}"
        )
    
    elapsed = time.time() - start_time
    
    # Estimate tokens (rough approximation)
    tokens = int(len(response.split()) * 1.3)
    tps = tokens / elapsed if elapsed > 0 else 0
    
    # Cache the response for future requests
    if performance_manager:
        performance_manager.cache_response(
            request.npc_name,
            request.player_input,
            response,
            request.game_state,
            metadata={"tokens": tokens, "model": npc.model}
        )
        performance_manager.record_request(elapsed, tokens, from_cache=False)
    
    # --- Quest extraction from NPC response ---
    quest_available_data = None
    if quest_extractor and quest_manager:
        active = list(quest_manager.active_quests.values())
        extracted = quest_extractor.extract_quest(
            npc_name=request.npc_name,
            npc_response=response,
            active_quests=active,
        )
        if extracted:
            quest_manager.register_quest(extracted)
            quest_available_data = extracted.to_dict()
    
    # --- Auto-update TALK_TO_NPC objectives ---
    if quest_manager:
        quest_manager.update_progress(ObjectiveType.TALK_TO_NPC, request.npc_name, 1)
        
        # Check if any active quests just completed
        for quest in list(quest_manager.active_quests.values()):
            if quest.quest_giver == request.npc_name and quest.is_complete():
                rewards = quest_manager.complete_quest(quest.id)
                if rewards and quest.id == quest_accepted_id:
                    quest_completed_data = rewards
    
    return GenerateResponse(
        response=response,
        npc_name=request.npc_name,
        tokens=tokens,
        elapsed_time=round(elapsed, 2),
        tokens_per_second=round(tps, 1),
        quest_available=quest_available_data,
        quest_accepted=quest_accepted_id,
        quest_completed=quest_completed_data,
    )


@app.post("/api/dialogue/generate/stream")
async def generate_dialogue_stream(request: GenerateRequest):
    """Generate NPC response with streaming (SSE)."""
    if not manager:
        raise HTTPException(status_code=503, detail="Server not initialized")
    
    # Load NPC if not already loaded
    if request.npc_name not in manager.npcs:
        char_path = CHARACTER_CARDS_DIR / f"{request.npc_name.lower()}.json"
        if not char_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Character '{request.npc_name}' not found"
            )
        manager.load_character(str(char_path), player_id=request.player_id)
    
    npc = manager.npcs[request.npc_name]
    
    async def event_generator():
        """Generate SSE events for streaming response."""
        try:
            messages = npc._format_messages(request.player_input, request.game_state)
            full_response = ""
            
            for token in npc.provider.generate_stream(
                messages=messages,
                model=npc.model,
                temperature=npc.temperature,
                max_tokens=npc.max_tokens,
            ):
                full_response += token
                yield f"data: {json.dumps({'token': token})}\n\n"
            
            # Update history
            npc.history.append({"role": "user", "content": request.player_input})
            npc.history.append({"role": "assistant", "content": full_response})
            if len(npc.history) > npc.max_history:
                npc.history = npc.history[-npc.max_history:]
            
            yield f"data: {json.dumps({'done': True, 'full_response': full_response})}\n\n"
                            
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )


# Character Management
@app.get("/api/characters")
async def list_characters():
    """List all loaded characters and available character cards."""
    loaded = list(manager.npcs.keys()) if manager else []
    
    # Find available character cards
    available = []
    if CHARACTER_CARDS_DIR.exists():
        for card_file in CHARACTER_CARDS_DIR.glob("*.json"):
            try:
                with open(card_file, 'r') as f:
                    card = json.load(f)
                available.append({
                    "name": card.get("name", card_file.stem),
                    "file": str(card_file),
                    "description": card.get("description", "")[:100] + "..."
                })
            except Exception:
                pass
    
    return {
        "loaded": loaded,
        "available": available
    }


@app.post("/api/characters/load")
async def load_character(request: LoadCharacterRequest):
    """Load a character from a file."""
    if not manager:
        raise HTTPException(status_code=503, detail="Server not initialized")
    
    # Resolve path
    char_path = Path(request.character_path)
    if not char_path.is_absolute():
        char_path = CHARACTER_CARDS_DIR / request.character_path
    
    if not char_path.exists():
        # Try adding .json extension
        char_path = Path(str(char_path) + ".json")
    
    if not char_path.exists():
        raise HTTPException(status_code=404, detail=f"Character file not found: {request.character_path}")
    
    try:
        npc = manager.load_character(
            str(char_path),
            player_id=request.player_id
        )
        return {
            "status": "loaded",
            "character_name": npc.character_name,
            "description": npc.character_card.get("description", "")[:100]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load character: {str(e)}")


@app.get("/api/characters/{npc_name}")
async def get_character_info(npc_name: str):
    """Get information about a loaded character."""
    if not manager or npc_name not in manager.npcs:
        raise HTTPException(status_code=404, detail=f"Character '{npc_name}' not loaded")
    
    npc = manager.npcs[npc_name]
    
    return CharacterInfo(
        name=npc.character_name,
        description=npc.character_card.get("description", ""),
        personality=npc.character_card.get("personality", ""),
        speaking_style=npc.character_card.get("speaking_style", "")
    )


@app.delete("/api/characters/{npc_name}")
async def unload_character(npc_name: str):
    """Unload a character from memory."""
    if not manager or npc_name not in manager.npcs:
        raise HTTPException(status_code=404, detail=f"Character '{npc_name}' not loaded")
    
    # Save history before unloading
    npc = manager.npcs[npc_name]
    npc.save_history()
    
    del manager.npcs[npc_name]
    
    if manager.active_npc == npc:
        manager.active_npc = None
    
    return {"status": "unloaded", "npc_name": npc_name}


# Relationships
@app.get("/api/relationships/{npc_name}/{player_id}", response_model=RelationshipInfo)
async def get_relationship(npc_name: str, player_id: str):
    """Get relationship between NPC and player."""
    if not relationship_tracker:
        raise HTTPException(status_code=503, detail="Relationship system not initialized")
    
    score = relationship_tracker.get_relationship(npc_name, player_id)
    level = relationship_tracker.get_level_name(npc_name, player_id)
    history = relationship_tracker.get_history(npc_name, player_id)
    
    return RelationshipInfo(
        npc_name=npc_name,
        player_id=player_id,
        score=score,
        level=level,
        history_count=len(history)
    )


@app.post("/api/relationships/update")
async def update_relationship(request: UpdateRelationshipRequest):
    """Update relationship between NPC and player."""
    if not relationship_tracker:
        raise HTTPException(status_code=503, detail="Relationship system not initialized")
    
    new_score = relationship_tracker.update(
        request.npc_name,
        request.player_id,
        request.change,
        request.reason
    )
    
    level = relationship_tracker.get_level_name(request.npc_name, request.player_id)
    
    return {
        "status": "updated",
        "npc_name": request.npc_name,
        "player_id": request.player_id,
        "new_score": new_score,
        "level": level
    }


@app.get("/api/relationships/player/{player_id}")
async def get_player_relationships(player_id: str):
    """Get all relationships for a player."""
    if not relationship_tracker:
        raise HTTPException(status_code=503, detail="Relationship system not initialized")
    
    return relationship_tracker.get_all_relationships(player_id)


@app.post("/api/factions/set")
async def set_npc_faction(request: SetFactionRequest):
    """Assign an NPC to a faction."""
    if not relationship_tracker:
        raise HTTPException(status_code=503, detail="Relationship system not initialized")
    
    relationship_tracker.set_faction(request.npc_name, request.faction)
    
    return {
        "status": "set",
        "npc_name": request.npc_name,
        "faction": request.faction
    }


@app.get("/api/factions/{faction}/reputation/{player_id}")
async def get_faction_reputation(faction: str, player_id: str):
    """Get average reputation with a faction."""
    if not relationship_tracker:
        raise HTTPException(status_code=503, detail="Relationship system not initialized")
    
    score = relationship_tracker.get_faction_reputation(player_id, faction)
    
    return {
        "faction": faction,
        "player_id": player_id,
        "average_reputation": score
    }


# Conversation History
@app.post("/api/history/save")
async def save_conversation_history(request: SaveHistoryRequest):
    """Save conversation history for an NPC-player pair."""
    if not manager or request.npc_name not in manager.npcs:
        raise HTTPException(status_code=404, detail=f"Character '{request.npc_name}' not loaded")
    
    npc = manager.npcs[request.npc_name]
    npc.save_history(request.directory)
    
    return {
        "status": "saved",
        "npc_name": request.npc_name,
        "player_id": request.player_id,
        "turns": len(npc.history) // 2
    }


@app.post("/api/history/load")
async def load_conversation_history(request: LoadHistoryRequest):
    """Load conversation history for an NPC-player pair."""
    if not manager or request.npc_name not in manager.npcs:
        raise HTTPException(status_code=404, detail=f"Character '{request.npc_name}' not loaded")
    
    npc = manager.npcs[request.npc_name]
    npc.load_history(request.directory)
    
    return {
        "status": "loaded",
        "npc_name": request.npc_name,
        "player_id": request.player_id,
        "turns": len(npc.history) // 2
    }


@app.delete("/api/history/{npc_name}/{player_id}")
async def clear_conversation_history(npc_name: str, player_id: str):
    """Clear conversation history for an NPC-player pair."""
    if not manager or npc_name not in manager.npcs:
        raise HTTPException(status_code=404, detail=f"Character '{npc_name}' not loaded")
    
    npc = manager.npcs[npc_name]
    npc.reset_history()
    
    return {
        "status": "cleared",
        "npc_name": npc_name,
        "player_id": player_id
    }


# Game State Integration
@app.post("/api/dialogue/generate-with-state")
async def generate_with_game_state(
    npc_name: str,
    player_input: str,
    player_id: str = "player",
    location: Optional[str] = None,
    time_of_day: Optional[str] = None,
    current_quest: Optional[str] = None,
    player_health: Optional[int] = None,
    player_gold: Optional[int] = None,
    player_inventory: Optional[Dict[str, int]] = None
):
    """Generate NPC response with structured game state."""
    game_state = {}
    
    if location:
        game_state["location"] = location
    if time_of_day:
        game_state["time"] = time_of_day
    if current_quest:
        game_state["current_quest"] = current_quest
    if player_health is not None:
        game_state["player_health"] = player_health
    if player_gold is not None:
        game_state["player_gold"] = player_gold
    if player_inventory:
        game_state["player_inventory"] = player_inventory
    
    request = GenerateRequest(
        npc_name=npc_name,
        player_input=player_input,
        player_id=player_id,
        game_state=game_state if game_state else None
    )
    
    return await generate_dialogue(request)


# Utility Endpoints
@app.post("/api/save-all")
async def save_all_data():
    """Save all conversation history and relationship data."""
    if manager:
        for npc in manager.npcs.values():
            npc.save_history()
    
    if relationship_tracker:
        relationship_tracker.save()
    
    return {"status": "saved", "message": "All data saved successfully"}


@app.post("/api/export/{player_id}")
async def export_player_data(player_id: str):
    """Export all player data for game save."""
    if not relationship_tracker:
        raise HTTPException(status_code=503, detail="Relationship system not initialized")
    
    return relationship_tracker.export_player_data(player_id)


@app.post("/api/import")
async def import_player_data(data: Dict[str, Any]):
    """Import player data from game save."""
    if not relationship_tracker:
        raise HTTPException(status_code=503, detail="Relationship system not initialized")
    
    try:
        relationship_tracker.import_player_data(data)
        return {"status": "imported", "player_id": data.get("player_id")}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Import failed: {str(e)}")


# ============================================
# QUEST SYSTEM ENDPOINTS
# ============================================

class GenerateQuestsRequest(BaseModel):
    npc_name: str
    npc_archetype: Optional[str] = None
    npc_location: Optional[str] = None
    player_level: int = 1
    count: int = 2


class QuestProgressRequest(BaseModel):
    objective_type: str
    target: str
    amount: int = 1


class QuestInfo(BaseModel):
    id: str
    name: str
    description: str
    quest_giver: str
    quest_type: str
    status: str
    difficulty: int
    progress: float
    objectives: List[Dict[str, Any]]
    time_remaining: Optional[int]


@app.get("/api/quests/npc/{npc_name}")
async def get_npc_quests(npc_name: str, player_level: int = 1):
    """Get available quests from an NPC."""
    if not quest_manager:
        raise HTTPException(status_code=503, detail="Quest system not initialized")
    
    player_state = {"level": player_level}
    quests = quest_manager.get_available_quests(npc_name, player_state)
    
    return {
        "npc_name": npc_name,
        "quests": [q.to_dict() for q in quests]
    }


@app.post("/api/quests/generate")
async def generate_quests(request: GenerateQuestsRequest):
    """Generate new quests for an NPC."""
    if not quest_manager:
        raise HTTPException(status_code=503, detail="Quest system not initialized")
    
    npc_data = {}
    if request.npc_archetype:
        npc_data["archetype"] = request.npc_archetype
    if request.npc_location:
        npc_data["location"] = request.npc_location
    
    player_state = {"level": request.player_level}
    
    quests = quest_manager.generate_quests_for_npc(
        npc_name=request.npc_name,
        npc_data=npc_data,
        player_state=player_state,
        count=request.count
    )
    
    return {
        "status": "generated",
        "npc_name": request.npc_name,
        "quests": [q.to_dict() for q in quests]
    }


@app.post("/api/quests/{quest_id}/accept")
async def accept_quest(quest_id: str):
    """Accept a quest."""
    if not quest_manager:
        raise HTTPException(status_code=503, detail="Quest system not initialized")
    
    quest = quest_manager.accept_quest(quest_id)
    
    if not quest:
        raise HTTPException(status_code=404, detail=f"Quest '{quest_id}' not found or not available")
    
    return {
        "status": "accepted",
        "quest": quest.to_dict()
    }


@app.post("/api/quests/{quest_id}/abandon")
async def abandon_quest(quest_id: str):
    """Abandon an active quest."""
    if not quest_manager:
        raise HTTPException(status_code=503, detail="Quest system not initialized")
    
    success = quest_manager.abandon_quest(quest_id)
    
    if not success:
        raise HTTPException(status_code=404, detail=f"Quest '{quest_id}' not found or not active")
    
    return {"status": "abandoned", "quest_id": quest_id}


@app.get("/api/quests/active")
async def get_active_quests():
    """Get all active quests for the player."""
    if not quest_manager:
        raise HTTPException(status_code=503, detail="Quest system not initialized")
    
    quests = quest_manager.get_active_quests()
    
    return {
        "count": len(quests),
        "quests": [q.to_dict() for q in quests]
    }


@app.get("/api/quests/{quest_id}")
async def get_quest(quest_id: str):
    """Get details of a specific quest."""
    if not quest_manager:
        raise HTTPException(status_code=503, detail="Quest system not initialized")
    
    quest = quest_manager.get_quest(quest_id)
    
    if not quest:
        raise HTTPException(status_code=404, detail=f"Quest '{quest_id}' not found")
    
    return quest.to_dict()


@app.post("/api/quests/{quest_id}/progress")
async def update_quest_progress(quest_id: str, request: QuestProgressRequest):
    """Update progress on a quest objective."""
    if not quest_manager:
        raise HTTPException(status_code=503, detail="Quest system not initialized")
    
    try:
        obj_type = ObjectiveType(request.objective_type)
    except ValueError:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid objective type: {request.objective_type}"
        )
    
    # Update progress on all matching active quests
    updates = quest_manager.update_progress(obj_type, request.target, request.amount)
    
    if quest_id not in updates:
        raise HTTPException(
            status_code=404, 
            detail=f"Quest '{quest_id}' not found or objective doesn't match"
        )
    
    quest = quest_manager.get_quest(quest_id)
    
    return {
        "status": "updated",
        "quest_id": quest_id,
        "progress": updates[quest_id],
        "is_complete": quest.is_complete() if quest else False
    }


@app.post("/api/quests/{quest_id}/complete")
async def complete_quest(quest_id: str):
    """Complete a quest and claim rewards."""
    if not quest_manager:
        raise HTTPException(status_code=503, detail="Quest system not initialized")
    
    rewards = quest_manager.complete_quest(quest_id)
    
    if not rewards:
        quest = quest_manager.get_quest(quest_id)
        if quest and not quest.is_complete():
            raise HTTPException(
                status_code=400, 
                detail="Quest objectives not complete"
            )
        raise HTTPException(status_code=404, detail=f"Quest '{quest_id}' not found or not active")
    
    return {
        "status": "completed",
        "quest_id": quest_id,
        "rewards": rewards
    }


@app.get("/api/quests/summary")
async def get_quest_summary():
    """Get summary of quest state."""
    if not quest_manager:
        raise HTTPException(status_code=503, detail="Quest system not initialized")
    
    return quest_manager.get_summary()


@app.post("/api/quests/save")
async def save_quest_data(player_id: str = "player"):
    """Save quest state to file."""
    if not quest_manager:
        raise HTTPException(status_code=503, detail="Quest system not initialized")
    
    quest_manager.save(player_id)
    
    return {"status": "saved", "player_id": player_id}


@app.post("/api/quests/load")
async def load_quest_data(player_id: str = "player"):
    """Load quest state from file."""
    if not quest_manager:
        raise HTTPException(status_code=503, detail="Quest system not initialized")
    
    quest_manager.load(player_id)
    
    return {
        "status": "loaded",
        "player_id": player_id,
        "summary": quest_manager.get_summary()
    }


# ============================================
# GAME EVENT + QUEST-AWARE DIALOGUE ENDPOINTS
# ============================================

class GameEventRequest(BaseModel):
    event_type: str
    target: str
    amount: int = 1
    location: Optional[str] = None
    player_id: str = "player"
    player_gold: Optional[int] = None
    player_health: Optional[int] = None
    player_inventory: Optional[Dict[str, int]] = None


EVENT_TYPE_ALIASES = {
    "collect": ObjectiveType.COLLECT_ITEM,
    "collect_item": ObjectiveType.COLLECT_ITEM,
    "gather": ObjectiveType.COLLECT_ITEM,
    "pickup": ObjectiveType.COLLECT_ITEM,
    "kill": ObjectiveType.KILL_TARGET,
    "kill_target": ObjectiveType.KILL_TARGET,
    "defeat": ObjectiveType.KILL_TARGET,
    "reach": ObjectiveType.REACH_LOCATION,
    "reach_location": ObjectiveType.REACH_LOCATION,
    "travel": ObjectiveType.REACH_LOCATION,
    "arrive": ObjectiveType.REACH_LOCATION,
    "talk": ObjectiveType.TALK_TO_NPC,
    "talk_to_npc": ObjectiveType.TALK_TO_NPC,
    "speak": ObjectiveType.TALK_TO_NPC,
    "deliver": ObjectiveType.DELIVER_ITEM,
    "deliver_item": ObjectiveType.DELIVER_ITEM,
    "give": ObjectiveType.DELIVER_ITEM,
    "escort": ObjectiveType.ESCORT_NPC,
    "escort_npc": ObjectiveType.ESCORT_NPC,
    "boss": ObjectiveType.DEFEAT_BOSS,
    "defeat_boss": ObjectiveType.DEFEAT_BOSS,
}


@app.post("/api/game/event")
async def handle_game_event(request: GameEventRequest):
    """
    Handle a gameplay event and update quest progress.
    
    Called by the game engine when the player performs actions like
    collecting items, killing enemies, reaching locations, etc.
    This is the canonical way to update quest objectives — NOT dialogue.
    """
    if not quest_manager:
        raise HTTPException(status_code=503, detail="Quest system not initialized")
    
    obj_type = EVENT_TYPE_ALIASES.get(request.event_type.lower())
    if not obj_type:
        valid = ", ".join(sorted(set(EVENT_TYPE_ALIASES.keys())))
        raise HTTPException(
            status_code=400,
            detail=f"Unknown event type '{request.event_type}'. Valid types: {valid}"
        )
    
    updates = quest_manager.update_progress(obj_type, request.target, request.amount)
    
    completions = []
    for quest_id in updates:
        quest = quest_manager.get_quest(quest_id)
        if quest and quest.is_complete():
            completions.append({
                "quest_id": quest_id,
                "name": quest.name,
                "rewards": quest.rewards.to_dict(),
            })
    
    return {
        "status": "processed",
        "event_type": request.event_type,
        "target": request.target,
        "amount": request.amount,
        "quests_updated": len(updates),
        "progress_updates": {qid: f"{pct:.0f}%" for qid, pct in updates.items()},
        "quests_ready_to_complete": completions,
    }


@app.post("/api/dialogue/generate-with-quests", response_model=GenerateResponse)
async def generate_dialogue_with_quests(request: GenerateRequest):
    """Generate NPC response with automatic quest context injection."""
    game_state = dict(request.game_state) if request.game_state else {}
    
    if quest_manager:
        active_quests = quest_manager.get_active_quests()
        npc_quests = [q for q in active_quests if q.quest_giver == request.npc_name]
        if npc_quests:
            game_state["active_quests"] = [
                {
                    "name": q.name,
                    "quest_type": q.quest_type.value,
                    "progress": q.progress_percent(),
                    "is_complete": q.is_complete(),
                    "objectives": [o.to_dict() for o in q.objectives],
                }
                for q in npc_quests
            ]
        
        pending = quest_extractor.get_pending_quest(request.npc_name) if quest_extractor else None
        if pending:
            game_state["pending_quest"] = {
                "name": pending.name,
                "description": pending.description,
            }
    
    request.game_state = game_state if game_state else None
    return await generate_dialogue(request)

class SynthesizeRequest(BaseModel):
    text: str
    npc_name: Optional[str] = None
    voice_id: Optional[str] = None
    provider: Optional[str] = None
    speed: float = 1.0
    use_cache: bool = True


class RegisterVoiceRequest(BaseModel):
    npc_name: str
    voice_id: str
    provider: str = "edge_tts"
    speed: float = 1.0
    pitch: float = 1.0


@app.post("/api/voice/synthesize")
async def synthesize_voice(request: SynthesizeRequest):
    """Synthesize speech from text."""
    if not voice_system:
        raise HTTPException(status_code=503, detail="Voice system not initialized")
    
    # Build voice config
    voice_config = None
    if request.voice_id:
        provider = VoiceProvider(request.provider) if request.provider else None
        voice_config = VoiceConfig(
            name="Custom",
            provider=provider or voice_system.default_provider,
            voice_id=request.voice_id,
            speed=request.speed,
        )
    
    # Determine provider
    provider = None
    if request.provider:
        try:
            provider = VoiceProvider(request.provider)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid provider: {request.provider}")
    
    # Synthesize
    result = await voice_system.synthesize(
        text=request.text,
        npc_name=request.npc_name,
        voice_config=voice_config,
        provider=provider,
        use_cache=request.use_cache,
    )
    
    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)
    
    return {
        "success": result.success,
        "audio_path": result.audio_path,
        "duration_seconds": result.duration_seconds,
        "provider": result.provider.value,
        "voice_id": result.voice_id,
        "cached": result.cached,
    }


@app.get("/api/voice/synthesize/{npc_name}")
async def synthesize_npc_voice(npc_name: str, text: str):
    """Synthesize speech for a specific NPC using their registered voice."""
    if not voice_system:
        raise HTTPException(status_code=503, detail="Voice system not initialized")
    
    result = await voice_system.synthesize(
        text=text,
        npc_name=npc_name,
    )
    
    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)
    
    return {
        "success": result.success,
        "audio_path": result.audio_path,
        "duration_seconds": result.duration_seconds,
        "provider": result.provider.value,
        "voice_id": result.voice_id,
        "cached": result.cached,
    }


@app.post("/api/voice/register")
async def register_npc_voice(request: RegisterVoiceRequest):
    """Register a voice profile for an NPC."""
    if not voice_system:
        raise HTTPException(status_code=503, detail="Voice system not initialized")
    
    try:
        provider = VoiceProvider(request.provider)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid provider: {request.provider}")
    
    voice_config = VoiceConfig(
        name=request.npc_name,
        provider=provider,
        voice_id=request.voice_id,
        speed=request.speed,
        pitch=request.pitch,
    )
    
    voice_system.register_voice(request.npc_name, voice_config)
    
    return {
        "status": "registered",
        "npc_name": request.npc_name,
        "voice_id": request.voice_id,
        "provider": request.provider,
    }


@app.get("/api/voice/profiles")
async def get_voice_profiles():
    """Get all registered voice profiles."""
    if not voice_system:
        raise HTTPException(status_code=503, detail="Voice system not initialized")
    
    return {
        "profiles": {
            name: config.to_dict()
            for name, config in voice_system.voice_profiles.items()
        }
    }


@app.get("/api/voice/providers")
async def get_voice_providers():
    """Get available TTS providers."""
    if not voice_system:
        raise HTTPException(status_code=503, detail="Voice system not initialized")
    
    return {
        "providers": voice_system.get_available_providers()
    }


@app.get("/api/voice/voices")
async def get_available_voices(provider: Optional[str] = None):
    """Get available voices."""
    if not voice_system:
        raise HTTPException(status_code=503, detail="Voice system not initialized")
    
    prov = VoiceProvider(provider) if provider else None
    voices = voice_system.get_available_voices(prov)
    
    return {
        "count": len(voices),
        "voices": voices
    }


@app.get("/api/voice/audio/{filename}")
async def get_audio_file(filename: str):
    """Serve an audio file."""
    from fastapi.responses import FileResponse
    
    # Check cache directory
    cache_path = Path("voice_cache") / filename
    if cache_path.exists():
        return FileResponse(
            cache_path,
            media_type="audio/mpeg" if filename.endswith(".mp3") else "audio/wav"
        )
    
    # Check output directory
    output_path = Path("voice_output") / filename
    if output_path.exists():
        return FileResponse(
            output_path,
            media_type="audio/mpeg" if filename.endswith(".mp3") else "audio/wav"
        )
    
    raise HTTPException(status_code=404, detail=f"Audio file not found: {filename}")


@app.post("/api/voice/profiles/save")
async def save_voice_profiles():
    """Save voice profiles to file."""
    if not voice_system:
        raise HTTPException(status_code=503, detail="Voice system not initialized")
    
    voice_system.save_profiles("voice_profiles.json")
    
    return {"status": "saved"}


@app.post("/api/voice/profiles/load")
async def load_voice_profiles():
    """Load voice profiles from file."""
    if not voice_system:
        raise HTTPException(status_code=503, detail="Voice system not initialized")
    
    voice_system.load_profiles("voice_profiles.json")
    
    return {
        "status": "loaded",
        "profiles_count": len(voice_system.voice_profiles)
    }


# ============================================
# MULTIPLAYER WEBSOCKET ENDPOINTS
# ============================================

class ZoneChangeRequest(BaseModel):
    zone_id: str


class DialogueRequest(BaseModel):
    npc_id: str
    message: str


@app.websocket("/ws/{player_id}")
async def websocket_endpoint(websocket: WebSocket, player_id: str):
    """
    Main WebSocket endpoint for multiplayer connections.
    
    Protocol:
    - Client sends: {"action": "subscribe", "topic": "zone:village"}
    - Client sends: {"action": "pong"} (response to ping)
    - Client sends: {"action": "dialogue", "npc_id": "...", "message": "..."}
    - Server sends: {"type": "event", "event": {...}}
    - Server sends: {"type": "ping"} (connection health check)
    """
    if not event_system:
        await websocket.close(code=1011, reason="Event system not initialized")
        return
    
    await websocket.accept()
    
    subscriber_id = f"{player_id}_{int(time.time() * 1000)}"
    
    try:
        # Connect player
        subscriber = await event_system.connect_player(
            subscriber_id=subscriber_id,
            websocket=websocket,
            player_id=player_id,
        )
        
        # Main message loop
        while True:
            try:
                data = await websocket.receive_text()
                message = json.loads(data)
                
                action = message.get("action")
                
                if action == "subscribe":
                    topic = message.get("topic")
                    if topic:
                        await event_system.broadcaster.subscribe(subscriber_id, topic)
                
                elif action == "unsubscribe":
                    topic = message.get("topic")
                    if topic:
                        await event_system.broadcaster.unsubscribe(subscriber_id, topic)
                
                elif action == "pong":
                    await event_system.broadcaster.pong(subscriber_id)
                
                elif action == "zone_change":
                    new_zone = message.get("zone_id")
                    if new_zone:
                        await event_system.zone_change(player_id, subscriber_id, new_zone)
                
                elif action == "dialogue":
                    npc_id = message.get("npc_id")
                    text = message.get("message")
                    if npc_id and text:
                        # Add to dialogue history
                        await event_system.dialogue(
                            player_id=player_id,
                            npc_id=npc_id,
                            role="user",
                            content=text,
                        )
                        
                        # Generate NPC response (if NPC manager available)
                        if manager and npc_id in manager.npcs:
                            npc = manager.npcs[npc_id]
                            response = npc.generate_response(text)
                            
                            # Store and broadcast NPC response
                            await event_system.dialogue(
                                player_id=player_id,
                                npc_id=npc_id,
                                role="assistant",
                                content=response,
                            )
                            
                            # Send response directly to player
                            await websocket.send_json({
                                "type": "dialogue_response",
                                "npc_id": npc_id,
                                "response": response,
                            })
                
                elif action == "quest_accept":
                    quest_id = message.get("quest_id")
                    if quest_id:
                        event = event_system.state_manager.accept_quest(player_id, quest_id)
                        await event_system.broadcaster.broadcast(event)
                
                elif action == "quest_complete":
                    quest_id = message.get("quest_id")
                    shared = message.get("shared", True)
                    if quest_id:
                        event = event_system.state_manager.complete_quest(player_id, quest_id, shared)
                        await event_system.broadcaster.broadcast(event)
                
                elif action == "relationship_update":
                    npc_id = message.get("npc_id")
                    change = message.get("change", 0)
                    reason = message.get("reason", "")
                    if npc_id:
                        event = event_system.state_manager.update_relationship(player_id, npc_id, change, reason)
                        await event_system.broadcaster.broadcast(event)
                
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "Invalid JSON"})
            except Exception as e:
                await websocket.send_json({"type": "error", "message": str(e)})
    
    except WebSocketDisconnect:
        await event_system.disconnect_player(subscriber_id, player_id)
    except Exception as e:
        print(f"WebSocket error: {e}")
        await event_system.disconnect_player(subscriber_id, player_id)


@app.get("/api/multiplayer/status")
async def get_multiplayer_status():
    """Get multiplayer system status."""
    if not event_system:
        raise HTTPException(status_code=503, detail="Event system not initialized")
    
    return event_system.get_summary()


@app.get("/api/multiplayer/players")
async def get_connected_players():
    """Get list of connected players."""
    if not event_system:
        raise HTTPException(status_code=503, detail="Event system not initialized")
    
    players = event_system.state_manager.get_connected_players()
    
    return {
        "count": len(players),
        "players": [
            {
                "player_id": p.player_id,
                "zone": p.current_zone,
                "connected_at": p.connected_at,
            }
            for p in players
        ]
    }


@app.get("/api/multiplayer/players/{player_id}")
async def get_player_state(player_id: str):
    """Get state for a specific player."""
    if not event_system:
        raise HTTPException(status_code=503, detail="Event system not initialized")
    
    player = event_system.state_manager.get_player(player_id)
    
    if not player:
        raise HTTPException(status_code=404, detail=f"Player {player_id} not found")
    
    return player.to_dict()


@app.get("/api/multiplayer/world")
async def get_world_state():
    """Get shared world state."""
    if not event_system:
        raise HTTPException(status_code=503, detail="Event system not initialized")
    
    return event_system.state_manager.world.to_dict()


@app.get("/api/multiplayer/npcs")
async def get_npc_states():
    """Get all NPC world states."""
    if not event_system:
        raise HTTPException(status_code=503, detail="Event system not initialized")
    
    return {
        "npcs": [npc.to_dict() for npc in event_system.state_manager.npcs.values()]
    }


@app.get("/api/multiplayer/npcs/{npc_id}")
async def get_npc_state(npc_id: str):
    """Get specific NPC world state."""
    if not event_system:
        raise HTTPException(status_code=503, detail="Event system not initialized")
    
    npc = event_system.state_manager.get_npc(npc_id)
    
    if not npc:
        raise HTTPException(status_code=404, detail=f"NPC {npc_id} not found")
    
    return npc.to_dict()


@app.post("/api/multiplayer/npcs/{npc_id}/register")
async def register_npc(npc_id: str, name: str, zone: str = "default"):
    """Register an NPC in the world."""
    if not event_system:
        raise HTTPException(status_code=503, detail="Event system not initialized")
    
    npc = event_system.state_manager.register_npc(npc_id, name, zone)
    
    return {
        "status": "registered",
        "npc": npc.to_dict()
    }


@app.post("/api/multiplayer/save")
async def save_multiplayer_state():
    """Save multiplayer state to disk."""
    if not event_system:
        raise HTTPException(status_code=503, detail="Event system not initialized")
    
    event_system.state_manager.save_state()
    
    return {"status": "saved"}


@app.post("/api/multiplayer/load")
async def load_multiplayer_state():
    """Load multiplayer state from disk."""
    if not event_system:
        raise HTTPException(status_code=503, detail="Event system not initialized")
    
    loaded = event_system.state_manager.load_state()
    
    return {
        "status": "loaded" if loaded else "no_save_found",
        "summary": event_system.state_manager.get_summary()
    }


# ============================================
# NPC-TO-NPC CONVERSATION ENDPOINTS
# ============================================

class StartNPCConversationRequest(BaseModel):
    npc1_name: str
    npc2_name: str
    trigger: str = "forced"  # forced, proximity, scheduled, event
    max_turns: int = 6
    location: Optional[str] = None
    context: Optional[Dict[str, Any]] = None


class NPCConversationResponse(BaseModel):
    conversation_id: str
    npc1_name: str
    npc2_name: str
    state: str
    trigger: str
    location: Optional[str] = None


@app.post("/api/conversations/start", response_model=NPCConversationResponse)
async def start_npc_conversation(request: StartNPCConversationRequest):
    """Start a conversation between two NPCs."""
    if not conversation_manager:
        raise HTTPException(status_code=503, detail="Conversation manager not initialized")
    
    # Check NPCs are loaded
    if request.npc1_name not in manager.npcs:
        raise HTTPException(status_code=404, detail=f"NPC '{request.npc1_name}' not loaded")
    if request.npc2_name not in manager.npcs:
        raise HTTPException(status_code=404, detail=f"NPC '{request.npc2_name}' not loaded")
    
    try:
        trigger = ConversationTrigger(request.trigger)
    except ValueError:
        trigger = ConversationTrigger.FORCED
    
    try:
        conversation = conversation_manager.start_conversation(
            npc1_name=request.npc1_name,
            npc2_name=request.npc2_name,
            trigger=trigger,
            location=request.location,
            max_turns=request.max_turns,
            context=request.context
        )
        
        return NPCConversationResponse(
            conversation_id=conversation.conversation_id,
            npc1_name=conversation.npc1_name,
            npc2_name=conversation.npc2_name,
            state=conversation.state.value,
            trigger=conversation.trigger.value,
            location=conversation.location
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/conversations/{conversation_id}/turn")
async def run_conversation_turn(conversation_id: str):
    """Run a single turn in a conversation."""
    if not conversation_manager:
        raise HTTPException(status_code=503, detail="Conversation manager not initialized")
    
    exchange = await conversation_manager.run_conversation_turn(conversation_id)
    
    if not exchange:
        raise HTTPException(status_code=404, detail="Conversation not found or not active")
    
    conversation = conversation_manager.get_conversation(conversation_id)
    
    return {
        "exchange": {
            "speaker": exchange.speaker,
            "listener": exchange.listener,
            "message": exchange.message,
            "timestamp": exchange.timestamp,
            "topic": exchange.topic
        },
        "conversation_state": conversation.state.value if conversation else "completed",
        "current_turn": conversation.current_turn if conversation else 0
    }


@app.post("/api/conversations/{conversation_id}/run")
async def run_full_conversation(
    conversation_id: str,
    turn_delay: float = 2.0,
    background_tasks: BackgroundTasks = None
):
    """Run a complete conversation from start to finish."""
    if not conversation_manager:
        raise HTTPException(status_code=503, detail="Conversation manager not initialized")
    
    conversation = conversation_manager.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    # Run in background for async execution
    full_conv = await conversation_manager.run_full_conversation(
        npc1_name=conversation.npc1_name,
        npc2_name=conversation.npc2_name,
        trigger=conversation.trigger,
        max_turns=conversation.max_turns,
        turn_delay=turn_delay,
        location=conversation.location,
        context=conversation.metadata
    )
    
    return {
        "conversation_id": full_conv.conversation_id,
        "state": full_conv.state.value,
        "duration": full_conv.get_duration(),
        "exchange_count": len(full_conv.exchanges),
        "exchanges": [
            {
                "speaker": e.speaker,
                "listener": e.listener,
                "message": e.message,
                "topic": e.topic
            }
            for e in full_conv.exchanges
        ]
    }


@app.post("/api/conversations/{conversation_id}/end")
async def end_conversation(conversation_id: str):
    """End an active conversation."""
    if not conversation_manager:
        raise HTTPException(status_code=503, detail="Conversation manager not initialized")
    
    conversation = conversation_manager.end_conversation(conversation_id)
    
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    return {
        "status": "ended",
        "conversation_id": conversation.conversation_id,
        "duration": conversation.get_duration(),
        "exchanges": len(conversation.exchanges)
    }


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    """Get details of a specific conversation."""
    if not conversation_manager:
        raise HTTPException(status_code=503, detail="Conversation manager not initialized")
    
    conversation = conversation_manager.get_conversation(conversation_id)
    
    if not conversation:
        # Check history
        for c in conversation_manager.conversation_history:
            if c.conversation_id == conversation_id:
                return c.to_dict()
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    return conversation.to_dict()


@app.get("/api/conversations")
async def list_conversations(
    location: Optional[str] = None,
    include_history: bool = False
):
    """List all active conversations."""
    if not conversation_manager:
        raise HTTPException(status_code=503, detail="Conversation manager not initialized")
    
    active = conversation_manager.get_active_conversations(location)
    
    result = {
        "active_count": len(active),
        "active_conversations": [c.to_dict() for c in active]
    }
    
    if include_history:
        result["history_count"] = len(conversation_manager.conversation_history)
        result["history"] = [
            {
                "conversation_id": c.conversation_id,
                "npc1_name": c.npc1_name,
                "npc2_name": c.npc2_name,
                "duration": c.get_duration(),
                "exchanges": len(c.exchanges)
            }
            for c in conversation_manager.conversation_history[-20:]
        ]
    
    return result


@app.get("/api/conversations/npc/{npc_name}")
async def get_npc_conversation(npc_name: str):
    """Get the conversation an NPC is currently in."""
    if not conversation_manager:
        raise HTTPException(status_code=503, detail="Conversation manager not initialized")
    
    conversation = conversation_manager.get_npc_conversation(npc_name)
    
    if not conversation:
        return {"npc_name": npc_name, "in_conversation": False}
    
    return {
        "npc_name": npc_name,
        "in_conversation": True,
        "conversation": conversation.to_dict()
    }


@app.get("/api/conversations/location/{location}")
async def get_location_conversations(location: str):
    """Get all conversations at a specific location (for player overhearing)."""
    if not conversation_manager:
        raise HTTPException(status_code=503, detail="Conversation manager not initialized")
    
    conversations = conversation_manager.get_overhearable_conversations(location)
    
    return {
        "location": location,
        "conversation_count": len(conversations),
        "conversations": [c.to_dict() for c in conversations]
    }


@app.post("/api/conversations/proximity/check")
async def check_proximity_conversations(location: Optional[str] = None):
    """Check for and start proximity-triggered conversations."""
    if not conversation_manager:
        raise HTTPException(status_code=503, detail="Conversation manager not initialized")
    
    await conversation_manager.check_proximity_conversations(location)
    
    return {
        "status": "checked",
        "active_conversations": len(conversation_manager.active_conversations)
    }


@app.post("/api/conversations/location/{npc_name}")
async def update_npc_location(npc_name: str, location: str):
    """Update an NPC's location for proximity-based conversations."""
    if not conversation_manager:
        raise HTTPException(status_code=503, detail="Conversation manager not initialized")
    
    conversation_manager.update_npc_location(npc_name, location)
    
    return {
        "status": "updated",
        "npc_name": npc_name,
        "location": location
    }


@app.get("/api/conversations/topics")
async def list_conversation_topics():
    """List available conversation topics."""
    if not conversation_manager:
        raise HTTPException(status_code=503, detail="Conversation manager not initialized")
    
    topics = conversation_manager.engine.topic_registry.topics
    
    return {
        "topic_count": len(topics),
        "topics": [
            {
                "topic_id": t.topic_id,
                "name": t.name,
                "description": t.description,
                "min_relationship": t.min_relationship,
                "priority": t.priority
            }
            for t in topics.values()
        ]
    }


@app.post("/api/conversations/save")
async def save_npc_conversation_history():
    """Save conversation history to disk."""
    if not conversation_manager:
        raise HTTPException(status_code=503, detail="Conversation manager not initialized")
    
    conversation_manager.save_history("conversation_history/npc_conversations.json")
    
    return {
        "status": "saved",
        "history_count": len(conversation_manager.conversation_history)
    }


@app.post("/api/conversations/load")
async def load_npc_conversation_history():
    """Load conversation history from disk."""
    if not conversation_manager:
        raise HTTPException(status_code=503, detail="Conversation manager not initialized")
    
    conversation_manager.load_history("conversation_history/npc_conversations.json")
    
    return {
        "status": "loaded",
        "history_count": len(conversation_manager.conversation_history)
    }


# ============================================
# PERFORMANCE OPTIMIZATION ENDPOINTS
# ============================================

class BatchRequest(BaseModel):
    requests: List[Dict[str, Any]]  # List of {npc_name, player_input, player_id, game_state}


class PreGenerateRequest(BaseModel):
    npc_name: str
    npc_type: Optional[str] = None


@app.post("/api/performance/cache/clear")
async def clear_cache(npc_name: Optional[str] = None):
    """Clear the response cache."""
    if not performance_manager:
        raise HTTPException(status_code=503, detail="Performance manager not initialized")
    
    performance_manager.cache.invalidate(npc_name)
    
    return {
        "status": "cleared",
        "npc_name": npc_name or "all"
    }


@app.post("/api/performance/cache/save")
async def save_cache():
    """Save cache to disk."""
    if not performance_manager:
        raise HTTPException(status_code=503, detail="Performance manager not initialized")
    
    performance_manager.save_state()
    
    return {
        "status": "saved",
        "cache_size": len(performance_manager.cache._cache)
    }


@app.post("/api/performance/cache/load")
async def load_cache():
    """Load cache from disk."""
    if not performance_manager:
        raise HTTPException(status_code=503, detail="Performance manager not initialized")
    
    performance_manager.load_state()
    
    return {
        "status": "loaded",
        "cache_size": len(performance_manager.cache._cache)
    }


@app.post("/api/performance/batch")
async def process_batch_requests(request: BatchRequest):
    """Process multiple NPC requests in parallel."""
    if not performance_manager:
        raise HTTPException(status_code=503, detail="Performance manager not initialized")
    
    if not manager:
        raise HTTPException(status_code=503, detail="NPC manager not initialized")
    
    async def process_single(req: Dict) -> Dict:
        """Process a single request."""
        npc_name = req.get("npc_name")
        player_input = req.get("player_input")
        player_id = req.get("player_id", "player")
        game_state = req.get("game_state")
        
        # Check cache first
        cached = performance_manager.get_cached_response(npc_name, player_input, game_state)
        if cached:
            return {
                "npc_name": npc_name,
                "response": cached,
                "from_cache": True
            }
        
        # Get NPC
        if npc_name not in manager.npcs:
            return {
                "npc_name": npc_name,
                "error": f"NPC '{npc_name}' not loaded",
                "from_cache": False
            }
        
        npc = manager.npcs[npc_name]
        
        # Generate response
        import time
        start = time.time()
        response = npc.generate_response(player_input, game_state)
        elapsed = time.time() - start
        
        # Cache it
        performance_manager.cache_response(npc_name, player_input, response, game_state)
        
        # Record metrics
        tokens = len(response.split()) * 1.3
        performance_manager.record_request(elapsed, int(tokens), from_cache=False)
        
        return {
            "npc_name": npc_name,
            "response": response,
            "from_cache": False,
            "generation_time": elapsed
        }
    
    results = await performance_manager.batch_processor.process_batch(
        request.requests,
        process_single
    )
    
    return {
        "total_requests": len(request.requests),
        "results": results
    }


@app.post("/api/performance/pregenerate")
async def pregenerate_responses(request: PreGenerateRequest):
    """Pre-generate common responses for an NPC."""
    if not performance_manager:
        raise HTTPException(status_code=503, detail="Performance manager not initialized")
    
    performance_manager.optimize_for_npc(request.npc_name, request.npc_type)
    
    return {
        "status": "pregenerated",
        "npc_name": request.npc_name,
        "cache_size": len(performance_manager.cache._cache)
    }


@app.post("/api/performance/metrics/reset")
async def reset_metrics():
    """Reset all performance metrics."""
    if not performance_manager:
        raise HTTPException(status_code=503, detail="Performance manager not initialized")
    
    performance_manager.reset_metrics()
    
    return {"status": "reset"}


@app.get("/api/performance/cache/stats")
async def get_cache_stats():
    """Get detailed cache statistics."""
    if not performance_manager:
        raise HTTPException(status_code=503, detail="Performance manager not initialized")
    
    return performance_manager.cache.get_stats()


@app.get("/api/performance/stats")
async def get_performance_stats():
    """Get comprehensive performance statistics."""
    if not performance_manager:
        raise HTTPException(status_code=503, detail="Performance manager not initialized")
    
    return performance_manager.get_stats()


@app.get("/api/performance/health")
async def get_performance_health():
    """Get performance system health status."""
    if not performance_manager:
        return {"status": "not_initialized"}
    
    stats = performance_manager.get_stats()
    cache_stats = stats.get("cache", {})
    perf_stats = stats.get("performance", {})
    
    # Calculate health score
    cache_hit_rate = float(cache_stats.get("cache_hit_rate", "0%").replace("%", ""))
    avg_gen_time = float(perf_stats.get("avg_generation_time", "0").replace("s", ""))
    error_rate = perf_stats.get("errors", 0) / max(perf_stats.get("total_requests", 1), 1) * 100
    
    health_score = 100
    if cache_hit_rate < 30:
        health_score -= 20
    if avg_gen_time > 5:
        health_score -= 15
    if error_rate > 5:
        health_score -= 25
    
    return {
        "status": "healthy" if health_score >= 70 else "degraded",
        "health_score": health_score,
        "cache_hit_rate": cache_stats.get("cache_hit_rate"),
        "avg_generation_time": perf_stats.get("avg_generation_time"),
        "total_requests": perf_stats.get("total_requests"),
        "error_rate": f"{error_rate:.1f}%",
        "connection_pool_healthy": stats.get("connection_pool", {}).get("healthy", False),
        "cache_size": cache_stats.get("cache_size", 0),
        "recommendations": _get_performance_recommendations(cache_hit_rate, avg_gen_time, error_rate)
    }


def _get_performance_recommendations(cache_hit_rate: float, avg_gen_time: float, error_rate: float) -> list:
    """Generate performance recommendations."""
    recommendations = []
    
    if cache_hit_rate < 20:
        recommendations.append("Low cache hit rate. Consider pre-generating common NPC responses.")
    if avg_gen_time > 3:
        recommendations.append("Slow generation times. Consider using a smaller/faster model.")
    if error_rate > 2:
        recommendations.append("High error rate. Check Ollama connection stability.")
    if not recommendations:
        recommendations.append("Performance is optimal!")
    
    return recommendations


def _register_dm_directive_consumers():
    if not event_system or not dungeon_master:
        return
    cb = event_system.state_manager.event_callback
    cb.on(EventType.DM_QUEST_SUGGESTION, _handle_dm_quest)
    cb.on(EventType.DM_NPC_DIRECTIVE, _handle_dm_npc_directive)
    cb.on(EventType.DM_WORLD_EVENT, _handle_dm_world_event)
    cb.on(EventType.DM_CONVERSATION_TRIGGER, _handle_dm_conversation)
    cb.on(EventType.DM_LORE_UPDATE, _handle_dm_lore_update)
    cb.on(EventType.DM_RELATIONSHIP_OVERRIDE, _handle_dm_relationship)


async def _handle_dm_quest(event: StateEvent):
    params = event.data
    if not quest_manager:
        return
    try:
        from quest_generator import QuestType
        quest = quest_manager.generator.create_quest(
            quest_type=QuestType(params.get("quest_type", "fetch")),
            npc_name=params.get("npc_name", "Unknown"),
            title=params.get("title", "DM Suggested Quest"),
            description=params.get("description", ""),
        )
        quest_manager.add_quest(quest, params.get("npc_name", "Unknown"))
        print(f"DM: Quest suggestion accepted: {params.get('title', 'Unknown')}")
    except Exception as e:
        print(f"DM quest suggestion failed: {e}")


async def _handle_dm_npc_directive(event: StateEvent):
    params = event.data
    npc_name = params.get("npc_name", "")
    if not manager or npc_name not in manager.npcs:
        return
    npc = manager.npcs[npc_name]
    npc.set_dm_directive(
        directive=params.get("directive", ""),
        prompt_modifier=params.get("prompt_modifier", ""),
        expires_after=params.get("expires_after_events", 5),
    )
    print(f"DM: NPC directive applied to {npc_name}: {params.get('directive', '')}")


async def _handle_dm_world_event(event: StateEvent):
    params = event.data
    zones = params.get("affected_zones", [])
    for zone in zones:
        if zone:
            dungeon_master.state.world_conditions.add(f"{params.get('event_name', 'event')}_{zone}")
    print(f"DM: World event: {params.get('event_name', 'Unknown')} (severity: {params.get('severity', 'unknown')})")


async def _handle_dm_conversation(event: StateEvent):
    params = event.data
    if not conversation_manager:
        return
    try:
        trigger = ConversationTrigger.EVENT
        conversation_manager.start_conversation(
            npc1_name=params.get("npc1", ""),
            npc2_name=params.get("npc2", ""),
            topic=params.get("topic"),
            trigger=trigger,
            location=params.get("location"),
        )
        print(f"DM: Conversation triggered: {params.get('npc1', '')} <-> {params.get('npc2', '')}")
    except Exception as e:
        print(f"DM conversation trigger failed: {e}")


async def _handle_dm_lore_update(event: StateEvent):
    params = event.data
    try:
        from lore_system import LoreSystem, LoreEntry
        if not hasattr(_handle_dm_lore_update, '_lore_system'):
            _handle_dm_lore_update._lore_system = LoreSystem()
        _handle_dm_lore_update._lore_system.add_entry(LoreEntry(
            id=params.get("lore_id", f"dm_{int(time.time())}"),
            title=params.get("title", ""),
            content=params.get("content", ""),
            category=params.get("category", "events"),
            known_by=params.get("known_by", ["everyone"]),
            importance=params.get("importance", 0.5),
        ))
        print(f"DM: Lore updated: {params.get('title', 'Unknown')}")
    except Exception as e:
        print(f"DM lore update failed: {e}")


async def _handle_dm_relationship(event: StateEvent):
    params = event.data
    if not relationship_tracker:
        return
    for change in params.get("changes", []):
        try:
            if "npc" in change:
                relationship_tracker.update_score(
                    change["npc"], change["delta"], change.get("reason", "dm_override")
                )
            elif "faction" in change:
                relationship_tracker.update_faction(
                    change["faction"], change["delta"], change.get("reason", "dm_override")
                )
        except Exception as e:
            print(f"DM relationship override failed for {change}: {e}")


# ============================================
# DM API ENDPOINTS
# ============================================


class DmTriggerRequest(BaseModel):
    event_type: str
    data: Dict[str, Any] = {}
    player_id: Optional[str] = None
    npc_id: Optional[str] = None
    zone_id: Optional[str] = None


class DmArcRequest(BaseModel):
    title: str
    description: str = ""
    involved_npcs: List[str] = []
    involved_players: List[str] = []
    resolution_conditions: List[str] = []


@app.get("/api/dm/status")
async def get_dm_status():
    if not dungeon_master:
        raise HTTPException(status_code=503, detail="Dungeon Master not initialized")
    return dungeon_master.get_status()


@app.get("/api/dm/arcs")
async def get_dm_arcs():
    if not dungeon_master:
        raise HTTPException(status_code=503, detail="Dungeon Master not initialized")
    arcs = []
    for arc_id, arc in dungeon_master.state.active_arcs.items():
        arcs.append({
            "arc_id": arc.arc_id,
            "title": arc.title,
            "status": arc.status,
            "involved_npcs": arc.involved_npcs,
            "tension_level": arc.tension_level,
            "event_count": len(arc.key_events),
            "last_event_at": arc.last_event_at,
        })
    return {"arcs": arcs}


@app.get("/api/dm/arcs/{arc_id}")
async def get_dm_arc(arc_id: str):
    if not dungeon_master:
        raise HTTPException(status_code=503, detail="Dungeon Master not initialized")
    arc = dungeon_master.state.active_arcs.get(arc_id)
    if not arc:
        raise HTTPException(status_code=404, detail=f"Arc '{arc_id}' not found")
    return arc.to_dict()


@app.post("/api/dm/arcs")
async def create_dm_arc(request: DmArcRequest):
    if not dungeon_master:
        raise HTTPException(status_code=503, detail="Dungeon Master not initialized")
    arc = dungeon_master.create_arc(
        title=request.title,
        description=request.description,
        involved_npcs=request.involved_npcs,
        involved_players=request.involved_players,
        resolution_conditions=request.resolution_conditions,
    )
    return {"arc_id": arc.arc_id, "status": arc.status}


@app.get("/api/dm/rules")
async def get_dm_rules():
    if not dungeon_master or not dm_rule_engine:
        raise HTTPException(status_code=503, detail="Dungeon Master not initialized")
    active = [
        {
            "rule_id": r.get("rule_id"),
            "rule_name": r.get("rule_name"),
            "trigger": r.get("trigger", {}).get("event_type"),
            "priority": r.get("priority", 0),
            "confidence": r.get("confidence", 0),
            "times_observed": r.get("times_observed", 0),
            "created_at": r.get("created_at"),
        }
        for r in dm_rule_engine.active_rules
    ]
    pending = [
        {
            "rule_id": r.get("rule_id"),
            "rule_name": r.get("rule_name"),
            "confidence": r.get("confidence", 0),
            "status": "awaiting_review",
        }
        for r in dm_rule_engine.pending_rules
    ]
    return {"active": active, "pending": pending}


@app.get("/api/dm/rules/{rule_id}")
async def get_dm_rule(rule_id: str):
    if not dm_rule_engine:
        raise HTTPException(status_code=503, detail="Dungeon Master not initialized")
    rule = dm_rule_engine.get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    return rule


@app.post("/api/dm/rules/{rule_id}/approve")
async def approve_dm_rule(rule_id: str):
    if not dm_rule_engine:
        raise HTTPException(status_code=503, detail="Dungeon Master not initialized")
    ok = dm_rule_engine.activate_rule(rule_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Pending rule '{rule_id}' not found")
    import datetime
    return {"rule_id": rule_id, "status": "active", "activated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}


@app.post("/api/dm/rules/{rule_id}/deactivate")
async def deactivate_dm_rule(rule_id: str):
    if not dm_rule_engine:
        raise HTTPException(status_code=503, detail="Dungeon Master not initialized")
    ok = dm_rule_engine.deactivate_rule(rule_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Active rule '{rule_id}' not found")
    return {"rule_id": rule_id, "status": "inactive"}


@app.delete("/api/dm/rules/{rule_id}")
async def delete_dm_rule(rule_id: str):
    if not dm_rule_engine:
        raise HTTPException(status_code=503, detail="Dungeon Master not initialized")
    ok = dm_rule_engine.delete_rule(rule_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    return {"deleted": True, "rule_id": rule_id}


@app.post("/api/dm/trigger")
async def trigger_dm_event(request: DmTriggerRequest):
    if not dungeon_master:
        raise HTTPException(status_code=503, detail="Dungeon Master not initialized")
    try:
        et = EventType(request.event_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown event type: {request.event_type}")
    event = StateEvent(
        event_type=et,
        data=request.data,
        player_id=request.player_id,
        npc_id=request.npc_id,
        zone_id=request.zone_id,
    )
    await dungeon_master.handle_event(event)
    return {
        "triggered": True,
        "event_type": request.event_type,
        "total_events_processed": dungeon_master.state.total_events_processed,
    }


@app.post("/api/dm/save")
async def save_dm_state():
    if not dungeon_master:
        raise HTTPException(status_code=503, detail="Dungeon Master not initialized")
    dungeon_master.save_state()
    if dm_rule_engine:
        dm_rule_engine.save_rules()
    return {"saved": True}


@app.post("/api/dm/reset")
async def reset_dm_state():
    if not dungeon_master:
        raise HTTPException(status_code=503, detail="Dungeon Master not initialized")
    from dungeon_master import NarrativeState
    dungeon_master.state = NarrativeState()
    dungeon_master.raw_events = []
    return {"reset": True}




# ─── Eavesdrop Endpoints ─────────────────────────────────────────────────


class PresenceRequest(BaseModel):
    player_id: str = "player"
    position: List[float]
    posture: str = "stand"
    concealed: bool = False
    location: str = ""


class ListenRequest(BaseModel):
    player_id: str = "player"
    spot_id: str
    force_live: bool = False


def _require_eavesdrop() -> EavesdropManager:
    if not eavesdrop_manager:
        raise HTTPException(status_code=503, detail="Eavesdrop manager not initialized")
    return eavesdrop_manager


@app.get("/eavesdrop")
async def serve_eavesdrop_ui():
    """地图界面：走动、看偷听点、按下偷听。"""
    page = Path("static/eavesdrop.html")
    if not page.exists():
        raise HTTPException(status_code=404, detail="static/eavesdrop.html not found")
    return FileResponse(page)


@app.get("/api/eavesdrop/map")
async def get_eavesdrop_map(player_id: str = "player"):
    """全部偷听点 + NPC 位置 + 玩家当前能不能听。答案（intel）不会出现在这里。"""
    mgr = _require_eavesdrop()
    return {
        "player_id": player_id,
        "spots": mgr.list_spots(player_id),
        "npc_positions": [
            {"name": n, "position": list(p)} for n, p in mgr.npc_positions.items()
        ],
        "postures": list(Posture.ALL),
    }


@app.post("/api/eavesdrop/presence")
async def set_eavesdrop_presence(req: PresenceRequest):
    """前端持续上报玩家位置与姿态。偷听判定完全建立在这个状态上。"""
    mgr = _require_eavesdrop()
    presence = mgr.set_presence(
        player_id=req.player_id,
        position=(req.position[0], req.position[1]),
        posture=req.posture,
        concealed=req.concealed,
        location=req.location,
    )
    return {"presence": presence.__dict__}


@app.get("/api/eavesdrop/check/{spot_id}")
async def check_eavesdrop_spot(spot_id: str, player_id: str = "player"):
    """问"我现在能听吗"，无论能不能都会给出人类可读的原因。"""
    mgr = _require_eavesdrop()
    return mgr.check(spot_id, player_id).to_dict()


@app.post("/api/eavesdrop/listen")
async def eavesdrop_listen(req: ListenRequest):
    """按下偷听按钮。成功返回完整对白 + 情报，失败返回原因而不是报错。"""
    mgr = _require_eavesdrop()
    try:
        return await mgr.eavesdrop(req.spot_id, req.player_id, force_live=req.force_live)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/eavesdrop/session/{player_id}")
async def get_eavesdrop_session(player_id: str):
    """最近一次偷听到的对白，用于回看。"""
    mgr = _require_eavesdrop()
    return mgr.last_session.get(player_id) or {"ok": False, "reason": "还没有偷听过"}


@app.get("/api/eavesdrop/status/{player_id}")
async def get_eavesdrop_status(player_id: str):
    mgr = _require_eavesdrop()
    return mgr.status(player_id)


@app.get("/api/eavesdrop/intel/{player_id}")
async def get_eavesdrop_intel(player_id: str):
    """玩家已获得的情报。可整段注入 NPC 的 system prompt。"""
    mgr = _require_eavesdrop()
    return {
        "intel": mgr.get_intel(player_id),
        "prompt_brief": mgr.intel_brief(player_id),
    }


@app.post("/api/eavesdrop/prefetch")
async def prefetch_eavesdrop(player_id: str = "player"):
    """把所有偷听点的对白提前生成好。

    本地模型跑一段对谈要十几秒。等玩家按下按钮才开始生成，体验会直接崩掉。
    推荐在关卡加载时调一次，之后偷听是瞬时的。
    """
    mgr = _require_eavesdrop()
    count = await mgr.prefetch_all()
    return {"prefetched": count, "total": len(mgr.spots)}



# Run server
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api_server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info"
    )
