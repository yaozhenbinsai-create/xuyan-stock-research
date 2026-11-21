"""Non-SSE response identities use HTTP substitutes and an in-memory receipt store."""
import json
import unittest
from unittest.mock import patch

import httpx
from foundation import database as store
from research import model_transport
from test_transport_followthrough import ReceiptDB


class JsonReceiptReviewTests(unittest.TestCase):
    def complete(self,protocol='openai',identifier='chatcmpl_json_original'):
        frozen={'model':{'id':6,'name':'synthetic','protocol':protocol,'base_url':'https://synthetic.invalid/v1',
                         'model_identifier':'synthetic','recoverable_responses':protocol=='responses'},
                'config_hash':'synthetic-hash','budget':{'max_completion_tokens':None,'timeout_seconds':1}}
        database=ReceiptDB();real=httpx.AsyncClient
        value={'id':identifier,'model':'synthetic','usage':{'input_tokens':10,'output_tokens':2,'total_tokens':12}}
        if protocol=='responses':
            value.update(status='completed',output=[{'type':'message','content':[{'type':'output_text','text':'{"ok":true}'}]}])
        else:value['choices']=[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]
        transport=httpx.MockTransport(lambda req:httpx.Response(200,headers={'x-request-id':'header-only'},json=value))
        with patch.object(store,'db',database.db),patch.object(model_transport,'_secret',return_value='synthetic-key'),\
             patch.object(httpx,'AsyncClient',side_effect=lambda **kw:real(transport=transport,**kw)):
            text,cid=model_transport.complete('run',frozen,'system',{})
        self.assertEqual(json.loads(text),{'ok':True})
        return json.loads(database.calls[cid]['data'])

    def test_chat_json_completion_id_is_preserved_separately_from_header(self):
        for protocol in ('openai','xai'):
            with self.subTest(protocol=protocol):
                receipt=self.complete(protocol)
                self.assertEqual(receipt['provider_request_id'],'header-only')
                self.assertEqual(receipt['completion_id'],'chatcmpl_json_original')

    def test_non_string_completion_id_is_not_coerced_into_a_receipt(self):
        for identifier in (123,{'arbitrary':'text'},None):
            with self.subTest(identifier=identifier):self.assertEqual(self.complete(identifier=identifier)['completion_id'],'')

    def test_key_echo_cannot_enter_completion_identity(self):
        self.assertEqual(self.complete(identifier='chatcmpl_synthetic-key')['completion_id'],'')

    def test_background_responses_identity_and_frozen_endpoint_are_unchanged(self):
        receipt=self.complete('responses','resp_json_original')
        self.assertEqual(receipt['response_id'],'resp_json_original')
        self.assertEqual(receipt['recovery_base'],'https://synthetic.invalid/v1')
        self.assertTrue(receipt['recoverable'])
        self.assertEqual(receipt['completion_id'],'')
        self.assertEqual(receipt['provider_request_id'],'header-only')


if __name__=='__main__':unittest.main()
