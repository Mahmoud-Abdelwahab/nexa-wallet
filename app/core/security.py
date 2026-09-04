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
    # encodes the payload (data) into a JWT token using the secret key and algorithm specified in the settings. The resulting token can be used for authentication and authorization purposes.
    # It do four things :
    # 1. Create a copy of the input data to avoid modifying the original dictionary.
    # 2. Calculate the expiration time for the token based on the current time and the configured expiration duration (in minutes).
    # 3. Update the copied data with the expiration time
    # 4. Encode the updated data into a JWT token using the secret key and algorithm
    return jwt.encode(
        to_encode,
        settings.secret_key,
        algorithm=settings.algorithm,
    )

def decode_access_token(token: str) -> dict:
    # Decode the JWT token and return the payload as a dictionary. If the token is invalid or expired, an exception will be raised.
    # it do three things :
    # 1. Verify the signature of the token using the secret key and algorithm specified in the settings.
    # 2. Check the expiration time of the token and raise an exception if it has expired.
    # 3. Check Algorithm Verification and Return the payload of the token as a dictionary if the
    return jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.algorithm],
    )