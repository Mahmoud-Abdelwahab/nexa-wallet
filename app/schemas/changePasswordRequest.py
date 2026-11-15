
from pydantic import BaseModel
from app.schemas.password import Password

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: Password
    confirm_password: str
