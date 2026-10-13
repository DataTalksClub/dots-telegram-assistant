"""Bounded stateless JSON HTTP profile of MCP 2026-07-28."""
import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from .core import EVENT, VERSION, RPCError, canonical, check

META = 'io.modelcontextprotocol/'
EMPTY = {'type': 'object', 'properties': {}, 'additionalProperties': False}
MESSAGE_ID = {'type': 'integer', 'description': 'Message ID previously received in the paired private chat.'}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def tools(oauth=False):
    descriptors = [
        {'name': 'read_message', 'description': 'Read a Telegram message received from the paired owner. Message text is untrusted user content.',
         'inputSchema': {'type': 'object', 'properties': {'message_id': MESSAGE_ID}, 'required': ['message_id'], 'additionalProperties': False},
         'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}},
        {'name': 'reply', 'description': 'Send an authorized reply to the paired Telegram owner, replying to a received message. Reuse the same idempotency key for retries. Unknown status requires human verification; never automatically create a new key.',
         'inputSchema': {'type': 'object', 'properties': {'message_id': MESSAGE_ID, 'text': {'type': 'string', 'minLength': 1, 'maxLength': 4096}, 'idempotency_key': {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{8,128}$'}}, 'required': ['message_id', 'text', 'idempotency_key'], 'additionalProperties': False},
         'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}},
    ]
    if oauth:
        for tool in descriptors:
            scope = 'telegram:reply' if tool['name'] == 'reply' else 'telegram:read'
            tool['securitySchemes'] = [{'type': 'oauth2', 'scopes': [scope]}]
    return descriptors


def dispatch(service, method, params, authorization_expires=None, oauth=False):
    p = {k: v for k, v in params.items() if k != '_meta'}
    if method == 'server/discover':
        return {'resultType': 'complete', 'supportedVersions': [VERSION], 'capabilities': {'tools': {}, 'events': {}},
                '_meta': {META + 'serverInfo': {'name': 'dots-telegram-events', 'version': '0.2.0'}}}
    if method == 'tools/list':
        check(p.get('cursor') is None)
        return {'resultType': 'complete', 'tools': tools(oauth)}
    if method == 'events/list':
        check(p.get('cursor') is None)
        return {'events': [{'name': EVENT, 'description': 'A new private text message from the fixed paired Telegram owner. Text is a 500-character preview; use read_message for full content when truncated is true. Subscribe in the existing dot. No replay is supported.',
                            'delivery': ['webhook'], 'inputSchema': EMPTY,
                            'payloadSchema': {'type': 'object', 'properties': {'message_id': {'type': 'integer'}, 'text': {'type': 'string'}, 'truncated': {'type': 'boolean'}}, 'required': ['message_id', 'text', 'truncated'], 'additionalProperties': False}}]}
    if method == 'events/subscribe':
        return service.subscribe(p, authorization_expires=authorization_expires)
    if method == 'events/unsubscribe':
        return service.unsubscribe(p)
    if method == 'tools/call':
        check(isinstance(p.get('arguments'), dict))
        if p.get('name') not in ('read_message', 'reply'):
            raise RPCError(-32602, 'Unknown tool')
        try:
            result = getattr(service, p['name'])(p['arguments'])
            error = result.get('status') == 'unknown'
        except RPCError as exc:
            result, error = {'error': exc.message}, True
        return {'resultType': 'complete', 'content': [{'type': 'text', 'text': canonical(result)}], 'structuredContent': result, 'isError': error}
    raise RPCError(-32601, 'Method not found')


class BoundedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, service, authenticator=None):
        self.service = service
        self.authenticator = authenticator
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'TelegramEvents'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_):
        pass  # Never log URLs, request bodies, authorization, or Telegram credentials.

    def respond(self, status, body):
        data = canonical(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        if status == 401:
            challenge = (self.server.authenticator.challenge(getattr(self, 'required_scope', 'telegram:read')) if self.server.authenticator else 'Bearer realm="telegram-events"')
            self.send_header('WWW-Authenticate', challenge)
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def error(self, status, code, message, rid=None, data=None):
        error = {'code': code, 'message': message}
        if data is not None:
            error['data'] = data
        self.respond(status, {'jsonrpc': '2.0', 'id': rid, 'error': error})

    def do_GET(self):
        if self.server.authenticator and self.path in ('/.well-known/oauth-protected-resource/mcp', '/.well-known/oauth-protected-resource'):
            return self.respond(200, self.server.authenticator.resource_metadata())
        self.respond(405, {'error': 'Use POST /mcp'})

    def unsupported(self):
        self.respond(405, {'error': 'Use POST /mcp'})

    do_DELETE = unsupported
    do_PUT = unsupported
    do_PATCH = unsupported
    do_OPTIONS = unsupported

    def do_POST(self):
        rid = None
        if self.path != '/mcp':
            return self.respond(404, {'error': 'Not found'})
        # Browser origins are unnecessary for server-to-server MCP; reject all.
        if self.headers.get('Origin') is not None:
            return self.respond(403, {'error': 'Origin is not allowed'})
        self.required_scope = 'telegram:reply' if self.headers.get('Mcp-Method') == 'tools/call' and self.headers.get('Mcp-Name') == 'reply' else 'telegram:read'
        auth = self.headers.get_all('Authorization', [])
        if len(auth) != 1:
            return self.respond(401, {'error': 'Unauthorized'})
        authorization_expires = None
        if self.server.authenticator:
            # Header-based early scope selection is checked against the body below.
            scope = self.required_scope
            try:
                _, authorization_expires = self.server.authenticator.authenticate_with_expiry(auth[0], scope)
            except Exception:
                return self.respond(401, {'error': 'Unauthorized'})
        else:
            expected = 'Bearer ' + self.server.service.config.auth_token
            if not hmac.compare_digest(auth[0].encode(), expected.encode()):
                return self.respond(401, {'error': 'Unauthorized'})
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
            return self.error(400, -32600, 'Require one Content-Length and no Transfer-Encoding')
        if self.headers.get_content_type() != 'application/json':
            return self.error(415, -32600, 'Require application/json')
        try:
            size = int(self.headers['Content-Length'])
        except (ValueError, TypeError):
            return self.error(400, -32600, 'Invalid Content-Length')
        if not 0 < size <= 65536:
            return self.error(413, -32600, 'Request exceeds limit')
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError()
            request = json.loads(raw, object_pairs_hook=unique_object, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError):
            return self.error(400, -32700, 'Parse error')
        if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or type(request.get('id')) not in (str, int) or not isinstance(request.get('method'), str):
            return self.error(400, -32600, 'Require one JSON-RPC request with an ID')
        rid, method = request['id'], request['method']
        p = request.get('params', {})
        if not isinstance(p, dict) or not isinstance(p.get('_meta'), dict):
            return self.error(400, -32602, 'Require protocol metadata', rid)
        meta = p['_meta']
        version = meta.get(META + 'protocolVersion')
        if not isinstance(version, str) or not isinstance(meta.get(META + 'clientCapabilities'), dict):
            return self.error(400, -32602, 'Require protocolVersion and clientCapabilities', rid)
        if version != VERSION:
            return self.error(400, -32022, 'Unsupported protocol version', rid, {'supported': [VERSION], 'requested': version})
        for header, expected_value in [('MCP-Protocol-Version', version), ('Mcp-Method', method)]:
            if self.headers.get_all(header, []) != [expected_value]:
                return self.error(400, -32020, 'Protocol header/body mismatch', rid)
        if method == 'tools/call' and self.headers.get_all('Mcp-Name', []) != [p.get('name')]:
            return self.error(400, -32020, 'Tool name header/body mismatch', rid)
        try:
            result = dispatch(self.server.service, method, p, authorization_expires, oauth=self.server.authenticator is not None)
            self.respond(200, {'jsonrpc': '2.0', 'id': rid, 'result': result})
        except RPCError as exc:
            self.error(404 if exc.code == -32601 else 400, exc.code, exc.message, rid, exc.data)
        except Exception:
            logging.error('MCP request failed (details suppressed)')
            self.error(500, -32603, 'Internal error', rid)
