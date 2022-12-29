import os
import socket
import random
import asyncio
import time
import fire

from fastapi_websocket_rpc import RpcMethodsBase, WebSocketRpcClient
from main import MTDA_FASTAPI

class Client:
    def __init__(self,session=None):
        self.loop = asyncio.get_event_loop()
        agent = MTDA_FASTAPI()
        self._agent = agent
        self._agent.is_remote = True
        if session is None:
            HOST = socket.gethostname()
            USER = os.getenv("USER")
            WORDS = "/usr/share/dict/words"
            if os.path.exists(WORDS):
                WORDS = open(WORDS).read().splitlines()
                name = random.choice(WORDS)
                if name.endswith("'s"):
                    name = name.replace("'s", "")
            elif USER is not None and HOST is not None:
                name = "%s@%s" % (USER, HOST)
            else:
                name = "mtda"
            self._session = os.getenv('MTDA_SESSION', name)
        else:
            self._session = session

    def session(self):
        return self._session

    def console_remote(self, host, screen):
        return self._agent.console_remote(host, screen)

    def monitor_remote(self, host, screen):
        return self._agent.monitor_remote(host, screen)

    def console_init(self):
        return self._agent.console_init()

    def console_prefix_key(self):
        return self._agent.console_prefix_key()

    def console_getkey(self):
        return self._agent.console_getkey()

    async def _target_on(self):
        async with WebSocketRpcClient("ws://134.86.62.135:9000/ws", RpcMethodsBase()) as self.client:
            res = await self.client.other.target_on(session=self._session)

    async def _target_off(self):
        async with WebSocketRpcClient("ws://134.86.62.135:9000/ws", RpcMethodsBase()) as self.client:
            res = await self.client.other.target_off(session=self._session) 

    async def _target_locked(self):
        async with WebSocketRpcClient("ws://134.86.62.135:9000/ws", RpcMethodsBase()) as self.client:
            res = await self.client.other.target_locked(session=self._session)
            
    async def _console_send(self,data,raw=False):
        async with WebSocketRpcClient("ws://134.86.62.135:9000/ws", RpcMethodsBase()) as self.client:
            res = await self.client.other.console_send(data=data,raw=raw,session=self._session)
            return eval(res.result)

    async def _monitor_send(self,data,raw=False):
        async with WebSocketRpcClient("ws://134.86.62.135:9000/ws", RpcMethodsBase()) as self.client:
            res = await self.client.other.console_send(data=data,raw=raw,session=self._session)

    async def _storage_status(self):
        async with WebSocketRpcClient("ws://134.86.62.135:9000/ws", RpcMethodsBase()) as self.client:
            res = await self.client.other.storage_status(session=self._session)
            return res.result

    async def _target_status(self):
        async with WebSocketRpcClient("ws://134.86.62.135:9000/ws", RpcMethodsBase()) as self.client:
            res = await self.client.other.target_status(session=self._session)
            return res.result

    async def _agent_version(self):
        async with WebSocketRpcClient("ws://134.86.62.135:9000/ws", RpcMethodsBase()) as self.client:
            res = await self.client.other.agent_version()
            return eval(res.result)
        
    async def _usb_ports(self):
        async with WebSocketRpcClient("ws://134.86.62.135:9000/ws", RpcMethodsBase()) as self.client:
            res = await self.client.other.usb_ports(session=self._session)
            return eval(res.result)

    async def _target_uptime(self):
        async with WebSocketRpcClient("ws://134.86.62.135:9000/ws", RpcMethodsBase()) as self.client:
            res = await self.client.other.target_uptime(session=self._session)
            return eval(res.result)

    def agent_version(self):
        return self.loop.run_until_complete(self._agent_version())

    def target_uptime(self):
        return self.loop.run_until_complete(self._target_uptime())

    def usb_ports(self):
        return self.loop.run_until_complete(self._usb_ports())

    def target_on(self):
        return self.loop.run_until_complete(self._target_on())

    def target_off(self):
        return self.loop.run_until_complete(self._target_off())

    def target_locked(self):
        return self.loop.run_until_complete(self._target_locked()) 

    def console_send(self,data,raw):
        return self.loop.run_until_complete(self._console_send(data,raw))

    def monitor_send(self,data,raw):
        return self.loop.run_until_complete(self._monitor_send(data,raw))

    def storage_status(self):
        return self.loop.run_until_complete(self._storage_status())

    def target_status(self):
        return self.loop.run_until_complete(self._target_status())



