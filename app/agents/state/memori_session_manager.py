"""
Memori-backed Session Manager for persistent conversation state.
Uses SQL database (SQLite/PostgreSQL) for scalable session storage.

Key benefits:
- Sessions persist across server restarts
- Multiple instances can share state
- Queryable and auditable session history
- Automatic memory management and context retrieval
"""

import logging
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
from sqlalchemy import create_engine, Column, String, Text, DateTime, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool
logger = logging.getLogger(__name__)

SessionBase = declarative_base()

class ConversationSessionRecord(SessionBase):
    """
    SQLAlchemy model for storing conversation sessions.
    This provides queryable, persistent storage for multi-turn conversations.
    """
    __tablename__ = "conversation_sessions"
    session_id = Column(String(64), primary_key=True, index=True)
    user_id = Column(String(128), index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    # Intent and operation
    intent = Column(String(128), nullable=True)
    resource_type = Column(String(64), nullable=True)
    operation = Column(String(32), nullable=True)
    user_query = Column(Text, nullable=True)
    # Status
    status = Column(String(32), default="initiated")
    active_agent = Column(String(64), nullable=True)
    # Parameters - stored as JSON for flexibility
    required_params = Column(JSON, default=list)
    optional_params = Column(JSON, default=list)
    collected_params = Column(JSON, default=dict)
    missing_params = Column(JSON, default=list)
    invalid_params = Column(JSON, default=dict)
    # Conversation tracking
    conversation_history = Column(JSON, default=list)
    clarification_count = Column(String(8), default="0")
    max_clarifications = Column(String(8), default="5")
    # Execution tracking
    execution_result = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    # Agent handoffs
    agent_handoffs = Column(JSON, default=list)
    # Additional metadata stored as JSON for flexibility (named extra_data to avoid SQLAlchemy reserved word)
    extra_data = Column(JSON, default=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert record to dictionary for ConversationState reconstruction."""
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "intent": self.intent,
            "resource_type": self.resource_type,
            "operation": self.operation,
            "user_query": self.user_query,
            "status": self.status,
            "active_agent": self.active_agent,
            "required_params": self.required_params or [],
            "optional_params": self.optional_params or [],
            "collected_params": self.collected_params or {},
            "missing_params": self.missing_params or [],
            "invalid_params": self.invalid_params or {},
            "conversation_history": self.conversation_history or [],
            "clarification_count": int(self.clarification_count) if self.clarification_count else 0,
            "max_clarifications": int(self.max_clarifications) if self.max_clarifications else 5,
            "execution_result": self.execution_result,
            "error_message": self.error_message,
            "agent_handoffs": self.agent_handoffs or [],
            "metadata": self.extra_data or {}}

class MemoriSessionManager:
    """
    Persistent session manager using SQL database.
    Provides the same interface as ConversationStateManager but with 
    persistent storage that survives server restarts and scales across
    multiple instances.
    Features:
    - Automatic session expiration
    - Queryable session history
    - Full audit trail
    - Support for SQLite (development) and PostgreSQL (production)
    """
    def __init__(self,database_url: Optional[str] = None,session_ttl_hours: int = 24,auto_cleanup: bool = True,engine=None):
        """
        Initialize the Memori session manager.
        Args:
            database_url: SQL connection string (optional). By default the shared agent-data engine is used.
            session_ttl_hours: Session time-to-live in hours
            auto_cleanup: Whether to automatically cleanup expired sessions
        """
        # Default: share the agent-data engine (real PostgreSQL, or the fake postgres when it is unreachable).
        if engine is None and database_url is None:
            from app.services import fake_postgres
            engine = fake_postgres.get_engine()
        if engine is not None:
            self.engine = engine
            self.database_url = engine.url.render_as_string(hide_password=False)
        else:
            self.database_url = database_url
            if self.database_url.startswith("sqlite"):
                self.engine = create_engine(
                    self.database_url,
                    connect_args={"check_same_thread": False},
                    poolclass=StaticPool)
            else:
                self.engine = create_engine(
                    self.database_url,
                    pool_size=5,
                    max_overflow=10,
                    pool_pre_ping=True)
        self.session_ttl_hours = session_ttl_hours
        self.auto_cleanup = auto_cleanup
        # Create session factory
        self.SessionFactory = sessionmaker(bind=self.engine)
        # Create tables
        SessionBase.metadata.create_all(self.engine)
        # In-memory cache for active sessions (hot cache)
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ttl_seconds = 300  # 5 minute cache
        logger.info(f"✅ MemoriSessionManager initialized with database: {self._mask_connection_string()}")
    
    def _mask_connection_string(self) -> str:
        """Mask sensitive parts of connection string for logging."""
        if "@" in self.database_url:
            parts = self.database_url.split("@")
            return parts[0].split(":")[0] + "://*****@" + parts[1]
        return self.database_url
    
    def _get_session(self) -> Session:
        """Get a database session."""
        return self.SessionFactory()
    
    def save_state(self, state_dict: Dict[str, Any]) -> bool:
        """
        Save or update a conversation state to the database.
        Args:
            state_dict: Dictionary representation of ConversationState   
        Returns:
            True if saved successfully
        """
        session_id = state_dict.get("session_id")
        if not session_id:
            logger.error("Cannot save state without session_id")
            return False
        
        db = self._get_session()
        try:
            # Check if record exists
            record = db.query(ConversationSessionRecord).filter_by(
                session_id=session_id).first()
            now = datetime.utcnow()
            expires_at = now + timedelta(hours=self.session_ttl_hours)
            if record:
                # Update existing record
                record.updated_at = now
                record.expires_at = expires_at
                record.user_id = state_dict.get("user_id", record.user_id)
                record.intent = state_dict.get("intent")
                record.resource_type = state_dict.get("resource_type")
                record.operation = state_dict.get("operation")
                record.user_query = state_dict.get("user_query")
                record.status = state_dict.get("status", "initiated")
                record.active_agent = state_dict.get("active_agent")
                record.required_params = state_dict.get("required_params", [])
                record.optional_params = state_dict.get("optional_params", [])
                record.collected_params = state_dict.get("collected_params", {})
                record.missing_params = state_dict.get("missing_params", [])
                record.invalid_params = state_dict.get("invalid_params", {})
                record.conversation_history = state_dict.get("conversation_history", [])
                record.clarification_count = str(state_dict.get("clarification_count", 0))
                record.max_clarifications = str(state_dict.get("max_clarifications", 5))
                record.execution_result = state_dict.get("execution_result")
                record.error_message = state_dict.get("error_message")
                record.agent_handoffs = state_dict.get("agent_handoffs", [])
                record.extra_data = state_dict.get("metadata", {})
            else:
                # Create new record
                record = ConversationSessionRecord(
                    session_id=session_id,
                    user_id=state_dict.get("user_id", "anonymous"),
                    created_at=now,
                    updated_at=now,
                    expires_at=expires_at,
                    intent=state_dict.get("intent"),
                    resource_type=state_dict.get("resource_type"),
                    operation=state_dict.get("operation"),
                    user_query=state_dict.get("user_query"),
                    status=state_dict.get("status", "initiated"),
                    active_agent=state_dict.get("active_agent"),
                    required_params=state_dict.get("required_params", []),
                    optional_params=state_dict.get("optional_params", []),
                    collected_params=state_dict.get("collected_params", {}),
                    missing_params=state_dict.get("missing_params", []),
                    invalid_params=state_dict.get("invalid_params", {}),
                    conversation_history=state_dict.get("conversation_history", []),
                    clarification_count=str(state_dict.get("clarification_count", 0)),
                    max_clarifications=str(state_dict.get("max_clarifications", 5)),
                    execution_result=state_dict.get("execution_result"),
                    error_message=state_dict.get("error_message"),
                    agent_handoffs=state_dict.get("agent_handoffs", []),
                    extra_data=state_dict.get("metadata", {}))
                db.add(record)
            db.commit()
            # Update cache
            self._cache[session_id] = {"data": record.to_dict(),"cached_at": datetime.utcnow()}
            logger.debug(f"💾 Saved session state: {session_id}")
            return True
        except Exception as e:
            db.rollback()
            logger.error(f"❌ Failed to save session state: {str(e)}")
            return False
        finally:
            db.close()

    def load_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Load a conversation state from the database.
        Args:
            session_id: Session identifier
        Returns:
            Dictionary representation of state, or None if not found
        """
        # Check cache first
        if session_id in self._cache:
            cache_entry = self._cache[session_id]
            cache_age = (datetime.utcnow() - cache_entry["cached_at"]).total_seconds()
            if cache_age < self._cache_ttl_seconds:
                logger.debug(f"📦 Cache hit for session: {session_id}")
                return cache_entry["data"]
        db = self._get_session()
        try:
            record = db.query(ConversationSessionRecord).filter_by(
                session_id=session_id).first()
            if not record:
                logger.debug(f"🔍 Session not found: {session_id}")
                return None
            # Check if expired
            if record.expires_at and record.expires_at < datetime.utcnow():
                logger.info(f"⏰ Session expired: {session_id}")
                self.delete_state(session_id)
                return None
            state_dict = record.to_dict()
            # Update cache
            self._cache[session_id] = {
                "data": state_dict,
                "cached_at": datetime.utcnow()}
            logger.debug(f"📖 Loaded session state: {session_id}")
            return state_dict
        except Exception as e:
            logger.error(f"❌ Failed to load session state: {str(e)}")
            return None
        finally:
            db.close()

    def delete_state(self, session_id: str) -> bool:
        """
        Delete a conversation state from the database.
        Args:
            session_id: Session identifier 
        Returns:
            True if deleted, False if not found
        """
        # Remove from cache
        self._cache.pop(session_id, None)
        db = self._get_session()
        try:
            result = db.query(ConversationSessionRecord).filter_by(
                session_id=session_id).delete()
            db.commit()
            
            if result > 0:
                logger.info(f"🗑️ Deleted session: {session_id}")
                return True
            return False
        except Exception as e:
            db.rollback()
            logger.error(f"❌ Failed to delete session: {str(e)}")
            return False
        finally:
            db.close()
    
    def get_user_sessions(self, user_id: str, status: Optional[str] = None,limit: int = 10) -> List[Dict[str, Any]]:
        """
        Get all sessions for a user.
        Args:
            user_id: User identifier
            status: Optional status filter
            limit: Maximum number of sessions to return
        Returns:
            List of session dictionaries
        """
        db = self._get_session()
        try:
            query = db.query(ConversationSessionRecord).filter_by(user_id=user_id)
            if status:
                query = query.filter_by(status=status)
            query = query.order_by(ConversationSessionRecord.updated_at.desc())
            query = query.limit(limit)
            records = query.all()
            return [r.to_dict() for r in records]
        except Exception as e:
            logger.error(f"❌ Failed to get user sessions: {str(e)}")
            return []
        finally:
            db.close()
    
    def get_active_sessions(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get all active (non-completed/cancelled) sessions.
        Returns:
            List of active session dictionaries
        """
        db = self._get_session()
        try:
            records = db.query(ConversationSessionRecord).filter(
                ConversationSessionRecord.status.notin_(["completed", "cancelled", "failed"])
            ).order_by(
                ConversationSessionRecord.updated_at.desc()
            ).limit(limit).all()
            return [r.to_dict() for r in records]
        except Exception as e:
            logger.error(f"❌ Failed to get active sessions: {str(e)}")
            return []
        finally:
            db.close()
    
    def cleanup_expired_sessions(self) -> int:
        """
        Remove expired sessions from the database.
        Returns:
            Number of sessions cleaned up
        """
        db = self._get_session()
        try:
            now = datetime.utcnow()
            result = db.query(ConversationSessionRecord).filter(
                ConversationSessionRecord.expires_at < now).delete()
            db.commit()
            if result > 0:
                logger.info(f"🧹 Cleaned up {result} expired sessions")
            return result
        except Exception as e:
            db.rollback()
            logger.error(f"❌ Failed to cleanup expired sessions: {str(e)}")
            return 0
        finally:
            db.close()
    
    def cleanup_old_completed_sessions(self, max_age_hours: int = 24) -> int:
        """
        Clean up old completed/cancelled sessions.
        Args:
            max_age_hours: Maximum age in hours before cleanup
        Returns:
            Number of sessions cleaned up
        """
        db = self._get_session()
        try:
            cutoff_time = datetime.utcnow() - timedelta(hours=max_age_hours)
            result = db.query(ConversationSessionRecord).filter(
                ConversationSessionRecord.status.in_(["completed", "cancelled", "failed"]),
                ConversationSessionRecord.updated_at < cutoff_time
            ).delete()
            db.commit()
            if result > 0:
                logger.info(f"🧹 Cleaned up {result} old completed sessions")
            return result
        except Exception as e:
            db.rollback()
            logger.error(f"❌ Failed to cleanup old sessions: {str(e)}")
            return 0
        finally:
            db.close()
    
    def get_session_stats(self) -> Dict[str, Any]:
        """
        Get statistics about sessions in the database.
        Returns:
            Dictionary with session statistics
        """
        db = self._get_session()
        try:
            from sqlalchemy import func
            total = db.query(func.count(ConversationSessionRecord.session_id)).scalar()
            status_counts = dict(
                db.query(
                    ConversationSessionRecord.status,
                    func.count(ConversationSessionRecord.session_id)
                ).group_by(ConversationSessionRecord.status).all())
            # Get recent activity
            hour_ago = datetime.utcnow() - timedelta(hours=1)
            recent_active = db.query(func.count(ConversationSessionRecord.session_id)).filter(
                ConversationSessionRecord.updated_at > hour_ago
            ).scalar()
            return {
                "total_sessions": total,
                "status_breakdown": status_counts,
                "active_last_hour": recent_active,
                "cache_size": len(self._cache)}
        except Exception as e:
            logger.error(f"❌ Failed to get session stats: {str(e)}")
            return {}
        finally:
            db.close()
    def search_sessions(self,resource_type: Optional[str] = None,operation: Optional[str] = None,status: Optional[str] = None,user_id: Optional[str] = None,limit: int = 50) -> List[Dict[str, Any]]:
        """
        Search sessions with various filters.
        Args:
            resource_type: Filter by resource type (e.g., "k8s_cluster")
            operation: Filter by operation (e.g., "create")
            status: Filter by status
            user_id: Filter by user
            limit: Maximum results
        Returns:
            List of matching session dictionaries
        """
        db = self._get_session()
        try:
            query = db.query(ConversationSessionRecord)
            if resource_type:
                query = query.filter_by(resource_type=resource_type)
            if operation:
                query = query.filter_by(operation=operation)
            if status:
                query = query.filter_by(status=status)
            if user_id:
                query = query.filter_by(user_id=user_id)
            query = query.order_by(ConversationSessionRecord.updated_at.desc())
            query = query.limit(limit)
            records = query.all()
            return [r.to_dict() for r in records]
        except Exception as e:
            logger.error(f"❌ Failed to search sessions: {str(e)}")
            return []
        finally:
            db.close()
    
    def get_recent_active_sessions(self,max_age_minutes: int = 5,limit: int = 10) -> List[Dict[str, Any]]:
        """
        Get recently updated sessions that are in COLLECTING_PARAMS status.
        Used to find active conversations for session recovery.
        Args:
            max_age_minutes: Maximum age of sessions to return
            limit: Maximum number of sessions to return
        Returns:
            List of recent active session dictionaries, sorted by most recent first
        """
        db = self._get_session()
        try:
            cutoff_time = datetime.utcnow() - timedelta(minutes=max_age_minutes)
            records = db.query(ConversationSessionRecord).filter(
                ConversationSessionRecord.status == "collecting_params",
                ConversationSessionRecord.updated_at >= cutoff_time
            ).order_by(
                ConversationSessionRecord.updated_at.desc()
            ).limit(limit).all()
            result = [r.to_dict() for r in records]
            logger.debug(f"🔍 Found {len(result)} recent active sessions within {max_age_minutes} minutes")
            return result
        except Exception as e:
            logger.error(f"❌ Failed to get recent active sessions: {str(e)}")
            return []
        finally:
            db.close()
# Global instance
memori_session_manager = MemoriSessionManager()