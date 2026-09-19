"""
NPC-to-NPC Conversation System
Enables dynamic conversations between NPCs without player involvement.

Features:
- Proximity-based conversations (NPCs near each other)
- Scheduled conversations (time-based triggers)
- Event-triggered conversations (world events, quest completion)
- Overhearable by players (can listen in)
- Relationship-aware dialogue between NPCs
- Shared knowledge exchange (rumors, news)
"""

import json
import time
import random
import asyncio
import itertools
from typing import List, Dict, Optional, Callable, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
import requests

from npc_dialogue import NPCDialogue, NPCManager
from relationship_tracking import RelationshipTracker


class ConversationTrigger(Enum):
    """How a conversation was triggered."""
    PROXIMITY = "proximity"       # NPCs near each other
    SCHEDULED = "scheduled"       # Time-based
    EVENT = "event"               # World event triggered
    PLAYER_NEARBY = "player_nearby"  # Player entered area
    RANDOM = "random"             # Random ambient chatter
    EAVESDROP = "eavesdrop"       # Player deliberately listened in (see eavesdrop.py)
    FORCED = "forced"             # Scripted/forced conversation


class ConversationState(Enum):
    """State of an NPC conversation."""
    IDLE = "idle"
    STARTING = "starting"
    ACTIVE = "active"
    PAUSED = "paused"
    ENDING = "ending"
    COMPLETED = "completed"


@dataclass
class ConversationTopic:
    """A topic that NPCs can discuss."""
    topic_id: str
    name: str
    description: str
    keywords: List[str] = field(default_factory=list)
    required_knowledge: List[str] = field(default_factory=list)
    min_relationship: float = -100.0  # Minimum relationship to discuss
    max_uses: int = 3  # Max times this topic can be used
    cooldown_minutes: int = 60  # Cooldown before topic can be reused
    priority: int = 0  # Higher priority = more likely to be chosen


@dataclass
class ConversationExchange:
    """A single exchange in a conversation (one NPC speaking)."""
    speaker: str
    listener: str
    message: str
    timestamp: float = field(default_factory=time.time)
    emotion: str = "neutral"
    topic: Optional[str] = None


@dataclass
class NPCConversation:
    """
    Represents an active conversation between two NPCs.
    """
    conversation_id: str
    npc1_name: str
    npc2_name: str
    trigger: ConversationTrigger
    state: ConversationState = ConversationState.IDLE
    
    # Conversation content
    exchanges: List[ConversationExchange] = field(default_factory=list)
    current_speaker: Optional[str] = None
    current_topic: Optional[str] = None
    topics_discussed: List[str] = field(default_factory=list)
    
    # Timing
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    max_turns: int = 6
    current_turn: int = 0
    turn_delay: float = 2.0  # Seconds between turns
    
    # Context
    location: Optional[str] = None
    nearby_players: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def get_duration(self) -> float:
        """Get conversation duration in seconds."""
        if self.started_at is None:
            return 0.0
        end = self.ended_at or time.time()
        return end - self.started_at
    
    def get_last_message(self) -> Optional[ConversationExchange]:
        """Get the most recent exchange."""
        if self.exchanges:
            return self.exchanges[-1]
        return None
    
    def to_dict(self) -> Dict:
        """Serialize conversation to dict."""
        return {
            "conversation_id": self.conversation_id,
            "npc1_name": self.npc1_name,
            "npc2_name": self.npc2_name,
            "trigger": self.trigger.value,
            "state": self.state.value,
            "exchanges": [
                {
                    "speaker": e.speaker,
                    "listener": e.listener,
                    "message": e.message,
                    "timestamp": e.timestamp,
                    "emotion": e.emotion,
                    "topic": e.topic
                }
                for e in self.exchanges
            ],
            "current_speaker": self.current_speaker,
            "current_topic": self.current_topic,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration": self.get_duration(),
            "location": self.location,
            "nearby_players": self.nearby_players,
        }


class ConversationTopicRegistry:
    """Manages available conversation topics."""
    
    def __init__(self):
        self.topics: Dict[str, ConversationTopic] = {}
        self.topic_cooldowns: Dict[str, Dict[str, float]] = {}  # topic_id -> {npc_pair -> last_used}
        self._load_default_topics()
    
    def _load_default_topics(self):
        """Load built-in conversation topics."""
        default_topics = [
            ConversationTopic(
                topic_id="gossip",
                name="Gossip",
                description="Sharing rumors and hearsay about others",
                keywords=["heard", "rumor", "saying", "apparently", "word is"],
                min_relationship=-20.0,
                priority=5
            ),
            ConversationTopic(
                topic_id="trade",
                name="Trade & Business",
                description="Discussing commerce, prices, and business matters",
                keywords=["price", "cost", "buy", "sell", "trade", "gold", "coins"],
                min_relationship=-50.0,
                priority=4
            ),
            ConversationTopic(
                topic_id="weather",
                name="Weather",
                description="Commenting on current weather conditions",
                keywords=["weather", "rain", "sun", "storm", "cold", "warm"],
                min_relationship=-100.0,
                max_uses=2,
                priority=1
            ),
            ConversationTopic(
                topic_id="quests",
                name="Recent Quests",
                description="Discussing recent adventuring activities",
                keywords=["quest", "adventure", "journey", "mission", "task"],
                min_relationship=10.0,
                priority=6
            ),
            ConversationTopic(
                topic_id="politics",
                name="Politics & Factions",
                description="Discussing faction matters and political news",
                keywords=["faction", "leader", "council", "politics", "alliance"],
                min_relationship=20.0,
                priority=5
            ),
            ConversationTopic(
                topic_id="personal",
                name="Personal Matters",
                description="Sharing personal stories and feelings",
                keywords=["feel", "think", "family", "friend", "worried", "happy"],
                min_relationship=40.0,
                priority=7
            ),
            ConversationTopic(
                topic_id="skills",
                name="Craft & Skills",
                description="Discussing professional skills and techniques",
                keywords=["craft", "skill", "technique", "learn", "practice", "master"],
                min_relationship=0.0,
                priority=3
            ),
            ConversationTopic(
                topic_id="dangers",
                name="Dangers & Threats",
                description="Warning about nearby threats and dangers",
                keywords=["danger", "threat", "monster", "enemy", "careful", "avoid"],
                min_relationship=-30.0,
                priority=8
            ),
            ConversationTopic(
                topic_id="memories",
                name="Shared Memories",
                description="Reminiscing about past experiences together",
                keywords=["remember", "last time", "when we", "years ago", "back then"],
                min_relationship=60.0,
                priority=6
            ),
            ConversationTopic(
                topic_id="plans",
                name="Future Plans",
                description="Discussing upcoming plans and goals",
                keywords=["plan", "going to", "tomorrow", "next week", "hope to"],
                min_relationship=30.0,
                priority=4
            ),
        ]
        
        for topic in default_topics:
            self.topics[topic.topic_id] = topic
    
    def register_topic(self, topic: ConversationTopic):
        """Register a new conversation topic."""
        self.topics[topic.topic_id] = topic
    
    def get_available_topics(
        self, 
        npc1: str, 
        npc2: str, 
        relationship_score: float,
        topics_used: List[str]
    ) -> List[ConversationTopic]:
        """Get topics available for a specific NPC pair."""
        available = []
        npc_pair = f"{min(npc1, npc2)}_{max(npc1, npc2)}"
        current_time = time.time()
        
        for topic in self.topics.values():
            # Check relationship requirement
            if relationship_score < topic.min_relationship:
                continue
            
            # Check usage limit
            uses = topics_used.count(topic.topic_id)
            if uses >= topic.max_uses:
                continue
            
            # Check cooldown
            if topic.topic_id in self.topic_cooldowns:
                last_used = self.topic_cooldowns[topic.topic_id].get(npc_pair, 0)
                cooldown_seconds = topic.cooldown_minutes * 60
                if current_time - last_used < cooldown_seconds:
                    continue
            
            available.append(topic)
        
        return available
    
    def select_topic(
        self,
        npc1: str,
        npc2: str,
        relationship_score: float,
        topics_used: List[str],
        context: Optional[Dict] = None
    ) -> Optional[ConversationTopic]:
        """Select a topic for conversation, weighted by priority."""
        available = self.get_available_topics(npc1, npc2, relationship_score, topics_used)
        
        if not available:
            return None
        
        # Weight by priority
        weights = [t.priority for t in available]
        selected = random.choices(available, weights=weights, k=1)[0]
        
        return selected
    
    def mark_topic_used(self, topic_id: str, npc1: str, npc2: str):
        """Mark a topic as used for an NPC pair."""
        npc_pair = f"{min(npc1, npc2)}_{max(npc1, npc2)}"
        if topic_id not in self.topic_cooldowns:
            self.topic_cooldowns[topic_id] = {}
        self.topic_cooldowns[topic_id][npc_pair] = time.time()


class NPCConversationEngine:
    """
    Core engine for generating NPC-to-NPC conversations.
    
    Handles:
    - Turn-taking between NPCs
    - Topic selection and progression
    - Relationship-aware dialogue
    - Context injection from lore system
    """
    
    def __init__(
        self,
        npc_manager: NPCManager,
        relationship_tracker: Optional[RelationshipTracker] = None,
        lore_system: Optional[Any] = None,
        model: str = "llama3.2:1b",
        api_url: str = "http://localhost:11434/api/chat"
    ):
        self.npc_manager = npc_manager
        self.relationship_tracker = relationship_tracker
        self.lore_system = lore_system
        self.model = model
        self.api_url = api_url
        
        self.topic_registry = ConversationTopicRegistry()
        self.ollama_available = self._check_ollama()
    
    def _check_ollama(self) -> bool:
        """Check if Ollama is available."""
        try:
            response = requests.get("http://localhost:11434/api/tags", timeout=2)
            return response.status_code == 200
        except Exception:
            return False
    
    def _get_npc_card(self, npc_name: str) -> Optional[Dict]:
        """Get NPC character card."""
        if npc_name in self.npc_manager.npcs:
            return self.npc_manager.npcs[npc_name].character_card
        return None
    
    def _get_relationship(self, npc1: str, npc2: str) -> float:
        """Get relationship score between two NPCs."""
        if not self.relationship_tracker:
            return 0.0
        # Use NPC-to-NPC relationship if available, otherwise default
        return self.relationship_tracker.get_relationship(npc1).score if npc1 in self.relationship_tracker.relationships else 0.0
    
    def _build_conversation_prompt(
        self,
        speaker_name: str,
        listener_name: str,
        topic: Optional[ConversationTopic],
        conversation_history: List[ConversationExchange],
        is_start: bool = False,
        is_end: bool = False,
        context: Optional[Dict] = None
    ) -> str:
        """Build the system prompt for NPC dialogue generation."""
        
        speaker_card = self._get_npc_card(speaker_name)
        listener_card = self._get_npc_card(listener_name)
        
        if not speaker_card or not listener_card:
            return ""
        
        # Get relationship
        relationship = self._get_relationship(speaker_name, listener_name)
        
        # Build prompt
        prompt = f"""You are {speaker_name} having a conversation with {listener_name}.

YOUR CHARACTER:
{speaker_card.get('description', '')}

YOUR PERSONALITY:
{speaker_card.get('personality', '')}

YOUR SPEAKING STYLE:
{speaker_card.get('speaking_style', 'Speak naturally')}

YOU ARE TALKING TO:
{listener_name} - {listener_card.get('description', 'Another person')}

RELATIONSHIP WITH {listener_name.upper()}:
Score: {relationship:+.1f} (range -100 to +100)
"""
        
        # Add topic context
        if topic:
            prompt += f"\nCURRENT TOPIC: {topic.name}\n{topic.description}\n"
            if topic.keywords:
                prompt += f"Consider using words like: {', '.join(topic.keywords[:5])}\n"
        
        # Add conversation history
        if conversation_history:
            prompt += "\nCONVERSATION SO FAR:\n"
            for exchange in conversation_history[-6:]:  # Last 6 exchanges
                prompt += f"{exchange.speaker}: {exchange.message}\n"
        
        # Add context
        if context:
            if context.get('location'):
                prompt += f"\nLOCATION: {context['location']}\n"
            if context.get('time_of_day'):
                prompt += f"TIME: {context['time_of_day']}\n"
            if context.get('recent_events'):
                prompt += f"RECENT EVENTS: {context['recent_events']}\n"
        
        # Add instructions
        if is_start:
            prompt += """
INSTRUCTION: Start a natural conversation with the other person. Greet them and bring up a topic. Keep it brief (1-2 sentences).
"""
        elif is_end:
            prompt += """
INSTRUCTION: End the conversation naturally. Say goodbye or indicate you need to go. Keep it brief.
"""
        else:
            prompt += """
INSTRUCTION: Respond naturally to continue the conversation. Stay in character. Be concise (1-3 sentences).
"""
        
        prompt += """
IMPORTANT:
- Stay fully in character
- Don't break the fourth wall
- React authentically to what was said
- Keep responses conversational and natural
- Don't use quotes or narration, just speak directly
"""
        
        return prompt
    
    def generate_response(
        self,
        speaker_name: str,
        listener_name: str,
        topic: Optional[ConversationTopic],
        conversation_history: List[ConversationExchange],
        is_start: bool = False,
        is_end: bool = False,
        context: Optional[Dict] = None,
        temperature: float = 0.9
    ) -> str:
        """Generate an NPC's response in a conversation."""
        
        if not self.ollama_available:
            # Fallback to simple templates if Ollama not available
            return self._template_response(speaker_name, listener_name, topic, is_start, is_end, history=conversation_history)
        
        system_prompt = self._build_conversation_prompt(
            speaker_name, listener_name, topic, conversation_history, is_start, is_end, context
        )
        
        # Build messages
        messages = [{"role": "system", "content": system_prompt}]
        
        # Add recent history as messages
        for exchange in conversation_history[-4:]:
            role = "assistant" if exchange.speaker == speaker_name else "user"
            messages.append({"role": role, "content": exchange.message})
        
        # If starting, add a minimal prompt
        if is_start:
            messages.append({"role": "user", "content": f"[Start a conversation with {listener_name}]"})
        elif not conversation_history:
            messages.append({"role": "user", "content": "[Say something]"})
        
        try:
            response = requests.post(
                self.api_url,
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "options": {
                        "temperature": temperature,
                        "num_predict": 150,
                    }
                },
                timeout=30
            )
            response.raise_for_status()
            result = response.json()
            return result['message']['content'].strip()
        except Exception as e:
            print(f"Error generating NPC response: {e}")
            return self._template_response(speaker_name, listener_name, topic, is_start, is_end, history=conversation_history)
    
    def _template_response(
        self,
        speaker_name: str,
        listener_name: str,
        topic: Optional[ConversationTopic],
        is_start: bool,
        is_end: bool,
        history: Optional[List[ConversationExchange]] = None
    ) -> str:
        """Fallback template responses when the LLM is unavailable.

        Deliberately **topic-aware**. A filler line that ignores the subject
        entirely ("Hmm, interesting point.") makes an offline run look broken
        rather than degraded — and this fallback is what every user without a
        model running will actually see.

        Lines already spoken in this conversation are filtered out, so a short
        exchange does not repeat itself.
        """
        spoken = {e.message for e in (history or [])}

        def pick(candidates: List[str]) -> str:
            fresh = [c for c in candidates if c not in spoken]
            return random.choice(fresh or candidates)

        if is_start:
            return pick([
                f"Hey there, {listener_name}.",
                f"Good to see you, {listener_name}.",
                f"Oh, {listener_name}. Just who I was hoping to run into.",
                f"{listener_name}. A word, if you have a moment.",
            ])

        if is_end:
            return pick([
                "Well, I should get going. Take care.",
                "I'd best be off. Until next time.",
                "Got to run. We'll catch up later.",
                "Enough for now. Don't repeat any of this.",
            ])

        subject = (topic.name if topic else "").lower()
        if subject:
            return pick([
                f"It comes back to {subject}, doesn't it.",
                f"I keep telling you — {subject} is not a small matter.",
                f"Say what you like. {subject.capitalize()} still doesn't add up.",
                f"We agree on {subject}, then. The rest we can argue about later.",
                f"You are asking me about {subject}, and I am telling you to leave it alone.",
                f"Nobody else is going to say it out loud, so I will: {subject}.",
            ])

        return pick([
            "Go on, then. I'm listening.",
            "That is not what you said last time.",
            "Keep your voice down and say that again.",
            "I don't like it either, but there it is.",
        ])


class ConversationManager:
    """
    Manages all NPC-to-NPC conversations in the game world.
    
    Features:
    - Create and track conversations
    - Proximity-based conversation triggers
    - Scheduled conversations
    - Event-driven conversations
    - Player overhearing
    - Conversation history
    """
    
    def __init__(
        self,
        npc_manager: NPCManager,
        conversation_engine: Optional[NPCConversationEngine] = None,
        relationship_tracker: Optional[RelationshipTracker] = None,
        lore_system: Optional[Any] = None,
        max_history_size: int = 500,
        ambient_chance: float = 0.1,
    ):
        self.npc_manager = npc_manager
        self.relationship_tracker = relationship_tracker
        self.lore_system = lore_system
        self.max_history_size = max_history_size

        #: 每次 proximity 检查时，两个 NPC 自行开聊的概率
        self.ambient_chance = ambient_chance

        #: 可选回调 (npc1, npc2) -> bool。返回 True 表示这一对**不要**开聊。
        #: 偷听系统用它来避让玩家正站在偷听点上的场景，见 eavesdrop.py。
        self.ambient_filter: Optional[Callable[[str, str], bool]] = None
        
        # Create engine if not provided
        self.engine = conversation_engine or NPCConversationEngine(
            npc_manager=npc_manager,
            relationship_tracker=relationship_tracker,
            lore_system=lore_system
        )
        
        # Active and completed conversations
        self.active_conversations: Dict[str, NPCConversation] = {}
        self.conversation_history: List[NPCConversation] = []
        
        # NPC location tracking for proximity
        self.npc_locations: Dict[str, str] = {}  # npc_name -> location_id
        
        # Callbacks
        self.on_conversation_start: Optional[Callable[[NPCConversation], None]] = None
        self.on_conversation_end: Optional[Callable[[NPCConversation], None]] = None
        self.on_exchange: Optional[Callable[[NPCConversation, ConversationExchange], None]] = None
        
        # Background task
        self._running = False
        self._task: Optional[asyncio.Task] = None
    
    def _generate_id(self) -> str:
        """Generate unique conversation ID."""
        return f"conv_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
    
    # ============================================
    # CONVERSATION MANAGEMENT
    # ============================================
    
    def start_conversation(
        self,
        npc1_name: str,
        npc2_name: str,
        trigger: ConversationTrigger = ConversationTrigger.FORCED,
        location: Optional[str] = None,
        max_turns: int = 6,
        context: Optional[Dict] = None,
        topic_id: Optional[str] = None
    ) -> NPCConversation:
        """Start a new conversation between two NPCs.

        Args:
            topic_id: 指定话题。传了就**不会**再随机抽取 —— 偷听点依赖这个，
                      因为随机话题会让玩家听到的多半是"今天天气不错"这类废话。
        """
        
        # Check if either NPC is already in conversation
        if self.is_npc_in_conversation(npc1_name):
            raise ValueError(f"{npc1_name} is already in a conversation")
        if self.is_npc_in_conversation(npc2_name):
            raise ValueError(f"{npc2_name} is already in a conversation")
        
        # Check both NPCs are loaded
        if npc1_name not in self.npc_manager.npcs:
            raise ValueError(f"NPC '{npc1_name}' not loaded")
        if npc2_name not in self.npc_manager.npcs:
            raise ValueError(f"NPC '{npc2_name}' not loaded")
        
        # Create conversation
        conversation = NPCConversation(
            conversation_id=self._generate_id(),
            npc1_name=npc1_name,
            npc2_name=npc2_name,
            trigger=trigger,
            state=ConversationState.STARTING,
            max_turns=max_turns,
            location=location or self.npc_locations.get(npc1_name),
            metadata=context or {}
        )

        # 指定话题：直接写进 current_topic，run_conversation_turn 就不会再去抽
        if topic_id:
            conversation.current_topic = topic_id
            if topic_id not in conversation.topics_discussed:
                conversation.topics_discussed.append(topic_id)

        self.active_conversations[conversation.conversation_id] = conversation
        
        # Trigger callback
        if self.on_conversation_start:
            self.on_conversation_start(conversation)
        
        return conversation
    
    def end_conversation(self, conversation_id: str, natural: bool = True) -> Optional[NPCConversation]:
        """End an active conversation."""
        
        conversation = self.active_conversations.get(conversation_id)
        if not conversation:
            return None
        
        conversation.state = ConversationState.ENDING
        conversation.ended_at = time.time()
        conversation.state = ConversationState.COMPLETED
        
        # Move to history
        self.conversation_history.append(conversation)
        del self.active_conversations[conversation_id]
        
        # Trim history
        if len(self.conversation_history) > self.max_history_size:
            self.conversation_history = self.conversation_history[-self.max_history_size:]
        
        # Trigger callback
        if self.on_conversation_end:
            self.on_conversation_end(conversation)
        
        return conversation
    
    def get_conversation(self, conversation_id: str) -> Optional[NPCConversation]:
        """Get a conversation by ID."""
        return self.active_conversations.get(conversation_id)
    
    def get_npc_conversation(self, npc_name: str) -> Optional[NPCConversation]:
        """Get the conversation an NPC is currently in."""
        for conv in self.active_conversations.values():
            if conv.npc1_name == npc_name or conv.npc2_name == npc_name:
                return conv
        return None
    
    def is_npc_in_conversation(self, npc_name: str) -> bool:
        """Check if an NPC is in a conversation."""
        return self.get_npc_conversation(npc_name) is not None
    
    def get_active_conversations(self, location: Optional[str] = None) -> List[NPCConversation]:
        """Get all active conversations, optionally filtered by location."""
        conversations = list(self.active_conversations.values())
        if location:
            conversations = [c for c in conversations if c.location == location]
        return conversations
    
    # ============================================
    # CONVERSATION EXECUTION
    # ============================================
    
    async def run_conversation_turn(self, conversation_id: str) -> Optional[ConversationExchange]:
        """Execute a single turn in a conversation."""
        
        conversation = self.active_conversations.get(conversation_id)
        if not conversation:
            return None
        
        if conversation.state not in [ConversationState.STARTING, ConversationState.ACTIVE]:
            return None
        
        # Initialize if starting
        if conversation.state == ConversationState.STARTING:
            conversation.started_at = time.time()
            conversation.state = ConversationState.ACTIVE
            conversation.current_speaker = conversation.npc1_name
        
        # Check if conversation should end
        if conversation.current_turn >= conversation.max_turns:
            # Generate closing exchange
            return await self._generate_closing_exchange(conversation)
        
        # Get relationship for topic selection
        relationship = self.engine._get_relationship(
            conversation.npc1_name, 
            conversation.npc2_name
        )
        
        # Select topic if needed
        if not conversation.current_topic:
            topic = self.engine.topic_registry.select_topic(
                conversation.npc1_name,
                conversation.npc2_name,
                relationship,
                conversation.topics_discussed,
                conversation.metadata
            )
            if topic:
                conversation.current_topic = topic.topic_id
        
        topic_obj = self.engine.topic_registry.topics.get(conversation.current_topic)
        
        # Determine speaker and listener
        speaker = conversation.current_speaker
        listener = conversation.npc2_name if speaker == conversation.npc1_name else conversation.npc1_name
        
        # Generate response
        is_start = conversation.current_turn == 0
        response = self.engine.generate_response(
            speaker_name=speaker,
            listener_name=listener,
            topic=topic_obj,
            conversation_history=conversation.exchanges,
            is_start=is_start,
            context=conversation.metadata
        )
        
        # Create exchange
        exchange = ConversationExchange(
            speaker=speaker,
            listener=listener,
            message=response,
            topic=conversation.current_topic
        )
        
        conversation.exchanges.append(exchange)
        conversation.current_turn += 1
        
        # Mark topic as discussed
        if conversation.current_topic and conversation.current_topic not in conversation.topics_discussed:
            conversation.topics_discussed.append(conversation.current_topic)
            self.engine.topic_registry.mark_topic_used(
                conversation.current_topic,
                conversation.npc1_name,
                conversation.npc2_name
            )
        
        # Switch speaker
        conversation.current_speaker = listener
        
        # Trigger callback
        if self.on_exchange:
            self.on_exchange(conversation, exchange)
        
        return exchange
    
    async def _generate_closing_exchange(self, conversation: NPCConversation) -> ConversationExchange:
        """Generate a closing exchange and end conversation."""
        
        speaker = conversation.current_speaker
        listener = conversation.npc2_name if speaker == conversation.npc1_name else conversation.npc1_name
        
        # Generate closing
        response = self.engine.generate_response(
            speaker_name=speaker,
            listener_name=listener,
            topic=None,
            conversation_history=conversation.exchanges,
            is_end=True
        )
        
        exchange = ConversationExchange(
            speaker=speaker,
            listener=listener,
            message=response,
            topic=None
        )
        
        conversation.exchanges.append(exchange)
        self.end_conversation(conversation.conversation_id)
        
        if self.on_exchange:
            self.on_exchange(conversation, exchange)
        
        return exchange
    
    async def run_full_conversation(
        self,
        npc1_name: str,
        npc2_name: str,
        trigger: ConversationTrigger = ConversationTrigger.FORCED,
        max_turns: int = 6,
        turn_delay: float = 2.0,
        location: Optional[str] = None,
        context: Optional[Dict] = None,
        topic_id: Optional[str] = None
    ) -> NPCConversation:
        """Run a complete conversation from start to finish.
        
        Args:
            topic_id: 指定话题，透传给 start_conversation（偷听点用）
        """
        
        conversation = self.start_conversation(
            npc1_name=npc1_name,
            npc2_name=npc2_name,
            trigger=trigger,
            location=location,
            max_turns=max_turns,
            context=context,
            topic_id=topic_id
        )
        
        conversation.turn_delay = turn_delay
        
        # Run all turns.
        #
        # NOTE: the loop must accept STARTING as well as ACTIVE. A freshly
        # started conversation is in STARTING; it only flips to ACTIVE inside
        # its first `run_conversation_turn`. Waiting for ACTIVE here meant the
        # loop body never executed and every conversation came back empty.
        guard = conversation.max_turns + 2
        while conversation.state in (ConversationState.STARTING, ConversationState.ACTIVE):
            await self.run_conversation_turn(conversation.conversation_id)
            guard -= 1
            if guard <= 0:
                # 兜底：万一某个 turn 没能把状态推到 COMPLETED，也不能死循环
                self.end_conversation(conversation.conversation_id, natural=False)
                break
            if conversation.state == ConversationState.ACTIVE:
                await asyncio.sleep(turn_delay)
        
        return conversation
    
    # ============================================
    # PROXIMITY-BASED CONVERSATIONS
    # ============================================
    
    def update_npc_location(self, npc_name: str, location: str):
        """Update an NPC's current location."""
        self.npc_locations[npc_name] = location
    
    def get_npcs_at_location(self, location: str) -> List[str]:
        """Get all NPCs at a specific location."""
        return [name for name, loc in self.npc_locations.items() if loc == location]
    
    async def check_proximity_conversations(self, location: Optional[str] = None):
        """Check for proximity-triggered conversations.

        这是"世界自己在运转"的那一半：玩家不介入时，同一区域的 NPC 有几率
        自己聊起来。概率由 `ambient_chance` 控制（默认每次检查 10%）。

        `ambient_filter` 回调用来避让玩家正在偷听的 NPC 对 —— 否则玩家走到
        偷听点上，这两个人可能已经自己聊上了，按钮会直接失效。
        """
        
        # Group NPCs by location
        locations_to_check = [location] if location else set(self.npc_locations.values())
        
        for loc in locations_to_check:
            npcs_here = self.get_npcs_at_location(loc)
            
            # Need at least 2 NPCs who aren't already conversing
            available = [n for n in npcs_here if not self.is_npc_in_conversation(n)]
            
            if len(available) < 2:
                continue
            
            # Random chance to start conversation
            if random.random() >= self.ambient_chance:
                continue
            
            # 候选组合里剔掉被偷听系统"占用"的配对
            pairs = list(itertools.combinations(available, 2))
            if self.ambient_filter:
                pairs = [p for p in pairs if not self.ambient_filter(p[0], p[1])]
            if not pairs:
                continue
            
            pair = random.choice(pairs)
            try:
                await self.run_full_conversation(
                    npc1_name=pair[0],
                    npc2_name=pair[1],
                    trigger=ConversationTrigger.PROXIMITY,
                    location=loc
                )
            except Exception as e:
                print(f"Error starting proximity conversation: {e}")
    
    # ============================================
    # PLAYER OVERHEARING
    # ============================================
    
    def get_overhearable_conversations(
        self, 
        player_location: str
    ) -> List[NPCConversation]:
        """Get conversations a player can overhear at their location."""
        return [
            conv for conv in self.active_conversations.values()
            if conv.location == player_location
        ]
    
    def add_player_listener(
        self, 
        conversation_id: str, 
        player_id: str
    ) -> bool:
        """Add a player as a listener to a conversation."""
        conversation = self.active_conversations.get(conversation_id)
        if not conversation:
            return False
        
        if player_id not in conversation.nearby_players:
            conversation.nearby_players.append(player_id)
        
        return True
    
    def remove_player_listener(
        self, 
        conversation_id: str, 
        player_id: str
    ) -> bool:
        """Remove a player listener from a conversation."""
        conversation = self.active_conversations.get(conversation_id)
        if not conversation:
            return False
        
        if player_id in conversation.nearby_players:
            conversation.nearby_players.remove(player_id)
        
        return True
    
    # ============================================
    # SCHEDULING
    # ============================================
    
    async def start_background_scheduler(self, check_interval: float = 30.0):
        """Start background task for scheduled/proximity conversations."""
        self._running = True
        self._task = asyncio.create_task(self._scheduler_loop(check_interval))
    
    async def stop_background_scheduler(self):
        """Stop the background scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
    
    async def _scheduler_loop(self, interval: float):
        """Background loop for checking scheduled events."""
        while self._running:
            try:
                await self.check_proximity_conversations()
            except Exception as e:
                print(f"Scheduler error: {e}")
            
            await asyncio.sleep(interval)
    
    # ============================================
    # SERIALIZATION
    # ============================================
    
    def save_history(self, filepath: str):
        """Save conversation history to file."""
        data = {
            "history": [c.to_dict() for c in self.conversation_history],
            "saved_at": time.time()
        }
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)
    
    def load_history(self, filepath: str):
        """Load conversation history from file."""
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
            
            # Reconstruct conversations (without full objects)
            self.conversation_history = []
            for cdata in data.get("history", []):
                conv = NPCConversation(
                    conversation_id=cdata["conversation_id"],
                    npc1_name=cdata["npc1_name"],
                    npc2_name=cdata["npc2_name"],
                    trigger=ConversationTrigger(cdata["trigger"]),
                    state=ConversationState.COMPLETED,
                    started_at=cdata.get("started_at"),
                    ended_at=cdata.get("ended_at"),
                    location=cdata.get("location"),
                )
                for ex in cdata.get("exchanges", []):
                    conv.exchanges.append(ConversationExchange(
                        speaker=ex["speaker"],
                        listener=ex["listener"],
                        message=ex["message"],
                        timestamp=ex.get("timestamp", time.time()),
                        emotion=ex.get("emotion", "neutral"),
                        topic=ex.get("topic")
                    ))
                self.conversation_history.append(conv)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Error loading conversation history: {e}")


# Convenience function for quick testing
async def demo_conversation(npc1_name: str = "Blacksmith", npc2_name: str = "Merchant"):
    """Demo function to show NPC-to-NPC conversation."""
    from pathlib import Path
    
    # Setup
    manager = NPCManager(model="llama3.2:1b")
    
    # Load characters
    cards_dir = Path("character_cards")
    manager.load_character(str(cards_dir / "blacksmith.json"))
    manager.load_character(str(cards_dir / "merchant.json"))
    
    # Create conversation manager
    conv_manager = ConversationManager(npc_manager=manager)
    
    # Run conversation
    print(f"\n{'='*60}")
    print(f"NPC-to-NPC Conversation: {npc1_name} & {npc2_name}")
    print(f"{'='*60}\n")
    
    conversation = await conv_manager.run_full_conversation(
        npc1_name=npc1_name,
        npc2_name=npc2_name,
        max_turns=6,
        turn_delay=1.0
    )
    
    print("\nConversation completed!")
    print(f"Duration: {conversation.get_duration():.1f}s")
    print(f"Exchanges: {len(conversation.exchanges)}")
    
    return conversation


if __name__ == "__main__":
    asyncio.run(demo_conversation())
