import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode

from app.api.demo_gateway import make_handler


class Receiver(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length'])).decode()
        self.calls.append((self.headers.get('X-Webhook-Key'), parse_qs(body)))
        data = b'{"status":"accepted"}'
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class DemoGatewayTests(unittest.TestCase):
    def setUp(self):
        Receiver.calls = []
        self.receiver = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        self.gateway = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(
            f'http://127.0.0.1:{self.receiver.server_port}/webhook/tilda',
            'secret-for-test-only', {'https://example.test'}, limit=2))
        for server in (self.receiver, self.gateway):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        for server in (self.gateway, self.receiver):
            server.shutdown()
            server.server_close()

    def post(self, payload, origin='https://example.test'):
        conn = http.client.HTTPConnection('127.0.0.1', self.gateway.server_port, timeout=3)
        conn.request('POST', '/demo/lead', urlencode(payload), {
            'Origin': origin, 'Content-Type': 'application/x-www-form-urlencoded'})
        response = conn.getresponse()
        result = response.status, json.loads(response.read()), response.getheader('Access-Control-Allow-Origin')
        conn.close()
        return result

    def test_forwarding_uses_only_synthetic_identity_and_returns_no_secret(self):
        status, data, cors = self.post({'service': 'capital', 'area': '60', 'budget': '500000'})
        self.assertEqual((status, data, cors), (200, {'status': 'accepted'}, 'https://example.test'))
        secret, sent = Receiver.calls[0]
        self.assertEqual(secret, 'secret-for-test-only')
        self.assertEqual(sent['Name'], ['Демо заявка'])
        self.assertEqual(sent['Phone'], ['+79990000000'])
        self.assertTrue(sent['tranid'][0].startswith('github-demo-'))

    def test_rejects_arbitrary_fields_bad_origin_and_rate_limits(self):
        payload = {'service': 'capital', 'area': '60', 'budget': '500000'}
        self.assertEqual(self.post({**payload, 'name': 'visitor'})[0], 400)
        self.assertEqual(self.post(payload, 'https://other.test')[0], 403)
        self.assertEqual(self.post({**payload, 'area': '-1'})[0], 400)
        self.assertEqual(self.post(payload)[0], 200)
        self.assertEqual(self.post(payload)[0], 200)
        self.assertEqual(self.post(payload)[0], 429)
        self.assertEqual(len(Receiver.calls), 2)

    def test_gateway_does_not_forward_key_on_redirect(self):
        receiver_port = self.receiver.server_port

        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.send_response(307)
                self.send_header('Location', f'http://127.0.0.1:{receiver_port}/stolen')
                self.send_header('Content-Length', '0')
                self.end_headers()

        redirect = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        threading.Thread(target=redirect.serve_forever, daemon=True).start()
        gateway = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(
            f'http://127.0.0.1:{redirect.server_port}/start', 'test-key', {'https://example.test'}))
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection('127.0.0.1', gateway.server_port, timeout=3)
            conn.request('POST', '/demo/lead', urlencode({'service': 'capital', 'area': '60', 'budget': '500000'}),
                         {'Origin': 'https://example.test', 'Content-Type': 'application/x-www-form-urlencoded'})
            response = conn.getresponse()
            self.assertEqual(response.status, 503)
            response.read()
            conn.close()
            self.assertEqual(Receiver.calls, [])
        finally:
            for server in (gateway, redirect):
                server.shutdown()
                server.server_close()
