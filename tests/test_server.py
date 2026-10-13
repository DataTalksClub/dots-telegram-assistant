"""Real localhost HTTP integration; all Telegram and callback services are fake."""
import http.client
import json
import threading
import unittest
from unittest.mock import Mock, patch

from telegram_events.core import Config, RPCError, Service, Store, VERSION
from telegram_events.server import BoundedServer, META


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(':memory:', 'owner')
        cls.telegram = Mock()
        cls.telegram.send.return_value = {'message_id': 101}
        cls.service = Service(Config('123:abc', 42, 42, 'a' * 32, frozenset({'example.com'})),
                              cls.store, Mock(), cls.telegram)
        cls.server = BoundedServer(('127.0.0.1', 0), cls.service)
        cls.worker = threading.Thread(target=cls.server.serve_forever, kwargs={'poll_interval': 0.01})
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.worker.join(2)
        cls.server.server_close()
        cls.store.close()

    def setUp(self):
        self.server.authenticator = None
        self.telegram.reset_mock()

    def rpc(self, method='tools/list', params=None, **kw):
        p = {'_meta': {META + 'protocolVersion': VERSION, META + 'clientCapabilities': {}}}
        if params:
            p.update(params)
        request = {'jsonrpc': '2.0', 'id': 'request1', 'method': method, 'params': p}
        return self.request(request, **kw)

    def request(self, request=None, *, raw=None, headers=None, remove=(), duplicates=(), path='/mcp', verb='POST'):
        data = raw if raw is not None else json.dumps(request).encode()
        req_method = request.get('method', '') if isinstance(request, dict) else ''
        hdr = {'Authorization': 'Bearer ' + 'a' * 32, 'Content-Type': 'application/json',
               'Content-Length': str(len(data)), 'MCP-Protocol-Version': VERSION, 'Mcp-Method': req_method}
        if req_method == 'tools/call':
            hdr['Mcp-Name'] = request.get('params', {}).get('name', '')
        hdr.update(headers or {})
        for key in remove:
            hdr.pop(key, None)
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.putrequest(verb, path)
            for key, value in list(hdr.items()) + list(duplicates):
                connection.putheader(key, value)
            connection.endheaders(data)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def test_authenticated_discovery_two_tools_and_event(self):
        status, headers, body = self.rpc('server/discover')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(body['id'], 'request1')
        self.assertEqual(body['result']['supportedVersions'], [VERSION])
        status, _, body = self.rpc()
        self.assertEqual(status, 200)
        self.assertEqual([tool['name'] for tool in body['result']['tools']], ['read_message', 'reply'])
        self.assertEqual(body['result']['resultType'], 'complete')
        status, _, body = self.rpc('events/list')
        self.assertEqual(body['result']['events'][0]['name'], 'telegram.message')

    def test_missing_wrong_and_duplicate_auth_denied(self):
        for options in [{'remove': ('Authorization',)}, {'headers': {'Authorization': 'Bearer wrong'}},
                        {'duplicates': [('Authorization', 'Bearer ' + 'a' * 32)]}]:
            with self.subTest(options=options):
                status, headers, body = self.rpc(**options)
                self.assertEqual(status, 401)
                self.assertIn('Bearer', headers['WWW-Authenticate'])
                self.assertNotIn('a' * 32, json.dumps(body))

    def test_browser_origin_and_wrong_path_rejected(self):
        self.assertEqual(self.rpc(headers={'Origin': 'https://example.com'})[0], 403)
        for path in ['/other', '/mcp?token=x', '/mcp/']:
            self.assertEqual(self.rpc(path=path)[0], 404)
        for verb in ['GET', 'PUT', 'DELETE', 'PATCH', 'OPTIONS']:
            self.assertEqual(self.request(verb=verb)[0], 405)

    def test_length_content_type_and_transfer_encoding(self):
        cases = [({'remove': ('Content-Length',)}, 400),
                 ({'duplicates': [('Content-Length', '1')]}, 400),
                 ({'headers': {'Transfer-Encoding': 'chunked'}}, 400),
                 ({'headers': {'Content-Length': '-1'}}, 413),
                 ({'headers': {'Content-Length': '65537'}}, 413),
                 ({'headers': {'Content-Length': 'invalid'}}, 400),
                 ({'headers': {'Content-Type': 'text/plain'}}, 415)]
        for options, expected in cases:
            with self.subTest(options=options):
                self.assertEqual(self.rpc(**options)[0], expected)

    def test_invalid_json_and_nonfinite_rejected(self):
        for raw in [b'{', b'\xff', b'{"x":NaN}', b'{"x":Infinity}',
                    b'{"id":1,"id":2}', b'{"nested":{"x":1,"x":2}}', b'[' * 10000 + b']' * 10000]:
            with self.subTest(raw=raw):
                status, _, body = self.request(raw=raw)
                self.assertEqual(status, 400)
                self.assertEqual(body['error']['code'], -32700)

    def test_batches_notifications_bad_ids_rejected(self):
        for request in [[], None, {'jsonrpc': '2.0', 'method': 'tools/list'},
                        {'jsonrpc': '2.0', 'method': 'tools/list', 'id': True},
                        {'jsonrpc': '1.0', 'method': 'tools/list', 'id': 1}]:
            with self.subTest(request=request):
                self.assertEqual(self.request(request)[2]['error']['code'], -32600)

    def test_protocol_metadata_required(self):
        for p in [None, [], {}, {'_meta': {}}, {'_meta': {META + 'protocolVersion': VERSION}},
                  {'_meta': {META + 'protocolVersion': VERSION, META + 'clientCapabilities': []}}]:
            request = {'jsonrpc': '2.0', 'method': 'tools/list', 'id': 1, 'params': p}
            self.assertEqual(self.request(request)[2]['error']['code'], -32602)

    def test_unsupported_version_and_header_mismatch(self):
        params = {'_meta': {META + 'protocolVersion': '2025-03-26', META + 'clientCapabilities': {}}}
        status, _, body = self.rpc(params=params)
        self.assertEqual(status, 400)
        self.assertEqual(body['error']['code'], -32022)
        self.assertEqual(body['error']['data']['supported'], [VERSION])
        for options in [{'headers': {'MCP-Protocol-Version': 'wrong'}}, {'headers': {'Mcp-Method': 'events/list'}},
                        {'remove': ('Mcp-Method',)}, {'duplicates': [('Mcp-Method', 'tools/list')]}]:
            self.assertEqual(self.rpc(**options)[2]['error']['code'], -32020)

    def test_list_cursor_and_unknown_method(self):
        for method in ['tools/list', 'events/list']:
            self.assertEqual(self.rpc(method, {'cursor': 'no-replay'})[2]['error']['code'], -32602)
        status, _, body = self.rpc('initialize')
        self.assertEqual(status, 404)
        self.assertEqual(body['error']['code'], -32601)

    def test_tool_errors_are_tool_results(self):
        status, _, body = self.rpc('tools/call', {'name': 'read_message', 'arguments': {'message_id': 999}})
        self.assertEqual(status, 200)
        self.assertTrue(body['result']['isError'])
        self.assertEqual(body['result']['resultType'], 'complete')
        status, _, body = self.rpc('tools/call', {'name': 'other', 'arguments': {}})
        self.assertEqual(body['error']['code'], -32602)
        self.assertEqual(status, 400)

    def test_http_read_and_reply_success_and_ambiguous_result(self):
        self.service.ingest([{'update_id': 1000, 'message': {'message_id': 1000, 'from': {'id': 42},
                            'chat': {'id': 42, 'type': 'private'}, 'text': 'owner message'}}])
        status, _, body = self.rpc('tools/call', {'name': 'read_message', 'arguments': {'message_id': 1000}})
        self.assertEqual(status, 200)
        self.assertFalse(body['result']['isError'])
        self.assertEqual(body['result']['structuredContent']['text'], 'owner message')
        args = {'message_id': 1000, 'text': 'authorized answer', 'idempotency_key': 'http_success'}
        status, _, body = self.rpc('tools/call', {'name': 'reply', 'arguments': args})
        self.assertEqual(status, 200)
        self.assertEqual(body['result']['structuredContent']['status'], 'sent')
        self.assertFalse(body['result']['isError'])
        self.telegram.send.side_effect = TimeoutError('secret token')
        try:
            args['idempotency_key'] = 'http_ambiguous'
            status, _, body = self.rpc('tools/call', {'name': 'reply', 'arguments': args})
            self.assertEqual(status, 200)
            self.assertTrue(body['result']['isError'])
            self.assertEqual(body['result']['structuredContent']['status'], 'unknown')
            self.assertNotIn('secret token', json.dumps(body))
        finally:
            self.telegram.send.side_effect = None

    def test_tool_header_must_match_body(self):
        status, _, body = self.rpc('tools/call', {'name': 'reply', 'arguments': {}}, headers={'Mcp-Name': 'read_message'})
        self.assertEqual(status, 400)
        self.assertEqual(body['error']['code'], -32020)
        self.telegram.send.assert_not_called()

    def test_callback_error_data_is_propagated(self):
        with patch.object(self.service, 'subscribe', side_effect=RPCError(-32015, 'Callback verification failed', {'reason': 'challenge_failed'})):
            status, _, body = self.rpc('events/subscribe', {'name': 'telegram.message'})
        self.assertEqual(status, 400)
        self.assertEqual(body['error']['code'], -32015)
        self.assertEqual(body['error']['data'], {'reason': 'challenge_failed'})

    def test_oauth_tool_security_schemes_and_required_challenge_scope(self):
        auth = Mock()
        auth.authenticate_with_expiry.return_value = ('owner', 4102444800)
        auth.challenge.return_value = 'Bearer'
        self.server.authenticator = auth
        self.service.config = Config('123:abc', 42, 42, 'a' * 32, frozenset({'example.com'}), auth_mode='oauth')
        try:
            status, _, body = self.rpc()
            self.assertEqual(status, 200)
            tools = body['result']['tools']
            self.assertEqual(tools[0]['securitySchemes'], [{'type': 'oauth2', 'scopes': ['telegram:read']}])
            self.assertEqual(tools[1]['securitySchemes'], [{'type': 'oauth2', 'scopes': ['telegram:reply']}])
            auth.authenticate_with_expiry.side_effect = ValueError('denied')
            self.rpc('tools/call', {'name': 'reply', 'arguments': {}})
            auth.challenge.assert_called_with('telegram:reply')
            self.rpc()
            auth.challenge.assert_called_with('telegram:read')
        finally:
            self.service.config = Config('123:abc', 42, 42, 'a' * 32, frozenset({'example.com'}))
        # Reset auth before checking the public protocol schema through authenticated bearer mode.
        self.server.authenticator = None
        _, _, body = self.rpc('events/list')
        schema = body['result']['events'][0]['payloadSchema']
        self.assertIn('truncated', schema['required'])
        self.assertEqual(schema['properties']['truncated'], {'type': 'boolean'})

    def test_oauth_metadata_is_public_but_not_on_non_get(self):
        auth = Mock()
        auth.authenticate_with_expiry.return_value = ('owner', 4102444800)
        auth.resource_metadata.return_value = {'resource': 'https://plugin.example.com/mcp'}
        self.server.authenticator = auth
        for path in ['/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp']:
            status, _, body = self.request(verb='GET', path=path, remove=('Authorization',))
            self.assertEqual(status, 200)
            self.assertEqual(body['resource'], 'https://plugin.example.com/mcp')
            self.assertEqual(self.request(verb='DELETE', path=path)[0], 405)
        auth.authenticate_with_expiry.assert_not_called()

    def test_oauth_read_reply_scopes_and_challenge(self):
        auth = Mock()
        auth.authenticate_with_expiry.return_value = ('owner', 4102444800)
        auth.challenge.return_value = 'Bearer resource_metadata="https://plugin.example.com/.well-known/oauth-protected-resource"'
        self.server.authenticator = auth
        self.rpc()
        auth.authenticate_with_expiry.assert_called_with('Bearer ' + 'a' * 32, 'telegram:read')
        self.rpc('tools/call', {'name': 'reply', 'arguments': {}})
        auth.authenticate_with_expiry.assert_called_with('Bearer ' + 'a' * 32, 'telegram:reply')
        auth.authenticate_with_expiry.side_effect = ValueError('private introspection secret')
        status, headers, body = self.rpc()
        self.assertEqual(status, 401)
        self.assertEqual(headers['WWW-Authenticate'], auth.challenge.return_value)
        self.assertNotIn('secret', json.dumps(body))

    def test_read_scope_cannot_send_reply_by_spoofing_headers(self):
        auth = Mock()
        auth.authenticate_with_expiry.return_value = ('owner', 4102444800)
        def authenticate(header, scope):
            if scope != 'telegram:read':
                raise ValueError('insufficient scope')
            return ('owner', 4102444800)
        auth.authenticate_with_expiry.side_effect = authenticate
        auth.challenge.return_value = 'Bearer'
        self.server.authenticator = auth
        args = {'name': 'reply', 'arguments': {'message_id': 10, 'text': 'hi', 'idempotency_key': 'request123'}}
        for hdr in [{'Mcp-Name': 'read_message'}, {'Mcp-Method': 'tools/list'},
                    {'Mcp-Method': 'tools/list', 'Mcp-Name': 'read_message'}]:
            status, _, body = self.rpc('tools/call', args, headers=hdr)
            self.assertEqual(status, 400)
            self.assertEqual(body['error']['code'], -32020)
        self.assertEqual(self.rpc('tools/call', args)[0], 401)
        self.telegram.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
