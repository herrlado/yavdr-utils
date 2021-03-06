import sys
import os
import codecs
import subprocess
import gobject
import socket
import string
import struct
import datetime
import syslog
import pyudev
from pyudev.glib import GUDevMonitorObserver
import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop

DBusGMainLoop(set_as_default=True)
bus = dbus.SystemBus()
class dbusRemote():
    '''wrapper for remote interface provided by the dbus2vdr plugin'''
    def __init__(self):
        self.dbusremote = bus.get_object("de.tvdr.vdr","/Remote")
        self.interface = 'de.tvdr.vdr.remote'

    def sendkey(self,key):
        answer, message = self.dbusremote.HitKey(dbus.String(key),dbus_interface=self.interface)
        if answer == 250:
            return True
        else:
            return False

    def enable(self):
        answer, message = self.dbusremote.Enable(dbus_interface=self.interface)
        if answer == 250:
            return True

    def disable(self):
        answer, message = self.dbusremote.Disable(dbus_interface=self.interface)
        if answer == 250:
            return True

    def status(self):
        answer, message = self.dbusremote.Status(dbus_interface=self.interface)
        if answer == 250:
            return True

class dbusShutdown():
    '''wrapper for shutdown interface provided by the dbus2vdr plugin'''
    def __init__(self):
        self.dbusshutdown = bus.get_object("de.tvdr.vdr","/Shutdown")
        self.interface = 'de.tvdr.vdr.shutdown'

    def manualstart(self):
        return self.dbusshutdown.ManualStart(dbus_interface=self.interface)

    def confirmShutdown(self,user=False):
        code, message, shutdownhooks, message = self.dbusshutdown.ConfirmShutdown(dbus.Boolean(user),dbus_interface=self.interface, timeout=120) # wait up to 120 second for a reply, because the lifeguard-addon is as fast as a snail
        if code in [250,990]:
            return True
        else:
            syslog.syslog(u"vdr not ready for shutdown: %s: %s"%(code,message))
            return False

class dbusSetup():
    '''wrapper for setup interface provided by the dbus2vdr plugin'''
    def __init__(self):
        self.dbussetup = bus.get_object("de.tvdr.vdr","/Setup")
        self.interface = 'de.tvdr.vdr.setup'

    def vdrsetupget(self,option):
        return self.dbussetup.Get(dbus.String(option),dbus_interface=self.interface)

class dbusTimer():
    def __init__(self):
        self.dbustimer = bus.get_object("de.tvdr.vdr","/Timers")
        self.interface = 'de.tvdr.vdr.timer'
    def wakeup_for_Timer(self):
        status,tid,rel,startt,stopt,event = self.dbustimer.Next(dbus_interface=self.interface)
        if status == 250 and (rel <= (6 * 60)):
            return True
        else:                                                                                                        return False

def get_dbusPlugins():
    '''wrapper for dbus plugin list'''
    dbusplugins = bus.get_object("de.tvdr.vdr","/Plugins")
    raw = dbusplugins.List(dbus_interface="de.tvdr.vdr.pluginmanager")
    plugins = {}
    if len(raw) > 0:
        for name, version in raw:
            plugins[name]=version
    return plugins


class dbusSofthddeviceFrontend():
    '''handler for softhddevice's svdrp plugin command interface provided by the dbus2vdr plugin'''
    def __init__(self):
        self.dbusfe = bus.get_object("de.tvdr.vdr","/Plugins/softhddevice")
        self.interface = 'de.tvdr.vdr.plugin'

    def status(self):
        code, mode = self.dbusfe.SVDRPCommand(dbus.String("STAT"),dbus.String(None),dbus_interface=self.interface)
        return mode.split()[-1]

    def attach(self):
        display = dbus.String("-d %s"%(settings.env["DISPLAY"]))
        reply, answer = self.dbusfe.SVDRPCommand(dbus.String("ATTA"),display,dbus_interface=self.interface)
        remote.enable()
        settings.frontend_active = 1

    def detach(self):
        reply, answer = self.dbusfe.SVDRPCommand(dbus.String("DETA"),dbus.String(None),dbus_interface=self.interface)
        remote.disable()
        settings.frontend_active = 0

    def resume(self,status):
        reply, answer = self.dbusfe.SVDRPCommand(dbus.String("RESU"),dbus.String(None),dbus_interface=self.interface)
        settings.frontend_active = 1


def setUserInactive():
    dbusshutdown = bus.get_object("de.tvdr.vdr","/Shutdown")
    dbusshutdown.SetUserInactive(dbus_interface='de.tvdr.vdr.shutdown')
    send_shutdown()
    settings.time = gobject.timeout_add(300000,send_shutdown)

def detach():
    # set background visible when frontend is detached
    subprocess.call(["/usr/bin/feh","--bg-fill",settings.conf['logo_detached']], env=settings.env)

    frontend.detach()
    graphtftng_switch()
    return True

def send_shutdown():
    try:
        if shutdown.confirmShutdown():
            remote.enable()
            remote.sendkey("POWER")
            remote.disable()
    except Exception as error:
        syslog.syslog(error)
        remote.enable()
        remote.sendkey("POWER")
        remote.disable()
    finally:
        return True

def soft_detach():
    detach()
    settings.timer = gobject.timeout_add(300000,send_shutdown)
    return False

def graphtftng_switch():
    if settings.graphtftng:
        dbusgraph = bus.get_object("de.tvdr.vdr","/Plugins/graphtftng")
        if settings.frontend_active == 0:
           dbusgraph.SVDRPCommand(dbus.String('TVIEW'),dbus.String(settings.conf['graphtftng_view']),dbus_interface='de.tvdr.vdr.plugin')
        elif settings.frontend_active == 1:
           dbusgraph.SVDRPCommand(dbus.String('RVIEW'),dbus.String(None),dbus_interface='de.tvdr.vdr.plugin')

def resume(status):
    # set background visible when frontend is detached
    subprocess.call(["/usr/bin/feh","--bg-fill",settings.conf['logo_attached']], env=settings.env)

    if status == "SUSPENDED":
        frontend.resume()
    elif status == "SUSPEND_DETACHED":
        frontend.attach()
    graphtftng_switch()
    

class Settings():
    ''' read and store configuration, handle input devices using udev'''
    def __init__(self):
        global gobject
        self.frontend_active = 0
        self.env = os.environ
        self.timer = None
        self.updateDisplay()
        self.manualstart = shutdown.manualstart()
        try:
            self.acpi_wakeup = self.check_acpi()
        except:
            self.acpi_wakeup = None
        self.context = pyudev.Context()
        self.monitor = pyudev.Monitor.from_netlink(self.context)
        self.monitor.filter_by(subsystem='input')
        self.devices = {}
        self.paths = {}
        # struct for kernel input devices on 64-Bit linux systems
        self.inputEventFormat = 'llHHi'
        self.inputEventSize = 24
        # on 32-Bit systems try
        # inputEventFormat = 'iihhi'
        # inputEventSize = 16
        # (untested)
        self.conf = {
        'logo_attached':"/usr/share/yavdr/images/yavdr_logo.png",
        'logo_detached':"/usr/share/yavdr/images/yaVDR_background_detached.jpg",
        'key_detach':"KEY_PROG1",
        'key_power':"KEY_POWER2",
        'start_always_detached':'0',
        'graphtftng_view':"NonLiveTv"
        }
        for i in self.conf:
            if i in os.environ:
                self.conf[i] = os.environ[i]
        self.check_graphtftng()
        self.get_event_devices()

    def check_graphtftng(self):
        plugins = get_dbusPlugins()
        if 'graphtftng' in plugins:
            #print "found graphtftng"
            self.graphtftng = True
        else:
            self.graphtftng = False


    def get_event_devices(self):
        '''filter all connected input devices and watch those not used by eventlircd'''
        for device in self.context.list_devices(subsystem='input',ID_INPUT_KEYBOARD=True):
            if device.sys_name.startswith('event') and not (('eventlircd_enable' in device) or ('eventlircd_enable' in device and device['eventlircd_enable'] is ('true'))):
                self.paths[device['DEVNAME']] = open(device['DEVNAME'],'rb')
                syslog.syslog(codecs.encode(u"watching %s: %s"%(device.parent['NAME'], device['DEVNAME']),'utf-8'))
                self.devices[device['DEVNAME']] = gobject.io_add_watch(self.paths[device['DEVNAME']], gobject.IO_IN, self.evthandler)
        self.observer = GUDevMonitorObserver(self.monitor)
        self.observer.connect('device-event',self.udev_event)
        self.monitor.start()
        syslog.syslog("started udev monitoring of input devices")

    def udev_event(self,observer,action, device):
        '''callback function to add/remove input devices'''
        if action == "add" and 'DEVNAME' in device:
            syslog.syslog("added %s"%device['DEVNAME'])
            if not "eventlircd_enable" in device:
                self.paths[device['DEVNAME']] = open(device['DEVNAME'],'rb')
                syslog.syslog(codecs.encode(u"watching %s: %s - %s"%(device.parent['NAME'], device['DEVNAME'],self.paths[device['DEVNAME']]),'utf-8'))
                self.devices[device['DEVNAME']] = gobject.io_add_watch(self.paths[device['DEVNAME']], gobject.IO_IN, self.evthandler)
        elif action == "remove" and 'DEVNAME' in device:
            try:
	            self.paths[device['DEVNAME']].close()
            except:
                pass
            try:
                gobject.source_remove(self.devices[device['DEVNAME']])
                syslog.syslog("removed %s"%device['DEVNAME'])
            except: pass #device already removed from watchlist

    def GetActiveWindowTitle(self):
        '''get title of active window'''
        return subprocess.Popen(["xprop", "-id", subprocess.Popen(["xprop", "-root", "_NET_ACTIVE_WINDOW"], stdout=subprocess.PIPE, env=settings.env).communicate()[0].strip().split()[-1], "WM_NAME"], stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate()[0].strip().split('"', 1)[-1][:-1]

    def evthandler(self,path, *args):
        '''callback function to handle keys sent by inputdevices'''
        try:
            event = path.read(self.inputEventSize)
        except:
            syslog.syslog('can not read from %s'%(path))
            return False
        (time1, time2, typeev, code, value) = struct.unpack(self.inputEventFormat, event)

        if typeev == 1 and code == 115: # code 115 = KEY_VOLUMEUP
           remote.sendkey("Volume+") 
	if typeev == 1 and code == 114: # code 115 = KEY_VOLUMEDOWN
           remote.sendkey("Volume-")
        if typeev == 1 and code == 113 and value == 1: # code 115 = KEY_MUTE
           remote.sendkey("Mute")
        if typeev == 1 and code == 172 and value == 1: # code 172 = KEY_HOMEPAGE - toggle frontend (attach/detach) 
           if self.frontend_active == 1:
               syslog.syslog("detached frontend")
               detach()
           elif self.frontend_active == 0:
               syslog.syslog("attached frontend")
               resume(frontend.status())
        if self.frontend_active == 0:
           if typeev == 1 and code == 28 and self.GetActiveWindowTitle() == 'WM_NAM': # resume frontend on KEY_ENTER if no window has focus
               syslog.syslog("frontend attached by keyboard activity")
               resume(frontend.status())
        return True


    def updateDisplay(self):
        self.env["DISPLAY"] = <?cs alt:desktop_display ?>":1"<?cs /alt ?>+self.getTempDisplay()

    def getTempDisplay(self):
        tempdisplay = subprocess.check_output(["dbget","vdr.tempdisplay"])
        if len(tempdisplay) == 0:
            tempdisplay = ".0"
        return tempdisplay

    def check_acpi(self):
        timestr = open('/var/cache/vdr/acpiwakeup.time.old','r').read().splitlines()[0]
        wakeup = datetime.datetime.strptime(timestr, "%Y-%m-%d %H:%M:%S")
        now = datetime.datetime.utcnow()
        syslog.syslog(u"acip-wakeup.time.old hatte Wert: %s"%(wakeup.ctime()))
        syslog.syslog(u"vergleiche mit jetzt: %s"%(now.ctime()))
        if wakeup < now:
            d = now - wakeup
        else:
            d = wakeup - now
        if d.seconds > 360:
            syslog.syslog("assuming manual start")
            return False
        else:
            syslog.syslog("assuming start for acpi-wakeup")
            return True

class dbusService(dbus.service.Object):
    def __init__(self, settings):
        bus_name = dbus.service.BusName('de.yavdr.frontend', bus=bus)#dbus.SessionBus())
        dbus.service.Object.__init__(self, bus_name, '/frontend')
        self.settings = settings

    @dbus.service.method('de.yavdr.frontend',out_signature='b')
    def deta(self):
        detach()
        return True

    @dbus.service.method('de.yavdr.frontend',out_signature='b')
    def atta(self):
        resume(frontend.status())
        return True

    @dbus.service.method('de.yavdr.frontend',in_signature='s',out_signature='b')
    def setBackground(self,path):
        if os.path.isfile(path):
            subprocess.call(["/usr/bin/feh","--bg-fill",path], env=settings.env)
            syslog.syslog("setting Background to %s"%(path))
            return True
        else:
            return False

    @dbus.service.method('de.yavdr.frontend', in_signature="s", out_signature='b')
    def updateDisplay(self, display=None):
        if display is not None:
            self.settings.env["DISPLAY"] = display
            return True
        else:
            return False

    @dbus.service.method('de.yavdr.frontend', out_signature='s')
    def checkDisplay(self):
        return self.settings.env["DISPLAY"]

class lircConnection():
    def __init__(self,socket_path="/var/run/lirc/lircd"):
        self.socket_path = socket_path
        self.try_connection()
        self.callback = None
        
    def connect_eventlircd(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)
        self.callback = gobject.io_add_watch(self.sock, gobject.IO_IN, self.handler)

    def try_connection(self):
        try:
            self.connect_eventlircd()
            syslog.syslog(u"conntected to Lirc-Socket on %s"%(self.socket_path))
            return False
        except:
            gobject.timeout_add(1000, self.try_connection)
            try:
                if self.callback:
                    gobject.source_remove(self.callback)
            except:
                pass
            syslog.syslog("Error: vdr-frontend could not connect to eventlircd socket")
            return False
            
    def handler(self, sock, *args):
        '''callback function for activity on eventlircd socket'''
        try:
            buf = sock.recv(1024)
            if not buf:
                self.sock.close()
                try:
                    if self.callback:
                        gobject.source_remove(self.callback)
                except:
                    pass
                syslog.syslog("Error reading from lircd socket")
                self.try_connection()
                return False
        except:
            sock.close()
            try:
                gobject.source_remove(self.callback)
            except: pass
            syslog.syslog('retry lirc connection')
            self.try_connection()
            return True
        lines = string.split(buf, "\n")
        for line in lines[:-1]:
            try: 
                code,count,cmd,device = string.split(line, " ")[:4]
                if count != "0": 
                    #syslog.syslog('repeated keypress')
                    return True
                else:
                    try:
                        gobject.source_remove(settings.timer)
                    except: pass
            except: 
                syslog.syslog(line)
                return True
            if cmd == settings.conf['key_detach']:#"KEY_PROG1":
                if frontend.status() == "NOT_SUSPENDED":
                    detach()
                    settings.frontend_active = 0
                else:
                    resume(frontend.status())
            elif cmd == settings.conf['key_power']:#"KEY_POWER2":
                if frontend.status() == "NOT_SUSPENDED":
                    settings.timer = gobject.timeout_add(15000,soft_detach)
                    settings.frontend_active = 0
                else:
                    send_shutdown()
            else:
                if settings.frontend_active == 0:
                    resume(frontend.status())
                    settings.frontend_active = 1
                else:
                    pass
        return True

if __name__ == '__main__':
    syslog.openlog(ident="vdr-frontend",logoption=syslog.LOG_PID)
    # Initialise dbus-control classes
    frontend = dbusSofthddeviceFrontend()
    remote = dbusRemote()
    shutdown = dbusShutdown()
    setup = dbusSetup()
    timer = dbusTimer()
    settings = Settings()

    # attach frontend if vdr has not been started for a timer or an acpi
    # wakeup event
    if settings.manualstart == True and settings.acpi_wakeup != True and settings.conf['start_always_detached'] == '0':
        resume(frontend.status())
    else:
        # set background visible when frontend is detached
        subprocess.call(["/usr/bin/feh","--bg-fill",settings.conf['logo_detached']], env=settings.env)
        # change graphtftng view if the plugin has been loaded
        graphtftng_switch()
        # check for timer within the next 6 minutes
        if settings.manualstart == False and timer.wakeup_for_Timer():
            settings.timer = gobject.timeout_add(300000, send_shutdown)
        # no timer, just an acpi-wakeup event
        elif settings.acpi_wakeup == True and setup.vdrsetupget("MinUserInactivity")[0] > 0:
            interval, default, answer = setup.vdrsetupget("MinEventTimeout")
            interval_ms = interval  * 60000 # * 60s * 1000ms
            settings.timer = gobject.timeout_add(interval_ms, setUserInactive)
        # neither woken for a timer nor for a acpi event
        elif setup.vdrsetupget("MinUserInactivity")[0] > 0:
            interval, default, answer = setup.vdrsetupget("MinEventTimeout")
            interval_ms = interval  * 60000 # * 60s * 1000ms
            settings.timer = gobject.timeout_add(interval_ms, setUserInactive)
        remote.disable()

    def connect_eventlircd():
        socket_path = "/var/run/lirc/lircd"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(socket_path)
        gobject.io_add_watch(sock, gobject.IO_IN, handler)

    def try_connection():
      try:
          connect_eventlircd()
      except:
        syslog.syslog("Error: vdr-frontend could not connect to eventlircd socket")

    lircconnection = lircConnection()
    dbusservice = dbusService(settings)
    loop = gobject.MainLoop()
    loop.run()

