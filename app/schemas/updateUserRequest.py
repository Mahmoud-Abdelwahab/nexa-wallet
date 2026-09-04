from pydantic import BaseModel

class UpdateUserRequest(BaseModel):
    username: str | None = None
    mobile: str | None = None

# PATCH /users/me

# Allowed:
# ├── username  optional
# └── mobile    optional

# Rules:
# ├── username → duplicates allowed
# ├── mobile   → must be unique
# └── at least one field must be provided


# as UpdateUserRequest used in patch request so we need to use model_fields_set to check if any field is provided or not, if not provided we will raise ValueError with message "At least one field must be provided" and status code 422 unprocessable entity.

# you need to differentitate between None and not provided, mayby user send {} didn't send the fields or send {"username": null} or {"mobile": null} so we need to check if the field is provided or not, if not provided we will raise ValueError with message "At least one field must be provided" and status code 422 unprocessable entity.

# if not request.model_fields_set:
#     raise ValueError("At least one field must be provided") 442 un processable request
