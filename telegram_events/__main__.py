import hashlib
import logging
import os
import signal
import threading

from .core import Config, Service, Store, canonical
from .network import CallbackClient
from .server import BoundedServer
from .telegram import Telegram


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = Config.from_env()
    authenticator = None
    owner = 'local-bearer'
    if config.auth_mode == 'oauth':
        from .auth import Authenticator, OAuthConfig
        auth_config = OAuthConfig.from_env()
        authenticator = Authenticator(auth_config)
        owner = os.environ['OAUTH_ISSUER'] + '|' + os.environ['OAUTH_SUBJECT']
    identity = hashlib.sha256(canonical([config.token.split(':')[0], config.user_id, config.chat_id, owner]).encode()).hexdigest()
    store = Store(config.state_path, identity)
    service = Service(config, store, CallbackClient(config.callback_hosts), Telegram(config.token, config.chat_id))
    stop = threading.Event()
    server = BoundedServer((config.host, config.port), service, authenticator)

    def poll():
        while not stop.is_set():
            try:
                service.ingest(service.telegram.updates(store.offset()))
            except Exception:
                logging.error('Telegram polling failed; retrying without advancing offset')
                stop.wait(5)

    def deliver():
        while not stop.is_set():
            try:
                if not service.deliver_one():
                    stop.wait(1)
            except Exception:
                logging.error('Event delivery worker failed; retrying')
                stop.wait(5)

    def shutdown(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    workers = [threading.Thread(target=poll), threading.Thread(target=deliver)]
    for worker in workers:
        worker.start()
    logging.info('Authenticated Telegram Events endpoint started')
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        for worker in workers:
            worker.join()
        store.close()


if __name__ == '__main__':
    main()
