"""Compatibility for websockets 15.0.1 proxy/TLS early disconnects.

Keep default proxy and TLS verification. Network failures still reach callers;
only make the upstream connection_lost cleanup safe before connection_made.
"""
from websockets.asyncio.client import ClientConnection, connect as _connect
from websockets.asyncio.messages import Assembler


class EarlyDisconnectConnection(ClientConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Proxy start_tls may invoke connection_lost before connection_made.
        # Normal connection_made replaces this unused assembler with transport
        # flow-control callbacks. No frames can arrive before that callback.
        self.recv_messages = Assembler(*self.max_queue)


def connect(uri, **kwargs):
    kwargs.setdefault("create_connection", EarlyDisconnectConnection)
    return _connect(uri, **kwargs)
