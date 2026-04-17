"""
Model failover module for handling LLM failures and automatic fallback.

Inspired by OpenClaw's failover-error.ts implementation:
- Classifies errors into different failover categories
- Manages cooldown periods for failed providers
- Provides failover chain logic
"""

from enum import Enum
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timedelta
import re
import logging

from pyclaw.constants import (
    HTTP_401_UNAUTHORIZED,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_503_SERVICE_UNAVAILABLE,
)

logger = logging.getLogger(__name__)


class FailoverStatus(Enum):
    """Failure classification for determining failover strategy."""
    
    NO_FAILOVER = "no_failover"           # No failover needed, retry or surface error
    BILLING_ERROR = "billing"              # Payment/quota issues
    RATE_LIMITED = "rate_limited"          # Too many requests
    AUTH_FAILED = "auth_failed"            # Authentication/authorization failed
    MODEL_UNAVAILABLE = "model_unavailable"  # Model not available or overloaded
    CONTEXT_TOO_LONG = "context_too_long"  # Context length exceeded
    NETWORK_ERROR = "network_error"        # Network connectivity issues
    TIMEOUT = "timeout"                    # Request timed out
    SERVER_ERROR = "server_error"          # 5xx server errors


# Error patterns for classification
ERROR_PATTERNS: Dict[FailoverStatus, List[str]] = {
    FailoverStatus.BILLING_ERROR: [
        r"insufficient.*quota",
        r"billing.*issue",
        r"payment.*required",
        r"credit.*exhausted",
        r"rate limit.*exceeded.*quota",
        r"insufficient_funds",
        r"account.*suspended",
    ],
    FailoverStatus.RATE_LIMITED: [
        r"rate.*limit",
        r"too many requests",
        r"throttl",
        r"retry.*after",
        r"overloaded",
    ],
    FailoverStatus.AUTH_FAILED: [
        r"invalid.*api.*key",
        r"unauthorized",
        r"403",
        r"authentication.*failed",
        r"invalid.*token",
        r"access.*denied",
        r"permission.*denied",
    ],
    FailoverStatus.MODEL_UNAVAILABLE: [
        r"model.*not.*found",
        r"model.*unavailable",
        r"does not exist",
        r"not.*supported",
        r"model.*overloaded",
        r"capacity",
    ],
    FailoverStatus.CONTEXT_TOO_LONG: [
        r"context.*length",
        r"max.*tokens",
        r"too.*long",
        r"maximum.*context",
        r"token.*limit",
        r"input.*too.*large",
    ],
    FailoverStatus.NETWORK_ERROR: [
        r"connection.*refused",
        r"network.*error",
        r"dns.*resolution",
        r"could not connect",
        r"connection.*reset",
        r"socket.*error",
    ],
    FailoverStatus.TIMEOUT: [
        r"timeout",
        r"timed.*out",
        r"deadline.*exceeded",
    ],
    FailoverStatus.SERVER_ERROR: [
        r"502",
        r"internal.*server.*error",
        r"bad.*gateway",
        r"service.*unavailable",
    ],
}


def resolve_failover_status(error: Exception) -> FailoverStatus:
    """
    Analyze an error and return the appropriate failover status.
    
    Args:
        error: The exception that occurred
        
    Returns:
        FailoverStatus indicating the type of failure
    """
    error_str = str(error).lower()
    error_type = type(error).__name__.lower()
    
    # Check each pattern category
    for status, patterns in ERROR_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, error_str, re.IGNORECASE):
                logger.debug("Error matched pattern '%s' -> %s", pattern, status)
                return status
            if re.search(pattern, error_type, re.IGNORECASE):
                logger.debug("Error type matched pattern '%s' -> %s", pattern, status)
                return status
    
    # Check for common HTTP status codes
    if hasattr(error, 'status_code'):
        status_code = error.status_code
        if status_code == 401 or status_code == 403:
            return FailoverStatus.AUTH_FAILED
        elif status_code == 429:
            return FailoverStatus.RATE_LIMITED
        elif status_code == 503:
            return FailoverStatus.MODEL_UNAVAILABLE
        elif status_code >= 500:
            return FailoverStatus.SERVER_ERROR
    
    # Default: no failover (let the error bubble up)
    return FailoverStatus.NO_FAILOVER


def should_failover(status: FailoverStatus) -> bool:
    """
    Determine if we should attempt failover to another model/provider.
    
    Args:
        status: The failover status
        
    Returns:
        True if failover should be attempted
    """
    # These errors warrant trying a different model/provider
    failover_statuses = {
        FailoverStatus.BILLING_ERROR,
        FailoverStatus.RATE_LIMITED,
        FailoverStatus.AUTH_FAILED,
        FailoverStatus.MODEL_UNAVAILABLE,
        FailoverStatus.SERVER_ERROR,
    }
    return status in failover_statuses


def should_retry(status: FailoverStatus) -> bool:
    """
    Determine if we should retry the same model/provider.
    
    Args:
        status: The failover status
        
    Returns:
        True if retry should be attempted
    """
    # These errors might be transient, worth retrying
    retry_statuses = {
        FailoverStatus.RATE_LIMITED,
        FailoverStatus.NETWORK_ERROR,
        FailoverStatus.TIMEOUT,
        FailoverStatus.SERVER_ERROR,
    }
    return status in retry_statuses


def get_retry_delay(status: FailoverStatus, attempt: int) -> float:
    """
    Get the recommended retry delay in seconds.
    
    Args:
        status: The failover status
        attempt: The retry attempt number (0-indexed)
        
    Returns:
        Delay in seconds before retrying
    """
    # Base delays for different error types
    base_delays = {
        FailoverStatus.RATE_LIMITED: 5.0,  # Rate limits need longer waits
        FailoverStatus.NETWORK_ERROR: 1.0,
        FailoverStatus.TIMEOUT: 2.0,
        FailoverStatus.SERVER_ERROR: 3.0,
    }
    
    base_delay = base_delays.get(status, 1.0)
    
    # Exponential backoff with jitter
    import random
    delay = base_delay * (2 ** attempt)
    jitter = delay * 0.2 * random.random()
    
    # Cap at 60 seconds
    return min(delay + jitter, 60.0)


def get_cooldown_duration(status: FailoverStatus) -> timedelta:
    """
    Get the recommended cooldown duration for a failed provider.
    
    Args:
        status: The failover status
        
    Returns:
        Cooldown duration
    """
    # Different cooldown periods based on failure type
    cooldowns = {
        FailoverStatus.BILLING_ERROR: timedelta(hours=1),
        FailoverStatus.AUTH_FAILED: timedelta(minutes=30),
        FailoverStatus.RATE_LIMITED: timedelta(minutes=5),
        FailoverStatus.MODEL_UNAVAILABLE: timedelta(minutes=10),
        FailoverStatus.SERVER_ERROR: timedelta(minutes=5),
    }
    return cooldowns.get(status, timedelta(minutes=5))


class ModelFailoverChain:
    """
    Manages a chain of models for failover.
    
    When a model fails, automatically tries the next model in the chain.
    Tracks cooldown periods and failure counts.
    """
    
    def __init__(
        self,
        models: List['ModelConfig'],
        max_retries: int = 2,
    ):
        """
        Initialize the failover chain.
        
        Args:
            models: List of ModelConfig in priority order
            max_retries: Maximum retries per model before failing over
        """
        self.models = models
        self.max_retries = max_retries
        
        # Track cooldowns: model_name -> cooldown_until
        self._cooldowns: Dict[str, datetime] = {}
        
        # Track failure counts: model_name -> count
        self._failure_counts: Dict[str, int] = {}
        
        # Track current model index
        self._current_index = 0
    
    def get_current_model(self) -> Optional['ModelConfig']:
        """
        Get the current active model, respecting cooldowns.
        
        Returns:
            The current model or None if all models are in cooldown
        """
        now = datetime.now()
        
        # Try to find an available model starting from current index
        for i in range(len(self.models)):
            idx = (self._current_index + i) % len(self.models)
            model = self.models[idx]
            
            # Check cooldown
            cooldown_until = self._cooldowns.get(model.name)
            if cooldown_until and now < cooldown_until:
                continue  # Model is in cooldown
            
            self._current_index = idx
            return model
        
        # All models in cooldown, return the one with earliest expiry
        earliest_model = None
        earliest_time = None
        
        for model in self.models:
            cooldown_until = self._cooldowns.get(model.name, datetime.min)
            if earliest_time is None or cooldown_until < earliest_time:
                earliest_time = cooldown_until
                earliest_model = model
        
        return earliest_model
    
    def mark_failure(
        self,
        model_name: str,
        status: FailoverStatus,
    ) -> Tuple[bool, Optional['ModelConfig']]:
        """
        Mark a model as failed and determine next action.
        
        Args:
            model_name: Name of the failed model
            status: The failure status
            
        Returns:
            Tuple of (should_failover, next_model)
        """
        # Increment failure count
        self._failure_counts[model_name] = self._failure_counts.get(model_name, 0) + 1
        
        if should_failover(status):
            # Set cooldown for this model
            cooldown = get_cooldown_duration(status)
            self._cooldowns[model_name] = datetime.now() + cooldown
            
            logger.info(
                f"Model {model_name} marked as failed ({status.value}), "
                f"cooldown for {cooldown}"
            )
            
            # Move to next model
            self._current_index = (self._current_index + 1) % len(self.models)
            next_model = self.get_current_model()
            
            return True, next_model
        
        elif should_retry(status):
            # Check if we've exceeded max retries
            if self._failure_counts[model_name] >= self.max_retries:
                # Failover to next model
                self._cooldowns[model_name] = datetime.now() + get_cooldown_duration(status)
                self._current_index = (self._current_index + 1) % len(self.models)
                next_model = self.get_current_model()
                return True, next_model
            
            # Retry same model
            return False, self.models[self._current_index]
        
        # No failover, error should bubble up
        return False, None
    
    def mark_success(self, model_name: str) -> None:
        """
        Mark a model as successful, resetting failure count.
        
        Args:
            model_name: Name of the successful model
        """
        self._failure_counts[model_name] = 0
        
        # Clear cooldown if set
        if model_name in self._cooldowns:
            del self._cooldowns[model_name]
    
    def get_cooldown_status(self) -> Dict[str, Any]:
        """
        Get the current cooldown status for all models.
        
        Returns:
            Dict with cooldown information
        """
        now = datetime.now()
        status = {}
        
        for model in self.models:
            cooldown_until = self._cooldowns.get(model.name)
            if cooldown_until:
                remaining = (cooldown_until - now).total_seconds()
                status[model.name] = {
                    "in_cooldown": remaining > 0,
                    "cooldown_until": cooldown_until.isoformat(),
                    "remaining_seconds": max(0, remaining),
                    "failure_count": self._failure_counts.get(model.name, 0),
                }
            else:
                status[model.name] = {
                    "in_cooldown": False,
                    "cooldown_until": None,
                    "remaining_seconds": 0,
                    "failure_count": self._failure_counts.get(model.name, 0),
                }
        
        return status
    
    def reset(self) -> None:
        """Reset all cooldowns and failure counts."""
        self._cooldowns.clear()
        self._failure_counts.clear()
        self._current_index = 0
