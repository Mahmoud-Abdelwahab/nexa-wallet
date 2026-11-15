from datetime import date

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.schemas.password import Password
from app.schemas.device_session import DeviceMetadata


class RegisterRequest(DeviceMetadata):
    # يرث بيانات الجهاز الاختيارية وحدودها من DeviceMetadata من غير تكرار التعريف.
    # التسجيل بيعمل Login كمان، فمحتاجين بيانات الجهاز للجلسة الأولى.
    username: str = Field(
        min_length=3,
        max_length=50,
    )

    email: EmailStr

    password: Password

    mobile: str = Field(
        min_length=8,
        max_length=20,
    )

    date_of_birth: date



class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: EmailStr
    mobile: str


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    user: UserResponse


class LoginRequest(DeviceMetadata):
    # الـ clients القديمة تقدر تبعت email/password بس؛ بيانات الجهاز لها defaults.
    email: str
    password: str
