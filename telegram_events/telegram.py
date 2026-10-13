"""Fixed-destination Telegram Bot API client; credentials never appear in logs."""
import http.client
import json
import ssl


class Telegram:
    def __init__(self, token, chat_id):
        self.token, self.chat_id = token, chat_id

    def call(self, method, payload):
        connection = http.client.HTTPSConnection('api.telegram.org', timeout=40, context=ssl.create_default_context())
        try:
            body = json.dumps(payload).encode()
            connection.request('POST', '/bot' + self.token + '/' + method, body, {'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024 or response.status != 200:
                raise RuntimeError('Telegram request failed')
            value = json.loads(raw)
            if value.get('ok') is not True:
                raise RuntimeError('Telegram request failed')
            return value['result']
        except Exception:
            raise RuntimeError('Telegram request failed; outcome may be unknown') from None
        finally:
            connection.close()

    def updates(self, offset):
        return self.call('getUpdates', {'offset': offset, 'timeout': 25, 'limit': 100, 'allowed_updates': ['message']})

    def send(self, text, message_id):
        return self.call('sendMessage', {'chat_id': self.chat_id, 'text': text, 'reply_parameters': {'message_id': message_id}, 'link_preview_options': {'is_disabled': True}})
