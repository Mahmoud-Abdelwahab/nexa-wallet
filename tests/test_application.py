import os
import unittest
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.main import app


class ApplicationEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_about_is_public_and_exposes_only_display_fields(self):
        with patch("app.api.routes.dependencies.redis_client") as redis:
            response = await self.client.get("/app/about")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(set(data), {"app_name", "api_version", "description", "supported_currencies"})
        self.assertEqual(data["app_name"], settings.app_name)
        self.assertEqual(data["api_version"], app.version)
        self.assertEqual(data["supported_currencies"], ["AED", "USD", "EUR"])
        self.assertEqual(redis.mock_calls, [])
        for field in ("secret_key", "database_url", "REDIS_URL", "SMTP_PASSWORD", "MOYASAR_SECRET_KEY"):
            self.assertNotIn(field, data)

    async def test_about_support_do_not_open_a_database_session(self):
        from app.database import get_db
        original = app.dependency_overrides.copy()

        def unexpected_database():
            raise AssertionError("Public app metadata must not access the database")

        app.dependency_overrides[get_db] = unexpected_database
        try:
            for path in ("/app/about", "/app/support"):
                self.assertEqual((await self.client.get(path)).status_code, 200)
        finally:
            app.dependency_overrides = original

    async def test_unconfigured_support_does_not_invent_contact_details(self):
        with patch.object(settings, "SUPPORT_EMAIL", None), patch.object(settings, "SUPPORT_URL", None):
            response = await self.client.get("/app/support")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "available": False, "email": None, "url": None,
            "message": "Support is currently unavailable.",
        })

    async def test_configured_support_returns_only_public_contact_data(self):
        for email, url in (("help@example.com", None), (None, "https://example.com/help"),
                           ("help@example.com", "https://example.com/help")):
            with patch.object(settings, "SUPPORT_EMAIL", email), patch.object(settings, "SUPPORT_URL", url):
                response = await self.client.get("/app/support")
            data = response.json()
            self.assertEqual(response.status_code, 200)
            self.assertTrue(data["available"])
            self.assertEqual(data["email"], email)
            self.assertEqual(data["url"], url)
            self.assertEqual(set(data), {"available", "email", "url", "message"})


class PublicContactSettingsTests(unittest.TestCase):
    def make_settings(self, **contacts):
        with patch.dict(os.environ, {"SECRET_KEY": "isolated-app-settings-test", "MOYASAR_SECRET_KEY": "sk_test_unused", "DEBUG": "false"}, clear=True):
            return Settings(_env_file=None, **contacts)

    def test_blank_contacts_are_optional(self):
        settings = self.make_settings(SUPPORT_EMAIL="", SUPPORT_URL="   ")
        self.assertIsNone(settings.SUPPORT_EMAIL)
        self.assertIsNone(settings.SUPPORT_URL)

    def test_bad_contact_configuration_is_rejected(self):
        for contacts in ({"SUPPORT_EMAIL": "not-an-email"}, {"SUPPORT_URL": "javascript:alert(1)"}):
            with self.assertRaises(ValidationError):
                self.make_settings(**contacts)
