from typing import Annotated

from pydantic import Field


# Registration and password reset must accept the same password policy.
Password = Annotated[str, Field(min_length=8, max_length=100)]
