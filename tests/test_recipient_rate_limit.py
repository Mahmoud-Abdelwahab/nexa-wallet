"""Opt-in checks against a dedicated test Redis; only delete generated test keys.

Set RECIPIENT_TEST_REDIS_URL to enable these tests.
"""

import asyncio
import os
import secrets
import unittest

from redis.asyncio import Redis

from app.services.recipient_rate_limit import allow_recipient_lookup


@unittest.skipUnless(os.getenv("RECIPIENT_TEST_REDIS_URL"), "Requires dedicated test Redis")
class RecipientRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = Redis.from_url(os.environ["RECIPIENT_TEST_REDIS_URL"], decode_responses=True)
        self.user_id = secrets.randbits(64)
        self.other_id = secrets.randbits(64)
        self.minute_key = f"recipient_lookup_rate:{self.user_id}:minute"
        self.day_key = f"recipient_lookup_rate:{self.user_id}:day"

    async def asyncTearDown(self):
        await self.redis.delete(
            self.minute_key, self.day_key,
            f"recipient_lookup_rate:{self.other_id}:minute",
            f"recipient_lookup_rate:{self.other_id}:day",
        )
        await self.redis.aclose()

    async def test_concurrent_requests_allow_exactly_five_and_users_are_independent(self):
        results = await asyncio.gather(*[
            allow_recipient_lookup(self.redis, self.user_id, 50) for _ in range(12)
        ])
        self.assertEqual(sum(results), 5)
        self.assertTrue(await allow_recipient_lookup(self.redis, self.other_id, 50))
        self.assertGreater(await self.redis.ttl(self.minute_key), 0)
        self.assertLessEqual(await self.redis.ttl(self.minute_key), 60)
        self.assertGreater(await self.redis.ttl(self.day_key), 0)
        self.assertLessEqual(await self.redis.ttl(self.day_key), 86400)

    async def test_daily_limit_persists_across_minute_windows(self):
        for _ in range(10):
            # Simulate the minute window expiring, while retaining the day key.
            await self.redis.delete(self.minute_key)
            for _ in range(5):
                self.assertTrue(await allow_recipient_lookup(self.redis, self.user_id, 50))
        await self.redis.delete(self.minute_key)
        self.assertFalse(await allow_recipient_lookup(self.redis, self.user_id, 50))
        self.assertEqual(await self.redis.get(self.day_key), "51")

    async def test_later_requests_do_not_extend_windows_and_expiry_resets_counts(self):
        self.assertTrue(await allow_recipient_lookup(self.redis, self.user_id, 50))
        await self.redis.expire(self.minute_key, 1)
        await self.redis.expire(self.day_key, 1)
        self.assertTrue(await allow_recipient_lookup(self.redis, self.user_id, 50))
        self.assertLessEqual(await self.redis.ttl(self.minute_key), 1)
        self.assertLessEqual(await self.redis.ttl(self.day_key), 1)
        await asyncio.sleep(1.1)
        self.assertTrue(await allow_recipient_lookup(self.redis, self.user_id, 50))
        self.assertEqual(await self.redis.get(self.minute_key), "1")
        self.assertEqual(await self.redis.get(self.day_key), "1")
