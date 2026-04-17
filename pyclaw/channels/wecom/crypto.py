"""
WeCom message encryption/decryption module.

Implements the official WeCom callback message encryption scheme
without depending on external libraries (wechatpy etc.).

Reference: https://developer.work.weixin.qq.com/document/path/91144
"""

import base64
import hashlib
import socket
import struct
import time
import xml.etree.ElementTree as ET
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend


def _pkcs7_pad(data: bytes, block_size: int = 32) -> bytes:
    """Apply PKCS#7 padding."""
    padding_len = block_size - (len(data) % block_size)
    return data + bytes([padding_len] * padding_len)


def _pkcs7_unpad(data: bytes) -> bytes:
    """Remove PKCS#7 padding."""
    padding_len = data[-1]
    if padding_len < 1 or padding_len > 32:
        raise ValueError("Invalid PKCS#7 padding")
    return data[:-padding_len]


class WeComCrypto:
    """WeCom message encryption/decryption handler.

    Args:
        token: Callback verification token.
        encoding_aes_key: 43-character Base64-encoded AES key.
        corp_id: Enterprise Corp ID.
    """

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        self.token = token
        self.corp_id = corp_id
        # encoding_aes_key is 43 chars, Base64-decode to get 32-byte AES key
        self.aes_key = base64.b64decode(encoding_aes_key + "=")
        self.iv = self.aes_key[:16]

    def _compute_signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        """Compute SHA1 signature for verification.

        Args:
            timestamp: Request timestamp.
            nonce: Request nonce.
            encrypt: Encrypted message string.

        Returns:
            SHA1 hex digest.
        """
        parts = sorted([self.token, timestamp, nonce, encrypt])
        return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

    def verify_signature(
        self, msg_signature: str, timestamp: str, nonce: str, echostr: str
    ) -> bool:
        """Verify callback URL signature.

        Args:
            msg_signature: Signature from request params.
            timestamp: Timestamp from request params.
            nonce: Nonce from request params.
            echostr: Encrypted echo string from request params.

        Returns:
            True if signature matches.
        """
        computed = self._compute_signature(timestamp, nonce, echostr)
        return computed == msg_signature

    def decrypt(self, encrypted_text: str) -> str:
        """Decrypt an AES-256-CBC encrypted message.

        Args:
            encrypted_text: Base64-encoded encrypted content.

        Returns:
            Decrypted plaintext message.

        Raises:
            ValueError: If corp_id mismatch or decryption fails.
        """
        cipher_data = base64.b64decode(encrypted_text)
        cipher = Cipher(
            algorithms.AES(self.aes_key), modes.CBC(self.iv), backend=default_backend()
        )
        decryptor = cipher.decryptor()
        plain_data = decryptor.update(cipher_data) + decryptor.finalize()
        plain_data = _pkcs7_unpad(plain_data)

        # Layout: 16-byte random + 4-byte msg_len (big-endian) + msg + corp_id
        msg_len = struct.unpack(">I", plain_data[16:20])[0]
        message = plain_data[20 : 20 + msg_len].decode("utf-8")
        from_corp_id = plain_data[20 + msg_len :].decode("utf-8")

        if from_corp_id != self.corp_id:
            raise ValueError(
                "Corp ID mismatch: expected %s, got %s" % (self.corp_id, from_corp_id)
            )
        return message

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext message using AES-256-CBC.

        Args:
            plaintext: Message to encrypt.

        Returns:
            Base64-encoded encrypted string.
        """
        import os

        random_prefix = os.urandom(16)
        msg_bytes = plaintext.encode("utf-8")
        corp_id_bytes = self.corp_id.encode("utf-8")
        msg_len = struct.pack(">I", len(msg_bytes))

        raw = random_prefix + msg_len + msg_bytes + corp_id_bytes
        padded = _pkcs7_pad(raw)

        cipher = Cipher(
            algorithms.AES(self.aes_key), modes.CBC(self.iv), backend=default_backend()
        )
        encryptor = cipher.encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        return base64.b64encode(encrypted).decode("utf-8")

    def decrypt_callback_echostr(self, echostr: str) -> str:
        """Decrypt the echostr for URL verification (GET request).

        Args:
            echostr: Encrypted echo string from query params.

        Returns:
            Decrypted echo string to return to WeCom.
        """
        return self.decrypt(echostr)

    def decrypt_message(
        self,
        xml_body: str,
        msg_signature: str,
        timestamp: str,
        nonce: str,
    ) -> str:
        """Decrypt an incoming callback message (POST request).

        Args:
            xml_body: Raw XML body from POST request.
            msg_signature: Signature from query params.
            timestamp: Timestamp from query params.
            nonce: Nonce from query params.

        Returns:
            Decrypted XML message content.

        Raises:
            ValueError: If signature verification fails.
        """
        root = ET.fromstring(xml_body)
        encrypt_node = root.find("Encrypt")
        if encrypt_node is None or encrypt_node.text is None:
            raise ValueError("Missing <Encrypt> element in callback XML")

        encrypted_text = encrypt_node.text

        # Verify signature
        computed = self._compute_signature(timestamp, nonce, encrypted_text)
        if computed != msg_signature:
            raise ValueError("Message signature verification failed")

        return self.decrypt(encrypted_text)

    def generate_reply_xml(
        self, reply_text: str, nonce: str, timestamp: Optional[str] = None
    ) -> str:
        """Generate encrypted reply XML for WeCom callback response.

        Args:
            reply_text: Plaintext reply content.
            nonce: Nonce for signature.
            timestamp: Timestamp (defaults to current time).

        Returns:
            Encrypted XML string ready to return as HTTP response.
        """
        if timestamp is None:
            timestamp = str(int(time.time()))

        encrypted = self.encrypt(reply_text)
        signature = self._compute_signature(timestamp, nonce, encrypted)

        return (
            "<xml>"
            "<Encrypt><![CDATA[%s]]></Encrypt>"
            "<MsgSignature><![CDATA[%s]]></MsgSignature>"
            "<TimeStamp>%s</TimeStamp>"
            "<Nonce><![CDATA[%s]]></Nonce>"
            "</xml>"
        ) % (encrypted, signature, timestamp, nonce)
