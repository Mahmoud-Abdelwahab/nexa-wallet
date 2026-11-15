# خريطة الاختبارات التعليمية:
# DeviceMetadataTests: حدود بيانات الجهاز وثبات الهوية والتواريخ أثناء Rotation.
# SessionAuthenticationTests: أكواد 401/503 وتشغيل DB خارج الـ Event Loop.
# DeviceSessionRedisTests: فلو HTTP كامل مع Redis حقيقية، ملكية الأجهزة والتزامن.
# الاختبارات الأخيرة اختيارية عبر DEVICE_SESSIONS_TEST_REDIS_URL؛ مفيش Redis flush.
import asyncio
import os
import secrets
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.security import create_access_token, decode_access_token, hash_password
from app.database import Base, get_db
from app.infrastructure.redis.refresh_token_redis_store import RefreshTokenRedisStore, active_session_key
from app.main import app
from app.models.user import User
from app.schemas.authentication import LoginRequest
from app.services.device_sessions_service import DeviceSessionsService
from app.services.refresh_token_service import RefreshTokenService


class DeviceMetadataTests(unittest.TestCase):
    def test_metadata_is_optional_bounded_and_platform_is_validated(self):
        login = LoginRequest(email="test@example.com", password="password")
        self.assertEqual((login.device_name, login.platform), ("Unknown device", "Unknown"))
        for changes in ({"device_name": "x" * 101}, {"device_name": ""}, {"platform": "untrusted"}):
            with self.assertRaises(ValidationError):
                LoginRequest(email="test@example.com", password="password", **changes)

    def test_rotation_preserves_identity_metadata_and_updates_last_seen(self):
        service = RefreshTokenService()
        _, _, session = service.create_refresh_session(1, 4, "Samsung S24", "Android")
        _, _, rotated = service.create_rotated_token(session)
        for field in ("session_id", "created_at", "device_name", "platform", "token_version", "absolute_expires_at"):
            self.assertEqual(getattr(rotated, field), getattr(session, field))
        self.assertGreaterEqual(rotated.last_seen_at, session.last_seen_at)


class SessionAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(self.engine)
        with self.factory() as db:
            db.add(User(id=42, username="Test User", email="test@example.com", mobile="12345678",
                        password_hash="unused", date_of_birth=date(2000, 1, 1), token_version=0))
            db.commit()
        self.old_overrides = app.dependency_overrides.copy()

        def test_db():
            with self.factory() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        _, _, self.session = RefreshTokenService().create_refresh_session(42)
        self.redis = AsyncMock()
        self.redis.eval.return_value = self.session.model_dump_json()
        self.patcher = patch("app.api.routes.dependencies.redis_client", self.redis)
        self.patcher.start()
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.patcher.stop()
        app.dependency_overrides = self.old_overrides
        self.engine.dispose()

    def headers(self, **changes):
        claims = {"sub": "42", "token_version": 0, "sid": self.session.session_id, **changes}
        return {"Authorization": "Bearer " + create_access_token(claims)}

    async def test_active_sid_is_checked_and_database_runs_in_worker(self):
        from app.api.routes import dependencies
        original = dependencies._load_user_sync
        loop_thread = threading.get_ident()

        def load(*args):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            return original(*args)

        with patch.object(dependencies, "_load_user_sync", side_effect=load):
            response = await self.client.get("/users/me", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.redis.eval.await_args.args[2], active_session_key(42, self.session.session_id))

    async def test_revoked_and_legacy_access_tokens_have_force_logout_code(self):
        self.redis.eval.return_value = None
        for headers in (self.headers(), self.headers(sid=None)):
            response = await self.client.get("/users/me", headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["detail"]["code"], "SESSION_REVOKED")

    async def test_password_version_change_also_signals_force_logout(self):
        response = await self.client.get("/users/me", headers=self.headers(token_version=99))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["code"], "SESSION_REVOKED")

    async def test_redis_outage_returns_retryable_503_not_force_logout(self):
        self.redis.eval.side_effect = RedisConnectionError("private")
        response = await self.client.get("/users/me", headers=self.headers())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "AUTH_SERVICE_UNAVAILABLE")
        self.assertNotIn("private", response.text)

    async def test_expired_access_token_has_refresh_code(self):
        from jose import jwt
        from app.core.config import settings
        token = jwt.encode({"sub": "42", "token_version": 0, "sid": self.session.session_id,
                            "exp": datetime.now(timezone.utc) - timedelta(seconds=10)},
                           settings.secret_key, algorithm=settings.algorithm)
        response = await self.client.get("/users/me", headers={"Authorization": "Bearer " + token})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["code"], "ACCESS_TOKEN_EXPIRED")

    async def test_current_session_delete_is_rejected_before_revoke(self):
        with patch("app.api.routes.authentication.DeviceSessionsService") as service:
            response = await self.client.delete(
                f"/auth/sessions/{self.session.session_id}", headers=self.headers()
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "CURRENT_SESSION_DELETE_FORBIDDEN")
        service.assert_not_called()
        # رفض الحذف ما يلغيش الجهاز الحالي؛ طلبه التالي يفضل ناجح.
        self.assertEqual((await self.client.get("/users/me", headers=self.headers())).status_code, 200)


@unittest.skipUnless(os.getenv("DEVICE_SESSIONS_TEST_REDIS_URL"), "Requires dedicated test Redis")
class DeviceSessionRedisTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password("OriginalPassword123!")

    async def asyncSetUp(self):
        self.redis = Redis.from_url(os.environ["DEVICE_SESSIONS_TEST_REDIS_URL"], decode_responses=True)
        self.store = RefreshTokenRedisStore(self.redis)
        self.tokens = RefreshTokenService()
        self.uid = secrets.randbelow(1000000000) + 1
        self.other_uid = self.uid + 1
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(self.engine)
        with self.factory() as db:
            for uid in (self.uid, self.other_uid):
                db.add(User(id=uid, username="Test User", email=f"{uid}@example.com", mobile=str(uid),
                            password_hash=self.password_hash, date_of_birth=date(2000, 1, 1), token_version=0))
            db.commit()
        self.old_overrides = app.dependency_overrides.copy()

        def test_db():
            with self.factory() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        self.patchers = [patch(f"app.api.routes.{module}.redis_client", self.redis)
                         for module in ("authentication", "dependencies", "password_reset")]
        for patcher in self.patchers:
            patcher.start()
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        for uid in (self.uid, self.other_uid):
            await self.store.revoke_all_sessions(uid)
            # Remove any expired/orphan test pointers too; never flush the database.
            for prefix in (f"auth_session:{uid}:*", f"user_refresh_sessions:{uid}"):
                keys = [key async for key in self.redis.scan_iter(match=prefix)]
                if keys:
                    await self.redis.delete(*keys)
        for patcher in self.patchers:
            patcher.stop()
        app.dependency_overrides = self.old_overrides
        self.engine.dispose()
        await self.redis.aclose()

    async def login(self, device="iPhone", platform="iOS", uid=None):
        response = await self.client.post("/auth/login", json={
            "email": f"{uid or self.uid}@example.com", "password": "OriginalPassword123!",
            "device_name": device, "platform": platform,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def headers(self, auth):
        return {"Authorization": "Bearer " + auth["access_token"]}

    async def test_list_revoke_blocks_target_access_and_refresh_but_not_other_device(self):
        iphone = await self.login()
        samsung = await self.login("Samsung S24", "Android")
        sid = decode_access_token(samsung["access_token"])["sid"]
        response = await self.client.get("/auth/sessions", headers=self.headers(iphone))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 2)
        for item in response.json():
            self.assertEqual(set(item), {"session_id", "is_current", "device_name", "platform", "created_at", "last_seen_at"})
            self.assertIsNotNone(item["created_at"])
        current_sid = decode_access_token(iphone["access_token"])["sid"]
        self.assertEqual({s["session_id"]: s["is_current"] for s in response.json()},
                         {current_sid: True, sid: False})
        # نفس القائمة من الجهاز التاني تعكس is_current؛ الاسم مش هو معيار المقارنة.
        other_view = await self.client.get("/auth/sessions", headers=self.headers(samsung))
        self.assertEqual({s["session_id"]: s["is_current"] for s in other_view.json()},
                         {current_sid: False, sid: True})
        response = await self.client.delete(f"/auth/sessions/{sid}", headers=self.headers(iphone))
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        # The same authentication dependency guards every protected route.
        for path in ("/users/me", "/wallets", "/auth/sessions"):
            response = await self.client.get(path, headers=self.headers(samsung))
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["detail"]["code"], "SESSION_REVOKED")
        response = await self.client.post("/auth/refresh", json={"refresh_token": samsung["refresh_token"]})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["code"], "SESSION_REVOKED")
        response = await self.client.get("/users/me", headers=self.headers(iphone))
        self.assertEqual(response.status_code, 200)

    async def test_refresh_keeps_sid_metadata_and_old_access_valid_until_revocation(self):
        auth = await self.login()
        sid = decode_access_token(auth["access_token"])["sid"]
        old_hash = self.tokens.hash_refresh_token(auth["refresh_token"])
        before = await self.store.get_session(old_hash)
        response = await self.client.post("/auth/refresh", json={"refresh_token": auth["refresh_token"]})
        self.assertEqual(response.status_code, 200)
        rotated = response.json()
        self.assertEqual(decode_access_token(rotated["access_token"])["sid"], sid)
        for token_pair in (auth, rotated):
            listed = await self.client.get("/auth/sessions", headers=self.headers(token_pair))
            self.assertEqual(listed.status_code, 200)
            self.assertEqual([(s["session_id"], s["is_current"]) for s in listed.json()], [(sid, True)])
        after = await self.store.get_active_session(self.uid, sid)
        self.assertEqual((after.device_name, after.platform, after.created_at),
                         (before.device_name, before.platform, before.created_at))
        self.assertGreaterEqual(after.last_seen_at, before.last_seen_at)
        self.assertIsNone(await self.store.get_session(old_hash))
        self.assertEqual((await self.client.get("/users/me", headers=self.headers(auth))).status_code, 200)
        # Reusing the old refresh token cannot destroy the rotated session.
        self.assertEqual((await self.client.post("/auth/refresh", json={"refresh_token": auth["refresh_token"]})).status_code, 401)
        self.assertIsNotNone(await self.store.get_active_session(self.uid, sid))

    async def test_other_users_cannot_list_or_revoke_session(self):
        own = await self.login()
        other = await self.login(uid=self.other_uid)
        sid = decode_access_token(other["access_token"])["sid"]
        response = await self.client.delete(f"/auth/sessions/{sid}", headers=self.headers(own))
        self.assertEqual(response.status_code, 404)
        items = (await self.client.get("/auth/sessions", headers=self.headers(own))).json()
        self.assertNotIn(sid, [s["session_id"] for s in items])
        self.assertEqual((await self.client.get("/users/me", headers=self.headers(other))).status_code, 200)

    async def test_logout_and_logout_all_invalidate_access(self):
        first, second = await self.login(), await self.login("Samsung", "Android")
        response = await self.client.post("/auth/logout", headers=self.headers(first), json={"refresh_token": first["refresh_token"]})
        self.assertEqual(response.status_code, 204)
        self.assertEqual((await self.client.get("/users/me", headers=self.headers(first))).status_code, 401)
        self.assertEqual((await self.client.get("/users/me", headers=self.headers(second))).status_code, 200)
        response = await self.client.post("/auth/logout-all", headers=self.headers(second))
        self.assertEqual(response.status_code, 204)
        self.assertEqual((await self.client.get("/users/me", headers=self.headers(second))).status_code, 401)

    async def test_logout_with_already_rotated_refresh_still_revokes_current_sid(self):
        auth = await self.login()
        response = await self.client.post("/auth/refresh", json={"refresh_token": auth["refresh_token"]})
        self.assertEqual(response.status_code, 200)
        rotated = response.json()
        response = await self.client.post("/auth/logout", headers=self.headers(auth),
                                          json={"refresh_token": auth["refresh_token"]})
        self.assertEqual(response.status_code, 204)
        self.assertEqual((await self.client.get("/users/me", headers=self.headers(rotated))).status_code, 401)
        self.assertEqual((await self.client.post("/auth/refresh", json={"refresh_token": rotated["refresh_token"]})).status_code, 401)

    async def test_logout_cannot_revoke_another_users_refresh_token(self):
        own = await self.login()
        other = await self.login(uid=self.other_uid)
        response = await self.client.post("/auth/logout", headers=self.headers(own),
                                          json={"refresh_token": other["refresh_token"]})
        self.assertEqual(response.status_code, 204)
        self.assertEqual((await self.client.get("/users/me", headers=self.headers(own))).status_code, 401)
        self.assertEqual((await self.client.get("/users/me", headers=self.headers(other))).status_code, 200)

    async def test_current_session_cannot_be_deleted_but_another_device_can_delete_it(self):
        auth, other = await self.login(), await self.login("Samsung", "Android")
        sid = decode_access_token(auth["access_token"])["sid"]
        response = await self.client.delete(f"/auth/sessions/{sid}", headers=self.headers(auth))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "CURRENT_SESSION_DELETE_FORBIDDEN")
        self.assertEqual((await self.client.get("/users/me", headers=self.headers(auth))).status_code, 200)
        self.assertEqual((await self.client.delete(f"/auth/sessions/{sid}", headers=self.headers(other))).status_code, 204)
        self.assertEqual((await self.client.get("/users/me", headers=self.headers(auth))).status_code, 401)
        self.assertEqual((await self.client.delete(f"/auth/sessions/{sid}", headers=self.headers(other))).status_code, 404)

    async def test_rotation_and_revoke_race_never_resurrects_session(self):
        # نبعث Rotate وRevoke سوا؛ أيا كان مين سبق، الجلسة تفضل ملغية في الآخر.
        for _ in range(15):
            _, old_hash, session = self.tokens.create_refresh_session(self.uid)
            await self.store.store_session(old_hash, session)
            _, new_hash, rotated = self.tokens.create_rotated_token(session)
            await asyncio.gather(
                self.store.rotate_session(old_hash, new_hash, rotated, 60),
                self.store.revoke_session_id(self.uid, session.session_id),
            )
            self.assertIsNone(await self.store.get_session(old_hash))
            self.assertIsNone(await self.store.get_session(new_hash))
            self.assertIsNone(await self.store.get_active_session(self.uid, session.session_id))

    async def test_concurrent_authentication_and_rotation_do_not_false_revoke(self):
        # عكس الاختبار السابق: من غير حذف، تبديل الهاش ما يسببش 401 غلط لنفس sid.
        auth = await self.login()
        sid = decode_access_token(auth["access_token"])["sid"]
        current_hash = self.tokens.hash_refresh_token(auth["refresh_token"])
        for _ in range(10):
            session = await self.store.get_session(current_hash)
            _, new_hash, rotated = self.tokens.create_rotated_token(session)
            results = await asyncio.gather(
                self.store.rotate_session(current_hash, new_hash, rotated, 60),
                *[self.store.get_active_session(self.uid, sid) for _ in range(10)],
            )
            self.assertTrue(results[0])
            self.assertTrue(all(s is not None for s in results[1:]))
            current_hash = new_hash

    async def test_expired_records_and_stale_indexes_are_not_active_or_listed(self):
        _, token_hash, session = self.tokens.create_refresh_session(self.uid)
        session.absolute_expires_at = datetime.now(timezone.utc) + timedelta(seconds=2)
        await self.store.store_session(token_hash, session)
        self.assertLessEqual(await self.redis.ttl(active_session_key(self.uid, session.session_id)), 2)
        await self.redis.expire(f"refresh_session:{token_hash}", 1)
        await asyncio.sleep(1.1)
        self.assertIsNone(await self.store.get_active_session(self.uid, session.session_id))
        self.assertEqual(await DeviceSessionsService(self.redis).list_sessions(self.uid, 0, session.session_id), [])
        self.assertFalse(await self.redis.sismember(f"user_refresh_sessions:{self.uid}", token_hash))
