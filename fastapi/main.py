#!/usr/bin/env python
import importlib
import os,sys,time,threading,re,socket
import json,gevent,zmq,socket
import configparser
import traceback
import uvicorn
import os
import traceback
import asyncio


from fastapi import FastAPI
from fastapi_websocket_rpc import RpcMethodsBase, WebsocketRPCEndpoint
from asyncinit import asyncinit
from utils import RepeatTimer
from console.logger import ConsoleLogger
from storage.writer import AsyncImageWriter
import keyboard.controller
import power.controller
import constants as CONSTS
from support.usb import Composite
from console.remote import RemoteConsole, RemoteMonitor
from console.input import ConsoleInput
import __version__


DEFAULT_PREFIX_KEY = 'ctrl-a'
DEFAULT_PASTEBIN_EP = "http://pastebin.com/api/api_post.php"

_NOPRINT_TRANS_TABLE = {
    i: '.' for i in range(0, sys.maxunicode + 1) if not chr(i).isprintable()
}

def _make_printable(s):
    return s.translate(_NOPRINT_TRANS_TABLE)

class MTDA_FASTAPI(RpcMethodsBase):
    def __init__(self):
        self.config_files = ['mtda.ini']
        self.power_controller = None
        self.storage_controller = None
        self.console = None
        self.monitor = None
        self.video = None
        self._www = None
        self.keyboard = None
        self.assistant = None
        self.is_server = None
        self.name = socket.gethostname()
        self.mtda = self
        self.debug_level = 0
        self.fuse = False
        self.env={}
        self.conport = 5558
        self._time_from_str = None
        self._time_until_str = None
        self.monitor_logger = None
        self.is_remote = False
        self.console_output = False
        self._power_lock = threading.Lock()
        self._session_lock = threading.Lock()
        self.power_monitors = []
        self._sessions = {}
        self._storage_opened = False
        self.usb_switches = []
        self._socket_lock = threading.Lock()
        self._lock_owner = None
        self._lock_expiry = None
        self._power_expiry = None
        self.prefix_key = self._prefix_key_code(DEFAULT_PREFIX_KEY)
        self._lock_owner = None
        self.version = __version__
        self.console_output = None
        self.monitor_output = None
        self.loop = asyncio.get_event_loop()

        home = os.getenv('HOME', '')
        if home != '':
            self.config_files.append(os.path.join(home, '.mtda', 'config'))

        # Config file in /etc/mtda/config
        if os.path.exists('/etc'):
            self.config_files.append(os.path.join('/etc', 'mtda', 'config'))
 

    def _session_check(self, session=None):
        self.mtda.debug(3, "main._session_check(%s)" % str(session))

        events = []
        now = time.monotonic()
        power_off = False
        result = None

        with self._session_lock:
            # Register new session
            if session is not None:
                if session not in self._sessions:
                    events.append("ACTIVE %s" % session)
                self._sessions[session] = now + self._session_timeout

            # Check for inactive sessions
            inactive = []
            for s in self._sessions:
                left = self._sessions[s] - now
                self.mtda.debug(3, "session %s: %d seconds" % (s, left))
                if left <= 0:
                    inactive.append(s)
            for s in inactive:
                events.append("INACTIVE %s" % s)
                self._sessions.pop(s, "")

                # Check if we should arm the auto power-off timer
                # i.e. when the last session is removed and a power timeout
                # was set
                if len(self._sessions) == 0 and self._power_timeout > 0:
                    self._power_expiry = now + self._power_timeout
                    self.mtda.debug(2, "device will be powered down in {} "
                                       "seconds".format(self._power_timeout))

            if len(self._sessions) > 0:
                # There are active sessions: reset power expiry
                self._power_expiry = None
            else:
                # Otherwise check if we should auto-power off the target
                if self._power_expiry is not None and now > self._power_expiry:
                    self._lock_expiry = 0
                    power_off = True

            # Release device if the session owning the lock is idle
            if self._lock_owner is not None:
                if session == self._lock_owner:
                    self._lock_expiry = now + self._lock_timeout
                elif now >= self._lock_expiry:
                    events.append("UNLOCKED %s" % self._lock_owner)
                    self._lock_owner = None

        # Send event sessions generated above
        for e in events:
            self._session_event(e)

        # Check if we should auto power-off the device
        if power_off is True:
            self._target_off()
            self.mtda.debug(2, "device powered down after {} seconds of "
                               "inactivity".format(self._power_timeout))

        self.mtda.debug(3, "main._session_check: %s" % str(result))
        return result

    def console_getkey(self):
        self.mtda.debug(3, "main.console_getkey()")
        result = None
        try:
            result = self.console_input.getkey()
        except AttributeError:
            print("Initialize the console using console_init first")
        self.mtda.debug(3, "main.console_getkey(): %s" % str(result))
        return result

    def console_prefix_key(self):
        self.mtda.debug(3, "main.console_prefix_key()")
        return self.prefix_key

    def console_locked(self, session=None):
        self.mtda.debug(3, "main.console_locked()")

        self._session_check(session)
        result = self._check_locked(session)

        self.mtda.debug(3, "main.console_locked(): %s" % str(result))
        return result

    def console_remote(self, host, screen):
        self.mtda.debug(3, "main.console_remote()")

        result = None
        if self.is_remote is True:
            # Stop previous remote console
            if self.console_output is not None:
                self.console_output.stop()
            if host is not None:
                # Create and start our remote console
                self.console_output = RemoteConsole(host, self.conport, screen)
                self.console_output.start()
            else:
                self.console_output = None

        self.mtda.debug(3, "main.console_remote(): %s" % str(result))
        return result

    def console_init(self):
        self.console_input = ConsoleInput()
        self.console_input.start()

    def console_prefix_key(self):
        self.mtda.debug(3, "main.console_prefix_key()")
        return self.prefix_key

    def _check_locked(self, session):
        self.mtda.debug(3, "main._check_locked()")

        owner = self.target_owner()
        if owner is None:
            return False
        status = False if session == owner else True
        return status

    def _prefix_key_code(self, prefix_key):
        prefix_key = prefix_key.lower()
        key_dict = {'ctrl-a': '\x01', 'ctrl-b': '\x02', 'ctrl-c': '\x03',
                    'ctrl-d': '\x04', 'ctrl-e': '\x05', 'ctrl-f': '\x06',
                    'ctrl-g': '\x07', 'ctrl-h': '\x08', 'ctrl-i': '\x09',
                    'ctrl-j': '\x0A', 'ctrl-k': '\x0B', 'ctrl-l': '\x0C',
                    'ctrl-n': '\x0E', 'ctrl-o': '\x0F', 'ctrl-p': '\x10',
                    'ctrl-q': '\x11', 'ctrl-r': '\x12', 'ctrl-s': '\x13',
                    'ctrl-t': '\x14', 'ctrl-u': '\x15', 'ctrl-v': '\x16',
                    'ctrl-w': '\x17', 'ctrl-x': '\x18', 'ctrl-y': '\x19',
                    'ctrl-z': '\x1A'}

        if prefix_key in key_dict:
            key_ascii = key_dict[prefix_key]
            return key_ascii
        else:
            raise ValueError("the prefix key specified '{0}' is not "
                             "supported".format(prefix_key))
    
    def _session_event(self, info):
        self.mtda.debug(3, "main._session_event(%s)" % str(info))

        result = None
        if info is not None:
            self.notify(CONSTS.EVENTS.SESSION, info)

        self.mtda.debug(3, "main._session_event: %s" % str(result))
        return result

    def _env_for_script(self):
        variant = 'unknown'
        if 'variant' in self.env:
            variant = self.env['variant']
        return {
            "env": self.env,
            "mtda": self,
            #"scripts": mtda.scripts,
            "sleep": gevent.sleep,
            "variant": variant
        }

    async def console_send(self, data, raw=False, session=None):
        self.mtda.debug(3, "main.console_send()")

        self._session_check(session)
        result = None
        if self.console_locked(session) is False and \
           self.console_logger is not None:
           result = self.console_logger.write(data, raw)

        self.mtda.debug(3, "main.console_send(): %s" % str(result))
        return result

    async def monitor_send(self, data, raw=False, session=None):
        self.mtda.debug(3, "main.monitor_send()")

        self._session_check(session)
        result = None
        if self.console_locked(session) is False and \
           self.monitor_logger is not None:
            result = self.monitor_logger.write(data, raw)

        self.mtda.debug(3, "main.monitor_send(): %s" % str(result))
        return result

    def debug(self, level, msg):
        if self.debug_level >= level:
            if self.debug_level == 0:
                prefix = "# "
            else:
                prefix = "# debug%d: " % level
            msg = str(msg).replace("\n", "\n%s ... " % prefix)
            lines = msg.splitlines()
            sys.stderr.buffer.write(prefix.encode("utf-8"))
            for line in lines:
                sys.stderr.buffer.write(_make_printable(line).encode("utf-8"))
                sys.stderr.buffer.write(b"\n")
                sys.stderr.buffer.flush()

    def env_set(self, name, value, session=None):
        self.mtda.debug(3, "env_set()")

        self._session_check(session)
        result = None

        if name in self.env:
            old_value = self.env[name]
            result = old_value
        else:
            old_value = value

        self.env[name] = value
        self.env["_%s" % name] = old_value

        self.mtda.debug(3, "env_set(): %s" % str(result))
        return result

    def exec_power_off_script(self):
        self.mtda.debug(3, "main.exec_power_off_script()")

        if self.power_off_script:
            exec(self.power_off_script, {"env": self.env, "mtda": self})

    def load_main_config(self, parser):
        self.mtda.debug(3, "main.load_main_config()")

        # Name of this agent
        self.name = parser.get('main', 'name', fallback=self.name)

        self.mtda.debug_level = int(
            parser.get('main', 'debug', fallback=self.mtda.debug_level))
        self.mtda.fuse = parser.getboolean(
            'main', 'fuse', fallback=self.mtda.fuse)

    def _load_device_scripts(self):
        env = self._env_for_script()
        mtda.scripts.load_device_scripts(env['variant'], env)
        for e in env.keys():
            setattr(mtda.scripts, e, env[e])

    def load_keyboard_config(self, parser):
        self.mtda.debug(3, "main.load_keyboard_config()")

        try:
            # Get variant
            variant = parser.get('keyboard', 'variant')
            # Try loading its support class
            mod = importlib.import_module("keyboard." + variant)
            factory = getattr(mod, 'instantiate')
            self.keyboard = factory(self)
            # Configure the keyboard controller
            self.keyboard.configure(dict(parser.items('keyboard')))
        except configparser.NoOptionError:
            print('keyboard controller variant not defined!', file=sys.stderr)
        except ImportError:
            print('keyboard controller "%s" could not be found/loaded!' % (
                variant), file=sys.stderr)

    def load_console_config(self, parser):
        self.mtda.debug(3, "main.load_console_config()")

        try:
            # Get variant
            variant = parser.get('console', 'variant')
            # Try loading its support class
            mod = importlib.import_module("console." + variant)
            factory = getattr(mod, 'instantiate')
            self.console = factory(self)
            print(self.console)
            self.console.variant = variant
            # Configure the console
            config = dict(parser.items('console'))
            self.console.configure(config)
            timestamps = parser.getboolean('console', 'timestamps',
                                           fallback=None)
            self._time_from_pwr = timestamps
            if timestamps is None or timestamps is True:
                # check 'time-from' / 'time-until' settings if timestamps is
                # either yes or unspecified
                if 'time-until' in config:
                    self._time_until_str = config['time-until']
                    self._time_from_pwr = True
                if 'time-from' in config:
                    self._time_from_str = config['time-from']
                    self._time_from_pwr = False
        except configparser.NoOptionError:
            print('console variant not defined!', file=sys.stderr)
        except ImportError:
            print('console "%s" could not be found/loaded!' % (
                variant), file=sys.stderr)

    def load_storage_config(self, parser):
        self.mtda.debug(3, "main.load_storage_config()")

        try:
            # Get variant
            variant = parser.get('storage', 'variant')
            # Try loading its support class
            mod = importlib.import_module("storage." + variant)
            factory = getattr(mod, 'instantiate')
            self.storage_controller = factory(self)
            self._writer = AsyncImageWriter(self, self.storage_controller)
            # Configure the storage controller
            self.storage_controller.configure(dict(parser.items('storage')))
        except configparser.NoOptionError:
            print('storage controller variant not defined!', file=sys.stderr)
        except ImportError as e:
            traceback.print_exc()
            print(f'The error in storage config {e}')
            print('power controller "%s" could not be found/loaded!' % (
                variant), file=sys.stderr)

    def load_usb_config(self, parser):
        self.mtda.debug(3, "main.load_usb_config()")

        try:
            # Get number of ports
            usb_ports = int(parser.get('usb', 'ports'))
            for port in range(0, usb_ports):
                port = port + 1
                section = "usb" + str(port)
                if parser.has_section(section):
                    self.load_usb_port_config(parser, section)
        except configparser.NoOptionError:
            usb_ports = 0

    def load_usb_port_config(self, parser, section):
        self.mtda.debug(3, "main.load_usb_port_config()")

        try:
            # Get attributes
            className = parser.get(section, 'class', fallback="")
            variant = parser.get(section, 'variant')

            # Try loading its support class
            mod = importlib.import_module("mtda_amqp.usb." + variant)
            factory = getattr(mod, 'instantiate')
            usb_switch = factory(self)

            # Configure and probe the USB switch
            usb_switch.configure(dict(parser.items(section)))
            usb_switch.probe()

            # Store other attributes
            usb_switch.className = className

            # Add this USB switch
            self.usb_switches.append(usb_switch)
        except configparser.NoOptionError:
            print('usb switch variant not defined!', file=sys.stderr)
        except ImportError:
            print('usb switch "%s" could not be found/loaded!' % (
                variant), file=sys.stderr)

    def load_power_config(self, parser):
        self.mtda.debug(3, "main.load_power_config()")

        try:
            # Get variant
            variant = parser.get('power', 'variant')
            # Try loading its support class
            mod = importlib.import_module("power." + variant)
            factory = getattr(mod, 'instantiate')
            self.power_controller = factory(self)
            self.power_controller.variant = variant
            # Configure the power controller
            self.power_controller.configure(dict(parser.items('power')))
        except configparser.NoOptionError:
            print('power controller variant not defined!', file=sys.stderr)
        except ImportError:
            print('power controller "%s" could not be found/loaded!' % (
                variant), file=sys.stderr)

    def load_environment(self, parser):
        self.mtda.debug(3, "main.load_environment()")

        for opt in parser.options('environment'):
            value = parser.get('environment', opt)
            self.mtda.debug(4, "main.load_environment(): "
                               "%s => %s" % (opt, value))
            self.env_set(opt, value)

    def load_timeouts_config(self, parser):
        self.mtda.debug(3, "main.load_timeouts_config()")

        result = None
        s = "timeouts"

        self._lock_timeout = int(parser.get(s, "lock",
                                 fallback=CONSTS.DEFAULTS.LOCK_TIMEOUT))
        self._power_timeout = int(parser.get(s, "power",
                                  fallback=CONSTS.DEFAULTS.POWER_TIMEOUT))
        self._session_timeout = int(parser.get(s, "session",
                                    fallback=CONSTS.DEFAULTS.SESSION_TIMEOUT))
        self._lock_timeout = self._lock_timeout * 60
        self._power_timeout = self._power_timeout * 60
        self._session_timeout = self._session_timeout * 60

        self.mtda.debug(3, "main.load_timeouts_config: %s" % str(result))
        return result

    def load_config(self, remote=None, is_server=False, config_files=None):
        self.mtda.debug(3, "main.load_config()")
        

        if config_files is None:
            config_files = os.getenv('MTDA_CONFIG', self.config_files)
        print(config_files)

        self.mtda.debug(2, "main.load_config(): "
                           "config_files={}".format(config_files))

        self.remote = remote
        self.is_remote = remote is not None
        self.is_server = is_server
        parser = configparser.ConfigParser()
        configs_found = parser.read(config_files)
        print(configs_found)
        if configs_found is False:
            return

        if parser.has_section('main'):
            self.load_main_config(parser)
        if parser.has_section('pastebin'):
            self.load_pastebin_config(parser)
        #if parser.has_section('remote'):
        #    self.load_remote_config(parser)
        self.load_timeouts_config(parser)
        if parser.has_section('ui'):
            self.load_ui_config(parser)
        if self.is_remote is False and is_server is True:
            '''
            if parser.has_section('assistant'):
                self.load_assistant_config(parser)
            if parser.has_section('environment'):
                self.load_environment(parser)
            '''
            if parser.has_section('power'):
                self.load_power_config(parser)
            if parser.has_section('console'):
                self.load_console_config(parser)
            if parser.has_section('keyboard'):
                self.load_keyboard_config(parser)
            if parser.has_section('monitor'):
                self.load_monitor_config(parser)
            if parser.has_section('storage'):
                self.load_storage_config(parser)
            '''
            if parser.has_section('usb'):
                self.load_usb_config(parser)
            if parser.has_section('video'):
                self.load_video_config(parser)
            '''
            if parser.has_section('scripts'):
                scripts = parser['scripts']
                self.power_on_script = self._parse_script(
                    scripts.get('power on', None))
                self.power_off_script = self._parse_script(
                    scripts.get('power off', None))
            else:
                self.power_on_script = self._parse_script(
                    "scripts.power_on()")
                self.power_off_script = self._parse_script(
                    "scripts.power_off()")
            #self._load_device_scripts()

            # web-base UI
            '''
            if www_support is True:
                self._www = mtda.www.Service(self)
                if parser.has_section('www'):
                    self.load_www_config(parser)
            '''

    def monitor_remote(self, host, screen):
        self.mtda.debug(3, "main.monitor_remote()")

        result = None
        if self.is_remote is True:
            # Stop previous remote console
            if self.monitor_output is not None:
                self.monitor_output.stop()
            if host is not None:
                # Create and start our remote console in paused
                # (i.e. buffering) state
                self.monitor_output = RemoteMonitor(host, self.conport, screen)
                self.monitor_output.pause()
                self.monitor_output.start()
            else:
                self.monitor_output = None

        self.mtda.debug(3, "main.monitor_remote(): %s" % str(result))
        return result

    async def usb_ports(self, session=None):
        self.mtda.debug(3, "main.usb_ports()")

        self._session_check(session)
        return len(self.usb_switches)

    def notify(self, what, info):
        self.mtda.debug(3, "main.notify({},{})".format(what, info))

        result = None
        if self.socket is not None:
            with self._socket_lock:
                self.socket.send(CONSTS.CHANNEL.EVENTS, flags=zmq.SNDMORE)
                self.socket.send_string("{} {}".format(what, info))
        if self._www is not None:
            self._www.notify(what, info)

        self.mtda.debug(3, "main.notify: {}".format(result))
        return result
            
    def start(self):
        self.mtda.debug(3, "main.start()")

        if self.is_remote is True:
            return True

        # Probe the specified power controller
        if self.power_controller is not None:
            status = self.power_controller.probe()
            if status is False:
                print('Probe of the Power Controller failed!', file=sys.stderr)
                return False

        # Probe the specified storage controller
        if self.storage_controller is not None:
            status = self.storage_controller.probe()
            if status is False:
                print('Probe of the shared storage device failed!',
                      file=sys.stderr)
                return False

        if self.console is not None:
            # Create a publisher
            if self.is_server is True:
                context = zmq.Context()
                socket = context.socket(zmq.PUB)
                socket.bind("tcp://*:%s" % self.conport)
            else:
                socket = None
            self.socket = socket

            # Create and start console logger
            status = self.console.probe()
            if status is False:
                print('Probe of the %s console failed!' % (
                      self.console.variant), file=sys.stderr)
                return False
            self.console_logger = ConsoleLogger(
                self, self.console, socket,
                self.power_controller, b'CON', self._www)
            if self._time_from_str is not None:
                self.console_logger.time_from = self._time_from_str
            if self._time_until_str is not None:
                self.console_logger.time_until = self._time_until_str
            if self._time_from_pwr is not None and self._time_from_pwr is True:
                self.toggle_timestamps()
            self.console_logger.start()

        if self.monitor is not None:
            # Create and start console logger
            status = self.monitor.probe()
            if status is False:
                print('Probe of the %s monitor console failed!' % (
                      self.monitor.variant), file=sys.stderr)
                return False
            self.monitor_logger = ConsoleLogger(
                self, self.monitor, socket, self.power_controller, b'MON')
            self.monitor_logger.start()

        if self.keyboard is not None:
            status = self.keyboard.probe()
            if status is False:
                print('Probe of the %s keyboard failed!' % (
                      self.keyboard.variant), file=sys.stderr)
                return False

        if self.assistant is not None:
            self.power_monitors.append(self.assistant)
            self.assistant.start()

        if self.video is not None:
            status = self.video.probe()
            if status is False:
                print('Probe of the %s video failed!' % (
                      self.video.variant), file=sys.stderr)
                return False
            self.video.start()

        if self._www is not None:
            self._www.start()

        if self.is_server is True:
            handler = self._session_check
            self._session_timer = RepeatTimer(10, handler)
            self._session_timer.start()

        # Start from a known state
        if self.power_controller is not None:
            self._target_off()
            self.storage_to_target()
        else:
            # Assume the target is ON if we cannot control power delivery
            # and start logging on available console(s)
            if self.console_logger is not None:
                self.console_logger.resume()
            if self.monitor_logger is not None:
                self.monitor_logger.resume()

        return True

    def storage_close(self, session=None):
        self.mtda.debug(3, "main.storage_close()")

        self._session_check(session)
        if self.storage_controller is None:
            result = False
        else:
            self._writer.stop()
            self._writer_data = None
            self._storage_opened = not self.storage_controller.close()
            self._storage_owner = None
            result = (self._storage_opened is False)

        self.mtda.debug(3, "main.storage_close(): %s" % str(result))
        return result

    async def storage_status(self, session=None):
        self.mtda.debug(3, "main.storage_status()")

        self._session_check(session)
        if self.storage_controller is None:
            self.mtda.debug(4, "storage_status(): no shared storage device")
            result = CONSTS.STORAGE.UNKNOWN, False, 0
        else:
            status = self.storage_controller.status()
            result = status, self._writer.writing, self._writer.written

        self.mtda.debug(3, "main.storage_status(): %s" % str(result))
        return result

    async def target_locked(self, session):
        self.mtda.debug(3, "main.target_locked()")

        self._session_check(session)
        return self._check_locked(session)

    def _parse_script(self, script):
        self.mtda.debug(3, "main._parse_script()")

        result = None
        if script is not None:
            result = script.replace("... ", "    ")

        self.mtda.debug(3, "main._parse_script(): %s" % str(result))
        return result

    async def agent_version(self):
        return self.version.__version__

    def _prefix_key_code(self, prefix_key):
        prefix_key = prefix_key.lower()
        key_dict = {'ctrl-a': '\x01', 'ctrl-b': '\x02', 'ctrl-c': '\x03',
                    'ctrl-d': '\x04', 'ctrl-e': '\x05', 'ctrl-f': '\x06',
                    'ctrl-g': '\x07', 'ctrl-h': '\x08', 'ctrl-i': '\x09',
                    'ctrl-j': '\x0A', 'ctrl-k': '\x0B', 'ctrl-l': '\x0C',
                    'ctrl-n': '\x0E', 'ctrl-o': '\x0F', 'ctrl-p': '\x10',
                    'ctrl-q': '\x11', 'ctrl-r': '\x12', 'ctrl-s': '\x13',
                    'ctrl-t': '\x14', 'ctrl-u': '\x15', 'ctrl-v': '\x16',
                    'ctrl-w': '\x17', 'ctrl-x': '\x18', 'ctrl-y': '\x19',
                    'ctrl-z': '\x1A'}

        if prefix_key in key_dict:
            key_ascii = key_dict[prefix_key]
            return key_ascii
        else:
            raise ValueError("the prefix key specified '{0}' is not "
                             "supported".format(prefix_key))

    def _storage_event(self, status):
        self.notify(CONSTS.EVENTS.STORAGE, status)

    def storage_to_target(self, session=None):
        self.mtda.debug(3, "main.storage_to_target()")

        self._session_check(session)
        if self.storage_locked(session) is False:
            self.storage_close()
            result = self.storage_controller.to_target()
            if result is True:
                self._storage_event(CONSTS.STORAGE.ON_TARGET)
        else:
            self.mtda.debug(1, "storage_to_target(): shared storage is locked")
            result = False

        self.mtda.debug(3, "main.storage_to_target(): %s" % str(result))
        return result

    def storage_locked(self, session=None):
        self.mtda.debug(3, "main.storage_locked()")

        self._session_check(session)
        if self._check_locked(session):
            result = True
        # Cannot swap the shared storage device between the host and target
        # without a driver
        elif self.storage_controller is None:
            self.mtda.debug(4, "storage_locked(): no shared storage device")
            result = True
        # If hotplugging is supported, swap only if the shared storage
        # isn't opened
        elif self.storage_controller.supports_hotplug() is True:
            result = self._storage_opened
        # We also need a power controller to be safe
        elif self.power_controller is None:
            self.mtda.debug(4, "storage_locked(): no power controller")
            result = True
        # The target shall be OFF
        elif self._target_status() != "OFF":
            self.mtda.debug(4, "storage_locked(): target isn't off")
            result = True
        # Lastly, the shared storage device shall not be opened
        elif self._storage_opened is True:
            self.mtda.debug(4, "storage_locked(): "
                               "shared storage is in used (opened)")
            result = True
        # We may otherwise swap our shared storage device
        else:
            result = False

        self.mtda.debug(3, "main.storage_locked(): %s" % str(result))
        return result

    async def target_status(self, session=None):
        self.mtda.debug(3, "main.target_status()")

        with self._power_lock:
            result = self._target_status(session)

        self.mtda.debug(3, "main.target_status(): {}".format(result))
        return result
    
    def _target_status(self):
        return self.loop.run_until_complete(self.target_status(session=self._session))


    def publish(self, topic, data):
        if self.socket is not None:
            with self._socket_lock:
                self.socket.send(topic, flags=zmq.SNDMORE)
                self.socket.send(data)

    def power_locked(self, session=None):
        self.mtda.debug(3, "main.power_locked()")
        self._session_check(session)
        print(self.power_controller)
        if self.power_controller is None:
            print('In power if cond')
            result = True
        else:
            print('In power else cond')
            result = self._check_locked(session)
        return result

    def _target_status(self, session=None):
        self.mtda.debug(3, "main._target_status()")

        if self.power_controller is None:
            result = CONSTS.POWER.UNSURE
        else:
            result = self.power_controller.status()

        self.mtda.debug(3, "main._target_status(): {}".format(result))
        return result

    def target_owner(self):
        self.mtda.debug(3, "main.target_owner()")

        return self._lock_owner

    async def target_uptime(self, session=None):
        self.mtda.debug(3, "main.target_uptime()")

        result = 0
        if self._uptime > 0:
            result = time.monotonic() - self._uptime

        self.mtda.debug(3, "main.target_uptime(): %s" % str(result))
        return result

    def _target_on(self, session=None):
        print(self.mtda.debug(3, "main._target_on()"))

        result = False
        if self.power_locked(session) is False:
            # Toggle the mass storage functions of the usbf controller
            result = self.power_controller.on()
            # Resume logging
            if result is True:
                if self.console_logger is not None:
                    self.console_logger.resume()
                if self.monitor_logger is not None:
                    self.monitor_logger.resume()

                # user-provided power-on script may now be executed
                # (target is up and logging running)
                #
                # power sequence:
                #   <power-on>
                #     <power-on-script>
                #       <runtime>
                #     <power-off-script>
                #   <power-off>
                #
                self.exec_power_on_script()
                self._power_event(CONSTS.POWER.ON)

        self.mtda.debug(3, "main._target_on(): {}".format(result))
        return result


    def _target_off(self, session=None):
        self.mtda.debug(3, "main._target_off()")

        # call power-off script before anything else
        #
        # power sequence:
        #   <power-on>
        #     <power-on-script>
        #       <runtime>
        #     <power-off-script>
        #   <power-off>
        #
        self.exec_power_off_script()

        # pause console
        if self.console_logger is not None:
            self.console_logger.reset_timer()
            self.console_logger.pause()

        # and monitor
        if self.monitor_logger is not None:
            self.monitor_logger.reset_timer()
            self.monitor_logger.pause()

        # release keyboard
        if self.keyboard is not None:
            self.keyboard.idle()

        result = True
        if self.power_controller is not None:
            result = self.power_controller.off()
        #self._composite_stop()
        self._power_event(CONSTS.POWER.OFF)

        self.mtda.debug(3, "main._target_off(): {}".format(result))
        return result

    def _power_event(self, status):
        self._power_expiry = None
        if status == CONSTS.POWER.ON:
            self._uptime = time.monotonic()
        elif status == CONSTS.POWER.OFF:
            self._uptime = 0

        for m in self.power_monitors:
            m.power_changed(status)
        self.notify(CONSTS.EVENTS.POWER,status)

    def exec_power_on_script(self):
        self.mtda.debug(3, "main.exec_power_on_script()")

        result = None
        if self.power_on_script:
            self.mtda.debug(4, "exec_power_on_script(): "
                               "%s" % self.power_on_script)
            env = self._env_for_script()
            result = exec(self.power_on_script, env)

    async def target_on(self,session=None):
        result = True
        self._session_check(session)
        with self._power_lock:
            status = self._target_status()
            if status != CONSTS.POWER.ON:
                result = False
                if self.power_locked(session) is False:
                    result = self._target_on(session)

        self.mtda.debug(3, "main.target_on(): {}".format(result))
        return result

    async def target_off(self,session=None):
        self.mtda.debug(3, "main.target_off()")

        result = True
        self._session_check(session)
        with self._power_lock:
            status = self._target_status()
            if status != CONSTS.POWER.OFF:
                result = False
                if self.power_locked(session) is False:
                    result = self._target_off(session)

        self.mtda.debug(3, "main.target_off(): {}".format(result))
        return result


