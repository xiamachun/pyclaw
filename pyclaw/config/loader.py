"""
Configuration Loader for PyClaw

This module handles loading configuration from multiple sources including
environment variables, .env files, and JSON configuration files.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from pyclaw.config.paths import PyClawPaths
from pyclaw.config.schema import PyClawConfig


def load_env_file(paths: PyClawPaths) -> Dict[str, str]:
    """Load environment variables from .env files with priority order.
    
    Priority: Process environment variables > ./.env > ~/.pyclaw/.env
    
    Args:
        paths: PyClawPaths instance containing path information
        
    Returns:
        Dictionary of environment variables
    """
    env_vars = {}
    
    # Start with process environment variables
    env_vars.update(os.environ)
    
    # Load from ~/.pyclaw/.env (lowest priority)
    state_env_file = paths.state_dir / '.env'
    if state_env_file.exists():
        _load_dotenv_file(state_env_file, env_vars)
    
    # Load from ./.env (medium priority)
    local_env_file = Path.cwd() / '.env'
    if local_env_file.exists():
        _load_dotenv_file(local_env_file, env_vars)
    
    # Process environment variables already have highest priority (already loaded first)
    
    return env_vars


def _load_dotenv_file(file_path: Path, env_vars: Dict[str, str]) -> None:
    """Load variables from a .env file.
    
    Args:
        file_path: Path to the .env file
        env_vars: Dictionary to update with loaded variables
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    
                    # Only set if not already present (lower priority)
                    if key not in env_vars:
                        env_vars[key] = value
    except Exception:
        # Silently ignore .env file errors
        pass


def _read_json_config(path: Optional[Path]) -> Dict[str, Any]:
    """Read configuration from a JSON file.
    
    Args:
        path: Path to the JSON configuration file
        
    Returns:
        Dictionary containing the configuration data
    """
    if path is None or not path.exists():
        return {}
    
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f'Invalid JSON in configuration file {path}: {e}')
    except Exception as e:
        raise ValueError(f'Error reading configuration file {path}: {e}')


def _apply_env_overrides(data: Dict[str, Any]) -> Dict[str, Any]:
    """Apply PYCLAW_ environment variable overrides to configuration.
    
    Environment variables with the prefix PYCLAW_ can override specific
    configuration values. The format is PYCLAW_<SECTION>_<KEY>.
    
    Args:
        data: Configuration data dictionary
        
    Returns:
        Updated configuration data dictionary
    """
    for key, value in os.environ.items():
        if key.startswith('PYCLAW_'):
            # Remove PYCLAW_ prefix and split by underscore
            parts = key[7:].lower().split('_')
            
            if len(parts) >= 2:
                section = parts[0]
                config_key = '_'.join(parts[1:])
                
                # Navigate to the correct section
                if section not in data:
                    data[section] = {}
                
                # Convert string value to appropriate type
                converted_value = _convert_env_value(value)
                data[section][config_key] = converted_value
    
    return data


def _convert_env_value(value: str) -> Any:
    """Convert environment variable string to appropriate type.
    
    Args:
        value: String value from environment variable
        
    Returns:
        Converted value (bool, int, float, or string)
    """
    # Boolean conversion
    if value.lower() in ('true', 'yes', '1'):
        return True
    if value.lower() in ('false', 'no', '0'):
        return False
    
    # Integer conversion
    try:
        return int(value)
    except ValueError:
        pass
    
    # Float conversion
    try:
        return float(value)
    except ValueError:
        pass
    
    # Return as string
    return value


def load_config(config_path: Optional[str] = None, paths: Optional[PyClawPaths] = None) -> PyClawConfig:
    """Load and validate PyClaw configuration from multiple sources.
    
    Loading order (highest priority first):
    1. PYCLAW_ environment variables
    2. JSON configuration file (config_path or default)
    3. .env files (./.env and ~/.pyclaw/.env)
    
    Args:
        config_path: Optional path to JSON configuration file
        paths: Optional PyClawPaths instance
        
    Returns:
        Validated PyClawConfig instance
    """
    if paths is None:
        paths = PyClawPaths()
    
    # Load environment variables
    env_vars = load_env_file(paths)
    
    # Read JSON configuration file
    if config_path:
        json_path = Path(config_path)
    else:
        json_path = paths.config_file
    
    config_data = _read_json_config(json_path)
    
    # Apply environment variable overrides
    config_data = _apply_env_overrides(config_data)
    
    # Validate and create PyClawConfig
    try:
        return PyClawConfig(**config_data)
    except Exception as e:
        raise ValueError(f'Configuration validation failed: {e}')
