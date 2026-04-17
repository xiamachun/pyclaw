"""
WeChat Work message encryption/decryption utility class

Implements message encryption/decryption functionality for WeChat Work callback mode.
Reference: WeChat Work official documentation: https://developer.work.weixin.qq.com/document/path/90930
"""

import base64
import hashlib
import logging
import random
import string
import struct
import time
from typing import Optional

import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

class WeChatCryptoError(Exception):
    """WeChat Work encryption/decryption exception"""
    pass


class WeChatCrypto:
    """WeChat Work message encryption/decryption utility
    
    Uses AES-CBC-256 encryption algorithm with PKCS7 padding
    """
    
    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        """Initialize encryption/decryption tool
        
        Args:
            token: Callback verification Token
            encoding_aes_key: Callback encryption Key (43 characters)
            corp_id: Enterprise ID
        """
        self.token = token
        self.corp_id = corp_id
        
        # Convert encoding_aes_key to AES key (Base64 decoded should be 32 bytes)
        self.key = base64.b64decode(encoding_aes_key + "=")
        
        if len(self.key) != 32:
            raise WeChatCryptoError(f"encoding_aes_key length error, should be 32 bytes, actual {len(self.key)} bytes")
    
    def _pkcs7_encode(self, text: bytes, block_size: int = 32) -> bytes:
        """PKCS7 padding"""
        padding_length = block_size - (len(text) % block_size)
        padding = bytes([padding_length] * padding_length)
        return text + padding
    
    def _pkcs7_decode(self, text: bytes, block_size: int = 32) -> bytes:
        """PKCS7 unpadding"""
        padding_length = text[-1]
        return text[:-padding_length]
    
    def _get_random_str(self) -> str:
        """Generate random string"""
        chars = string.ascii_letters + string.digits
        return ''.join(random.choice(chars) for _ in range(16))
    
    def _sha1(self, *args: str) -> str:
        """Calculate SHA1 signature"""
        sha = hashlib.sha1()
        for arg in args:
            if isinstance(arg, str):
                sha.update(arg.encode('utf-8'))
            else:
                sha.update(arg)
        return sha.hexdigest()
    
    def encrypt(self, text: str, receive_id: Optional[str] = None) -> str:
        """Encrypt text
        
        Args:
            text: Plain text to encrypt
            receive_id: Receiver ID (usually corp_id in WeChat Work)
            
        Returns:
            Encrypted XML string
        """
        receive_id = receive_id or self.corp_id
        
        # Generate random string
        random_str = self._get_random_str()
        
        # Assemble content to encrypt: random_str(16B) + text_len(4B) + text + receive_id
        text_bytes = text.encode('utf-8')
        receive_id_bytes = receive_id.encode('utf-8')
        
        # Pack text length using little-endian
        text_len_bytes = struct.pack('I', len(text_bytes))
        
        # Concatenate content to encrypt
        content = random_str.encode('utf-8') + text_len_bytes + text_bytes + receive_id_bytes
        
        # PKCS7 padding
        padded_content = self._pkcs7_encode(content)
        
        # AES encryption
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
        
        cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        encrypted = cipher.encrypt(padded_content)
        
        # Base64 encoding
        encrypted_base64 = base64.b64encode(encrypted).decode('utf-8')
        
        # Generate timestamp
        timestamp = str(int(time.time()))
        
        # Generate random nonce
        nonce = self._get_random_str()
        
        # Calculate signature
        signature = self._sha1(self.token, timestamp, nonce, encrypted_base64)
        
        # Build XML
        xml = f"""<xml>
<Encrypt><![CDATA[{encrypted_base64}]]></Encrypt>
<MsgSignature><![CDATA[{signature}]]></MsgSignature>
<TimeStamp>{timestamp}</TimeStamp>
<Nonce><![CDATA[{nonce}]]></Nonce>
</xml>"""
        
        return xml
    
    def decrypt(
        self,
        encrypted_xml: str,
        msg_signature: str,
        timestamp: str,
        nonce: str
    ) -> str:
        """Decrypt message
        
        Args:
            encrypted_xml: Encrypted XML string
            msg_signature: Message signature
            timestamp: Timestamp
            nonce: Random string
            
        Returns:
            Decrypted plain text
            
        Raises:
            WeChatCryptoError: Signature validation failed or decryption failed
        """
        # Parse XML to get Encrypt field
        try:
            root = ET.fromstring(encrypted_xml)
            encrypt_element = root.find('Encrypt')
            if encrypt_element is None:
                raise WeChatCryptoError("XML does not contain Encrypt field")
            
            encrypted_msg = encrypt_element.text
            if not encrypted_msg:
                raise WeChatCryptoError("Encrypt field is empty")
        except ET.ParseError as e:
            raise WeChatCryptoError(f"XML parsing failed: {e}")
        
        # Validate signature
        computed_signature = self._sha1(self.token, timestamp, nonce, encrypted_msg)
        if computed_signature != msg_signature:
            raise WeChatCryptoError(
                f"Signature validation failed: computed={computed_signature}, received={msg_signature}"
            )
        
        # Base64 decode
        encrypted_bytes = base64.b64decode(encrypted_msg)
        
        # AES decryption
        from Crypto.Cipher import AES
        
        cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        decrypted = cipher.decrypt(encrypted_bytes)
        
        # Remove PKCS7 padding
        decrypted = self._pkcs7_decode(decrypted)
        
        # Parse decrypted content
        # Format: random_str(16B) + text_len(4B) + text + receive_id
        try:
            # Skip 16 bytes of random string
            content = decrypted[16:]
            
            # Read text length (4 bytes, little-endian)
            text_len = struct.unpack('I', content[:4])[0]
            
            # Extract text
            text = content[4:4 + text_len].decode('utf-8')
            
            # Verify receive_id (should be corp_id)
            receive_id = content[4 + text_len:].decode('utf-8')
            if receive_id != self.corp_id:
                logger.warning(f"receive_id does not match: expected={self.corp_id}, got={receive_id}")
            
            return text
            
        except Exception as e:
            raise WeChatCryptoError(f"Decrypted content parsing failed: {e}")
    
    def verify_url(
        self,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        echostr: str
    ) -> str:
        """Verify callback URL
        
        Args:
            msg_signature: Message signature
            timestamp: Timestamp
            nonce: Random string
            echostr: Encrypted random string
            
        Returns:
            Decrypted echostr (to be returned to WeChat Work as is)
            
        Raises:
            WeChatCryptoError: Verification failed
        """
        # Calculate signature
        computed_signature = self._sha1(self.token, timestamp, nonce, echostr)
        
        if computed_signature != msg_signature:
            raise WeChatCryptoError(
                f"URL verification signature failed: computed={computed_signature}, received={msg_signature}"
            )
        
        # Decrypt echostr
        try:
            encrypted_bytes = base64.b64decode(echostr)
            
            from Crypto.Cipher import AES
            cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
            decrypted = cipher.decrypt(encrypted_bytes)
            
            # Remove PKCS7 padding
            decrypted = self._pkcs7_decode(decrypted)
            
            # Skip 16 bytes random string and 4 bytes length
            echostr_decrypted = decrypted[20:].decode('utf-8')
            
            return echostr_decrypted
            
        except Exception as e:
            raise WeChatCryptoError(f"echostr decryption failed: {e}")