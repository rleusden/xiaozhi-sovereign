import asyncio
import io
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from xiaozhi_gateway.config import Config
from xiaozhi_gateway.logging_safe import configure, event


class BackendSelectionTests(unittest.TestCase):
    def environment(self, folder, provider):
        root = Path(folder)
        token = root / 'device'
        token.write_text('SENTINEL_DEVICE')
        key = root / provider
        key.write_text('SENTINEL_PROVIDER')
        return {
            'PUBLIC_WS_URL': 'wss://example.invalid/xiaozhi/ws',
            'DEVICE_TOKEN_FILE': str(token),
            'OPENAI_API_KEY_FILE': str(root / 'openai'),
            'MISTRAL_API_KEY_FILE': str(root / 'mistral'),
        }

    def test_selected_secret_only_with_other_file_absent(self):
        for provider in ('openai', 'mistral'):
            for setting in ('AI_BACKEND', 'BACKEND'):
                with self.subTest(provider=provider, setting=setting), tempfile.TemporaryDirectory() as folder:
                    env = self.environment(folder, provider)
                    env[setting] = provider
                    with patch.dict(os.environ, env, clear=True):
                        config = Config.from_env()
                    self.assertEqual(config.backend, provider)
                    self.assertEqual(config.openai_api_key is not None, provider == 'openai')
                    self.assertEqual(config.mistral_api_key is not None, provider == 'mistral')
                    self.assertNotIn('SENTINEL', repr(config))

    def test_defaults_and_matching_aliases(self):
        for selectors in ({}, {'AI_BACKEND': '', 'BACKEND': ''},
                          {'AI_BACKEND': ' OPENAI ', 'BACKEND': 'openai'}):
            with tempfile.TemporaryDirectory() as folder:
                env = self.environment(folder, 'openai')
                env.update(selectors)
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(Config.from_env().backend, 'openai')

    def test_conflicts_and_invalid_values_fail_before_secret_access(self):
        cases = [
            {'AI_BACKEND': 'mistral', 'BACKEND': 'openai'},
            {'AI_BACKEND': 'mistarl'},
            {'AI_BACKEND': 'mistral', 'BACKEND': 'invalid'},
        ]
        for env in cases:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), patch('xiaozhi_gateway.config._secret') as secret:
                with self.assertRaises(RuntimeError):
                    Config.from_env()
                secret.assert_not_called()

    def test_blank_values_from_compose_mean_default(self):
        # compose.yaml passes ${VAR:-} as an empty string when .env lacks VAR.
        blanks = {
            name: ''
            for name in (
                'LOCAL_STT_BASE_URL', 'LOCAL_STT_TIMEOUT_SECONDS',
                'LOCAL_TTS_BASE_URL', 'LOCAL_TTS_TIMEOUT_SECONDS',
                'OLLAMA_BASE_URL', 'OLLAMA_MODEL', 'OLLAMA_TIMEOUT_SECONDS',
                'TIMEZONE', 'VAD_THRESHOLD', 'MISTRAL_TTS_GAIN',
            )
        }
        with tempfile.TemporaryDirectory() as folder:
            env = self.environment(folder, 'openai')
            env.update(blanks)
            with patch.dict(os.environ, env, clear=True):
                config = Config.from_env()
        self.assertEqual(config.local_stt_timeout_seconds, 30)
        self.assertEqual(config.local_tts_timeout_seconds, 30)
        self.assertTrue(config.local_stt_base_url.startswith('http://'))
        self.assertEqual(config.ollama_model, 'qwen2.5:7b')
        self.assertEqual(config.timezone, 'Europe/Amsterdam')
        self.assertEqual(config.vad_threshold, 300)
        self.assertEqual(config.mistral_tts_gain, 1.0)

    def test_malformed_number_gives_a_named_error(self):
        with tempfile.TemporaryDirectory() as folder:
            env = self.environment(folder, 'openai')
            env['LOCAL_STT_TIMEOUT_SECONDS'] = 'thirty'
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(RuntimeError, 'LOCAL_STT_TIMEOUT_SECONDS'):
                    Config.from_env()

    def test_local_backend_needs_no_provider_key(self):
        with tempfile.TemporaryDirectory() as folder:
            env = self.environment(folder, 'local')
            env['AI_BACKEND'] = 'local'
            with patch.dict(os.environ, env, clear=True):
                config = Config.from_env()
        self.assertEqual(config.backend, 'local')
        self.assertIsNone(config.openai_api_key)
        self.assertIsNone(config.mistral_api_key)

    def test_compute_url_defaults_to_the_bootstrap_host(self):
        with tempfile.TemporaryDirectory() as folder:
            env = self.environment(folder, 'openai')
            env['PUBLIC_WS_URL'] = 'wss://home.example:8443/xiaozhi/ws'
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(Config.from_env().public_compute_url, 'https://home.example')
            env['PUBLIC_COMPUTE_URL'] = 'https://home.example:8444/'
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(Config.from_env().public_compute_url, 'https://home.example:8444')
            env['PUBLIC_COMPUTE_URL'] = 'http://home.example:8444'
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(RuntimeError, 'PUBLIC_COMPUTE_URL'):
                    Config.from_env()

    def test_removed_hybrid_backend_is_rejected(self):
        with patch.dict(os.environ, {'AI_BACKEND': 'ollama-hybrid'}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'openai, mistral, local'):
                Config.from_env()


class LoggingPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_debug_keeps_metadata_and_suppresses_actual_wire_content(self):
        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        sink = io.StringIO()
        try:
            # Include an already-enabled child logger in the regression.
            logging.getLogger('websockets.client').setLevel(logging.DEBUG)
            configure('DEBUG')
            root.handlers = [logging.StreamHandler(sink)]
            root.setLevel(logging.DEBUG)

            async def handler(ws):
                await ws.recv()
                await ws.send('SENTINEL_RESPONSE')

            async with serve(handler, '127.0.0.1', 0) as server:
                port = server.sockets[0].getsockname()[1]
                async with connect(f'ws://127.0.0.1:{port}', additional_headers={
                    'Authorization': 'Bearer SENTINEL_TOKEN'
                }) as ws:
                    await ws.send('SENTINEL_TRANSCRIPT')
                    await ws.recv()
            event(logging.getLogger('xiaozhi'), 'response_complete', backend='mistral')
            logging.getLogger('xiaozhi').debug('application_debug_metadata')
            value = sink.getvalue()
            self.assertNotIn('SENTINEL', value)
            self.assertIn('response_complete', value)
            self.assertIn('application_debug_metadata', value)
        finally:
            root.handlers, root.level = saved_handlers, saved_level
