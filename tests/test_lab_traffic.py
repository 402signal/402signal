"""Isolated classification contract; no live facilitator or signer calls."""
import os
import time
import unittest
from unittest.mock import patch
from live402 import lab_traffic, route, replay
from test_success_only_billing import _winner, _miss, _routing_accept, _verified, _settled, _payload, _headers

ORIGIN = "https://402signal-lab-ross.fly.dev"
URL = ORIGIN + "/base/payload/sha256"

class LabTrafficTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"LIVE402_LAB_ORIGINS": ORIGIN, "LIVE402_FIXTURE": "1"})
        self.env.start()
    def tearDown(self):
        self.env.stop()
    def test_exact_origin_and_no_client_claim_authority(self):
        self.assertTrue(lab_traffic.is_lab_url(URL))
        self.assertTrue(lab_traffic.is_lab_url("  " + URL + "  "))
        self.assertTrue(lab_traffic.is_lab_url("https://402SIGNAL-LAB-ROSS.fly.dev:443/base/payload/sha256"))
        for url in [ORIGIN+".evil/path", "https://evil/"+ORIGIN, "https://user@402signal-lab-ross.fly.dev/x", "http://402signal-lab-ross.fly.dev/x"]:
            self.assertFalse(lab_traffic.is_lab_url(url))
        bad = {"url": "https://ordinary.example/x", "lab_test": lab_traffic.PROTOCOL}
        self.assertEqual(route._bad_request(bad)[0], 400)
        with patch.dict(os.environ, {"LIVE402_LAB_ORIGINS": "bad origin"}):
            self.assertFalse(lab_traffic.is_lab_url(URL))
    def test_advertised_capability_and_matching_encoded_header(self):
        import base64, json
        body, headers = route._required_pair("https://402signal.com/route")
        self.assertEqual(body['lab_testing']['origins'], [ORIGIN])
        self.assertEqual(json.loads(base64.b64decode(headers['PAYMENT-REQUIRED'])), body)
    def test_lab_transparency_requirement_rejected_before_settlement(self):
        self.assertEqual(route._bad_request({"url": URL, "require_transparency": True})[0],400)
        self.assertEqual(route._bad_request({"url": URL, "require_route_binding": True})[0],400)
    def test_direct_probe_does_not_write_or_attach_history(self):
        win=_winner();win['url']=URL
        with patch('live402.route._lookup_claimed', return_value=None), patch('live402.probe.probe_url', return_value=win), \
             patch('live402.history.persist_route_batch') as persist, patch('live402.history.attach_to_result') as attach:
            route.run_probe({'url':URL})
            persist.assert_not_called();attach.assert_not_called()
    def test_lab_success_settles_but_does_not_promote_or_append(self):
        win=_winner();win['url']=URL
        with patch('live402.facilitator.verify', return_value=_verified()), patch('live402.route.run_probe', return_value=(200,win)), \
             patch('live402.facilitator.settle', return_value=_settled()) as settle, \
             patch('live402.history.mark_batch_settled') as promote, patch('live402.route._attach_pq_trust') as pq:
            code,body,headers=route._paid_execute({'url':URL,'lab_test':lab_traffic.PROTOCOL},_payload(),_routing_accept(),
                'https://402signal.com/route',None,time.monotonic()+100)
            self.assertEqual(code,200);self.assertTrue(body['billing']['settled']);self.assertIn('PAYMENT-RESPONSE',headers)
            self.assertEqual(body['lab_testing'],lab_traffic.classification())
            settle.assert_called_once();promote.assert_not_called();pq.assert_not_called()
            # Historical replay serialization preserves the public classification.
            encoded=replay._encode_outcome((code,body,headers))
            self.assertIn('self_test',encoded)
    def test_lab_miss_is_free_and_classified(self):
        with patch('live402.facilitator.verify', return_value=_verified()), patch('live402.route.run_probe', return_value=(503,_miss())), \
             patch('live402.facilitator.settle') as settle, patch('live402.history.mark_batch_settled') as promote, \
             patch('live402.route._attach_pq_trust') as pq:
            code,body,_=route._paid_execute({'url':URL},_payload(),_routing_accept(),'https://402signal.com/route',None,time.monotonic()+100)
            self.assertEqual(code,503);self.assertFalse(body['billing']['settled']);self.assertEqual(body['lab_testing'],lab_traffic.classification())
            settle.assert_not_called();promote.assert_not_called();pq.assert_not_called()
    def test_normal_success_keeps_history_and_pq(self):
        with patch('live402.facilitator.verify', return_value=_verified()), patch('live402.route.run_probe', return_value=(200,_winner())), \
             patch('live402.facilitator.settle', return_value=_settled()), patch('live402.history.mark_batch_settled') as promote, \
             patch('live402.route._attach_pq_trust',side_effect=lambda code,result,body:result) as pq:
            code,body,_=route._paid_execute({'url':'https://seller.example/x402'},_payload(),_routing_accept(),'https://402signal.com/route',None,time.monotonic()+100)
            self.assertEqual(code,200);self.assertNotIn('lab_testing',body);promote.assert_called_once();pq.assert_called_once()
