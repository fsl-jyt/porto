import os
import time
import socket
import threading

from . import rpc_pb2
from . import exceptions


def _VarintEncoder():
    """Return an encoder for a basic varint value (does not include tag)."""

    local_chr = chr

    def EncodeVarint(write, value):
        bits = value & 0x7f
        value >>= 7
        while value:
            write(local_chr(0x80 | bits))
            bits = value & 0x7f
            value >>= 7
        return write(local_chr(bits))

    return EncodeVarint

_EncodeVarint = _VarintEncoder()


def _VarintDecoder(mask):
    """Return an encoder for a basic varint value (does not include tag).

    Decoded values will be bitwise-anded with the given mask before being
    returned, e.g. to limit them to 32 bits.  The returned decoder does not
    take the usual "end" parameter -- the caller is expected to do bounds checking
    after the fact (often the caller can defer such checking until later).  The
    decoder returns a (value, new_pos) pair.
    """

    local_ord = ord

    def DecodeVarint(buffer, pos):
        result = 0
        shift = 0
        while 1:
            b = local_ord(buffer[pos])
            result |= ((b & 0x7f) << shift)
            pos += 1
            if not (b & 0x80):
                result &= mask
                return (result, pos)
            shift += 7
            if shift >= 64:
                raise IOError('Too many bytes when decoding varint.')
    return DecodeVarint

_DecodeVarint = _VarintDecoder((1 << 64) - 1)
_DecodeVarint32 = _VarintDecoder((1 << 32) - 1)


class _RPC(object):
    def __init__(self, socket_path, timeout, socket_constructor,
                 lock_constructor, auto_reconnect):
        self.lock = lock_constructor()
        self.socket_path = socket_path
        self.timeout = timeout
        self.socket_constructor = socket_constructor
        self.sock = None
        self.sock_pid = None
        self.deadline = None
        self.auto_reconnect = auto_reconnect

    def _check_pid(self):
        if self.sock_pid is not None and self.sock_pid != os.getpid():
            raise exceptions.SocketError("Socket connected by pid {} current pid {}".format(self.sock_pid, os.getpid()))

    def _set_locked(fn):
        def _lock(*args, **kwargs):
            self = args[0]
            with self.lock:
                return fn(*args, **kwargs)
        return _lock

    def _set_deadline(fn):
        def _set(*args, **kwargs):
            self = args[0]

            if self.timeout is not None:
                self.deadline = time.time() + self.timeout
            else:
                self.deadline = None

            return fn(*args, **kwargs)
        return _set

    def _check_deadline(fn):
        def _check(*args, **kwargs):
            self = args[0]
            while (self.deadline is None) or (time.time() < self.deadline):
                try:
                    return fn(*args, **kwargs)

                except (exceptions.SocketError,
                        exceptions.SocketTimeout,
                        exceptions.UnknownError) as e:

                    if (not self.auto_reconnect):
                        raise e

                    time.sleep(0.05)
            raise exceptions.SocketTimeout("Got operation timeout")
        return _check

    def _set_timeout(self):
        if self.deadline is None:
            self.sock.settimeout(None)
        else:
            ts = time.time()
            if ts < self.deadline:
                self.sock.settimeout(self.deadline - ts)
            else:
                raise exceptions.SocketTimeout("Got operation timeout")

    def _connect(self):
        try:
            SOCK_CLOEXEC = 02000000
            self.sock = self.socket_constructor(socket.AF_UNIX, socket.SOCK_STREAM | SOCK_CLOEXEC)
            self.sock.connect(self.socket_path)
            self.sock_pid = os.getpid()
        except socket.error as e:
            self.sock = None
            raise exceptions.SocketError("Cannot connect to {}: {}".format(self.socket_path, e))

    def _senddata(self, data, flags=0):
        try:
            self._check_pid()
            return self.sock.sendall(data, flags)
        except socket.timeout:
            if self.auto_reconnect:
                self.sock = None
            raise exceptions.SocketTimeout("Got timeout on send")
        except socket.error as e:
            if self.auto_reconnect:
                self.sock = None
            raise exceptions.SocketError("Send error: {}".format(e))

    def _recvdata(self, count, flags=0):
        try:
            buf = []
            l = 0
            while l < count:
                self._check_pid()
                piece = self.sock.recv(count - l, flags)
                if len(piece) == 0:  # this means that socket is in invalid state
                    raise socket.error(socket.errno.ECONNRESET, os.strerror(socket.errno.ECONNRESET))
                buf.append(piece)
                l += len(piece)
            return ''.join(buf)
        except socket.timeout:
            if self.auto_reconnect:
                self.sock = None
            raise exceptions.SocketTimeout("Got timeout on receive")
        except socket.error as e:
            if self.auto_reconnect:
                self.sock = None
            raise exceptions.SocketError("Recv error: {}".format(e))

    @_check_deadline
    def _send_hdr(self, hdr_data):

        # reconnect after fork
        if self.sock_pid is not None and self.sock_pid != os.getpid():
            if self.auto_reconnect:
                self.sock = None
            else:
                self._check_pid()

        if self.sock is None:
            self._connect()

        self._senddata(hdr_data)

    def _send_request(self, request):
        data = request.SerializeToString()
        hdr = bytearray()
        _EncodeVarint(hdr.append, len(data))

        self._send_hdr(hdr)
        self._senddata(data)

    def _recv_response(self):
        msb = 1
        buf = ""
        while msb:
            self._set_timeout()

            b = self._recvdata(1)
            msb = ord(b) >> 7
            buf += b

        length = _DecodeVarint32(buf, 0)
        resp = rpc_pb2.TContainerResponse()

        self._set_timeout()

        buf += self._recvdata(length[0])
        resp.ParseFromString(buf[length[1]:])

        if resp.error != rpc_pb2.Success:
            raise exceptions.PortoException.Create(resp.error, resp.errorMsg)

        return resp

    @_set_locked
    @_set_deadline
    def call(self, request, timeout):
        self._send_request(request)

        if timeout is None:
            self.deadline = None
        elif timeout > self.timeout:
            self.deadline += timeout - self.timeout

        return self._recv_response()

    @_set_locked
    @_set_deadline
    @_check_deadline
    def connect(self):
        self._connect()

    @_set_locked
    def try_connect(self):
        self._connect()

    @_set_locked
    def disconnect(self):
        if self.sock is not None:
            self.sock.close()
        self.sock = None


class Container(object):
    def __init__(self, conn, name):
        assert isinstance(conn, Connection)
        self.conn = conn
        self.name = name

    def __str__(self):
        return self.name

    def __repr__(self):
        return 'Container `{}`'.format(self.name)

    def __div__(self, child):
        if self.name == "/":
            return Container(self.conn, child)
        return Container(self.conn, self.name + "/" + child)

    def __getitem__(self, property):
        return self.conn.GetProperty(self.name, property)

    def __setitem__(self, property, value):
        return self.conn.SetProperty(self.name, property, value)

    def Start(self):
        self.conn.Start(self.name)

    def Stop(self, timeout=None):
        self.conn.Stop(self.name, timeout)

    def Kill(self, sig):
        self.conn.Kill(self.name, sig)

    def Pause(self):
        self.conn.Pause(self.name)

    def Resume(self):
        self.conn.Resume(self.name)

    def Get(self, variables, nonblock=False, sync=False):
        return self.conn.Get([self.name], variables, nonblock, sync)[self.name]

    def Set(self, **kwargs):
        self.conn.Set(self.name, **kwargs)

    def GetProperties(self):
        return self.Get(self.conn.Plist())

    def GetProperty(self, property, sync=False):
        return self.conn.GetProperty(self.name, property, sync)

    def SetProperty(self, property, value):
        self.conn.SetProperty(self.name, property, value)

    def GetData(self, data, sync=False):
        return self.conn.GetData(self.name, data, sync)

    def SetSymlink(self, symlink, target):
        return self.conn.SetSymlink(self.name, symlink, target)

    def Wait(self, *args, **kwargs):
        return self.conn.Wait([self.name], *args, **kwargs)

    def Destroy(self):
        self.conn.Destroy(self.name)

    def ListVolumeLinks(self):
        return self.conn.ListVolumeLinks(container=self)

class Layer(object):
    def __init__(self, conn, name, place=None, pb=None):
        assert isinstance(conn, Connection)
        self.conn = conn
        self.name = name
        self.place = place
        self.owner_user = None
        self.owner_group = None
        self.last_usage = None
        self.private_value = None
        if pb is not None:
            self.Update(pb)

    def Update(self, pb=None):
        if pb is None:
            pb = self.conn._ListLayers(self.place, self.name).layers[0]
        self.owner_user = pb.owner_user
        self.owner_group = pb.owner_group
        self.last_usage = pb.last_usage
        self.private_value = pb.private_value

    def __str__(self):
        return self.name

    def __repr__(self):
        return 'Layer `{}` at `{}`'.format(self.name, self.place or "/place")

    def Merge(self, tarball, private_value=None):
        self.conn.MergeLayer(self.name, tarball, place=self.place, private_value=private_value)

    def Remove(self):
        self.conn.RemoveLayer(self.name, place=self.place)

    def Export(self, tarball, compress=None):
        self.conn.ReExportLayer(self.name, place=self.place, tarball=tarball, compress=compress)

    def GetPrivate(self):
        return self.conn.GetLayerPrivate(self.name, place=self.place)

    def SetPrivate(self, private_value):
        return self.conn.SetLayerPrivate(self.name, private_value, place=self.place)


class Storage(object):
    def __init__(self, conn, name, place, pb=None):
        assert isinstance(conn, Connection)
        self.conn = conn
        self.name = name
        self.place = place
        self.private_value = None
        self.owner_user = None
        self.owner_group = None
        self.last_usage = None
        if pb is not None:
            self.Update(pb)

    def __str__(self):
        return self.name

    def __repr__(self):
        return 'Storage `{}` at `{}`'.format(self.name, self.place or "/place")

    def Update(self, pb=None):
        if pb is None:
            pb = self.conn._ListStorages(self.place, self.name).storages[0]
        self.private_value = pb.private_value
        self.owner_user = pb.owner_user
        self.owner_group = pb.owner_group
        self.last_usage = pb.last_usage
        return self

    def GetProperty(self, name):
        self.Update().getattr(name)

    def Remove(self):
        self.conn.RemoveStorage(self.name, self.place)

    def Import(self, tarball):
        self.conn.ImportStorage(self.name, place=self.place, tarball=tarball, private_value=self.private_value)
        self.Update()

    def Export(self, tarball):
        self.conn.ExportStorage(self.name, place=self.place, tarball=tarball)

class MetaStorage(object):
    def __init__(self, conn, name, place, pb=None):
        assert isinstance(conn, Connection)
        self.conn = conn
        self.name = name
        self.place = place
        self.private_value = None
        self.owner_user = None
        self.owner_group = None
        self.last_usage = None
        self.space_limit = None
        self.inode_limit = None
        self.space_used = None
        self.inode_used = None
        self.space_available = None
        self.inode_available = None
        if pb is not None:
            self.Update(pb)

    def __str__(self):
        return self.name

    def __repr__(self):
        return 'MetaStorage `{}` at `{}`'.format(self.name, self.place or "/place")

    def Update(self, pb=None):
        if pb is None:
            pb = self.conn._ListStorages(self.place, self.name).meta_storages[0]
        self.private_value = pb.private_value
        self.owner_user = pb.owner_user
        self.owner_group = pb.owner_group
        self.last_usage = pb.last_usage
        self.space_limit = pb.space_limit
        self.inode_limit = pb.inode_limit
        self.space_used = pb.space_used
        self.inode_used = pb.inode_used
        self.space_available = pb.space_available
        self.inode_available = pb.inode_available
        return self

    def GetProperty(self, name):
        self.Update().getattr(name)

    def Resize(self, private_value=None, space_limit=None, inode_limit=None):
        self.conn.ResizeMetaStorage(self.name, self.place, private_value, space_limit, inode_limit)
        self.Update()

    def Remove(self):
        self.conn.RemoveMetaStorage(self.name, self.place)

    def ListLayers(self):
        return self.conn.ListLayers(place=self.place, mask=self.name + "/*")

    def ListStorages(self):
        return self.conn.ListStorages(place=self.place, mask=self.name + "/*")

    def FindLayer(self, subname):
        return self.conn.FindLayer(self.name + "/" + subname, place=self.place)

    def FindStorage(self, subname):
        return self.conn.FindStorage(self.name + "/" + subname, place=self.place)

class VolumeLink(object):
    def __init__(self, volume, container, target, read_only, required):
        self.volume = volume
        self.container = container
        self.target = target
        self.read_only = read_only
        self.required = required

    def __repr__(self):
        return 'VolumeLink `{}` `{}` `{}`'.format(self.volume.path, self.container.name, self.target)

    def Unlink(self):
        self.volume.Unlink(container)

class Volume(object):
    def __init__(self, conn, path, pb=None):
        assert isinstance(conn, Connection)
        self.conn = conn
        self.path = path
        if pb is not None:
            self.Update(pb)

    def Update(self, pb=None):
        if pb is None:
            pb = self.conn._ListVolumes(path=self.path)[0]

        self.containers = [Container(self.conn, c) for c in pb.containers]
        self.properties = {p.name: p.value for p in pb.properties}
        self.place = self.properties.get('place')
        self.private = self.properties.get('private')
        self.private_value = self.properties.get('private')
        self.id = self.properties.get('id')
        self.state = self.properties.get('state')
        self.backend = self.properties.get('backend')
        self.read_only = self.properties.get('read_only') == "true"
        self.owner_user = self.properties.get('owner_user')
        self.owner_group = self.properties.get('owner_group')

        if 'owner_container' in self.properties:
            self.owner_container = Container(self.conn, self.properties.get('owner_container'))
        else:
            self.owner_container = None

        layers = self.properties.get('layers', "")
        layers = layers.split(';') if layers else []
        self.layers = [Layer(self.conn, l, self.place) for l in layers]

        if self.properties.get('storage') and self.properties['storage'][0] != '/':
            self.storage = Storage(self.conn, self.properties['storage'], place=self.place)
        else:
            self.storage = None

        for name in ['space_limit', 'inode_limit', 'space_used', 'inode_used', 'space_available', 'inode_available', 'space_guarantee', 'inode_guarantee']:
            setattr(self, name, int(self.properties[name]) if name in self.properties else None)

        return self

    def __str__(self):
        return self.path

    def __repr__(self):
        return 'Volume `{}`'.format(self.path)

    def __getitem__(self, property):
        self.Update()
        return getattr(self, property)

    def __setitem__(self, property, value):
        self.Tune(**{property: value})
        self.Update()

    def GetProperties(self):
        self.Update()
        return self.properties

    def GetProperty(self, property):
        self.Update()
        return getattr(self, property)

    def GetContainers(self):
        self.Update()
        return self.containers

    def ListVolumeLinks(self):
        return self.conn.ListVolumeLinks(volume=self)

    def GetLayers(self):
        self.Update()
        return self.layers

    def Link(self, container=None, target=None, read_only=False, required=False):
        if isinstance(container, Container):
            container = container.name
        self.conn.LinkVolume(self.path, container, target=target, read_only=read_only, required=required)

    def Unlink(self, container=None, strict=None):
        if isinstance(container, Container):
            container = container.name
        self.conn.UnlinkVolume(self.path, container, strict)

    def Tune(self, **properties):
        self.conn.TuneVolume(self.path, **properties)

    def Export(self, tarball, compress=None):
        self.conn.ExportLayer(self.path, place=self.place, tarball=tarball, compress=compress)

    def Destroy(self):
        self.conn.UnlinkVolume(self.path, '***')


class Connection(object):
    def __init__(self,
                 socket_path='/run/portod.socket',
                 timeout=300,
                 socket_constructor=socket.socket,
                 lock_constructor=threading.Lock,
                 auto_reconnect=True):
        self.rpc = _RPC(socket_path=socket_path,
                        timeout=timeout,
                        socket_constructor=socket_constructor,
                        lock_constructor=lock_constructor,
                        auto_reconnect=auto_reconnect)

    def connect(self):
        self.rpc.connect()

    def TryConnect(self):
        self.rpc.try_connect()

    def disconnect(self):
        self.rpc.disconnect()

    def List(self, mask=None):
        request = rpc_pb2.TContainerRequest()
        request.list.CopyFrom(rpc_pb2.TContainerListRequest())
        if mask is not None:
            request.list.mask = mask
        return self.rpc.call(request, self.rpc.timeout).list.name

    def ListContainers(self, mask=None):
        return [Container(self, name) for name in self.List(mask)]

    def Find(self, name):
        self.GetProperty(name, "state")
        return Container(self, name)

    def Create(self, name, weak=False):
        request = rpc_pb2.TContainerRequest()
        if weak:
            request.createWeak.name = name
        else:
            request.create.name = name
        self.rpc.call(request, self.rpc.timeout)
        return Container(self, name)

    def CreateWeakContainer(self, name):
        return self.Create(name, weak=True)

    def Run(self, name, weak=True, start=True, root_volume=None, private_value=None, **kwargs):
        ct = self.Create(name, weak=True)
        for property, value in kwargs.iteritems():
            ct.SetProperty(property, value)
        if private_value is not None:
            ct.SetProperty('private', private_value)
        if root_volume is not None:
            root = self.CreateVolume(containers=name, **root_volume)
            ct.SetProperty('root', root.path)
        if start:
            ct.Start()
        if not weak:
            ct.SetProperty('weak', False)
        return ct

    def Destroy(self, container):
        if isinstance(container, Container):
            container = container.name
        request = rpc_pb2.TContainerRequest()
        request.destroy.name = container
        self.rpc.call(request, self.rpc.timeout)

    def Start(self, name):
        request = rpc_pb2.TContainerRequest()
        request.start.name = name
        self.rpc.call(request, self.rpc.timeout)

    def Stop(self, name, timeout=None):
        request = rpc_pb2.TContainerRequest()
        request.stop.name = name
        if timeout is not None and timeout >= 0:
            request.stop.timeout_ms = timeout * 1000
        else:
            timeout = 30
        self.rpc.call(request, max(self.rpc.timeout, timeout + 1))

    def Kill(self, name, sig):
        request = rpc_pb2.TContainerRequest()
        request.kill.name = name
        request.kill.sig = sig
        self.rpc.call(request, self.rpc.timeout)

    def Pause(self, name):
        request = rpc_pb2.TContainerRequest()
        request.pause.name = name
        self.rpc.call(request, self.rpc.timeout)

    def Resume(self, name):
        request = rpc_pb2.TContainerRequest()
        request.resume.name = name
        self.rpc.call(request, self.rpc.timeout)

    def Get(self, containers, variables, nonblock=False, sync=False):
        request = rpc_pb2.TContainerRequest()
        request.get.name.extend(containers)
        request.get.variable.extend(variables)
        request.get.sync = sync
        if nonblock:
            request.get.nonblock = nonblock
        resp = self.rpc.call(request, self.rpc.timeout)
        if resp.error != rpc_pb2.Success:
            raise exceptions.PortoException.Create(resp.error, resp.errorMsg)

        res = {}
        for container in resp.get.list:
            var = {}
            for kv in container.keyval:
                if kv.HasField('error'):
                    var[kv.variable] = exceptions.PortoException.Create(kv.error, kv.errorMsg)
                    continue
                if kv.value == 'false':
                    var[kv.variable] = False
                elif kv.value == 'true':
                    var[kv.variable] = True
                else:
                    var[kv.variable] = kv.value

            res[container.name] = var
        return res

    def GetProperty(self, name, property, sync=False):
        request = rpc_pb2.TContainerRequest()
        request.getProperty.name = name
        request.getProperty.property = property
        request.getProperty.sync = sync
        res = self.rpc.call(request, self.rpc.timeout).getProperty.value
        if res == 'false':
            return False
        elif res == 'true':
            return True
        return res

    def SetProperty(self, name, property, value):
        if value is False:
            value = 'false'
        elif value is True:
            value = 'true'
        elif value is None:
            value = ''
        elif isinstance(value, (int, long)):
            value = str(value)

        request = rpc_pb2.TContainerRequest()
        request.setProperty.name = name
        request.setProperty.property = property
        request.setProperty.value = value
        self.rpc.call(request, self.rpc.timeout)

    def Set(self, container, **kwargs):
        for name, value in kwargs.items():
            self.SetProperty(container, name, value)

    def GetData(self, name, data, sync=False):
        request = rpc_pb2.TContainerRequest()
        request.getData.name = name
        request.getData.data = data
        request.getData.sync = sync
        res = self.rpc.call(request, self.rpc.timeout).getData.value
        if res == 'false':
            return False
        elif res == 'true':
            return True
        return res

    def Plist(self):
        request = rpc_pb2.TContainerRequest()
        request.propertyList.CopyFrom(rpc_pb2.TContainerPropertyListRequest())
        return [item.name for item in self.rpc.call(request, self.rpc.timeout).propertyList.list]

    def Dlist(self):
        request = rpc_pb2.TContainerRequest()
        request.dataList.CopyFrom(rpc_pb2.TContainerDataListRequest())
        return [item.name for item in self.rpc.call(request, self.rpc.timeout).dataList.list]

    def Vlist(self):
        request = rpc_pb2.TContainerRequest()
        request.listVolumeProperties.CopyFrom(rpc_pb2.TVolumePropertyListRequest())
        result = self.rpc.call(request, self.rpc.timeout).volumePropertyList.properties
        return [prop.name for prop in result]

    def Wait(self, containers, timeout=None, timeout_s=None):
        request = rpc_pb2.TContainerRequest()
        request.wait.name.extend(containers)
        if timeout_s is not None:
            timeout = timeout_s * 1000
        if timeout is not None and timeout >= 0:
            request.wait.timeout_ms = timeout
            resp = self.rpc.call(request, timeout / 1000)
        else:
            resp = self.rpc.call(request, None)
        if resp.error != rpc_pb2.Success:
            raise exceptions.PortoException.Create(resp.error, resp.errorMsg)
        return resp.wait.name

    def CreateVolume(self, path=None, layers=[], storage=None, private_value=None, **properties):
        if layers:
            layers = [l.name if isinstance(l, Layer) else l for l in layers]
            properties['layers'] = ';'.join(layers)

        if storage is not None:
            properties['storage'] = str(storage)

        if private_value is not None:
            properties['private'] = private_value

        request = rpc_pb2.TContainerRequest()
        request.createVolume.CopyFrom(rpc_pb2.TVolumeCreateRequest())
        if path:
            request.createVolume.path = path
        for name, value in properties.iteritems():
            prop = request.createVolume.properties.add()
            prop.name, prop.value = name, value
        pb = self.rpc.call(request, self.rpc.timeout).volume
        return Volume(self, pb.path, pb)

    def FindVolume(self, path):
        pb = self._ListVolumes(path=path)[0]
        return Volume(self, path, pb)

    def LinkVolume(self, path, container, target=None, read_only=False, required=False):
        request = rpc_pb2.TContainerRequest()
        if target is not None or required:
            command = request.LinkVolumeTarget
        else:
            command = request.linkVolume
        command.path = path
        command.container = container
        if target is not None:
            command.target = target
        if read_only:
            command.read_only = True
        if required:
            command.required = True
        self.rpc.call(request, self.rpc.timeout)

    def UnlinkVolume(self, path, container=None, target=None, strict=None):
        request = rpc_pb2.TContainerRequest()
        if target is not None:
            command = request.UnlinkVolumeTarget
        else:
            command = request.unlinkVolume
        command.path = path
        if container:
            command.container = container
        if target is not None:
            command.target = target
        if strict is not None:
            command.strict = strict
        self.rpc.call(request, self.rpc.timeout)

    def DestroyVolume(self, volume):
        self.UnlinkVolume(volume.path if isinstance(volume, Volume) else volume, '***')

    def _ListVolumes(self, path=None, container=None):
        if isinstance(container, Container):
            container = container.name
        request = rpc_pb2.TContainerRequest()
        request.listVolumes.CopyFrom(rpc_pb2.TVolumeListRequest())
        if path:
            request.listVolumes.path = path
        if container:
            request.listVolumes.container = container
        return self.rpc.call(request, self.rpc.timeout).volumeList.volumes

    def ListVolumes(self, container=None):
        return [Volume(self, v.path, v) for v in self._ListVolumes(container)]

    def ListVolumeLinks(self, volume=None, container=None):
        links = []
        for v in self._ListVolumes(path=volume.path if isinstance(volume, Volume) else volume, container=container):
            for l in v.links:
                links.append(VolumeLink(Volume(self, v.path, v), Container(self, l.container), l.target, l.read_only, l.required))
        return links

    def GetVolumeProperties(self, path):
        return {p.name: p.value for p in self._ListVolumes(path=path)[0].properties}

    def TuneVolume(self, path, **properties):
        request = rpc_pb2.TContainerRequest()
        request.tuneVolume.CopyFrom(rpc_pb2.TVolumeTuneRequest())
        request.tuneVolume.path = path
        for name, value in properties.iteritems():
            prop = request.tuneVolume.properties.add()
            prop.name, prop.value = name, value
        self.rpc.call(request, self.rpc.timeout)

    def ImportLayer(self, layer, tarball, place=None, private_value=None):
        request = rpc_pb2.TContainerRequest()
        request.importLayer.layer = layer
        request.importLayer.tarball = tarball
        request.importLayer.merge = False
        if place is not None:
            request.importLayer.place = place
        if private_value is not None:
            request.importLayer.private_value = private_value

        self.rpc.call(request, self.rpc.timeout)
        return Layer(self, layer, place)

    def MergeLayer(self, layer, tarball, place=None, private_value=None):
        request = rpc_pb2.TContainerRequest()
        request.importLayer.layer = layer
        request.importLayer.tarball = tarball
        request.importLayer.merge = True
        if place is not None:
            request.importLayer.place = place
        if private_value is not None:
            request.importLayer.private_value = private_value
        self.rpc.call(request, self.rpc.timeout)
        return Layer(self, layer, place)

    def RemoveLayer(self, layer, place=None):
        request = rpc_pb2.TContainerRequest()
        request.removeLayer.layer = layer
        if place is not None:
            request.removeLayer.place = place
        self.rpc.call(request, self.rpc.timeout)

    def GetLayerPrivate(self, layer, place=None):
        request = rpc_pb2.TContainerRequest()
        request.getlayerprivate.layer = layer
        if place is not None:
            request.getlayerprivate.place = place
        return self.rpc.call(request, self.rpc.timeout).layer_private.private_value

    def SetLayerPrivate(self, layer, private_value, place=None):
        request = rpc_pb2.TContainerRequest()
        request.setlayerprivate.layer = layer
        request.setlayerprivate.private_value = private_value

        if place is not None:
            request.setlayerprivate.place = place
        self.rpc.call(request, self.rpc.timeout)

    def ExportLayer(self, volume, tarball, place=None, compress=None):
        request = rpc_pb2.TContainerRequest()
        request.exportLayer.volume = volume
        request.exportLayer.tarball = tarball
        if place is not None:
            request.exportLayer.place = place
        if compress is not None:
            request.exportLayer.compress = compress
        self.rpc.call(request, self.rpc.timeout)

    def ReExportLayer(self, layer, tarball, place=None, compress=None):
        request = rpc_pb2.TContainerRequest()
        request.exportLayer.volume = ""
        request.exportLayer.layer = layer
        request.exportLayer.tarball = tarball
        if place is not None:
            request.exportLayer.place = place
        if compress is not None:
            request.exportLayer.compress = compress
        self.rpc.call(request, self.rpc.timeout)

    def _ListLayers(self, place=None, mask=None):
        request = rpc_pb2.TContainerRequest()
        request.listLayers.CopyFrom(rpc_pb2.TLayerListRequest())
        if place is not None:
            request.listLayers.place = place
        if mask is not None:
            request.listLayers.mask = mask
        return self.rpc.call(request, self.rpc.timeout).layers

    def ListLayers(self, place=None, mask=None):
        response = self._ListLayers(place, mask)
        if response.layers:
            return [Layer(self, l.name, place, l) for l in response.layers]
        return [Layer(self, l, place) for l in response.layer]

    def FindLayer(self, layer, place=None):
        response = self._ListLayers(place, layer)
        if layer not in response.layer:
            raise exceptions.LayerNotFound("layer `%s` not found" % layer)
        if response.layers and response.layers[0].name == layer:
            return Layer(self, layer, place, response.layers[0])
        return Layer(self, layer, place)

    def _ListStorages(self, place=None, mask=None):
        request = rpc_pb2.TContainerRequest()
        request.listStorage.CopyFrom(rpc_pb2.TStorageListRequest())
        if place is not None:
            request.listStorage.place = place
        if mask is not None:
            request.listStorage.mask = mask
        return self.rpc.call(request, self.rpc.timeout).storageList

    def ListStorages(self, place=None, mask=None):
        return [Storage(self, s.name, place, s) for s in self._ListStorages(place, mask).storages]

    # deprecated
    def ListStorage(self, place=None, mask=None):
        return [Storage(self, s.name, place, s) for s in self._ListStorages(place, mask).storages]

    def FindStorage(self, name, place=None):
        response = self._ListStorages(place, name)
        if not response.storages:
            raise exceptions.VolumeNotFound("storage `%s` not found" % name)
        return Storage(self, name, place, response.storages[0])

    def ListMetaStorages(self, place=None, mask=None):
        return [MetaStorage(self, s.name, place, s) for s in self._ListStorages(place, mask).meta_storages]

    def FindMetaStorage(self, name, place=None):
        response = self._ListStorages(place, name)
        if not response.meta_storages:
            raise exceptions.VolumeNotFound("meta storage `%s` not found" % name)
        return MetaStorage(self, name, place, response.meta_storages[0])

    def RemoveStorage(self, name, place=None):
        request = rpc_pb2.TContainerRequest()
        request.removeStorage.name = name
        if place is not None:
            request.removeStorage.place = place
        self.rpc.call(request, self.rpc.timeout)

    def ImportStorage(self, name, tarball, place=None, private_value=None):
        request = rpc_pb2.TContainerRequest()
        request.importStorage.name = name
        request.importStorage.tarball = tarball
        if place is not None:
            request.importStorage.place = place
        if private_value is not None:
            request.importStorage.private_value = private_value
        self.rpc.call(request, self.rpc.timeout)
        return Storage(self, name, place)

    def ExportStorage(self, name, tarball, place=None):
        request = rpc_pb2.TContainerRequest()
        request.exportStorage.name = name
        request.exportStorage.tarball = tarball
        if place is not None:
            request.exportStorage.place = place
        self.rpc.call(request, self.rpc.timeout)

    def CreateMetaStorage(self, name, place=None, private_value=None, space_limit=None, inode_limit=None):
        request = rpc_pb2.TContainerRequest()
        request.CreateMetaStorage.name = name
        if place is not None:
            request.CreateMetaStorage.place = place
        if private_value is not None:
            request.CreateMetaStorage.private_value = private_value
        if space_limit is not None:
            request.CreateMetaStorage.space_limit = space_limit
        if inode_limit is not None:
            request.CreateMetaStorage.inode_limit = inode_limit
        self.rpc.call(request, self.rpc.timeout)
        return MetaStorage(self, name, place)

    def ResizeMetaStorage(self, name, place=None, private_value=None, space_limit=None, inode_limit=None):
        request = rpc_pb2.TContainerRequest()
        request.ResizeMetaStorage.name = name
        if place is not None:
            request.ResizeMetaStorage.place = place
        if private_value is not None:
            request.ResizeMetaStorage.private_value = private_value
        if space_limit is not None:
            request.ResizeMetaStorage.space_limit = space_limit
        if inode_limit is not None:
            request.ResizeMetaStorage.inode_limit = inode_limit
        self.rpc.call(request, self.rpc.timeout)

    def RemoveMetaStorage(self, name, place=None):
        request = rpc_pb2.TContainerRequest()
        request.RemoveMetaStorage.name = name
        if place is not None:
            request.RemoveMetaStorage.place = place
        self.rpc.call(request, self.rpc.timeout)

    def ConvertPath(self, path, source, destination):
        request = rpc_pb2.TContainerRequest()
        request.convertPath.path = path
        request.convertPath.source = source
        request.convertPath.destination = destination
        return self.rpc.call(request, self.rpc.timeout).convertPath.path

    def SetSymlink(self, name, symlink, target):
        request = rpc_pb2.TContainerRequest()
        request.SetSymlink.container = name
        request.SetSymlink.symlink = symlink
        request.SetSymlink.target = target
        self.rpc.call(request, self.rpc.timeout)

    def AttachProcess(self, name, pid, comm=""):
        request = rpc_pb2.TContainerRequest()
        request.attachProcess.name = name
        request.attachProcess.pid = pid
        request.attachProcess.comm = comm
        self.rpc.call(request, self.rpc.timeout)

    def LocateProcess(self, pid, comm=""):
        request = rpc_pb2.TContainerRequest()
        request.locateProcess.pid = pid
        request.locateProcess.comm = comm
        name = self.rpc.call(request, self.rpc.timeout).locateProcess.name
        return Container(self, name)

    def Version(self):
        request = rpc_pb2.TContainerRequest()
        request.version.CopyFrom(rpc_pb2.TVersionRequest())
        response = self.rpc.call(request, self.rpc.timeout)
        return (response.version.tag, response.version.revision)
