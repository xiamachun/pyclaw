"""
Embedding providers for vector search.
"""

from abc import ABC, abstractmethod
from typing import Optional

import httpx

from pyclaw.constants import DEFAULT_MAX_FEATURES, EMBEDDING_TIMEOUT_SECONDS, EMBEDDING_CONNECT_TIMEOUT_SECONDS


class EmbeddingProvider(ABC):
    """Abstract base class for embedding providers."""
    
    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        pass
    
    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        pass


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI-compatible embedding provider."""
    
    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-ada-002",
        base_url: Optional[str] = None,
    ):
        """Initialize the OpenAI embedding provider."""
        self.api_key = api_key
        self.model = model
        self.base_url = base_url or 'https://api.openai.com/v1'
        self._client: Optional[httpx.AsyncClient] = None
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=httpx.Timeout(EMBEDDING_TIMEOUT_SECONDS, connect=EMBEDDING_CONNECT_TIMEOUT_SECONDS),
            )
        return self._client
    
    async def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        results = await self.embed_batch([text])
        return results[0]
    
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        client = await self._get_client()
        
        response = await client.post(
            "/embeddings",
            json={
                "model": self.model,
                "input": texts,
            },
        )
        response.raise_for_status()
        data = response.json()
        
        embeddings = [item["embedding"] for item in data["data"]]
        return embeddings
    
    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None


class LocalEmbeddingProvider(EmbeddingProvider):
    """Simple TF-IDF based local embedding provider."""
    
    def __init__(self, max_features: int = DEFAULT_MAX_FEATURES):
        """Initialize the local embedding provider."""
        self.max_features = max_features
        self._vocabulary: dict[str, int] = {}
        self._idf: dict[str, float] = {}
        self._documents: list[dict[str, int]] = []
    
    def _tokenize(self, text: str) -> list[str]:
        """Simple tokenization."""
        import re
        words = re.findall(r"\b\w+\b", text.lower())
        return words
    
    def _build_vocabulary(self, texts: list[str]) -> None:
        """Build vocabulary from texts."""
        from collections import Counter
        
        word_counts = Counter()
        for text in texts:
            words = self._tokenize(text)
            word_counts.update(words)
        
        # Keep top max_features words
        top_words = word_counts.most_common(self.max_features)
        self._vocabulary = {word: idx for idx, (word, _) in enumerate(top_words)}
    
    def _calculate_idf(self, texts: list[str]) -> None:
        """Calculate IDF scores."""
        import math
        
        doc_count = len(texts)
        for word in self._vocabulary:
            containing_docs = sum(1 for text in texts if word in self._tokenize(text))
            self._idf[word] = math.log(doc_count / (1 + containing_docs))
    
    def _vectorize(self, text: str) -> dict[str, int]:
        """Convert text to TF vector."""
        from collections import Counter
        
        words = self._tokenize(text)
        word_counts = Counter(words)
        
        # Filter to vocabulary
        vector = {}
        for word, count in word_counts.items():
            if word in self._vocabulary:
                vector[word] = count
        
        return vector
    
    async def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        # For single text, just use TF (no IDF)
        vector = self._vectorize(text)
        
        # Convert to fixed-size float vector
        embedding = [0.0] * len(self._vocabulary)
        for word, count in vector.items():
            embedding[self._vocabulary[word]] = float(count)
        
        # Normalize
        import math
        magnitude = math.sqrt(sum(x * x for x in embedding))
        if magnitude > 0:
            embedding = [x / magnitude for x in embedding]
        
        return embedding
    
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        # Build vocabulary from all texts if not already built
        if not self._vocabulary:
            self._build_vocabulary(texts)
            self._calculate_idf(texts)
        
        embeddings = []
        for text in texts:
            vector = self._vectorize(text)
            
            # Calculate TF-IDF
            embedding = [0.0] * len(self._vocabulary)
            for word, count in vector.items():
                tf_idf = count * self._idf.get(word, 1.0)
                embedding[self._vocabulary[word]] = tf_idf
            
            # Normalize
            import math
            magnitude = math.sqrt(sum(x * x for x in embedding))
            if magnitude > 0:
                embedding = [x / magnitude for x in embedding]
            
            embeddings.append(embedding)
        
        return embeddings
