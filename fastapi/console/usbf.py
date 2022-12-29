# ---------------------------------------------------------------------------
# USB Function console driver for MTDA
# ---------------------------------------------------------------------------
#
# This software is a part of MTDA.
# Copyright (C) 2022 Siemens Digital Industries Software
#
# ---------------------------------------------------------------------------
# SPDX-License-Identifier: MIT
# ---------------------------------------------------------------------------

# Local imports
import asyncio,sys
from console.serial import SerialConsole
from support.usb import Composite


class UsbFunctionConsole(SerialConsole):

    def __init__(self, mtda):
        super().__init__(mtda)
        self.port = None
        self.rate = 9600
        self.loop = asyncio.get_event_loop()

    def configure(self, conf, role='console'):
        self.mtda.debug(3, "console.usbf.configure()")

        super().configure(conf)
        if self.port is None:
            self.port = "/dev/ttyGS0" if role == "console" else "/dev/ttyGS1"
        result = Composite.configure(role, conf)

        self.mtda.debug(3, "console.usbf.configure(): {}".format(result))
        return result

    def configure_systemd(self, dir):
        return None

    def probe(self):
        self.mtda.debug(3, "console.usbf.probe()")
        try:
            result = Composite.install()
            if result is True:
                result = super().probe()
        except Exception as e:
            print(e)
            sys.exit()

        self.mtda.debug(3, "console.usbf.probe(): {}".format(result))
        return result
    

def instantiate(mtda):
    return UsbFunctionConsole(mtda)
