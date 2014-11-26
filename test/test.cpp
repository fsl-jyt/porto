#include <sstream>

#include "test.hpp"
#include "util/file.hpp"
#include "util/folder.hpp"
#include "util/string.hpp"
#include "util/netlink.hpp"
#include "util/pwd.hpp"

using std::string;
using std::vector;

extern "C" {
#include <unistd.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <dirent.h>
#include <signal.h>
}

namespace test {

__thread int tid;
std::atomic<int> done;
std::vector<std::shared_ptr<TNlLink>> links;

std::basic_ostream<char> &Say(std::basic_ostream<char> &stream) {
    if (tid)
        return stream << "[" << tid << "] ";
    else
        return std::cerr << "- ";
}

void ExpectReturn(int ret, int exp, int line, const char *func) {
    if (ret == exp)
        return;
    throw std::string("Got " + std::to_string(ret) + ", but expected " + std::to_string(exp) + " at " + func + ":" + std::to_string(line));
}

int ReadPid(const std::string &path) {
    TFile f(path);
    int pid = 0;

    TError error = f.AsInt(pid);
    if (error)
        throw std::string(error.GetMsg());

    return pid;
}

vector<string> Popen(const std::string &cmd) {
    vector<string> lines;
    FILE *f = popen(cmd.c_str(), "r");
    if (f == nullptr)
        throw std::string("Can't execute ") + cmd;

    char *line = nullptr;
    size_t n;

    while (getline(&line, &n, f) >= 0) {
        lines.push_back(line);
    }

    fclose(f);

    return lines;
}

int Pgrep(const std::string &name) {
    vector<string> lines = Popen("pgrep -x " + name);
    return lines.size();
}

string GetRlimit(const std::string &pid, const std::string &type, const bool soft) {
    string kind = soft ? "SOFT" : "HARD";
    string cmd = "prlimit --pid " + pid + " --" + type + " -o " + kind + " --noheading";
    vector<string> lines = Popen(cmd);
    return StringTrim(lines[0]);
}

void WaitExit(TPortoAPI &api, const std::string &pid) {
    Say() << "Waiting for " << pid << " to exit..." << std::endl;

    int times = 100;
    int p = stoi(pid);

    do {
        if (times-- <= 0)
            break;

        usleep(100000);

        kill(p, 0);
    } while (errno != ESRCH);

    if (times <= 0)
        throw std::string("Waited too long for task to exit");
}

void WaitState(TPortoAPI &api, const std::string &name, const std::string &state, int sec) {
    Say() << "Waiting for " << name << " to be in state " << state << std::endl;

    int times = sec * 10;

    std::string ret;
    do {
        if (times-- <= 0)
            break;

        usleep(100000);

        (void)api.GetData(name, "state", ret);
    } while (ret != state);

    if (times <= 0)
        throw std::string("Waited too long for task to change state");
}

void WaitPortod(TPortoAPI &api) {
    Say() << "Waiting for portod startup" << std::endl;

    int times = 10;

    std::vector<std::string> clist;
    do {
        if (times-- <= 0)
            break;

        usleep(1000000);

    } while (api.List(clist) != 0);

    if (times <= 0)
        throw std::string("Waited too long for portod startup");
}

std::string ReadLink(const std::string &path) {
    TPath lnk;

    TPath f(path);
    TError error = f.ReadLink(lnk);
    if (error)
        throw error.GetMsg();

    return lnk.ToString();
}

std::string GetCwd(const std::string &pid) {
    return ReadLink("/proc/" + pid + "/cwd");
}

std::string GetRoot(const std::string &pid) {
    return ReadLink("/proc/" + pid + "/root");
}

std::string GetNamespace(const std::string &pid, const std::string &ns) {
    return ReadLink("/proc/" + pid + "/ns/" + ns);
}

std::map<std::string, std::string> GetCgroups(const std::string &pid) {
    std::map<std::string, std::string> cgmap;
    TFile f("/proc/" + pid + "/cgroup");
    std::vector<std::string> lines;
    TError error = f.AsLines(lines);
    if (error)
        throw std::string("Can't get cgroups: " + error.GetMsg());

    std::vector<std::string> tokens;
    for (auto l : lines) {
        tokens.clear();
        error = SplitString(l, ':', tokens, 3);
        if (error)
            throw std::string("Can't get cgroups: " + error.GetMsg());
        cgmap[tokens[1]] = tokens[2];
    }

    return cgmap;
}

std::string GetStatusLine(const std::string &pid, const std::string &prefix) {
    std::vector<std::string> st;
    TFile f("/proc/" + pid + "/status");
    if (f.AsLines(st))
        throw std::string("INVALID PID");

    for (auto &s : st)
        if (s.substr(0, prefix.length()) == prefix)
            return s;

    throw std::string("INVALID PREFIX");
}

std::string GetState(const std::string &pid) {
    std::stringstream ss(GetStatusLine(pid, "State:"));

    std::string name, state, desc;

    ss>> name;
    ss>> state;
    ss>> desc;

    if (name != "State:")
        throw std::string("PARSING ERROR");

    return state;
}

uint64_t GetCap(const std::string &pid, const std::string &type) {
    std::stringstream ss(GetStatusLine(pid, type + ":"));

    std::string name, val;

    ss>> name;
    ss>> val;

    if (name != type + ":")
        throw std::string("PARSING ERROR");

    return stoull(val, nullptr, 16);
}

void GetUidGid(const std::string &pid, int &uid, int &gid) {
    std::string name;
    std::string stuid = GetStatusLine(pid, "Uid:");
    std::stringstream ssuid(stuid);

    int euid, suid, fsuid;
    ssuid >> name;
    ssuid >> uid;
    ssuid >> euid;
    ssuid >> suid;
    ssuid >> fsuid;

    if (name != "Uid:" || uid != euid || euid != suid || suid != fsuid)
        throw std::string("Invalid uid");

    std::string stgid = GetStatusLine(pid, "Gid:");
    std::stringstream ssgid(stgid);

    int egid, sgid, fsgid;
    ssgid >> name;
    ssgid >> gid;
    ssgid >> egid;
    ssgid >> sgid;
    ssgid >> fsgid;

    if (name != "Gid:" || gid != egid || egid != sgid || sgid != fsgid)
        throw std::string("Invalid gid");
}

int UserUid(const std::string &user) {
    TUser u(user);
    TError error = u.Load();
    if (error)
        throw error.GetMsg();

    return u.GetId();
}

int GroupGid(const std::string &group) {
    TGroup g(group);
    TError error = g.Load();
    if (error)
        throw error.GetMsg();

    return g.GetId();
}

std::string GetEnv(const std::string &pid) {
    std::string env;
    TFile f("/proc/" + pid + "/environ");
    if (f.AsString(env))
        throw std::string("Can't get environment");

    return env;
}

bool CgExists(const std::string &subsystem, const std::string &name) {
    TFile f(CgRoot(subsystem, name));
    return f.Exists();
}

std::string CgRoot(const std::string &subsystem, const std::string &name) {
    return "/sys/fs/cgroup/" + subsystem + "/porto/" + name + "/";
}

std::string GetFreezer(const std::string &name) {
    std::string state;
    TFile m(CgRoot("freezer", name) + "freezer.state");
    if (m.AsString(state))
        throw std::string("Can't get freezer");
    return state;
}

void SetFreezer(const std::string &name, const std::string &state) {
    TFile m(CgRoot("freezer", name) + "freezer.state");
    if (m.WriteStringNoAppend(state))
        throw std::string("Can't set freezer");

    int retries = 1000000;
    while (retries--)
        if (GetFreezer(name) == state + "\n")
            return;

    throw std::string("Can't set freezer state to ") + state;
}

std::string GetCgKnob(const std::string &subsys, const std::string &name, const std::string &knob) {
    std::string val;
    TFile m(CgRoot(subsys, name) + knob);
    if (m.AsString(val))
        throw std::string("Can't get cgroup knob " + m.GetPath().ToString());
    return StringTrim(val, "\n");
}

bool HaveCgKnob(const std::string &subsys, const std::string &knob) {
    std::string val;
    TFile m(CgRoot(subsys, "") + knob);
    return m.Exists();
}

int GetVmRss(const std::string &pid) {
    std::stringstream ss(GetStatusLine(pid, "VmRSS:"));

    std::string name, size, unit;

    ss>> name;
    ss>> size;
    ss>> unit;

    if (name != "VmRSS:")
        throw std::string("PARSING ERROR");

    return std::stoi(size);
}

bool TcClassExist(uint32_t handle) {
    size_t nr = 0;
    for (auto &link : links) {
        TNlClass tclass(link, -1, handle);
        if (tclass.Exists())
            nr++;
    }
    return nr == links.size();
}

bool TcQdiscExist(uint32_t handle) {
    size_t nr = 0;
    for (auto &link : links) {
        TNlHtb qdisc(link, -1, handle);
        if (qdisc.Exists())
            nr++;
    }
    return nr == links.size();
}

bool TcCgFilterExist(uint32_t parent, uint32_t handle) {
    size_t nr = 0;
    for (auto &link : links) {
        TNlCgFilter filter(link, parent, handle);
        if (filter.Exists())
            nr++;
    }
    return nr == links.size();
}

int WordCount(const std::string &path, const std::string &word) {
    int nr = 0;

    std::vector<std::string> lines;
    TFile log(path);
    if (log.AsLines(lines))
        throw "Can't read log " + path;

    for (auto s : lines) {
        if (s.find(word) != std::string::npos)
            nr++;
    }

    return nr;
}

bool FileExists(const std::string &path) {
    TFile f(path);
    return f.Exists();
}

void AsUser(TPortoAPI &api, TUser &user, TGroup &group) {
    AsRoot(api);

    Expect(setregid(0, group.GetId()) == 0);
    Expect(setreuid(0, user.GetId()) == 0);
}

void AsRoot(TPortoAPI &api) {
    api.Cleanup();

    Expect(seteuid(0) == 0);
    Expect(setegid(0) == 0);
}

void AsNobody(TPortoAPI &api) {
    TUser nobody(GetDefaultUser());
    TError error = nobody.Load();
    if (error)
        throw error.GetMsg();

    TGroup nogroup(GetDefaultGroup());
    error = nogroup.Load();
    if (error)
        throw error.GetMsg();

    AsUser(api, nobody, nogroup);
}

std::string GetDefaultUser() {
    std::string users[] = { "nobody" };

    for (auto &user : users) {
        TUser u(user);
        TError error = u.Load();
        if (!error)
            return u.GetName();
    }

    return "daemon";
}

std::string GetDefaultGroup() {
    std::string groups[] = { "nobody", "nogroup" };

    for (auto &group : groups) {
        TGroup g(group);
        TError error = g.Load();
        if (!error)
            return g.GetName();
    }

    return "daemon";
}

void BootstrapCommand(const std::string &cmd, const std::string &path, bool remove) {
    TFolder d(path);
    if (remove)
        (void)d.Remove(true);

    vector<string> lines = Popen("ldd " + cmd);

    for (auto &line : lines) {
        vector<string> tokens;
        TError error = SplitString(line, ' ', tokens);
        if (error)
            throw error.GetMsg();

        TPath from;
        string name;
        if (tokens.size() == 2) {
            from = StringTrim(tokens[0]);
            TPath p(tokens[0]);
            name = p.BaseName();
        } else if (tokens.size() == 4) {
            if (tokens[2] == "")
                continue;

            from = StringTrim(tokens[2]);
            name = StringTrim(tokens[0]);
        } else {
            continue;
        }

        TFolder dest(path + "/" + from.DirName().ToString());
        if (!dest.Exists()) {
            error = dest.Create(0755, true);
            if (error)
                throw error.GetMsg();
        }

        Expect(system(("cp " + from.ToString() + " " + dest.GetPath().ToString() + "/" + name).c_str()) == 0);
    }
    Expect(system(("cp " + cmd + " " + path).c_str()) == 0);
}

void RestartDaemon(TPortoAPI &api) {
    std::cerr << ">>> Truncating logs and restarting porto..." << std::endl;

    if (Pgrep("portod") != 1)
        throw string("Porto is not running");

    if (Pgrep("portod-slave") != 1)
        throw string("Porto slave is not running");

    // Remove porto cgroup to clear statistics
    int pid = ReadPid(config().slave_pid().path());
    if (kill(pid, SIGINT))
        throw string("Can't send SIGINT to slave");

    WaitPortod(api);

    // Truncate slave log
    TFile slaveLog(config().slave_log().path());
    (void)slaveLog.Remove();

    pid = ReadPid(config().slave_pid().path());
    if (kill(pid, SIGUSR1))
        throw string("Can't send SIGUSR1 to slave");

    WaitPortod(api);

    // Truncate master log
    TFile masterLog(config().master_log().path());
    (void)masterLog.Remove();

    pid = ReadPid(config().master_pid().path());
    if (kill(pid, SIGUSR1))
        throw string("Can't send SIGUSR1 to master");

    WaitPortod(api);

    // Clean statistics
    pid = ReadPid(config().master_pid().path());
    if (kill(pid, SIGHUP))
        throw string("Can't send SIGHUP to master");

    WaitPortod(api);
}

static void PrintFds(const std::string &path, struct dirent **lst, int nr) {
    for (int i = 0; i < nr; i++) {
        if (lst[i]->d_name == string(".") ||
            lst[i]->d_name == string("..")) {
            Say() << "[" << i << "] " << lst[i]->d_name << std::endl;
        } else {
            Say() << "[" << i << "] " << lst[i]->d_name
                << " -> " << ReadLink(path + "/" + lst[i]->d_name) << std::endl;
        }
    }
}

bool NetworkEnabled() {
    return links.size() != 0;
}

void TestDaemon(TPortoAPI &api) {
    struct dirent **lst;
    int pid;

    AsRoot(api);

    api.Cleanup();
    sleep(1);

    Say() << "Make sure portod-slave doesn't have zombies" << std::endl;
    pid = ReadPid(config().slave_pid().path());

    Say() << "Make sure portod-slave doesn't have invalid FDs" << std::endl;

    std::string path = ("/proc/" + std::to_string(pid) + "/fd");

    // when sssd is running getgrnam opens unix socket to read database
    int sssFd = 0;
    if (WordCount("/etc/nsswitch.conf", "sss"))
        sssFd = 2;

    int nl = NetworkEnabled() ? 1 : 0;

    // . .. 0(stdin) 1(stdout) 2(stderr) 3(log) 4(rpc socket) 5(epoll) 6(netlink socket) 128(event pipe) 129(ack pipe)
    int nr = scandir(path.c_str(), &lst, NULL, alphasort);
    PrintFds(path, lst, nr);
    Expect(nr >= 2 + 8 + nl && nr <= 2 + 8 + nl + sssFd);

    Say() << "Make sure portod-master doesn't have zombies" << std::endl;
    pid = ReadPid(config().master_pid().path());

    Say() << "Make sure portod-master doesn't have invalid FDs" << std::endl;
    Say() << "Number of portod-master fds=" << nr << std::endl;
    path = ("/proc/" + std::to_string(pid) + "/fd");

    // . .. 0(stdin) 1(stdout) 2(stderr) 3(log) 5(event pipe) 6(ack pipe)
    nr = scandir(path.c_str(), &lst, NULL, alphasort);
    PrintFds(path, lst, nr);
    Expect(nr == 2 + 6);

    Say() << "Check portod-master queue size" << std::endl;
    std::string v;
    ExpectSuccess(api.GetData("/", "porto_stat[queued_statuses]", v));
    Expect(v == std::to_string(0));

    Say() << "Check portod-slave queue size" << std::endl;
    ExpectSuccess(api.GetData("/", "porto_stat[queued_events]", v));
    Expect(v == std::to_string(1)); // RotateLogs

    // TODO: check rtnl classes
}

bool IsCfqActive() {
    TFolder f("/sys/block");
    std::vector<std::string> items;
    (void)f.Items(EFileType::Any, items);
    for (auto d : items) {
        if ( (d.find(std::string("loop")) != std::string::npos) || (d.find(std::string("ram")) != std::string::npos) )
            continue;
        TFile f(std::string("/sys/block/" + d + "/queue/scheduler"));
        std::string data;
        std::vector<std::string> tokens;

        TError error = f.AsString(data);
        if (error)
            throw error.GetMsg();
        error = SplitString(data, ' ', tokens);
        if (error)
            throw error.GetMsg();
        bool cfqEnabled = false;
        for (auto t : tokens) {
            if (t == std::string("[cfq]"))
                cfqEnabled = true;
        }
        if (!cfqEnabled) {
            return false;
        }
    }
    return true;
}
}
