"""Research waiting is a saved plan setting, independent of model connections."""
import unittest
from unittest.mock import patch
from pydantic import ValidationError
from foundation import configuration
from research import model_transport


class ResearchWaitPolicyTests(unittest.TestCase):
    def freeze(self, override):
        plan={'id':3,'name':'isolated research','enabled':True,'model_ids':[1,2],
              'request_timeout_seconds':override}
        models=[{'id':1,'name':'first','enabled':True,'protocol':'openai','timeout_seconds':180},
                {'id':2,'name':'second','enabled':True,'protocol':'gemini','timeout_seconds':45}]
        with patch.object(configuration,'list_items',side_effect=lambda kind:
                          [plan] if kind=='plans' else models if kind=='models' else []):
            return model_transport.freeze_plan()

    def test_explicit_plan_wait_applies_to_each_saved_channel_without_mutating_models(self):
        frozen=self.freeze(600)
        self.assertEqual(frozen['saved_order'],[1,2])
        self.assertEqual(frozen['budget']['timeout_seconds'],600)
        self.assertEqual([model_transport.select_model(frozen,i)['budget']['timeout_seconds'] for i in (0,1)],[600,600])
        self.assertEqual([m['model']['timeout_seconds'] for m in frozen['chain']],[180,45])
        self.assertIsNone(frozen['budget']['max_requests'])

    def test_old_plan_without_override_uses_each_connection_wait(self):
        frozen=self.freeze(None)
        self.assertEqual([model_transport.select_model(frozen,i)['budget']['timeout_seconds'] for i in (0,1)],[180,45])

    def test_saved_plan_wait_validation_and_backward_compatibility(self):
        raw={'name':'isolated','model_ids':[1]}
        self.assertIsNone(configuration.PlanSave.model_validate(raw).request_timeout_seconds)
        self.assertEqual(configuration.PlanSave.model_validate({**raw,'request_timeout_seconds':600}).request_timeout_seconds,600)
        for bad in (0,9,601):
            with self.assertRaises(ValidationError):
                configuration.PlanSave.model_validate({**raw,'request_timeout_seconds':bad})
