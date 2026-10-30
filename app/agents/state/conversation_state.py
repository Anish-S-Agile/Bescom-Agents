"""
Conversation State Manager for multi-turn parameter collection.
Tracks user intent, collected parameters, and conversation flow.
"""

from typing import Any, Dict, List, Optional, Set
from datetime import datetime
from enum import Enum
import logging
logger = logging.getLogger(__name__)

class ConversationStatus(Enum):
    """Status of the conversation flow."""
    INITIATED = "initiated"
    COLLECTING_PARAMS = "collecting_params"
    AWAITING_SELECTION = "awaiting_selection"  # Waiting for user to select from options 
    AWAITING_FILTER_SELECTION = "awaiting_filter_selection"  # Waiting for user to select BU/Env/Zone filter
    VALIDATING = "validating"
    READY_TO_EXECUTE = "ready_to_execute"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class ConversationState:
    """
    Manages the state of a multi-turn conversation for CRUD operations.
    Tracks intent, parameters, validation status, and execution flow.
    """
    def __init__(self, session_id: str, user_id: str):
        """
        Initialize conversation state.
        Args:
            session_id: Unique session identifier
            user_id: User identifier for permission checks
        """
        self.session_id = session_id
        self.user_id = user_id
        self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
        self.user_query: Optional[str] = None  # Store original query for context
        # Intent and operation tracking
        self.intent: Optional[str] = None 
        self.resource_type: Optional[str] = None 
        self.operation: Optional[str] = None  
        # Parameter collection
        self.required_params: Set[str] = set()
        self.optional_params: Set[str] = set()
        self.collected_params: Dict[str, Any] = {}
        self.missing_params: Set[str] = set()
        self.invalid_params: Dict[str, str] = {}  
        # Conversation flow
        self.status: ConversationStatus = ConversationStatus.INITIATED
        self.conversation_history: List[Dict[str, Any]] = []
        self.clarification_count: int = 0
        self.max_clarifications: int = 5
        # Execution tracking
        self.execution_result: Optional[Dict[str, Any]] = None
        self.error_message: Optional[str] = None
        # Agent tracking
        self.active_agent: Optional[str] = None
        self.agent_handoffs: List[Dict[str, str]] = []
        # Filter selection tracking (for BU/Environment/Zone filtering)
        self.pending_filter_options: Optional[List[Dict[str, Any]]] = None
        self.pending_filter_type: Optional[str] = None  # "bu", "environment", "zone"
        logger.info(f"✅ Created conversation state for session {session_id}, user {user_id}")
    
    def set_intent(self,resource_type: str,operation: str,required_params: List[str],optional_params: Optional[List[str]] = None) -> None:
        """
        Set the detected intent and initialize parameter tracking.
        Args:
            resource_type: Type of resource (k8s_cluster, firewall, etc.)
            operation: CRUD operation (create, read, update, delete, list)
            required_params: List of required parameter names
            optional_params: List of optional parameter names
        """
        self.resource_type = resource_type
        self.operation = operation
        self.intent = f"{operation}_{resource_type}"
        self.required_params = set(required_params)
        self.optional_params = set(optional_params or [])
        self.missing_params = self.required_params.copy()
        self.status = ConversationStatus.COLLECTING_PARAMS
        self.updated_at = datetime.utcnow()
        logger.info(
            f"📋 Intent set: {self.intent} | "
            f"Required: {self.required_params} | "
            f"Missing: {self.missing_params}")
    
    def add_parameter(self, param_name: str, param_value: Any, is_valid: bool = True) -> None:
        """
        Add a collected parameter to the state.
        Args:
            param_name: Name of the parameter
            param_value: Value of the parameter
            is_valid: Whether the parameter passed validation
        """
        if is_valid:
            self.collected_params[param_name] = param_value
            self.missing_params.discard(param_name)
            if param_name in self.invalid_params:
                del self.invalid_params[param_name]
            logger.info(f"✅ Parameter collected: {param_name} = {param_value}")
        else:
            self.invalid_params[param_name] = f"Invalid value: {param_value}"
            logger.warning(f"⚠️ Invalid parameter: {param_name} = {param_value}")
        self.updated_at = datetime.utcnow()
    
    def add_parameters(self, params: Dict[str, Any]) -> None:
        """
        Add multiple parameters at once. 
        Args:
            params: Dictionary of parameter name-value pairs
        """
        for param_name, param_value in params.items():
            self.add_parameter(param_name, param_value)
    
    def mark_parameter_invalid(self, param_name: str, error_message: str) -> None:
        """
        Mark a parameter as invalid with an error message.
        Args:
            param_name: Name of the invalid parameter
            error_message: Description of why it's invalid
        """
        self.invalid_params[param_name] = error_message
        if param_name in self.collected_params:
            del self.collected_params[param_name]
        self.missing_params.add(param_name)
        self.updated_at = datetime.utcnow()
        logger.warning(f"❌ Parameter marked invalid: {param_name} - {error_message}")
    
    def is_ready_to_execute(self) -> bool:
        """
        Check if all required parameters are collected and valid.
        Returns:
            True if ready to execute, False otherwise
        """
        ready = (
            len(self.missing_params) == 0 and
            len(self.invalid_params) == 0 and
            self.status != ConversationStatus.FAILED)
        if ready and self.status == ConversationStatus.COLLECTING_PARAMS:
            self.status = ConversationStatus.READY_TO_EXECUTE
            self.updated_at = datetime.utcnow()
            logger.info(f"✅ Conversation ready to execute: {self.intent}")
        
        return ready
    
    def get_missing_params_message(self) -> str:
        """
        Generate a user-friendly message about missing parameters.
        Returns:
            Message describing missing parameters
        """
        if not self.missing_params:
            return "All required parameters collected."
        
        missing_list = ", ".join(sorted(self.missing_params))
        return f"I still need the following information: {missing_list}"
    
    def get_invalid_params_message(self) -> str:
        """
        Generate a user-friendly message about invalid parameters.
        Returns:
            Message describing invalid parameters
        """
        if not self.invalid_params:
            return ""
        messages = []
        for param, error in self.invalid_params.items():
            messages.append(f"- {param}: {error}")
        return "Please correct the following:\n" + "\n".join(messages)
    
    def add_message(self,role: str,content: str,metadata: Optional[Dict[str, Any]] = None ) -> None:
        """
        Add a message to conversation history.
        Args:
            role: Message role (user, assistant, system)
            content: Message content
            metadata: Additional metadata
        """
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.utcnow().isoformat(),
            "metadata": metadata or {}}
        self.conversation_history.append(message)
        self.updated_at = datetime.utcnow()
    
    def add_clarification(self, question: str) -> bool:
        """
        Add a clarification question to the conversation.
        Args:
            question: Clarification question to ask user  
        Returns:
            True if clarification added, False if max clarifications reached
        """
        if self.clarification_count >= self.max_clarifications:
            logger.warning(f"⚠️ Max clarifications ({self.max_clarifications}) reached")
            return False
        
        self.clarification_count += 1
        self.add_message("assistant", question, {"type": "clarification"})
        return True
    
    def handoff_to_agent(self, from_agent: str, to_agent: str, reason: str) -> None:
        """
        Record an agent handoff.
        Args:
            from_agent: Agent handing off
            to_agent: Agent receiving handoff
            reason: Reason for handoff
        """
        handoff = {
            "from": from_agent,
            "to": to_agent,
            "reason": reason,
            "timestamp": datetime.utcnow().isoformat()}
        self.agent_handoffs.append(handoff)
        self.active_agent = to_agent
        self.updated_at = datetime.utcnow()
        logger.info(f"🔄 Agent handoff: {from_agent} -> {to_agent} ({reason})")
    
    def set_execution_result(self, result: Dict[str, Any]) -> None:
        """
        Set the execution result.
        Args:
            result: Execution result dictionary
        """
        self.execution_result = result
        self.status = ConversationStatus.COMPLETED if result.get("success") else ConversationStatus.FAILED
        self.updated_at = datetime.utcnow()
        if result.get("success"):
            logger.info(f"✅ Execution completed successfully: {self.intent}")
        else:
            errors = [str(r.get("error")) for r in (result.get("data") or {}).get("results", []) if r.get("error")]
            self.error_message = result.get("error") or "; ".join(errors) or "Unknown error"
            logger.error(f"❌ Execution failed: {self.error_message}")
    
    def cancel(self, reason: str = "User cancelled") -> None:
        """
        Cancel the conversation.
        Args:
            reason: Reason for cancellation
        """
        self.status = ConversationStatus.CANCELLED
        self.error_message = reason
        self.updated_at = datetime.utcnow()
        logger.info(f"🚫 Conversation cancelled: {reason}")
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert state to dictionary for serialization.
        Returns:
            Dictionary representation of state
        """
        # Build metadata for additional attributes
        metadata = {}
        if hasattr(self, 'last_asked_param'):
            metadata['last_asked_param'] = self.last_asked_param
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "user_query": self.user_query,
            "intent": self.intent,
            "resource_type": self.resource_type,
            "operation": self.operation,
            "status": self.status.value,
            "required_params": list(self.required_params),
            "optional_params": list(self.optional_params),
            "collected_params": self.collected_params,
            "missing_params": list(self.missing_params),
            "invalid_params": self.invalid_params,
            "conversation_history": self.conversation_history,
            "clarification_count": self.clarification_count,
            "execution_result": self.execution_result,
            "error_message": self.error_message,
            "active_agent": self.active_agent,
            "agent_handoffs": self.agent_handoffs,
            "pending_filter_options": self.pending_filter_options,
            "pending_filter_type": self.pending_filter_type,
            "metadata": metadata}
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationState":
        """
        Create ConversationState from dictionary.
        Args:
            data: Dictionary representation
        Returns:
            ConversationState instance
        """
        state = cls(data["session_id"], data["user_id"])
        # Handle datetime fields with both string and datetime types
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            state.created_at = datetime.fromisoformat(created_at)
        elif created_at:
            state.created_at = created_at
        updated_at = data.get("updated_at")
        if isinstance(updated_at, str):
            state.updated_at = datetime.fromisoformat(updated_at)
        elif updated_at:
            state.updated_at = updated_at
        state.user_query = data.get("user_query")
        state.intent = data.get("intent")
        state.resource_type = data.get("resource_type")
        state.operation = data.get("operation")
        # Handle status field
        status = data.get("status", "initiated")
        if isinstance(status, str):
            state.status = ConversationStatus(status)
        else:
            state.status = status 
        state.required_params = set(data.get("required_params", []))
        state.optional_params = set(data.get("optional_params", []))
        state.collected_params = data.get("collected_params", {})
        state.missing_params = set(data.get("missing_params", []))
        state.invalid_params = data.get("invalid_params", {})
        state.conversation_history = data.get("conversation_history", [])
        state.clarification_count = data.get("clarification_count", 0)
        state.execution_result = data.get("execution_result")
        state.error_message = data.get("error_message")
        state.active_agent = data.get("active_agent")
        state.agent_handoffs = data.get("agent_handoffs", [])
        
        # Restore filter selection state
        state.pending_filter_options = data.get("pending_filter_options")
        state.pending_filter_type = data.get("pending_filter_type")
        
        # Restore additional attributes from metadata
        metadata = data.get("metadata", {})
        if metadata.get("last_asked_param"):
            state.last_asked_param = metadata["last_asked_param"]
        return state
    
    def __repr__(self) -> str:
        return (
            f"<ConversationState(session={self.session_id}, "
            f"intent={self.intent}, status={self.status.value}, "
            f"missing={len(self.missing_params)})>")

class ConversationStateManager:
    """
    Manages multiple conversation states across sessions.
    Uses SQL-backed persistent storage via MemoriSessionManager for scalability.
    Features:
    - Sessions persist across server restarts
    - Multiple instances can share session state
    - Automatic session expiration and cleanup
    - Full audit trail of conversations
    """
    def __init__(self, use_persistence: bool = True):
        """
        Initialize the conversation state manager.
        Args:
            use_persistence: If True, use SQL-backed storage. If False, use in-memory only.
        """
        self.use_persistence = use_persistence
        # In-memory cache for fast access during active conversations
        self._states: Dict[str, ConversationState] = {}
        # Persistent storage backend
        if use_persistence:
            try:
                from app.agents.state.memori_session_manager import memori_session_manager
                self._persistent_store = memori_session_manager
                logger.info("✅ ConversationStateManager initialized with SQL persistence (Memori)")
            except Exception as e:
                logger.warning(f"⚠️ Failed to initialize persistent storage: {e}. Using in-memory only.")
                self._persistent_store = None
                self.use_persistence = False
        else:
            self._persistent_store = None
            logger.info("✅ ConversationStateManager initialized (in-memory only)")
    
    def _save_to_persistent(self, state: ConversationState) -> None:
        """Save state to persistent storage."""
        if self._persistent_store:
            try:
                state_dict = state.to_dict()
                self._persistent_store.save_state(state_dict)
            except Exception as e:
                logger.error(f"❌ Failed to persist state: {e}")
    
    def _load_from_persistent(self, session_id: str) -> Optional[ConversationState]:
        """Load state from persistent storage."""
        if not self._persistent_store:
            return None
        try:
            state_dict = self._persistent_store.load_state(session_id)
            if state_dict:
                state = ConversationState.from_dict(state_dict)
                if "user_query" in state_dict:
                    state.user_query = state_dict["user_query"]
                if "last_asked_param" in state_dict.get("metadata", {}):
                    state.last_asked_param = state_dict["metadata"]["last_asked_param"]
                return state
        except Exception as e:
            logger.error(f"❌ Failed to load persistent state: {e}")
        return None
    
    def create_session(self, session_id: str, user_id: str) -> ConversationState:
        """
        Create a new conversation session.
        Args:
            session_id: Unique session identifier
            user_id: User identifier 
        Returns:
            New ConversationState instance
        """
        # Check in-memory cache first
        if session_id in self._states:
            logger.warning(f"⚠️ Session {session_id} already exists in cache, returning existing")
            return self._states[session_id]
        # Check persistent storage
        if self.use_persistence:
            existing = self._load_from_persistent(session_id)
            if existing:
                self._states[session_id] = existing
                logger.info(f"📖 Restored session from persistent storage: {session_id}")
                return existing
        # Create new session
        state = ConversationState(session_id, user_id)
        self._states[session_id] = state
        # Persist immediately
        self._save_to_persistent(state)
        logger.info(f"✅ Created new session: {session_id}")
        return state
    
    def get_session(self, session_id: str) -> Optional[ConversationState]:
        """
        Get an existing conversation session.
        Args:
            session_id: Session identifier 
        Returns:
            ConversationState if exists, None otherwise
        """
        # Check in-memory cache first
        if session_id in self._states:
            return self._states[session_id]
        # Try to load from persistent storage
        if self.use_persistence:
            state = self._load_from_persistent(session_id)
            if state:
                self._states[session_id] = state
                logger.info(f"📖 Loaded session from persistent storage: {session_id}")
                return state
        return None
    
    def update_session(self, state: ConversationState) -> None:
        """
        Explicitly update/persist a session state.
        Call this after making changes to a session.
        Args:
            state: The conversation state to persist
        """
        self._states[state.session_id] = state
        self._save_to_persistent(state)
    
    def delete_session(self, session_id: str) -> bool:
        """
        Delete a conversation session.
        Args:
            session_id: Session identifier
        Returns:
            True if deleted, False if not found
        """
        deleted = False
        # Remove from in-memory cache
        if session_id in self._states:
            del self._states[session_id]
            deleted = True
        # Remove from persistent storage
        if self._persistent_store:
            if self._persistent_store.delete_state(session_id):
                deleted = True
        if deleted:
            logger.info(f"🗑️ Deleted session: {session_id}")
        return deleted
    
    def get_active_sessions(self) -> List[ConversationState]:
        """
        Get all active (non-completed) sessions.
        Returns:
            List of active ConversationState instances
        """
        # Get from persistent storage first
        if self._persistent_store:
            try:
                active_dicts = self._persistent_store.get_active_sessions()
                for state_dict in active_dicts:
                    session_id = state_dict["session_id"]
                    if session_id not in self._states:
                        self._states[session_id] = ConversationState.from_dict(state_dict)
            except Exception as e:
                logger.error(f"❌ Failed to get active sessions from storage: {e}")
        # Return from cache
        return [
            state for state in self._states.values()
            if state.status not in [ConversationStatus.COMPLETED, ConversationStatus.CANCELLED]]
    
    def cleanup_old_sessions(self, max_age_hours: int = 24) -> int:
        """
        Clean up old completed/cancelled sessions.
        Args:
            max_age_hours: Maximum age in hours before cleanup  
        Returns:
            Number of sessions cleaned up
        """
        count = 0
        # Clean up persistent storage
        if self._persistent_store:
            count += self._persistent_store.cleanup_old_completed_sessions(max_age_hours)
            count += self._persistent_store.cleanup_expired_sessions()
        # Clean up in-memory cache
        from datetime import timedelta
        cutoff_time = datetime.utcnow() - timedelta(hours=max_age_hours)
        sessions_to_delete = []
        for session_id, state in self._states.items():
            if (
                state.status in [ConversationStatus.COMPLETED, ConversationStatus.CANCELLED]
                and state.updated_at < cutoff_time):
                sessions_to_delete.append(session_id)
        for session_id in sessions_to_delete:
            del self._states[session_id]
        if sessions_to_delete:
            logger.info(f"🧹 Cleaned up {len(sessions_to_delete)} old sessions from cache")
        return count + len(sessions_to_delete)

    def get_user_sessions(self, user_id: str, limit: int = 10) -> List[ConversationState]:
        """
        Get all sessions for a specific user.
        Args:
            user_id: User identifier
            limit: Maximum number of sessions to return
        Returns:
            List of ConversationState instances for the user
        """
        if self._persistent_store:
            try:
                session_dicts = self._persistent_store.get_user_sessions(user_id, limit=limit)
                return [ConversationState.from_dict(d) for d in session_dicts]
            except Exception as e:
                logger.error(f"❌ Failed to get user sessions: {e}")
        # Fallback to in-memory
        return [s for s in self._states.values() if s.user_id == user_id][:limit]
    
    def get_most_recent_active_session(self, max_age_minutes: int = 5) -> Optional[ConversationState]:
        """
        Get the most recently updated session that is in COLLECTING_PARAMS status.
        Used as a fallback when session_id doesn't match but user seems to be responding to a question.
        Args:
            max_age_minutes: Maximum age of session to consider (default 5 minutes)
        Returns:
            Most recent active ConversationState if found, None otherwise
        """
        from datetime import timedelta
        cutoff_time = datetime.utcnow() - timedelta(minutes=max_age_minutes)
        # Check in-memory sessions first
        active_sessions = [
            s for s in self._states.values()
            if s.status == ConversationStatus.COLLECTING_PARAMS and s.updated_at >= cutoff_time]
        # Sort by most recent first
        if active_sessions:
            active_sessions.sort(key=lambda s: s.updated_at, reverse=True)
            logger.info(f"🔍 Found {len(active_sessions)} active sessions in memory, most recent: {active_sessions[0].session_id}")
            return active_sessions[0]
        # Try persistent storage if available
        if self._persistent_store:
            try:
                # Query for recent active sessions
                recent_dicts = self._persistent_store.get_recent_active_sessions(
                    max_age_minutes=max_age_minutes,
                    limit=1)
                if recent_dicts:
                    state = ConversationState.from_dict(recent_dicts[0])
                    # Cache it for future use
                    self._states[state.session_id] = state
                    logger.info(f"🔍 Found recent active session in DB: {state.session_id}")
                    return state
            except Exception as e:
                logger.error(f"❌ Failed to query recent active sessions: {e}")
        logger.info("🔍 No recent active sessions found")
        return None
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get session manager statistics.
        Returns:
            Dictionary with statistics
        """
        stats = {
            "cache_size": len(self._states),
            "persistence_enabled": self.use_persistence}
    
        if self._persistent_store:
            try:
                stats.update(self._persistent_store.get_session_stats())
            except Exception as e:
                logger.error(f"❌ Failed to get persistent stats: {e}") 
        return stats

# Global instance with persistence enabled
conversation_state_manager = ConversationStateManager(use_persistence=True)