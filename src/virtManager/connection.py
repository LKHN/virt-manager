#
# Copyright (C) 2006 Red Hat, Inc.
# Copyright (C) 2006 Daniel P. Berrange <berrange@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.
#

import gobject
import libvirt
import logging
import os, sys
import traceback
from time import time
import logging
from socket import gethostbyaddr, gethostname
import dbus

from virtManager.domain import vmmDomain
from virtManager.network import vmmNetwork
from virtManager.netdev import vmmNetDevice

class vmmConnection(gobject.GObject):
    __gsignals__ = {
        "vm-added": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                     [str, str]),
        "vm-started": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                     [str, str]),
        "vm-removed": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                       [str, str]),
        "net-added": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                      [str, str]),
        "net-removed": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                        [str, str]),
        "net-started": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                        [str, str]),
        "net-stopped": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                        [str, str]),
        "netdev-added": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                         [str]),
        "netdev-removed": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                           [str]),
        "resources-sampled": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                              []),
        "disconnected": (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE, [str])
        }

    def __init__(self, config, uri, readOnly, active=True):
        self.__gobject_init__()
        self.config = config
        self.uri = uri
        self.readOnly = readOnly
        self._active = active

        openURI = uri
        if openURI == "Xen":
            openURI = None

        if readOnly is None:
            try:
                self.vmm = libvirt.open(openURI)
                self.readOnly = False
            except:
                self.vmm = libvirt.openReadOnly(openURI)
                self.readOnly = True
        else:
            if readOnly:
                self.vmm = libvirt.openReadOnly(openURI)
            else:
                self.vmm = libvirt.open(openURI)

        self.netdevs = {}
        self.nets = {}
        self.vms = {}
        self.activeUUIDs = []
        self.record = []

        self.detect_network_devices()

    def detect_network_devices(self):
        try:
            # Get a connection to the SYSTEM bus
            self.bus = dbus.SystemBus()
            # Get a handle to the HAL service
            hal_object = self.bus.get_object('org.freedesktop.Hal', '/org/freedesktop/Hal/Manager')
            self.hal_iface = dbus.Interface(hal_object, 'org.freedesktop.Hal.Manager')

            # Track device add/removes so we can detect newly inserted CD media
            self.hal_iface.connect_to_signal("DeviceAdded", self._device_added)
            self.hal_iface.connect_to_signal("DeviceRemoved", self._device_removed)

            # Find info about all current present media
            for path in self.hal_iface.FindDeviceByCapability("net"):
                self._device_added(path)
        except:
            (type, value, stacktrace) = sys.exc_info ()
            logging.error("Unable to connect to HAL to list network devices: '%s'" + \
                          str(type) + " " + str(value) + "\n" + \
                          traceback.format_exc (stacktrace))
            self.bus = None
            self.hal_iface = None

    def _device_added(self, path):
        obj = self.bus.get_object("org.freedesktop.Hal", path)
        if obj.QueryCapability("net"):
            name = obj.GetPropertyString("net.interface")
            mac = obj.GetPropertyString("net.address")

            # Now magic to determine if the device is part of a bridge
            shared = False
            bridge = None
            try:
                # XXX Linux specific - needs porting for other OS - patches
                # welcomed...
                sysfspath = obj.GetPropertyString("linux.sysfs_path")

                # Sick, disgusting hack for Xen netloop crack which renames
                # ethN -> pethN, but which HAL never sees
                psysfspath = sysfspath[0:len(sysfspath)-len(name)] + "p" + name
                if os.path.exists(psysfspath):
                    sysfspath = psysfspath

                brportpath = os.path.join(sysfspath, "brport")

                if os.path.exists(brportpath):
                    shared = True
                    brlinkpath = os.path.join(brportpath, "bridge")
                    dest = os.readlink(brlinkpath)
                    (head,tail) = os.path.split(dest)
                    bridge = tail
            except:
                (type, value, stacktrace) = sys.exc_info ()
                logging.error("Unable to determine if device is shared:" +
                              str(type) + " " + str(value) + "\n" + \
                              traceback.format_exc (stacktrace))

            if self.netdevs.has_key(path):
                currDev = self.netdevs[path]
                if currDev.get_info() == (name, mac, shared, bridge):
                    return
                del self.netdevs[path]
            dev = vmmNetDevice(self.config, self, name, mac, shared, bridge)
            self.netdevs[path] = dev
            self.emit("netdev-added", dev.get_name())

    def _device_removed(self, path):
        if self.netdevs.has_key(path):
            dev = self.netdevs[path]
            self.emit("netdev-removed", dev.get_name())
            del self.netdevs[path]

    def is_read_only(self):
        return self.readOnly

    def get_type(self):
        return self.vmm.getType()

    def get_local_hostname(self):
        try:
            (host, aliases, ipaddrs) = gethostbyaddr(gethostname())
            return host
        except:
            logging.warning("Unable to resolve local hostname for machine")
            return "localhost"

    def get_hostname(self):
        try:
            (scheme, netloc, path, query, fragment) = self.uri_split()

            if netloc != "":
                return netloc
        except Exception, e:
            logging.warning("Cannot parse URI %s: %s" % (self.uri, str(e)))

        return self.get_local_hostname()

    def get_name(self):
        try:
            (scheme, netloc, path, query, fragment) = self.uri_split()

            i = scheme.find("+")
            if i > 0:
                scheme = scheme[0:i]

            if netloc == "":
                netloc = self.get_local_hostname()

            if scheme == "xen":
                return "Xen on %s" % netloc
            elif scheme == "qemu":
                if path == "/session":
                    return "QEMU session on %s" % netloc
                else:
                    return "QEMU system on %s" % netloc
            elif scheme == "test":
                return "Test on %s" % netloc
        except Exception, e:
            logging.warning("Cannot parse URI %s: %s" % (self.uri, str(e)))

        return self.uri

    def is_remote(self):
        try:
            (scheme, netloc, path, query, fragment) = self.uri_split()
            if netloc == "":
                return False
            return True
        except:
            return True

    def get_uri(self):
        return self.uri

    def get_vm(self, uuid):
        return self.vms[uuid]

    def get_net(self, uuid):
        return self.nets[uuid]

    def get_net_device(self, path):
        return self.netdevs[path]

    def close(self):
        if self.vmm == None:
            return

        #self.vmm.close()
        self.vmm = None
        self.emit("disconnected", self.uri)

    def list_vm_uuids(self):
        return self.vms.keys()

    def list_net_uuids(self):
        return self.nets.keys()

    def list_net_device_paths(self):
        return self.netdevs.keys()

    def get_host_info(self):
        return self.hostinfo

    def get_max_vcpus(self):
        try:
            return self.vmm.getMaxVcpus(self.get_type())
        except Exception, e:
            logging.debug('Unable to get max vcpu')
            return 32;

    def connect(self, name, callback):
        handle_id = gobject.GObject.connect(self, name, callback)

        if name == "vm-added":
            for uuid in self.vms.keys():
                self.emit("vm-added", self.uri, uuid)

        return handle_id

    def pretty_host_memory_size(self):
        mem = self.host_memory_size()
        if mem > (1024*1024):
            return "%2.2f GB" % (mem/(1024.0*1024.0))
        else:
            return "%2.2f MB" % (mem/1024.0)


    def host_memory_size(self):
        return self.hostinfo[1]*1024

    def host_architecture(self):
        return self.hostinfo[0]

    def host_active_processor_count(self):
        return self.hostinfo[2]

    def host_maximum_processor_count(self):
        return self.hostinfo[4] * self.hostinfo[5] * self.hostinfo[6] * self.hostinfo[7]

    def create_network(self, xml, start=True, autostart=True):
        net = self.vmm.networkDefineXML(xml)
        uuid = self.uuidstr(net.UUID())
        self.nets[uuid] = vmmNetwork(self.config, self, net, uuid, False)
        self.nets[uuid].start()
        self.nets[uuid].set_active(True)
        self.nets[uuid].set_autostart(True)
        self.emit("net-added", self.uri, uuid)
        self.emit("net-started", self.uri, uuid)
        return self.nets[uuid]

    def define_domain(self, xml):
        self.vmm.defineXML(xml)

    def restore(self, frm):
        status = self.vmm.restore(frm)
        if(status == 0):
            os.remove(frm)
        return status

    def tick(self, noStatsUpdate=False):
        if self.vmm == None:
            return

        if not self.is_active:
            return

        oldNets = self.nets
        startNets = {}
        stopNets = {}
        self.nets = {}
        newNets = {}
        newActiveNetNames = []
        newInactiveNetNames = []
        try:
            newActiveNetNames = self.vmm.listNetworks()
        except:
            logging.warn("Unable to list active networks")
        try:
            newInactiveNetNames = self.vmm.listDefinedNetworks()
        except:
            logging.warn("Unable to list inactive networks")

        # check of net devices
        newPaths = self.hal_iface.FindDeviceByCapability("net")
        for newPath in newPaths:
            self._device_added(newPath)

        for name in newActiveNetNames:
            net = self.vmm.networkLookupByName(name)
            uuid = self.uuidstr(net.UUID())
            if not oldNets.has_key(uuid):
                self.nets[uuid] = vmmNetwork(self.config, self, net, uuid, True)
                newNets[uuid] = self.nets[uuid]
                startNets[uuid] = newNets[uuid]
            else:
                self.nets[uuid] = oldNets[uuid]
                if not self.nets[uuid].is_active():
                    self.nets[uuid].set_active(True)
                    startNets[uuid] = self.nets[uuid]
                del oldNets[uuid]
        for name in newInactiveNetNames:
            net = self.vmm.networkLookupByName(name)
            uuid = self.uuidstr(net.UUID())
            if not oldNets.has_key(uuid):
                self.nets[uuid] = vmmNetwork(self.config, self, net, uuid, False)
                newNets[uuid] = self.nets[uuid]
            else:
                self.nets[uuid] = oldNets[uuid]
                if self.nets[uuid].is_active():
                    self.nets[uuid].set_active(False)
                    stopNets[uuid] = self.nets[uuid]
                del oldNets[uuid]

        oldActiveIDs = {}
        oldInactiveNames = {}
        for uuid in self.vms.keys():
            # first pull out all the current inactive VMs we know about
            vm = self.vms[uuid]
            if vm.get_id() == -1:
                oldInactiveNames[vm.get_name()] = vm

        for uuid in self.activeUUIDs:
            # Now get all the vms that were active the last time around and are still active
            vm = self.vms[uuid]
            if vm.get_id() != -1:
                oldActiveIDs[vm.get_id()] = vm

        # Now we can clear the list of actives from the last time through
        self.activeUUIDs = []

        newActiveIDs = self.vmm.listDomainsID()
        newInactiveNames = []
        try:
            newInactiveNames = self.vmm.listDefinedDomains()
        except:
            logging.warn("Unable to list inactive domains")

        newUUIDs = {}
        oldUUIDs = {}
        curUUIDs = {}
        maybeNewUUIDs = {}
        startedUUIDs = []

        # NB in these first 2 loops, we go to great pains to
        # avoid actually instantiating a new VM object so that
        # the common case of 'no new/old VMs' avoids hitting
        # XenD too much & thus slowing stuff down.

        # Filter out active domains which haven't changed
        if newActiveIDs != None:
            for id in newActiveIDs:
                if oldActiveIDs.has_key(id):
                    # No change, copy across existing VM object
                    vm = oldActiveIDs[id]
                    curUUIDs[vm.get_uuid()] = vm
                    self.activeUUIDs.append(vm.get_uuid())
                else:
                    # May be a new VM, we have no choice but
                    # to create the wrapper so we can see
                    # if its a previously inactive domain.
                    vm = self.vmm.lookupByID(id)
                    uuid = self.uuidstr(vm.UUID())
                    maybeNewUUIDs[uuid] = vm
                    # also add the new or newly started VM to the "started" list
                    startedUUIDs.append(uuid)
                    #print "Maybe new active " + str(maybeNewUUIDs[uuid].get_name()) + " " + uuid
                    self.activeUUIDs.append(uuid)

        # Filter out inactive domains which haven't changed
        if newInactiveNames != None:
            for name in newInactiveNames:
                if oldInactiveNames.has_key(name):
                    # No change, copy across existing VM object
                    vm = oldInactiveNames[name]
                    #print "Existing inactive " + str(vm.get_name()) + " " + vm.get_uuid()
                    curUUIDs[vm.get_uuid()] = vm
                else:
                    # May be a new VM, we have no choice but
                    # to create the wrapper so we can see
                    # if its a previously inactive domain.
                    try:
                        vm = self.vmm.lookupByName(name)
                        uuid = self.uuidstr(vm.UUID())
                        maybeNewUUIDs[uuid] = vm
                    except libvirt.libvirtError:
                        logging.debug("Couldn't fetch domain id " + str(id) + "; it probably went away")
                    #print "Maybe new inactive " + str(maybeNewUUIDs[uuid].get_name()) + " " + uuid

        # At this point, maybeNewUUIDs has domains which are
        # either completely new, or changed state.

        # Filter out VMs which merely changed state, leaving
        # only new domains
        for uuid in maybeNewUUIDs.keys():
            rawvm = maybeNewUUIDs[uuid]
            if not(self.vms.has_key(uuid)):
                #print "Completely new VM " + str(vm)
                vm = vmmDomain(self.config, self, rawvm, uuid)
                newUUIDs[vm.get_uuid()] = vm
                curUUIDs[uuid] = vm
            else:
                vm = self.vms[uuid]
                vm.release_handle()
                vm.set_handle(rawvm)
                curUUIDs[uuid] = vm
                #print "Mere state change " + str(vm)

        # Finalize list of domains which went away altogether
        for uuid in self.vms.keys():
            vm = self.vms[uuid]
            if not(curUUIDs.has_key(uuid)):
                #print "Completly old VM " + str(vm)
                oldUUIDs[uuid] = vm
            else:
                #print "Mere state change " + str(vm)
                pass

        # We have our new master list
        self.vms = curUUIDs

        # Inform everyone what changed
        for uuid in oldUUIDs:
            self.emit("vm-removed", self.uri, uuid)
            oldUUIDs[uuid].release_handle()

        for uuid in newUUIDs:
            self.emit("vm-added", self.uri, uuid)

        for uuid in startedUUIDs:
            self.emit("vm-started", self.uri, uuid)

        for uuid in oldNets:
            self.emit("net-removed", self.uri, uuid)

        for uuid in newNets:
            self.emit("net-added", self.uri, uuid)

        for uuid in startNets:
            self.emit("net-started", self.uri, uuid)

        for uuid in stopNets:
            self.emit("net-stopped", self.uri, uuid)

        # Finally, we sample each domain
        now = time()
        self.hostinfo = self.vmm.getInfo()

        updateVMs = self.vms
        if noStatsUpdate:
            updateVMs = newUUIDs

        for uuid in updateVMs.keys():
            self.vms[uuid].tick(now)

        if not noStatsUpdate:
            self.recalculate_stats(now)

        return 1

    def recalculate_stats(self, now):
        expected = self.config.get_stats_history_length()
        current = len(self.record)
        if current > expected:
            del self.record[expected:current]

        mem = 0
        cpuTime = 0

        for uuid in self.vms:
            vm = self.vms[uuid]
            if vm.get_id() != -1:
                cpuTime = cpuTime + vm.get_cputime()
                mem = mem + vm.get_memory()


        pcentCpuTime = 0
        if len(self.record) > 0:
            prevTimestamp = self.record[0]["timestamp"]

            pcentCpuTime = (cpuTime) * 100.0 / ((now - prevTimestamp)*1000.0*1000.0*1000.0*self.host_active_processor_count())
            # Due to timing diffs between getting wall time & getting
            # the domain's time, its possible to go a tiny bit over
            # 100% utilization. This freaks out users of the data, so
            # we hard limit it.
            if pcentCpuTime > 100.0:
                pcentCpuTime = 100.0
            # Enforce >= 0 just in case
            if pcentCpuTime < 0.0:
                pcentCpuTime = 0.0

        pcentMem = mem * 100.0 / self.host_memory_size()

        newStats = {
            "timestamp": now,
            "memory": mem,
            "memoryPercent": pcentMem,
            "cpuTime": cpuTime,
            "cpuTimePercent": pcentCpuTime
        }

        self.record.insert(0, newStats)
        self.emit("resources-sampled")

    def cpu_time_vector(self):
        vector = []
        stats = self.record
        for i in range(self.config.get_stats_history_length()+1):
            if i < len(stats):
                vector.append(stats[i]["cpuTimePercent"]/100.0)
            else:
                vector.append(0)
        return vector

    def cpu_time_vector_limit(self, limit):
        cpudata = self.cpu_time_vector()
        if len(cpudata) > limit:
            cpudata = cpudata[0:limit]
        return cpudata

    def cpu_time_percentage(self):
        if len(self.record) == 0:
            return 0
        return self.record[0]["cpuTimePercent"]

    def current_memory(self):
        if len(self.record) == 0:
            return 0
        return self.record[0]["memory"]

    def pretty_current_memory(self):
        mem = self.current_memory()
        if mem > (1024*1024):
            return "%2.2f GB" % (mem/(1024.0*1024.0))
        else:
            return "%2.2f MB" % (mem/1024.0)

    def current_memory(self):
        if len(self.record) == 0:
            return 0
        return self.record[0]["memory"]

    def current_memory_percentage(self):
        if len(self.record) == 0:
            return 0
        return self.record[0]["memoryPercent"]

    def current_memory_vector(self):
        vector = []
        stats = self.record
        for i in range(self.config.get_stats_history_length()+1):
            if i < len(stats):
                vector.append(stats[i]["memoryPercent"]/100.0)
            else:
                vector.append(0)
        return vector

    def uuidstr(self, rawuuid):
        hex = ['0','1','2','3','4','5','6','7','8','9','a','b','c','d','e','f']
        uuid = []
        for i in range(16):
            uuid.append(hex[((ord(rawuuid[i]) >> 4) & 0xf)])
            uuid.append(hex[(ord(rawuuid[i]) & 0xf)])
            if i == 3 or i == 5 or i == 7 or i == 9:
                uuid.append('-')
        return "".join(uuid)


    # Standard python urlparse is utterly braindead - refusing to parse URIs
    # in any useful fashion unless the 'scheme' is in some pre-defined white
    # list. Theis functions is a hacked version of urlparse

    def uri_split(self):
        uri = self.uri
        netloc = query = fragment = ''
        i = uri.find(":")
        if i > 0:
            scheme, uri = uri[:i].lower(), uri[i+1:]
            if uri[:2] == '//':
                netloc, uri = self._splitnetloc(uri, 2)
            if '#' in uri:
                uri, fragment = uri.split('#', 1)
            if '?' in uri:
                uri, query = uri.split('?', 1)
        else:
            scheme = uri.lower()

        return scheme, netloc, uri, query, fragment

    def _splitnetloc(self, url, start=0):
        for c in '/?#': # the order is important!
            delim = url.find(c, start)
            if delim >= 0:
                break
        else:
            delim = len(url)
        return url[start:delim], url[delim:]


    def is_active(self):
        return self._active
    def set_active(self, val):
        self._active = val
    active = property(is_active, set_active)


gobject.type_register(vmmConnection)

