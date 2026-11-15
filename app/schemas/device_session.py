# Request device metadata and the safe session response returned to mobile clients.
# DeviceMetadata -> Login/Register -> RefreshSession stored in Redis
# DeviceSessionResponse -> Devices & Sessions screen, without credentials
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# Share this type between schemas and services; plain str could contain unsupported values.
DevicePlatform = Literal["iOS", "Android", "Web", "Unknown"]


class DeviceMetadata(BaseModel):
    # Client-supplied device labels are for display only, not proof of device identity.
    # Field rejects empty/oversized labels; Literal restricts the allowed platforms.
    device_name: str = Field(default="Unknown device", min_length=1, max_length=100)
    platform: DevicePlatform = "Unknown"


class DeviceSessionResponse(BaseModel):
    # Expose safe display fields only; omit refresh tokens, hashes and user IDs.
    session_id: str
    # Compare against sid from the verified JWT, not the client-supplied device name.
    # Mobile clients label the current session and hide its delete action when this is True.
    is_current: bool
    device_name: str
    platform: DevicePlatform
    # None supports older records without timestamps, provided all active-session checks pass.
    created_at: datetime | None
    last_seen_at: datetime | None
