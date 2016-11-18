import os
import subprocess
import porto
import shutil
import time
from test_common import *

if os.getuid() != 0:
    SwitchRoot()

TMPDIR = "/tmp/test-release-upgrade"
PORTOD_PATH = "/run/portod"

try:
    os.mkdir(TMPDIR)
except BaseException as e:
    shutil.rmtree(TMPDIR)
    os.mkdir(TMPDIR)

#FIXME: remove it in the future, use capabilities from snapshot
def CheckCaps(r, new_porto):
    app_caps =  "CHOWN;DAC_OVERRIDE;FOWNER;FSETID;KILL;SETGID;SETUID;SETPCAP;"\
                "LINUX_IMMUTABLE;NET_BIND_SERVICE;NET_ADMIN;NET_RAW;IPC_LOCK;"\
                "SYS_CHROOT;SYS_PTRACE;SYS_ADMIN;SYS_BOOT;SYS_NICE;SYS_RESOURCE;"\
                "MKNOD;AUDIT_WRITE;SETFCAP"

    os_caps = "CHOWN;DAC_OVERRIDE;FOWNER;FSETID;KILL;SETGID;SETUID;"\
              "NET_BIND_SERVICE;NET_ADMIN;NET_RAW;IPC_LOCK;SYS_CHROOT;"\
              "SYS_PTRACE;SYS_BOOT;MKNOD;AUDIT_WRITE"

    legacy_os_caps = "AUDIT_CONTROL; AUDIT_READ; AUDIT_WRITE; BLOCK_SUSPEND; CHOWN; "\
                     "DAC_OVERRIDE; DAC_READ_SEARCH; FOWNER; FSETID; IPC_LOCK; IPC_OWNER; "\
                     "KILL; LEASE; LINUX_IMMUTABLE; MAC_ADMIN; MAC_OVERRIDE; MKNOD; "\
                     "NET_ADMIN; NET_BIND_SERVICE; NET_BROADCAST; NET_RAW; SETFCAP; "\
                     "SETGID; SETPCAP; SETUID; SYSLOG; SYS_ADMIN; SYS_BOOT; SYS_CHROOT; "\
                     "SYS_MODULE; SYS_NICE; SYS_PACCT; SYS_PTRACE; SYS_RAWIO; SYS_RESOURCE; "\
                     "SYS_TIME; SYS_TTY_CONFIG; WAKE_ALARM"

    if r.GetProperty("virt_mode") == "app":
        caps = app_caps if new_porto else ""
        value = r.GetProperty("capabilities")
        assert value == caps

    elif r.GetProperty("virt_mode") == "os":
        caps = os_caps if new_porto else legacy_os_caps
        assert r.GetProperty("capabilities") == caps

    else:
        raise AssertionError("Found unexpected virt_mode value")

#FIXME: remove it in ther future
def PropTrim(prop):
    if not (type(prop) is str or type(prop) is unicode):
        return prop
    return str(prop.replace("; ", ";"))

def SetProps(r, props):
    for p in props:
        r.SetProperty(p[0], p[1])

def VerifyProps(r, props):
    for p in props:
        value = r.GetProperty(p[0])
        try:
            assert PropTrim(p[1]) == PropTrim(value)
        except AssertionError as e:
            print "{} prop value <{}> != <{}>".format(p[0], p[1], value)
            raise e

def SnapshotProps(r):
    #FIXME: add controllers, cpu_set, owner_group, owner_user, umask later
    props = [ "aging_time", "anon_limit", "bind",
              #"bind_dns", #FIXME enable later, false -> true
              #"capabilities", #FIXME enable later, os: "<set1>" -> "<set2>" ,
                               #app: "" -> "<not empty>"
              "command", "cpu_guarantee", "cpu_limit", "cpu_policy",
              "cwd", "devices", "dirty_limit", "enable_porto", "env",
              "group", "hostname", "io_limit", "io_ops_limit", "io_policy", "ip",
              "isolate", "max_respawns", "memory_guarantee", "memory_limit", "net",
              #"net_guarantee", #FIXME enable later, "default:0" -> ""
              #"net_limit", #FIXME enable later, "default:0" -> ""
              "net_priority", "porto_namespace",
              "private", "recharge_on_pgfault",
              "resolv_conf", "respawn", "root", "root_readonly",
              #"stderr_path", #FIXME enable later, "/dev/null" -> ""
              #"stdout_path", #FIXME enable later, "/dev/null" -> ""
              "stdin_path", "stdout_limit", "ulimit",
              "user", "virt_mode", "weak" ]
    d = dict()
    for p in props:
        d[p] = r.GetProperty(p)

    return d

def VerifySnapshot(r, props):
    props2 = SnapshotProps(r)
    for i in props:
        try:
            assert PropTrim(props[i]) == PropTrim(props2[i])
        except AssertionError as e:
            print "{} prop value <{}> != <{}>".format(i, props[i], props2[i])
            raise e

#Check  working with older version

print "Checking upgrade from the older version..."

cwd=os.path.abspath(os.getcwd())

os.chdir(TMPDIR)
try:
    #FIXME: Remove 2.10 suffix later
    download = subprocess.check_output(["apt-get", "download", "yandex-porto=2.10*"])
except:
    print "Cannot download old version of porto, skipping test..."
    os.chdir(cwd)
    os.rmdir(TMPDIR)
    sys.exit(0)

print "Package successfully downloaded"

subprocess.check_call([portod, "stop"])

downloads = download.split()
pktname = downloads[2] + "_" + downloads[3] + "_amd64.deb"

os.mkdir("old")
subprocess.check_call(["dpkg", "-x", pktname, "old"])
os.unlink(pktname)

os.chdir(cwd)

os.unlink(PORTOD_PATH)
os.symlink(TMPDIR + "/old/usr/sbin/portod", PORTOD_PATH)
subprocess.check_call([PORTOD_PATH + "&"], shell=True)
time.sleep(1)

oldver = subprocess.check_output([portod, "--version"]).split()[6]

c = porto.Connection(timeout=3)
c.Create("test")
c.SetProperty("test", "command", "sleep 5")
c.Start("test")

c.Create("test2")
c.SetProperty("test2", "command", "bash -c 'sleep 20 && echo 456'")
c.Start("test2")

parent_knobs = [
    ("private", "parent"),
    ("respawn", False),
    ("ulimit", "data: 16000000 32000000;memlock: 4096 4096;"\
               "nofile: 100 200;nproc: 500 1000"),
    ("isolate", True),
    ("user", "porto-alice"),
    ("env", "CONTAINER=porto;PARENT=1")
]

r = c.Create("parent_app")
SetProps(r, parent_knobs)
VerifyProps(r, parent_knobs)
snap_parent_app = SnapshotProps(r)

app_knobs = [
    ("cpu_limit", "1c"),
    ("private", "parent_app"),
    ("respawn", False),
    ("dirty_limit", "131072000"),
    ("cpu_policy", "normal"),
    ("memory_guarantee", "16384000"),
    ("command", "sleep 20"),
    ("memory_limit", "512000000"),
    ("cwd", portosrc),
    ("net_limit", "default: 0"),
    ("cpu_guarantee", "0.01c"),
    ("io_policy", "normal"),
    ("ulimit", "data: 16000000 32000000;memlock: 4096 4096;"\
               "nofile: 100 200;nproc: 500 1000"\
               ),
    ("io_limit", "300000"),
    ("isolate", False),
    ("user", "porto-alice"),
    ("env", "CONTAINER=porto;PARENT=1;TAG=mytag mytag2 mytag3")
]

r = c.Create("parent_app/app")
SetProps(r, app_knobs)
VerifyProps(r, app_knobs)
snap_app = SnapshotProps(r)
r.Start()

v = c.CreateVolume(None, layers=["ubuntu-precise"])
r = c.Create("parent_os")
SetProps(r, parent_knobs)
VerifyProps(r, parent_knobs)
snap_parent_os = SnapshotProps(r)

os_knobs = [
    ("virt_mode", "os"),
    ("porto_namespace", "parent"),
    ("bind", "{} /portobin ro; {} /portosrc ro".format(portobin, portosrc)),
    ("hostname", "shiny_os_container"),
    ("root_readonly", False),
    ("cpu_policy", "normal"),
    ("command", "/sbin/init"),
    ("env", "VIRT_MODE=os;BIND=;HOSTNAME=shiny_new_container;"\
            "ROOT_READONLY=false;CPU_POLICY=normal;COMMAND=/sbin/init;"\
            "NET=macvlan eth0 eth0;"\
            "ROOT={};RECHARGE_ON_PGFAULT=true".format(v.path)),
    ("net", "macvlan eth0 eth0"),
    ("root", v.path),
    ("recharge_on_pgfault", True)
]

r = c.Create("parent_os/os")
SetProps(r, os_knobs)
VerifyProps(r, os_knobs)
snap_os = SnapshotProps(r)
r.Start()

c.disconnect()

os.unlink(PORTOD_PATH)
os.symlink(portod, PORTOD_PATH)
subprocess.check_call([portod, "reload"])
time.sleep(1)

assert subprocess.check_output([portod, "--version"]).split()[6] != oldver
#That means we've upgraded successfully

c = porto.Connection(timeout=3)
c.Wait(["test"])

#Checking if we can create subcontainers successfully (cgroup migration involved)

r = c.Create("a")
r.SetProperty("command", "bash -c '" + portoctl + " run -W a/a command=\"echo 123\"'")
r.Start()
assert r.Wait() == "a"
assert r.GetProperty("exit_status") == "0"

r2 = c.Find("a/a")
r2.Wait() == "a/a"
assert r2.GetProperty("exit_status") == "0"
assert r2.GetProperty("stdout") == "123\n"
r2.Destroy()
r.Destroy()

assert c.GetProperty("test", "exit_status") == "0"

r = c.Find("parent_app")
VerifyProps(r, parent_knobs)
VerifySnapshot(r, snap_parent_app)
CheckCaps(r, True)

r = c.Find("parent_app/app")
VerifyProps(r, app_knobs)
VerifySnapshot(r, snap_app)
CheckCaps(r, True)

r = c.Find("parent_os")
VerifyProps(r, parent_knobs)
VerifySnapshot(r, snap_parent_os)
CheckCaps(r, True)

r = c.Find("parent_os/os")
VerifyProps(r, os_knobs)
VerifySnapshot(r, snap_os)
CheckCaps(r, True)

c.disconnect()

#Now, the downgrade
os.unlink(PORTOD_PATH)
os.symlink(TMPDIR + "/old/usr/sbin/portod", PORTOD_PATH)

subprocess.check_call([portod, "reload"])
time.sleep(1)

assert subprocess.check_output([portod, "--version"]).split()[6] == oldver

c = porto.Connection(timeout=3)

r = c.Find("test2")
assert r.Wait() == "test2"
assert r.GetProperty("stdout") == "456\n"
assert r.GetProperty("exit_status") == "0"

assert c.GetProperty("parent_os/os", "state") == "running"

r = c.Find("parent_app")
VerifyProps(r, parent_knobs)
VerifySnapshot(r, snap_parent_app)
CheckCaps(r, False)

r = c.Find("parent_app/app")
VerifyProps(r, app_knobs)
VerifySnapshot(r, snap_app)
CheckCaps(r, False)

r = c.Find("parent_os")
VerifyProps(r, parent_knobs)
CheckCaps(r, False)

r = c.Find("parent_os/os")
VerifyProps(r, os_knobs)
VerifySnapshot(r, snap_os)
CheckCaps(r, False)

c.disconnect()

subprocess.check_call([portod, "--verbose", "--discard", "restart"])

shutil.rmtree(TMPDIR)