#pragma once

#include "common.hpp"

#include <functional>
#include <vector>
#include <set>

#include "util/signal.hpp"
#include "util/path.hpp"

extern "C" {
#include <sys/resource.h>
}

class TPath;

struct TTask {
    pid_t Pid = 0;
    int Status = 0;
    bool Running = false;

    TError Fork(bool detach = false);
    TError Wait();
    static bool Deliver(pid_t pid, int status);

    bool Exists() const;
    bool IsZombie() const;
    pid_t GetPPid() const;
    TError Kill(int signal) const;
    TError KillPg(int signal) const;
};

TString FormatTime(time_t t, const char *fmt = "%F %T");
void LocalTime(const time_t *time, struct tm &tm);

pid_t GetPid();
pid_t GetPPid();
pid_t GetTid();
TError GetTaskChildrens(pid_t pid, std::vector<pid_t> &childrens);

uint64_t GetCurrentTimeMs();
bool WaitDeadline(uint64_t deadline, uint64_t sleep = 10);
uint64_t GetTotalMemory();
uint64_t GetHugetlbMemory();
void SetProcessName(const TString &name);
void SetDieOnParentExit(int sig);
TString GetTaskName(pid_t pid = 0);
uint64_t TaskHandledSignals(pid_t pid);
TError GetTaskCgroups(const int pid, std::map<TString, TString> &cgmap);
TString GetHostName();
TError SetHostName(const TString &name);
TError GetSysctl(const TString &name, TString &value);
TError SetSysctl(const TString &name, const TString &value);
TError SetSysctlAt(const TFile &proc_sys, const TString &name, const TString &value);

TError SetOomScoreAdj(int value);

TError TranslatePid(pid_t pid, pid_t pidns, pid_t &result);

TString FormatExitStatus(int status);
int GetNumCores();

int SetIoPrio(pid_t pid, int ioprio);

class TUnixSocket : public TPortoNonCopyable {
    int SockFd;
public:
    static TError SocketPair(TUnixSocket &sock1, TUnixSocket &sock2);
    TUnixSocket(int sock = -1) : SockFd(sock) {};
    ~TUnixSocket() { Close(); };
    void operator=(int sock);
    TUnixSocket& operator=(TUnixSocket&& Sock);
    void Close();
    int GetFd() const { return SockFd; }
    TError SendInt(int val) const;
    TError RecvInt(int &val) const;
    TError SendZero() const { return SendInt(0); }
    TError RecvZero() const { int zero; return RecvInt(zero); }
    TError SendPid(pid_t pid) const;
    TError RecvPid(pid_t &pid, pid_t &vpid) const;
    TError SendError(const TError &error) const;
    TError RecvError() const;
    TError SendFd(int fd) const;
    TError RecvFd(int &fd) const;
    TError SetRecvTimeout(int timeout_ms) const;
};

class TPidFile {
public:
    TPath Path;
    TString Name;
    TString AltName;
    pid_t Pid = 0;

    TPidFile(const TString &path, const TString &name, const TString &altname): Path(path), Name(name), AltName(altname) { }
    TError Read();
    bool Running();
    TError Save(pid_t pid);
    TError Remove();
};
