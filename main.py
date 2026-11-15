# main.py — entrypoint (asyncio server).
#
# The harness runs `python3 -P -s /bot/main.py` (-P suppresses automatic
# sys.path insertion, so this file inserts its own directory first). It
# binds an ephemeral port (never 8080), registers it with the env
# (POST $MAKEFUN_ENV_URL/register {"port": N}; 200 first wins, 409 later),
# serves /healthz, then polls GET $MAKEFUN_ENV_URL/done until {"done": true}
# and exits 0. Without MAKEFUN_ENV_URL (local dev) it serves forever on
# the ephemeral port and prints the port.
#
# The server runs on one asyncio event loop in a daemon thread; the main
# thread does the env register + /done poll with blocking urllib, exactly
# like the threaded version did.

import asyncio
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

# -P: add our own directory so the sibling modules import.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server as server_mod
from store import Store


def _post_json(url, payload, timeout=10):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _get_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    store = Store()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    srv, port = loop.run_until_complete(
        server_mod.start_server(store, "127.0.0.1", 0))
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        if not env_url:
            print("forge listening on 127.0.0.1:%d" % port, flush=True)
            try:
                thread.join()
            except KeyboardInterrupt:
                pass
            return 0
        try:
            status, _body = _post_json(env_url + "/register", {"port": port})
        except urllib.error.HTTPError as err:
            if err.code == 409:
                # Another entry registered first; stand down quietly.
                return 0
            raise
        while True:
            try:
                data = _get_json(env_url + "/done")
                if data.get("done"):
                    break
            except Exception:
                pass
            time.sleep(0.5)
        return 0
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        try:
            srv.close()
            loop.run_until_complete(srv.wait_closed())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
