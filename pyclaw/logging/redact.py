"""
Secret redaction for logging.

Provides pattern-based redaction of sensitive information like API keys,
tokens, passwords, and other secrets.
"""

import re
from typing import Any

import structlog
from structlog.types import EventDict, Processor


# Patterns to match various types of secrets
_SECRET_PATTERNS = [
    # OpenAI API keys
    re.compile(r'sk-[a-zA-Z0-9]{20,}'),
    # Anthropic API keys
    re.compile(r'sk-ant-[a-zA-Z0-9_-]{20,}'),
    # Bearer tokens
    re.compile(r'Bearer\s+[a-zA-Z0-9_-]{20,}', re.IGNORECASE),
    # AWS access keys
    re.compile(r'AKIA[0-9A-Z]{16}'),
    # AWS secret keys (when paired with access key)
    re.compile(r'[a-zA-Z0-9/+]{40}'),
    # Private key blocks
    re.compile(r'-----BEGIN [A-Z]+ PRIVATE KEY-----.*?-----END [A-Z]+ PRIVATE KEY-----', re.DOTALL),
    # Password in URL
    re.compile(r'://[^:]+:[^@]+@', re.IGNORECASE),
    # Generic secret assignments (password, token, secret, key)
    re.compile(r'["\']?(password|token|secret|api_key|apikey|access_key)["\']?\s*[:=]\s*["\']?([^"\'>\s,}]{8,})["\']?', re.IGNORECASE),
]


def redact_secrets(text: str) -> str:
    """
    Redact secrets from text using pattern matching.
    
    Args:
        text: Text that may contain secrets
    
    Returns:
        Text with secrets replaced by [REDACTED]
    """
    if not isinstance(text, str):
        return text
    
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub('[REDACTED]', redacted)
    
    return redacted


class SecretRedactingProcessor:
    """
    Structlog processor that redacts secrets from log messages.
    
    Redacts the 'event' field and any string values in extra keys.
    """
    
    def __call__(self, logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
        """
        Process event dict to redact secrets.
        
        Args:
            logger: Logger instance (unused)
            method_name: Method name (unused)
            event_dict: Event dictionary to process
        
        Returns:
            Event dictionary with secrets redacted
        """
        # Redact the event message
        if 'event' in event_dict and isinstance(event_dict['event'], str):
            event_dict['event'] = redact_secrets(event_dict['event'])
        
        # Redact all string values in extra keys
        for key, value in event_dict.items():
            if isinstance(value, str) and key != 'event':
                event_dict[key] = redact_secrets(value)
        
        return event_dict


def redact_sensitive_data(data: dict[str, Any]) -> dict[str, Any]:
    """
    Recursively redact sensitive data in dictionary.
    
    Args:
        data: Dictionary to redact
    
    Returns:
        Redacted dictionary
    """
    if not isinstance(data, dict):
        return data
    
    result = {}
    for key, value in data.items():
        if isinstance(value, dict):
            result[key] = redact_sensitive_data(value)
        elif isinstance(value, str):
            result[key] = redact_secrets(value)
        elif isinstance(value, list):
            result[key] = [redact_sensitive_data(item) if isinstance(item, dict) else redact_secrets(item) if isinstance(item, str) else item for item in value]
        else:
            result[key] = value
    
    return result