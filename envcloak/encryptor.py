import os
import base64
import json
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend
import click
from click import style
from envcloak.exceptions import (
    InvalidSaltException,
    InvalidKeyException,
    EncryptionException,
    DecryptionException,
    FileEncryptionException,
    FileDecryptionException,
    IntegrityCheckFailedException,
)
from envcloak.constants import NONCE_SIZE, KEY_SIZE, SALT_SIZE
from envcloak.utils import compute_sha256


def derive_key(password: str, salt: bytes) -> bytes:
    """
    Derive a cryptographic key from a password and salt using PBKDF2.
    :param password: User-provided password.
    :param salt: Salt for key derivation (must be 16 bytes).
    :return: Derived key (32 bytes for AES-256).
    """
    if len(salt) != SALT_SIZE:
        raise InvalidSaltException(
            details=f"Expected salt of size {SALT_SIZE}, got {len(salt)} bytes."
        )
    try:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=KEY_SIZE,
            salt=salt,
            iterations=100000,
            backend=default_backend(),
        )
        return kdf.derive(password.encode())
    except Exception as e:
        raise InvalidKeyException(details=str(e)) from e


def generate_salt() -> bytes:
    """
    Generate a secure random salt of the standard size.
    :return: Randomly generated salt (16 bytes).
    """
    try:
        return os.urandom(SALT_SIZE)
    except Exception as e:
        raise EncryptionException(details=f"Failed to generate salt: {str(e)}") from e


def encrypt(data: str, key: bytes) -> dict:
    """
    Encrypt the given data using AES-256-GCM.

    :param data: Plaintext data to encrypt.
    :param key: Encryption key (32 bytes for AES-256).
    :return: Dictionary with encrypted data, nonce, and associated metadata.
    """
    try:
        nonce = os.urandom(NONCE_SIZE)  # Generate a secure random nonce
        cipher = Cipher(
            algorithms.AES(key), modes.GCM(nonce), backend=default_backend()
        )
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(data.encode()) + encryptor.finalize()

        return {
            "ciphertext": base64.b64encode(ciphertext).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "tag": base64.b64encode(encryptor.tag).decode(),
        }
    except Exception as e:
        raise EncryptionException(details=str(e)) from e


def decrypt(encrypted_data: dict, key: bytes, validate_integrity: bool = True) -> str:
    """
    Decrypt the given encrypted data using AES-256-GCM.

    :param encrypted_data: Dictionary containing ciphertext, nonce, and tag.
    :param key: Decryption key (32 bytes for AES-256).
    :param validate_integrity: Whether to enforce integrity checks (default: True).
    :return: Decrypted plaintext.
    """
    try:
        nonce = base64.b64decode(encrypted_data["nonce"])
        ciphertext = base64.b64decode(encrypted_data["ciphertext"])
        tag = base64.b64decode(encrypted_data["tag"])

        cipher = Cipher(
            algorithms.AES(key), modes.GCM(nonce, tag), backend=default_backend()
        )
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()

        if validate_integrity:
            # Validate plaintext hash if present
            if "sha" in encrypted_data:
                sha_hash = compute_sha256(plaintext.decode())
                if sha_hash != encrypted_data["sha"]:
                    raise IntegrityCheckFailedException(
                        details="Integrity check failed! The file may have been tampered with or corrupted."
                    )

        return plaintext.decode()
    except Exception as e:
        raise DecryptionException(details=str(e)) from e


def encrypt_file(input_file: str, output_file: str, key: bytes):
    """
    Encrypt the contents of a file and write the result to another file,
    including SHA-256 of the entire encrypted JSON structure.
    """
    try:
        with open(input_file, "r", encoding="utf-8") as infile:
            data = infile.read()

        # Encrypt plaintext
        encrypted_data = encrypt(data, key)

        # Compute hash of plaintext for integrity
        encrypted_data["sha"] = compute_sha256(data)
        print(
            f"Debug: SHA-256 hash of plaintext during encryption: {encrypted_data['sha']}"
        )

        # Compute hash of the entire encrypted structure
        file_hash = compute_sha256(json.dumps(encrypted_data, ensure_ascii=False))
        encrypted_data["file_sha"] = file_hash  # Store this hash in the structure
        print(
            f"Debug: SHA-256 hash of encrypted structure (file_sha): {encrypted_data['file_sha']}"
        )

        with open(output_file, "w", encoding="utf-8") as outfile:
            json.dump(encrypted_data, outfile, ensure_ascii=False)
    except Exception as e:
        raise FileEncryptionException(details=str(e)) from e


def decrypt_file(
    input_file: str, output_file: str, key: bytes, validate_integrity: bool = True
):
    """
    Decrypt the contents of a file and validate SHA-256 integrity for both
    the plaintext and the encrypted file.

    :param input_file: Path to the encrypted input file.
    :param output_file: Path to save the decrypted file.
    :param key: Encryption key (32 bytes for AES-256).
    :param validate_integrity: Whether to enforce integrity checks (default: True).
    """
    try:
        with open(input_file, "r", encoding="utf-8") as infile:
            encrypted_data = json.load(infile)

        if validate_integrity:
            # Validate hash of the entire encrypted file (excluding file_sha)
            expected_file_sha = encrypted_data.get("file_sha")
            if expected_file_sha:
                # Exclude "file_sha" itself from the recomputed hash
                data_to_hash = encrypted_data.copy()
                data_to_hash.pop("file_sha")
                actual_file_sha = compute_sha256(
                    json.dumps(data_to_hash, ensure_ascii=False)
                )
                # print(f"Debug: Stored file_sha: {expected_file_sha}")
                # print(f"Debug: Computed file_sha: {actual_file_sha}")
                if expected_file_sha != actual_file_sha:
                    raise IntegrityCheckFailedException(
                        details="Encrypted file integrity check failed! The file may have been tampered with or corrupted."
                    )
            else:
                click.echo(
                    style(
                        "⚠️  Warning: file_sha missing. Encrypted file integrity check skipped.",
                        fg="yellow",
                    )
                )

        # Decrypt the plaintext
        decrypted_data = decrypt(
            encrypted_data, key, validate_integrity=validate_integrity
        )

        if validate_integrity:
            # Validate hash of plaintext
            if "sha" in encrypted_data:
                sha_hash = compute_sha256(decrypted_data)
                # print(f"Debug: Stored sha: {encrypted_data['sha']}")
                # print(f"Debug: Computed sha: {sha_hash}")
                if sha_hash != encrypted_data["sha"]:
                    raise IntegrityCheckFailedException(
                        details="Decrypted plaintext integrity check failed! The file may have been tampered with or corrupted."
                    )
            else:
                click.echo(
                    style(
                        "⚠️  Warning: sha missing. Plaintext integrity check skipped.",
                        fg="yellow",
                    )
                )

        # Write plaintext to the output file
        with open(output_file, "w", encoding="utf-8") as outfile:
            outfile.write(decrypted_data)
    except Exception as e:
        raise FileDecryptionException(details=str(e)) from e
