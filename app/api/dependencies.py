from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.security import decode_access_token
from app.models.user import User
from app.repositories import UserRepository
# this file manly used to define the dependencies that will be used in the API routes. It includes a function to get the current authenticated user based on the JWT token provided in the request headers. The function uses the HTTPBearer security scheme to extract the token and then decodes it to retrieve the user ID. It then fetches the user from the database using the UserRepository. If any step fails (e.g., invalid token, user not found), it raises an HTTPException with a 401 Unauthorized status code.

#                  get_current_user
#                         │
#         ┌───────────────┴───────────────┐
#         ▼                               ▼
# decode_access_token()          UserRepository
#         │                               │
#         ▼                               ▼
#    JWT validation                 DB lookup
#         │                               │
#         └───────────────┬───────────────┘
#                         ▼
#                        User
bearer_scheme = HTTPBearer() # This creates an instance of the HTTPBearer class, which is a security scheme that expects an HTTP Authorization header with a Bearer token. It will be used to extract the token from incoming requests.

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
     db: Session = Depends(get_db),
):
    token = credentials.credentials
    try:
        payload = decode_access_token(token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )
    payload = decode_access_token(token)
    user_id = payload.get("sub")

    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )

    try: 
        user_id = int(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user ID in token",
        )
    user_repository = UserRepository(db)
    user = user_repository.get_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return user

# Now any endpoint need to depend on the jwt token to get the current user
# will do this will be used in any endpoint protected by jwt token,

# def get_me(
#     current_user: User = Depends(get_current_user),
# ):


    #             HTTP Request
    #                  │
    #                  ▼
    #         Authorization Header
    #                  │
    #                  ▼
    #             HTTPBearer
    #                  │
    #                  ▼
    #               JWT
    #                  │
    #                  ▼
    #       decode_access_token()
    #                  │
    #                  ▼
    #               payload
    #                  │
    #                  ▼
    #              get "sub"
    #                  │
    #                  ▼
    #            user_id = int()
    #                  │
    #                  ▼
    #    UserRepository.get_by_id()
    #                  │
    #          ┌───────┴───────┐
    #          ▼               ▼
    #       User            No User
    #          │               │
    #          ▼               ▼
    #      Endpoint           401