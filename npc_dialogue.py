"""
NPC Dialogue System - Core Logic
Local LLM-powered character conversations for games
"""

import json
import os
import time
from pathlib import Path
from typing import List, Dict, Optional, TYPE_CHECKING
import requests
from relationship_tracking import RelationshipTracker, RelationshipLevel
from llm_providers import create_provider, LLMProvider, OllamaProvider, DEFAULT_MODELS

# Lore system import (optional)
try:
    from lore_system import LoreSystem
    HAS_LORE_SYSTEM = True
except ImportError:
    HAS_LORE_SYSTEM = False
    LoreSystem = None


class NPCDialogue:
    """
    An AI-powered NPC that can have conversations with players.
    Uses local LLM (via Ollama) for dialogue generation.
    
    Supports optional connection pooling for better performance.
    """
    
    # Class-level connection pool for all instances
    _shared_connection_pool = None

    #: 后端不可用只提示一次，避免一局游戏刷屏
    _warned_no_backend = False

    #: 后端探测失败后的静默窗口。一局游戏要创建几十个 NPC，
    #: 每次启动都去撞一次连不上的端口会白白拖慢几秒。
    _backend_retry_after = 0.0
    _BACKEND_PROBE_COOLDOWN = 30.0
    
    def __init__(
        self,
        character_name: str,
        character_card_path: str,
        model: str = "llama3.2:1b",
        player_id: str = "player",
        max_history: int = 20,
        temperature: float = 0.8,
        max_tokens: int = 500,
        relationship_tracker: Optional[RelationshipTracker] = None,
        lore_system: Optional['LoreSystem'] = None,
        connection_pool: Optional['OllamaConnectionPool'] = None,
        use_shared_pool: bool = True,
        backend: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        """
        Initialize an NPC with a character card and model settings.
        
        Args:
            character_name: Name of the NPC
            character_card_path: Path to JSON character card
            model: Ollama model name (e.g., "llama3.2:1b")
            player_id: Unique identifier for the player
            max_history: Number of conversation turns to remember
            temperature: Randomness (0.0-1.0, higher = more creative)
            max_tokens: Maximum response length
            relationship_tracker: Optional RelationshipTracker instance
            connection_pool: Optional OllamaConnectionPool for connection reuse
            use_shared_pool: Use class-level shared connection pool
        """
        self.character_name = character_name
        self.model = model
        self.player_id = player_id
        self.max_history = max_history
        self.base_temperature = temperature  # Store base temperature
        self.temperature = temperature
        self.max_tokens = max_tokens
        
        # Load character card
        self.character_card = self._load_character_card(character_card_path)
        
        # Conversation history
        self.history: List[Dict[str, str]] = []
        
        # Relationship tracking
        self.relationship_tracker = relationship_tracker
        if self.relationship_tracker:
            # Adjust temperature based on current relationship
            self.temperature = self.relationship_tracker.get_temperature_adjustment(
                character_name, self.base_temperature
            )
        
        # Lore system (RAG for world knowledge)
        self.lore_system = lore_system
        
        # Connection pooling for better performance
        self._connection_pool = connection_pool
        if use_shared_pool and not connection_pool:
            self._connection_pool = self._get_or_create_shared_pool()
        
        # LLM Provider (Ollama or Groq)
        self.backend = backend or os.getenv("LLM_BACKEND", "ollama")
        self._provider = create_provider(
            backend=self.backend,
            api_key=api_key,
            ollama_url="http://localhost:11434",
        )

        self._dm_directive = None
        
        # Ollama API endpoint (legacy fallback)
        self.api_url = "http://localhost:11434/api/chat"
        
        # Verify connection
        self._check_connection()
    
    @classmethod
    def _get_or_create_shared_pool(cls):
        """Get or create the shared connection pool."""
        if cls._shared_connection_pool is None:
            try:
                from performance import OllamaConnectionPool
                cls._shared_connection_pool = OllamaConnectionPool(
                    base_url="http://localhost:11434",
                    pool_size=10,
                    max_retries=3,
                    timeout=120
                )
            except ImportError:
                pass
        return cls._shared_connection_pool
    
    @classmethod
    def set_shared_pool(cls, pool):
        """Set a shared connection pool for all NPC instances."""
        cls._shared_connection_pool = pool
    
    def set_dm_directive(self, directive: str, prompt_modifier: str, expires_after: int = 5):
        self._dm_directive = {
            "directive": directive,
            "prompt_modifier": prompt_modifier,
            "expires_after": expires_after,
            "events_remaining": expires_after,
        }

    def _get_dm_directive_modifier(self) -> str:
        if self._dm_directive:
            modifier = self._dm_directive["prompt_modifier"]
            self._dm_directive["events_remaining"] -= 1
            if self._dm_directive["events_remaining"] <= 0:
                self._dm_directive = None
            return modifier
        return ""

    def _load_character_card(self, path: str) -> Dict:
        """Load character definition from JSON file."""
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def _check_connection(self, strict: Optional[bool] = None):
        """Verify LLM backend is accessible.

        Default behaviour is to **warn**, not to crash. The rest of the system
        is built to degrade gracefully — `NPCConversationEngine` falls back to
        template responses, the demos run offline, the unit tests need no model.
        A hard raise here contradicts that design and makes the whole codebase
        untestable unless Ollama happens to be running on the machine.

        Pass `strict=True` (or set `NPC_STRICT_BACKEND=1`) to restore the old
        fail-fast behaviour, e.g. for production deployments.
        """
        if strict is None:
            strict = os.getenv("NPC_STRICT_BACKEND", "").lower() in ("1", "true", "yes")

        if self.backend == "ollama":
            # 上次刚探测失败过，冷却期内不再重复撞端口
            if time.time() < NPCDialogue._backend_retry_after:
                return
            try:
                response = requests.get("http://localhost:11434/api/tags", timeout=5)
                models = [m['name'] for m in response.json().get('models', [])]

                if self.model not in models:
                    print(f"⚠️  Warning: Model '{self.model}' not found in Ollama.")
                    print(f"   Run: ollama pull {self.model}")
                    available = ", ".join(models[:5]) + ("..." if len(models) > 5 else "")
                    print(f"   Available: {available}")
            except requests.exceptions.ConnectionError:
                NPCDialogue._backend_retry_after = time.time() + NPCDialogue._BACKEND_PROBE_COOLDOWN
                message = (
                    "Ollama is not running! Start it with: ollama serve\n"
                    "Or install: curl -fsSL https://ollama.com/install.sh | sh"
                )
                if strict:
                    raise ConnectionError(message)
                if not NPCDialogue._warned_no_backend:
                    NPCDialogue._warned_no_backend = True
                    print(f"⚠️  {message.splitlines()[0]}")
                    print("   Falling back to template responses "
                          "(set NPC_STRICT_BACKEND=1 to fail fast instead).")
        elif self.backend == "groq":
            if not self._provider.check_connection():
                print(f"⚠️  Warning: Could not verify Groq API connection.")
                print(f"   Check your GROQ_API_KEY in .env")
    
    def _build_system_prompt(self, game_state: Optional[Dict] = None) -> str:
        """Build the system prompt with character personality and context."""
        
        # Base character info
        prompt = f"""You are {self.character_name}. 

CHARACTER DESCRIPTION:
{self.character_card.get('description', '')}

PERSONALITY:
{self.character_card.get('personality', '')}

IMPORTANT:
- Stay fully in character at all times
- {self.character_card.get('speaking_style', 'Speak naturally')}
- Don't break the fourth wall
- Be engaging but concise (2-4 sentences typical)
- Remember previous conversations with this player
"""

        # Add relationship context if tracker is available
        if self.relationship_tracker:
            rel_level = self.relationship_tracker.get_level(self.character_name)
            rel_modifier = self.relationship_tracker.get_speaking_style_modifier(self.character_name)
            
            if rel_modifier:
                prompt += f"\nYOUR RELATIONSHIP WITH THIS PLAYER:\n{rel_modifier}\n"
            
            prompt += f"\nCurrent relationship level: {rel_level.name}\n"
        
        # Add quest context if present in game state
        if game_state:
            quest_context = self._format_quest_context(game_state)
            if quest_context:
                prompt += quest_context

        # Add any game state context
        if game_state:
            non_quest_state = {k: v for k, v in game_state.items()
                              if k not in ("active_quests", "pending_quest", "player_inventory", "_inventory_override")}
            if non_quest_state:
                prompt += f"\nCURRENT SITUATION:\n{self._format_game_state(non_quest_state)}\n"
        
        # Add inventory context
        if game_state and game_state.get("player_inventory"):
            prompt += f"\nPLAYER'S INVENTORY:\n{self._format_inventory(game_state['player_inventory'])}\n"
            prompt += (
                "\nIMPORTANT: The player can only give, trade, or offer items that are listed in their "
                "inventory above. If they claim to have or offer an item NOT in their inventory, you MUST "
                "refuse and point out that they don't have it. Stay in character while doing so.\n"
            )
        
        # Add inventory override (server-side detected fraud)
        if game_state and game_state.get("_inventory_override"):
            prompt += f"\nCRITICAL: {game_state['_inventory_override']}\n"

        # Add DM directive if active
        dm_modifier = self._get_dm_directive_modifier()
        if dm_modifier:
            prompt += f"\nDUNGEON MASTER DIRECTIVE:\n{dm_modifier}\n"

        return prompt

    def _format_quest_context(self, game_state: Dict) -> str:
        """Format quest information for the system prompt."""
        parts = []

        active_quests = game_state.get("active_quests", [])
        if active_quests:
            parts.append("\nQUESTS YOU HAVE GIVEN THIS PLAYER:")
            for quest in active_quests:
                name = quest.get("name", "Unknown")
                qtype = quest.get("quest_type", "")
                progress = quest.get("progress", 0)
                is_complete = quest.get("is_complete", False)
                status = "COMPLETE" if is_complete else f"{progress:.0f}% done"
                parts.append(f"- {name} ({qtype}): {status}")
                for obj in quest.get("objectives", []):
                    cur = obj.get("current", 0)
                    req = obj.get("required", 1)
                    desc = obj.get("description", "")
                    check = "done" if cur >= req else f"{cur}/{req}"
                    parts.append(f"  Objective: {desc} [{check}]")
            parts.append("You remember these quests. Reference them naturally if relevant.")

        pending_quest = game_state.get("pending_quest")
        if pending_quest:
            parts.append(f"\nYou just offered the player a quest: \"{pending_quest.get('name', '')}\"")
            parts.append("Wait for their answer before assuming they accepted.")

        if parts:
            return "\n".join(parts) + "\n"
        return ""
    
    def _build_system_prompt_with_lore(
        self, 
        user_input: str,
        game_state: Optional[Dict] = None
    ) -> str:
        """Build system prompt with relevant lore context."""
        prompt = self._build_system_prompt(game_state)
        
        # Add lore context if available
        if self.lore_system:
            lore_context = self.lore_system.get_context_for_npc(
                npc_name=self.character_name,
                query=user_input,
                max_tokens=400
            )
            if lore_context:
                prompt += f"\n{lore_context}\n"
        
        return prompt
    
    def _format_game_state(self, game_state: Dict) -> str:
        """Format game state for context."""
        formatted = []
        for key, value in game_state.items():
            formatted.append(f"- {key}: {value}")
        return "\n".join(formatted)
    
    def _format_inventory(self, inventory: Dict[str, int]) -> str:
        """Format player inventory for system prompt."""
        if not inventory:
            return "- (empty)"
        formatted = []
        for item, qty in inventory.items():
            if qty == 1:
                formatted.append(f"- {item}")
            else:
                formatted.append(f"- {item} x{qty}")
        return "\n".join(formatted)
    
    def _format_messages(
        self,
        user_input: str,
        game_state: Optional[Dict] = None,
        use_lore: bool = True
    ) -> List[Dict]:
        """Format messages for Ollama API."""
        
        # Use lore-enhanced prompt if available and enabled
        if use_lore and self.lore_system:
            system_content = self._build_system_prompt_with_lore(user_input, game_state)
        else:
            system_content = self._build_system_prompt(game_state)
        
        messages = [
            {"role": "system", "content": system_content},
        ]
        
        # Add conversation history
        messages.extend(self.history)
        
        # Add current user input
        messages.append({"role": "user", "content": user_input})
        
        return messages
    
    def generate_response(
        self,
        user_input: str,
        game_state: Optional[Dict] = None,
        show_thinking: bool = False
    ) -> str:
        """
        Generate an NPC response to player input.
        
        Args:
            user_input: What the player said/asked
            game_state: Current game context (location, quests, etc.)
            show_thinking: Print generation info
            
        Returns:
            NPC's response as text
        """
        if show_thinking:
            print(f"🤖 {self.character_name} is thinking...", end="", flush=True)
        
        start_time = time.time()
        
        # Generate response
        try:
            messages = self._format_messages(user_input, game_state)
            result = self._provider.generate(
                messages=messages,
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            
            npc_response = result['content']
            
            # Update history
            self.history.append({"role": "user", "content": user_input})
            self.history.append({"role": "assistant", "content": npc_response})
            
            # Trim history if too long
            if len(self.history) > self.max_history:
                self.history = self.history[-self.max_history:]
            
            elapsed = time.time() - start_time

            if show_thinking:
                tokens = result['tokens']
                tps = tokens / elapsed if elapsed > 0 else 0
                print(f" ✓ ({tokens} tokens, {elapsed:.1f}s, {tps:.1f} tok/s)")

            return npc_response
            
        except requests.exceptions.RequestException as e:
            return f"[Error: Failed to generate response - {e}]"
    
    def save_history(self, directory: str = "conversation_history"):
        """Save conversation history to disk."""
        os.makedirs(directory, exist_ok=True)
        
        filename = os.path.join(
            directory,
            f"{self.character_name}_{self.player_id}.json"
        )
        
        data = {
            "character_name": self.character_name,
            "player_id": self.player_id,
            "model": self.model,
            "timestamp": time.time(),
            "history": self.history,
        }
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    def load_history(self, directory: str = "conversation_history"):
        """Load conversation history from disk."""
        filename = os.path.join(
            directory,
            f"{self.character_name}_{self.player_id}.json"
        )
        
        if not os.path.exists(filename):
            return
        
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
            self.history = data.get('history', [])
        
        print(f"📜 Loaded {len(self.history) // 2} past conversations")
    
    def reset_history(self):
        """Clear conversation history."""
        self.history = []
        print(f"🗑️  Conversation history cleared")
    
    def get_stats(self) -> Dict:
        """Get statistics about current conversation."""
        turns = len(self.history) // 2
        npc_words = sum(
            len(msg['content'].split())
            for msg in self.history
            if msg['role'] == 'assistant'
        )
        
        return {
            "character": self.character_name,
            "model": self.model,
            "backend": self.backend,
            "turns": turns,
            "npc_words": npc_words,
            "history_size": len(self.history),
        }
    
    def print_character_info(self):
        """Display character card information."""
        print(f"\n{'='*60}")
        print(f"CHARACTER: {self.character_name}")
        print(f"{'='*60}")
        print(f"\n📖 Description:")
        print(f"   {self.character_card.get('description', 'N/A')}")
        print(f"\n🎭 Personality:")
        print(f"   {self.character_card.get('personality', 'N/A')}")
        print(f"\n💬 Speaking Style:")
        print(f"   {self.character_card.get('speaking_style', 'Natural')}")
        print(f"\n📝 First Message:")
        print(f"   {self.character_card.get('first_mes', 'N/A')}")
        
        # Show relationship info if available
        if self.relationship_tracker:
            rel_level = self.relationship_tracker.get_level(self.character_name)
            rel = self.relationship_tracker.get_relationship(self.character_name)
            if rel is not None and rel_level is not None:
                print(f"\n💖 Relationship: {rel_level.name} ({rel.score:+.1f})")
        
        print(f"\n{'='*60}\n")
    
    def get_relationship_level(self) -> Optional[str]:
        """Get current relationship level with the player."""
        if not self.relationship_tracker:
            return None
        return self.relationship_tracker.get_level(self.character_name).name
    
    def get_relationship_score(self) -> Optional[float]:
        """Get current relationship score with the player."""
        if not self.relationship_tracker:
            return None
        return self.relationship_tracker.get_relationship(self.character_name).score
    
    def update_from_quest(self, quest_id: str, success: bool = True, reward: float = 15.0) -> Optional[float]:
        """
        Update relationship after quest completion.
        
        Args:
            quest_id: Unique quest identifier
            success: Whether quest was completed successfully
            reward: Score change on success (penalty on failure)
            
        Returns:
            New relationship score, or None if no tracker
        """
        if not self.relationship_tracker:
            return None
        return self.relationship_tracker.update_from_quest(
            self.character_name, quest_id, success, reward
        )
    
    def update_from_gift(self, item_name: str, value: float = 5.0,
                         player_inventory: Optional[Dict[str, int]] = None) -> Optional[float]:
        """
        Update relationship after giving a gift.
        
        Args:
            item_name: Name of the gifted item
            value: Relationship value of the item
            player_inventory: Optional inventory to verify the player has the item
            
        Returns:
            New relationship score, or None if no tracker or player doesn't have item
        """
        if not self.relationship_tracker:
            return None
        return self.relationship_tracker.update_from_gift(
            self.character_name, item_name, value, player_inventory=player_inventory
        )
    
    def update_from_dialogue(self, dialogue_type: str, sentiment: float = 0.0) -> Optional[float]:
        """
        Update relationship from dialogue choice.
        
        Args:
            dialogue_type: Type of dialogue ('friendly', 'hostile', 'neutral', 'flirt', 'insult')
            sentiment: Custom sentiment value (-1.0 to 1.0)
            
        Returns:
            New relationship score, or None if no tracker
        """
        if not self.relationship_tracker:
            return None
        return self.relationship_tracker.update_from_dialogue(
            self.character_name, dialogue_type, sentiment
        )
    
    def refresh_temperature(self):
        """Refresh temperature based on current relationship score."""
        if self.relationship_tracker:
            self.temperature = self.relationship_tracker.get_temperature_adjustment(
                self.character_name, self.base_temperature
            )
    
    @property
    def provider(self) -> LLMProvider:
        return self._provider



class NPCManager:
    """
    Manage multiple NPCs in a game world.
    Handles loading characters and maintaining separate conversations.
    """
    
    def __init__(
        self, 
        model: Optional[str] = None, 
        relationship_tracker: Optional[RelationshipTracker] = None,
        lore_system: Optional['LoreSystem'] = None,
        backend: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.backend = backend or os.getenv("LLM_BACKEND", "ollama")
        self.api_key = api_key or os.getenv("GROQ_API_KEY")
        
        if model:
            self.model = model
        else:
            self.model = DEFAULT_MODELS.get(self.backend, "llama3.2:1b")
        
        self.relationship_tracker = relationship_tracker
        self.lore_system = lore_system
        self.npcs: Dict[str, NPCDialogue] = {}
        self.active_npc: Optional[NPCDialogue] = None
        
        self._provider = create_provider(
            backend=self.backend,
            api_key=self.api_key,
        )
    
    def load_character(
        self,
        character_path: str,
        player_id: str = "player",
        **kwargs
    ) -> NPCDialogue:
        """Load a character from a JSON file."""
        with open(character_path, 'r', encoding='utf-8') as f:
            card = json.load(f)
        
        character_name = card.get('name', 'Unknown')
        
        # Pass relationship_tracker and lore_system if not already specified
        if 'relationship_tracker' not in kwargs:
            kwargs['relationship_tracker'] = self.relationship_tracker
        if 'lore_system' not in kwargs:
            kwargs['lore_system'] = self.lore_system
        if 'backend' not in kwargs:
            kwargs['backend'] = self.backend
        if 'api_key' not in kwargs:
            kwargs['api_key'] = self.api_key
        
        npc = NPCDialogue(
            character_name=character_name,
            character_card_path=character_path,
            model=self.model,
            player_id=player_id,
            **kwargs
        )
        
        self.npcs[character_name] = npc
        return npc
    
    def set_active(self, character_name: str):
        """Set the currently active NPC for conversation."""
        if character_name not in self.npcs:
            raise ValueError(f"NPC '{character_name}' not loaded")
        self.active_npc = self.npcs[character_name]
    
    def get_active(self) -> Optional[NPCDialogue]:
        """Get the currently active NPC."""
        return self.active_npc
    
    def list_characters(self) -> List[str]:
        """List all loaded character names."""
        return list(self.npcs.keys())
    
    def save_all_histories(self, directory: str = "conversation_history"):
        """Save all conversation histories."""
        for npc in self.npcs.values():
            npc.save_history(directory)
    
    def load_all_histories(self, directory: str = "conversation_history"):
        """Load all conversation histories."""
        for npc in self.npcs.values():
            npc.load_history(directory)
    
    def save_relationships(self, filepath: Optional[str] = None):
        """Save relationship data for all NPCs."""
        if not self.relationship_tracker:
            print("⚠️  No relationship tracker configured")
            return
        self.relationship_tracker.save(filepath)
    
    def load_relationships(self, filepath: Optional[str] = None):
        """Load relationship data for all NPCs."""
        if not self.relationship_tracker:
            print("⚠️  No relationship tracker configured")
            return
        self.relationship_tracker.load(filepath)
        # Refresh temperatures for all NPCs based on loaded relationships
        for npc in self.npcs.values():
            npc.refresh_temperature()
    
    def print_relationship_summary(self):
        """Print relationship summary for all NPCs."""
        if not self.relationship_tracker:
            print("⚠️  No relationship tracker configured")
            return
        self.relationship_tracker.print_summary()
    
    def get_relationship_tracker(self) -> Optional[RelationshipTracker]:
        """Get the relationship tracker instance."""
        return self.relationship_tracker

