"""Exercise credential destination protection against an isolated MySQL schema."""
from pathlib import Path
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from fastapi import HTTPException
from foundation import configuration, database as store
from tools.mysql_instance import create_isolated_database, drop_isolated_database


class CredentialDestinationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = 'stock_test_config_review_' + uuid.uuid4().hex[:10]
        cls.temp = tempfile.TemporaryDirectory(prefix='stock-config-review-')
        cls.env = patch.dict(os.environ, {'STOCK_DATA_ROOT': cls.temp.name,
            'STOCK_MYSQL_DATABASE': cls.schema, 'STOCK_DISABLE_WORKER': '1',
            'REVIEW_FAKE_KEY': 'fictional-isolated-key'})
        cls.env.start()
        create_isolated_database(cls.schema)

    @classmethod
    def tearDownClass(cls):
        try:
            drop_isolated_database(cls.schema)
        finally:
            cls.env.stop()
            cls.temp.cleanup()

    def model(self, **values):
        raw = {'name': 'credential-review-' + uuid.uuid4().hex[:8],
            'protocol': 'openai', 'model_identifier': 'isolated-model',
            'base_url': 'https://original.invalid/v1', 'api_key_env': 'REVIEW_FAKE_KEY'}
        raw.update(values)
        return raw

    def test_environment_credential_cannot_follow_changed_destination_even_when_clear_requested(self):
        for clear in (False, True):
            raw = self.model()
            saved = configuration.save('models', raw)
            with self.assertRaises(HTTPException) as error:
                configuration.save('models', {**raw, 'base_url': 'https://changed.invalid/v1',
                    'clear_api_key': clear}, saved['id'])
            self.assertEqual(error.exception.status_code, 422)
            self.assertEqual(configuration.item(configuration.model_row(saved['id']), 'models')['base_url'], raw['base_url'])

    def test_removing_old_environment_binding_allows_unconfigured_destination(self):
        raw = self.model()
        saved = configuration.save('models', raw)
        changed = configuration.save('models', {**raw, 'base_url': 'https://changed.invalid/v1',
            'api_key_env': ''}, saved['id'])
        self.assertFalse(changed['configured'])

    def test_explicit_new_key_is_encrypted_and_does_not_leak_to_public_config(self):
        raw = self.model()
        saved = configuration.save('models', raw)
        changed = configuration.save('models', {**raw, 'base_url': 'https://changed.invalid/v1',
            'api_key_env': '', 'api_key': 'new-fictional-isolated-key'}, saved['id'])
        self.assertEqual(changed['key_source'], 'encrypted')
        self.assertNotIn('new-fictional-isolated-key', store.dumps(changed))
        self.assertEqual(configuration.visible_model_key(saved['id']), 'new-fictional-isolated-key')

    def test_same_destination_edit_keeps_environment_binding(self):
        raw = self.model()
        saved = configuration.save('models', raw)
        changed = configuration.save('models', {**raw, 'name': raw['name'] + '-edited'}, saved['id'])
        self.assertTrue(changed['configured'])
        self.assertEqual(changed['key_source'], 'environment')
