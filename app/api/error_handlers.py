"""Keep internal failures and submitted secrets out of API error responses."""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


async def validation_error_handler(request: Request, error: Exception):
    if not isinstance(error, RequestValidationError):
        return await internal_error_handler(request, error)
    # Keep the field and reason, but never echo passwords, tokens, or body input.
    details = [
        {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
        for item in error.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": details})


async def internal_error_handler(request: Request, error: Exception):
    # The server logs the raised exception; clients never receive SQL or internals.
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected error occurred. Please try again later."},
    )
