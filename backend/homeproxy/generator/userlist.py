"""The escaping filter for basic auth hashes in the generated userlists.

A hash is written without quotes, because HAProxy reads `$` as the start of an environment variable
only inside double quotes. The filter accepts only the SHA-512 crypt format, so a hash cannot end
the word or the line. Like the filters in `escape`, it never trusts that a model validated the hash.
"""

from pydantic import SecretStr

from homeproxy.generator.escape import Escaped, GeneratorError
from homeproxy.models.fields import SHA512_CRYPT_HASH


def password_hash(value: SecretStr) -> Escaped:
    """Return a SHA-512 crypt hash as one word. Raises `GeneratorError` for anything else."""
    secret = value.get_secret_value() if isinstance(value, SecretStr) else None
    if secret is None or SHA512_CRYPT_HASH.fullmatch(secret) is None:
        raise GeneratorError("a basic auth hash is not a SHA-512 crypt hash")
    return Escaped(secret)
