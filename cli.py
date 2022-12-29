import os
import socket
import random
import asyncio
import time
import fire
import uvicorn
import sys

from fastapi import FastAPI
from fastapi_websocket_rpc import RpcMethodsBase, WebsocketRPCEndpoint

from client import Client
from main import MTDA_FASTAPI
from console.screen import ScreenOutput
 
class MTDA_Application:

    def __init__(self):
        self.agent = Client()
        self.exiting = False
        self.channel = "console"
        self.remote = "134.86.62.135"
        self.screen = ScreenOutput(self)

    def client(self):
        return self.agent

    def target_on(self):
        self.agent.target_on()
  
    def target_off(self):
        self.agent.target_off()

    def console_interactive(self):
        client = self.agent
        server = self.client()

        if sys.stdin.isatty():
            self.target_info()

        client.console_remote(self.remote,self.screen)
        client.monitor_remote(self.remote,self.screen)

        client.console_init()

        prefix_key = None
        if sys.stdin.isatty():
            prefix_key = client.console_prefix_key()

        while self.exiting is False:
            c = client.console_getkey()
            if prefix_key is not None and c == prefix_key:
                c = client.console_getkey()
                self.console_menukey(c)
            elif self.channel == 'console':
                server.console_send(c, True)
            else:
                server.monitor_send(c, True)

        print("\r\nThank you for using MTDA!\r\n\r\n")


    def console_menukey(self, c):
        client = self.agent
        server = self.client()
        if c == 'a':
            status = server.target_lock()
            if status is True:
                server.console_print("\r\n*** Target was acquired ***\r\n")
        elif c == 'b':
            self.console_pastebin()
        elif c == 'c':
            if self.screen.capture_enabled() is False:
                self.screen.print(b"\r\n*** Screen capture started... ***\r\n")
                self.screen.capture_start()
            else:
                self.screen.capture_stop()
                self.screen.print(b"\r\n*** Screen capture stopped ***\r\n")
        elif c == 'i':
            self.target_info()
        elif c == 'm':
            if self.channel == 'console':
                # Switch the alternate screen buffer
                print("\x1b[?1049h")  # same as tput smcup
                self.channel = 'monitor'
            else:
                # Return to the main screen buffer
                print("\x1b[?1049l")  # same as tput rmcup
                self.channel = 'console'
            client.console_toggle()
        elif c == 'p':
            previous_status = server.target_status()
            server.target_toggle()
            new_status = server.target_status()
            if previous_status != new_status:
                server.console_print(
                    "\r\n*** Target is now %s ***\r\n" % (new_status))
        elif c == 'q':
            self.screen.capture_stop()
            self.exiting = True
        elif c == 'r':
            status = server.target_unlock()
            if status is True:
                server.console_print("\r\n*** Target was released ***\r\n")
        elif c == 's':
            previous_status, writing, written = server.storage_status()
            server.storage_swap()
            new_status, writing, written = server.storage_status()
            if new_status != previous_status:
                server.console_print(
                    "\r\n*** Storage now connected to "
                    "%s ***\r\n" % (new_status))
        elif c == 't':
            server.toggle_timestamps()
        elif c == 'u':
            server.usb_toggle(1)


    def _human_readable_size(self, size):
        if size < 1024*1024:
            return "{:d} KiB".format(int(size/1024))
        elif size < 1024*1024*1024:
            return "{:d} MiB".format(int(size/1024/1024))
        else:
            return "{:.2f} GiB".format(size/1024/1024/1024)

    def target_info(self, args=None):
        sys.stdout.write("\rFetching target information...\r")
        sys.stdout.flush()

        # Get general information
        client = self.client()
        locked = " (locked)" if client.target_locked() else ""
        remote = "Local" if self.remote is None else self.remote
        session = client.session()
        storage_status, writing, written = eval(client.storage_status())
        writing = "WRITING" if writing is True else "IDLE"
        written = self._human_readable_size(written)
        tgt_status = client.target_status()
        uptime = ""
        if tgt_status == "ON":
            uptime = " (up %s)" % self.target_uptime()
        remote_version = client.agent_version()

        host = MTDA_FASTAPI()
        prefix_key = chr(ord(client.console_prefix_key()) + ord('a') - 1)

        # Print general information
        print("Host           : %s (%s)%30s\r" % (
              socket.gethostname(), host.version.__version__, ""))
        print("Remote         : %s (%s)%30s\r" % (
              remote, remote_version, ""))
        print("Prefix key:    : ctrl-%s\r" % (prefix_key))
        print("Session        : %s\r" % (session))
        print("Target         : %-6s%s%s\r" % (tgt_status, locked, uptime))
        print("Storage on     : %-6s%s\r" % (storage_status, locked))
        print("Storage writes : %s (%s)\r" % (written, writing))

        # Print status of the USB ports
        ports = client.usb_ports()
        for ndx in range(0, ports):
            status = client.usb_status(ndx+1)
            print("USB #%-2d        : %s\r" % (ndx+1, status))

        # Print video stream details
        #url = client.video_url()
        #if url is not None:
        #   print("Video stream   : %s\r" % (url)

    def target_uptime_cmd(self, args=None):
        uptime = self.target_uptime()
        print(uptime)
        return 0

    def target_uptime(self):
        result = ""
        uptime = self.client().target_uptime()
        days = int(uptime / (24 * 60 * 60.0))
        if days > 0:
            result = result + " %d days" % int(days)
            uptime = uptime % (24 * 60 * 60.0)
        hours = int(uptime / (60 * 60.0))
        if hours > 0:
            result = result + " %d hours" % int(hours)
            uptime = uptime % (60 * 60.0)
        minutes = int(uptime / 60.0)
        if minutes > 0:
            result = result + " %d minutes" % int(minutes)
            uptime = uptime % 60.0
        seconds = int(uptime)
        if seconds > 0:
            result = result + " %d seconds" % int(seconds)
        return result.strip()

    def run_server(self):
        obj = MTDA_FASTAPI()
        obj.load_config(None,True,None)
        obj.start()
        app =  FastAPI()
        endpoint = WebsocketRPCEndpoint(obj)
        endpoint.register_route(app, "/ws")
        uvicorn.run(app, host="134.86.62.135", port=9000)
  
if __name__=='__main__':
    fire.Fire(MTDA_Application)                                        
