"""File security checker for path traversal and sensitive file access."""

import os
from pathlib import Path
from typing import Tuple


DANGEROUS_EXTENSIONS: frozenset[str] = frozenset({
    '.exe', '.bat', '.cmd', '.sh', '.ps1', '.vbs', '.js', '.msi', '.dll', '.so'
})

SENSITIVE_DIRECTORIES: list[str] = [
    '/etc',
    '/root',
    '/var/log',
    os.path.expanduser('~/.ssh'),
    os.path.expanduser('~/.aws'),
    os.path.expanduser('~/.gnupg'),
]


class FileSecurityChecker:
    """Checker for file system security violations."""
    
    def check_path_traversal(self, path: str) -> bool:
        """
        Check if path contains traversal attempts.
        
        Args:
            path: File path to check.
            
        Returns:
            bool: True if path traversal detected.
        """
        # Check for common traversal patterns
        traversal_patterns = ['../', '..\\', '..%2f', '..%5c', '%2e%2e', '%252e']
        path_lower = path.lower()
        
        for pattern in traversal_patterns:
            if pattern in path_lower:
                return True
        
        # Resolve path and check if it escapes intended directory
        try:
            resolved = Path(path).resolve()
            # Check if path contains '..' after normalization
            if '..' in str(resolved.parts):
                return True
        except Exception:
            return True
        
        return False
    
    def check_sensitive_path(self, path: str) -> bool:
        """
        Check if path points to a sensitive directory.
        
        Args:
            path: File path to check.
            
        Returns:
            bool: True if path is sensitive.
        """
        try:
            resolved_path = Path(path).resolve().absolute()
            
            for sensitive_dir in SENSITIVE_DIRECTORIES:
                sensitive_path = Path(sensitive_dir).resolve().absolute()
                
                # Check if path is exactly the sensitive directory
                if resolved_path == sensitive_path:
                    return True
                
                # Check if path is within the sensitive directory
                try:
                    resolved_path.relative_to(sensitive_path)
                    return True
                except ValueError:
                    continue
        except Exception:
            return True
        
        return False
    
    def check_file_extension(self, path: str) -> bool:
        """
        Check if file has a dangerous extension.
        
        Args:
            path: File path to check.
            
        Returns:
            bool: True if file extension is dangerous.
        """
        file_path = Path(path)
        extension = file_path.suffix.lower()
        
        return extension in DANGEROUS_EXTENSIONS
    
    def validate_file_access(self, path: str) -> Tuple[bool, str | None]:
        """
        Validate file access against all security checks.
        
        Args:
            path: File path to validate.
            
        Returns:
            tuple[bool, str | None]: (is_valid, error_message)
        """
        # Check path traversal
        if self.check_path_traversal(path):
            return False, f"Path traversal detected: {path}"
        
        # Check sensitive paths
        if self.check_sensitive_path(path):
            return False, f"Access to sensitive directory denied: {path}"
        
        # Check dangerous extensions
        if self.check_file_extension(path):
            return False, f"Dangerous file extension detected: {path}"
        
        return True, None
