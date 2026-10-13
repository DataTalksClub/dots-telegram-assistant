"""Small JSON clients. Never expose URLs, headers, raw bodies, or exception text."""
import http.client
import json
import socket
import urllib.error
import urllib.request


class APIError(Exception):
    def __init__(self, service, code, retryable=False, retry_after=0):
        self.service = service
        self.code = code
        self.retryable = retryable
        self.retry_after = min(max(retry_after, 0), 3600)
        super().__init__(f"{service} request failed ({code})")


def post_json(url, payload, headers=None, timeout=75):
    request = urllib.request.Request(url, json.dumps(payload).encode(),
                                     {"Content-Type": "application/json", **(headers or {})})
    # No redirects: a redirect must never forward credentials to another origin.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise APIError("remote", "oversize")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        delay = exc.headers.get("Retry-After", "0") if exc.headers else "0"
        # Telegram commonly places retry_after in its JSON error body.
        if exc.code == 429 and not (delay.isdigit() and int(delay) > 0):
            try:
                body = json.loads(exc.read(65536))
                parameters = body.get("parameters", {}) if isinstance(body, dict) else {}
                seconds = parameters.get("retry_after", 0) if isinstance(parameters, dict) else 0
                delay = str(seconds) if type(seconds) is int else "0"
            except (ValueError, OSError):
                delay = "0"
        raise APIError("remote", exc.code, exc.code in (408, 429) or exc.code >= 500,
                       int(delay) if delay.isdigit() else 0) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError, http.client.HTTPException):
        raise APIError("remote", "network", True) from None
    except (ValueError, UnicodeError):
        raise APIError("remote", "invalid_json", True) from None


class Telegram:
    def __init__(self, token, transport=post_json):
        self._base = f"https://api.telegram.org/bot{token}/"
        self.transport = transport

    def call(self, method, payload):
        try:
            result = self.transport(self._base + method, payload)
        except APIError as exc:
            raise APIError("telegram", exc.code, exc.retryable, exc.retry_after) from None
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise APIError("telegram", "invalid_response", True)
        if not result.get("ok"):
            raw_code = result.get("error_code")
            code = raw_code if type(raw_code) is int else "unknown"
            parameters = result.get("parameters")
            delay = parameters.get("retry_after", 0) if isinstance(parameters, dict) else 0
            raise APIError("telegram", code, code == 429 or isinstance(code, int) and code >= 500,
                           delay if isinstance(delay, int) else 0)
        return result.get("result")

    def updates(self, offset, timeout=30):
        value = self.call("getUpdates", {"offset": offset, "timeout": timeout,
                                       "limit": 100, "allowed_updates": ["message"]})
        if not isinstance(value, list):
            raise APIError("telegram", "invalid_updates", True)
        return value

    def send(self, chat_id, text):
        return self.call("sendMessage", {"chat_id": chat_id, "text": text,
                                        "link_preview_options": {"is_disabled": True}})


INSTRUCTIONS = (
    "You are a standalone personal assistant in Telegram. Be helpful, clear, and concise. "
    "You have only this bot's supplied conversation history. You are separate from ChatGPT "
    "and any assistant the user uses elsewhere; never claim to share their identity, memory, "
    "accounts, tools, or ongoing work. You have no browsing, filesystem, shell, or external "
    "action tools. Never claim you executed actions. Use plain text, not Markdown tables."
)


class OpenAI:
    def __init__(self, config, transport=post_json):
        self.config = config
        self.transport = transport

    def answer(self, messages):
        try:
            result = self.transport("https://api.openai.com/v1/responses", {
                "model": self.config.model, "instructions": INSTRUCTIONS,
                "input": messages, "max_output_tokens": self.config.max_output_tokens,
                "store": False, "tools": [],
            }, headers={"Authorization": f"Bearer {self.config.openai_key}"})
        except APIError as exc:
            raise APIError("openai", exc.code, exc.retryable or exc.code == 409, exc.retry_after) from None
        if not isinstance(result, dict) or result.get("status") not in ("completed", "incomplete"):
            raise APIError("openai", "invalid_response", True)
        texts = []
        output = result.get("output")
        if not isinstance(output, list):
            raise APIError("openai", "invalid_response", True)
        for item in output:
            if isinstance(item, dict) and item.get("type") == "message":
                content = item.get("content")
                if not isinstance(content, list):
                    raise APIError("openai", "invalid_response", True)
                for part in content:
                    if isinstance(part, dict):
                        value = part.get("text") if part.get("type") == "output_text" else part.get("refusal")
                        if isinstance(value, str):
                            texts.append(value)
        text = "\n".join(texts).strip()
        if not text:
            text = "I couldn't produce a text reply. Please try a shorter question."
        # Independent output guard even if the upstream response is malformed.
        text = text[:32000]
        if result.get("status") == "incomplete":
            text += "\n\n[Reply stopped at the generation limit or by the service.]"
        return text
