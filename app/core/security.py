from datetime import datetime, timedelta, timezone

from jose import jwt
from passlib.context import CryptContext

from app.core.config import settings


pwd_context = CryptContext(
    schemes=["bcrypt"], # bcrypt is a widely used and secure hashing algorithm for password storage. It is designed to be slow, which makes brute-force attacks more difficult.
    deprecated="auto", # auto means that the library will automatically mark any previously used hashing algorithms as deprecated if they are no longer considered secure. This helps ensure that your application uses the most secure hashing algorithm available.
    # eg : if you previously used "sha256_crypt" and now you are using "bcrypt", the library will automatically mark "sha256_crypt" as deprecated and will use "bcrypt" for new password hashes.
)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)



def verify_password(
    plain_password: str,
    hashed_password: str,
) -> bool:
    return pwd_context.verify(
        plain_password,
        hashed_password,
    )



def create_access_token(
    data: dict,
):
    to_encode = data.copy()

    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )

    to_encode.update(
        {
            "exp": expire
        }
    )

    return jwt.encode(
        to_encode,
        settings.secret_key,
        algorithm=settings.algorithm,
    )