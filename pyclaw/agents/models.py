"""
Model selector for choosing LLM models based on session context.
"""

from typing import Dict, Any, List, Optional
from pydantic import BaseModel


class ModelConfig(BaseModel):
    """Configuration for an LLM model."""
    
    name: str
    provider: str
    provider_type: str = "ollama"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    max_tokens: int = 4096
    temperature: float = 0.7
    model_params: Dict[str, Any] = {}
    internal_config: Optional[Dict[str, Any]] = None


class ModelSelector:
    """Selects appropriate LLM model for agent sessions."""
    
    def __init__(self, models_config: Dict[str, Any]):
        """
        Initialize model selector.
        
        Args:
            models_config: Configuration dictionary for available models
        """
        self.models_config = models_config
        self._models_cache: Dict[str, ModelConfig] = {}
    
    def select(self, session: Dict[str, Any]) -> ModelConfig:
        """
        Select model for the given session.
        
        Args:
            session: Session context containing preferences and metadata
            
        Returns:
            Selected ModelConfig
        """
        # Check for session-specific model preference
        preferred_model = session.get('preferred_model')
        if preferred_model:
            config = self._get_model_config(preferred_model)
            if config:
                return config
        
        # Get default model from config
        default_model_name = self.models_config.get('default_model')
        if default_model_name:
            config = self._get_model_config(default_model_name)
            if config:
                return config
        
        # Fallback to first available model
        available_models = self.models_config.get('models', {})
        if available_models:
            first_model_name = list(available_models.keys())[0]
            return self._get_model_config(first_model_name)
        
        # No models available - raise error
        raise ValueError("No models configured")
    
    def select_with_fallback(self, session: Dict[str, Any]) -> List[ModelConfig]:
        """
        Get list of models with fallback chain.
        
        Args:
            session: Session context
            
        Returns:
            List of ModelConfig objects in fallback order
        """
        models = []
        
        # Add preferred model if specified
        preferred_model = session.get('preferred_model')
        if preferred_model:
            config = self._get_model_config(preferred_model)
            if config:
                models.append(config)
        
        # Add default model
        default_model_name = self.models_config.get('default_model')
        if default_model_name and default_model_name != preferred_model:
            config = self._get_model_config(default_model_name)
            if config:
                models.append(config)
        
        # Add all other models as fallbacks
        available_models = self.models_config.get('models', {})
        for model_name in available_models.keys():
            if model_name != preferred_model and model_name != default_model_name:
                config = self._get_model_config(model_name)
                if config and config not in models:
                    models.append(config)
        
        return models
    
    def _get_model_config(self, model_name: str) -> Optional[ModelConfig]:
        """
        Get ModelConfig for a model name.
        
        Args:
            model_name: Name of the model
            
        Returns:
            ModelConfig or None if not found
        """
        # Check cache first
        if model_name in self._models_cache:
            return self._models_cache[model_name]
        
        # Load from config
        available_models = self.models_config.get('models', {})
        if model_name not in available_models:
            return None
        
        model_data = available_models[model_name]
        
        # Build ModelConfig
        config = ModelConfig(
            name=model_name,
            provider=model_data.get('provider', 'unknown'),
            provider_type=model_data.get('provider_type', 'ollama'),
            api_key=model_data.get('api_key'),
            base_url=model_data.get('base_url'),
            max_tokens=model_data.get('max_tokens', 4096),
            temperature=model_data.get('temperature', 0.7),
            model_params=model_data.get('model_params', {}),
            internal_config=model_data.get('internal_config'),
        )
        
        # Cache it
        self._models_cache[model_name] = config
        
        return config
    
    def list_available_models(self) -> List[str]:
        """
        Get list of all available model names.
        
        Returns:
            List of model names
        """
        return list(self.models_config.get('models', {}).keys())
