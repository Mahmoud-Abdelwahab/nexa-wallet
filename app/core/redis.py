# FastAPI starts
#      ↓
# Create Redis client
#      ↓
# Create connection pool

# FastAPI stops
#      ↓
# Close Redis client
#      ↓
# Close connections


# Redis is infrastructure/configuration concern not a bussiness logic concern, so we can use a singleton pattern to create a single instance of the Redis client and connection pool that can be shared across the application. This ensures that we don't create multiple connections to the Redis server, which can lead to resource exhaustion and performance issues.

#            Refresh Session
#                        │
#           ┌────────────┴────────────┐
#           ▼                         ▼
#      Idle Expiration           Absolute Expiration
#         30 days                    90 days
#           │                         │
#        Redis TTL              stored timestamp  
# إذن الـ contract بتاعنا من دلوقتي:
# Policy	Value
# Access Token	Short-lived
# Refresh idle expiration	30 days
# Refresh absolute expiration	90 days
# Refresh token	Random opaque
# Storage	Redis
# Stored value	SHA-256 hash + session metadata
# Rotation	Every refresh
# Revocation	Supported
# Old refresh token reuse	Rejected
 
import redis.asyncio as redis

from app.core.config import settings


redis_client = redis.from_url(
    settings.REDIS_URL,
    decode_responses=True, # important to be true to avoid getting bytes instead of strings when getting values from Redis
)