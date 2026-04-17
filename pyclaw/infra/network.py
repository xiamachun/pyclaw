"""Network security utilities for SSRF protection and policy enforcement."""

import socket
from ipaddress import IPv4Address, IPv6Address, IPv4Network, IPv6Network, ip_address
from typing import Tuple
from urllib.parse import urlparse

import httpx


PRIVATE_IP_RANGES = [
    IPv4Network('10.0.0.0/8'),
    IPv4Network('172.16.0.0/12'),
    IPv4Network('192.168.0.0/16'),
    IPv4Network('127.0.0.0/8'),
    IPv4Network('169.254.0.0/16'),
    IPv6Network('::1/128'),
    IPv6Network('fc00::/7'),
]


class SSRFProtector:
    """Protection against Server-Side Request Forgery (SSRF) attacks."""
    
    def check_url(self, url: str) -> Tuple[bool, str | None]:
        """
        Check if URL is safe from SSRF attacks.
        
        Args:
            url: URL to check.
            
        Returns:
            tuple[bool, str | None]: (is_safe, error_message)
        """
        try:
            parsed = urlparse(url)
            
            # Check for invalid schemes
            if parsed.scheme not in ['http', 'https']:
                return False, f"Invalid URL scheme: {parsed.scheme}"
            
            # Check for internal hostname patterns
            hostname = parsed.hostname or ''
            
            # Check localhost variations
            localhost_patterns = [
                'localhost', '127.0.0.1', '0.0.0.0', '::1',
                '0:0:0:0:0:0:0:1', '[::1]'
            ]
            
            for pattern in localhost_patterns:
                if pattern.lower() in hostname.lower():
                    return False, f"Localhost access denied: {hostname}"
            
            # Resolve hostname and check IP
            ip_list = self._resolve_host(hostname)
            
            for ip in ip_list:
                if self._is_private_ip(ip):
                    return False, f"Private IP access denied: {ip}"
            
            return True, None
            
        except Exception as e:
            return False, f"URL validation error: {str(e)}"
    
    def _resolve_host(self, hostname: str) -> list[str]:
        """
        Resolve hostname to IP addresses.
        
        Args:
            hostname: Hostname to resolve.
            
        Returns:
            list[str]: List of IP addresses.
        """
        ip_list = []
        
        try:
            # Try to parse as IP address directly
            ip = ip_address(hostname)
            return [str(ip)]
        except ValueError:
            pass
        
        try:
            # DNS resolution
            addr_info = socket.getaddrinfo(hostname, None)
            
            for addr in addr_info:
                ip = addr[4][0]
                if ip not in ip_list:
                    ip_list.append(ip)
        except Exception:
            pass
        
        return ip_list
    
    def _is_private_ip(self, ip_str: str) -> bool:
        """
        Check if IP address is in private range.
        
        Args:
            ip_str: IP address string.
            
        Returns:
            bool: True if IP is private.
        """
        try:
            ip = ip_address(ip_str)
            
            for network in PRIVATE_IP_RANGES:
                if ip in network:
                    return True
            
            return False
        except Exception:
            return True


class NetworkPolicy:
    """Network policy for outbound connection control."""
    
    def __init__(self, allowed_hosts: list[str] | None = None, blocked_hosts: list[str] | None = None):
        """
        Initialize network policy.
        
        Args:
            allowed_hosts: List of allowed host patterns. None means all allowed.
            blocked_hosts: List of blocked host patterns. None means none blocked.
        """
        self.allowed_hosts = allowed_hosts or []
        self.blocked_hosts = blocked_hosts or []
        self.ssrf_protector = SSRFProtector()
    
    def check_outbound(self, url: str) -> Tuple[bool, str | None]:
        """
        Check if outbound connection is allowed by policy.
        
        Args:
            url: URL to check.
            
        Returns:
            tuple[bool, str | None]: (is_allowed, error_message)
        """
        # First check SSRF protection
        is_safe, error = self.ssrf_protector.check_url(url)
        if not is_safe:
            return False, error
        
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname or ''
            
            # Check blocked hosts
            for blocked in self.blocked_hosts:
                if blocked.lower() in hostname.lower():
                    return False, f"Host blocked by policy: {hostname}"
            
            # Check allowed hosts (if defined)
            if self.allowed_hosts:
                allowed = False
                for allowed_pattern in self.allowed_hosts:
                    if allowed_pattern.lower() in hostname.lower():
                        allowed = True
                        break
                
                if not allowed:
                    return False, f"Host not in allowed list: {hostname}"
            
            return True, None
            
        except Exception as e:
            return False, f"Policy check error: {str(e)}"
