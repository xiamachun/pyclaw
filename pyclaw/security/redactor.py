import re
from typing import List, Set


_SENSITIVE_FILE_PATTERNS = [
    r"\.pem$",
    r"\.key$",
    r"id_rsa$",
    r"id_ed25519$",
    r"\.env$",
    r"credentials$",
    r"\.netrc$",
    r"\.pgpass$",
    r"\.my\.cnf$",
    r"aws/credentials$",
    r"\.docker/config\.json$",
    r"kube/config$",
]

_CONTENT_PATTERNS = {
    "OpenAI API Key": r"sk-[a-zA-Z0-9]{48}",
    "Anthropic API Key": r"sk-ant-[a-zA-Z0-9]{95}",
    "Bearer Token": r"Bearer [a-zA-Z0-9\-._~+/]+=*",
    "AWS Access Key ID": r"AKIA[0-9A-Z]{16}",
    "AWS Secret Key": r"[a-zA-Z0-9/+]{40}",
    "Private Key Block": r"-----BEGIN [A-Z]+ PRIVATE KEY-----",
    "Password in URL": r"[a-zA-Z]+://[^:/@]+:[^:/@]+@",
}


class CredentialRedactor:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._compiled_patterns = {
            name: re.compile(pattern)
            for name, pattern in _CONTENT_PATTERNS.items()
        }
        self._file_patterns = [
            re.compile(pattern) for pattern in _SENSITIVE_FILE_PATTERNS
        ]
    
    def redact(self, text: str) -> str:
        if not self.enabled:
            return text
        
        redacted_text = text
        for name, pattern in self._compiled_patterns.items():
            redacted_text = pattern.sub(f"[{name} REDACTED]", redacted_text)
        
        return redacted_text
    
    def is_sensitive_path(self, path: str) -> bool:
        for pattern in self._file_patterns:
            if pattern.search(path):
                return True
        return False
    
    def check_content_for_secrets(self, text: str) -> List[str]:
        secrets_found = []
        
        for name, pattern in self._compiled_patterns.items():
            if pattern.search(text):
                secrets_found.append(name)
        
        return secrets_found
