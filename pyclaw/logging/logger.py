"""
Logging configuration using structlog.

Provides centralized logging setup with console and file output,
secret redaction, and proper integration with stdlib logging.
"""

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

import structlog
from structlog.types import Processor


_CONFIGURED = False


def configure_logging(
    level: str = 'INFO',
    log_to_file: bool = False,
    log_dir: Optional[str] = None,
    max_log_size_mb: int = 10,
    max_log_files: int = 5,
    redact_secrets_flag: bool = True,
) -> None:
    """
    Configure structlog and stdlib logging.
    
    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_to_file: Whether to log to a file in addition to console
        log_dir: Directory for log files (defaults to ./logs)
        max_log_size_mb: Maximum size of each log file in MB before rotation
        max_log_files: Maximum number of log files to keep
        redact_secrets_flag: Whether to enable secret redaction
    """
    global _CONFIGURED
    
    if _CONFIGURED:
        return
    
    # Import here to avoid circular dependency
    from pyclaw.logging.redact import SecretRedactingProcessor
    
    # Configure stdlib root logger
    logging.basicConfig(
        format='%(message)s',
        level=getattr(logging, level.upper()),
        stream=sys.stdout,
    )
    
    # Build processors chain
    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt='iso'),
        structlog.processors.StackInfoRenderer(),
    ]
    
    if redact_secrets_flag:
        processors.append(SecretRedactingProcessor())
    
    processors.extend([
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ])
    
    # Add appropriate renderer based on TTY
    if sys.stdout.isatty():
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors.append(structlog.processors.JSONRenderer())
    
    # Configure structlog
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    
    # Add file handler if requested
    if log_to_file:
        if log_dir is None:
            log_dir = './logs'
        
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        
        file_handler = logging.handlers.RotatingFileHandler(
            log_path / 'pyclaw.log',
            maxBytes=max_log_size_mb * 1024 * 1024,
            backupCount=max_log_files,
        )
        file_handler.setLevel(getattr(logging, level.upper()))
        file_handler.setFormatter(logging.Formatter('%(message)s'))
        
        root_logger = logging.getLogger()
        root_logger.addHandler(file_handler)
    
    _CONFIGURED = True


def get_logger(name: str) -> structlog.BoundLogger:
    """
    Get a structured logger instance.
    
    Args:
        name: Logger name (typically __name__)
    
    Returns:
        A structlog BoundLogger instance
    """
    return structlog.get_logger(name)
