"""Plugin: solver_bg  (background solver manager)
Run foamRun detached (nohup+setsid) so it keeps going after the tool returns,
then poll status / stop later without blocking the agent. Lets a user check in
occasionally on a multi-day DDES run.

action=start : write a runner .sh (sources OF env + mpirun foamRun -parallel),
               launch detached, save PID to case/.bgsolver.pid, return immediately.
action=status: parse log.foamRun for latest Time/deltaT/Courant + whether PID alive.
action=stop  : kill the saved PID (solver restarts from latestTime next start).

NOTE: plugin handlers run in a confined sandbox; whether a detached child
survives the handler return is verified empirically via status. If it does not
survive, use the core run_solver in time-boxed segments instead.
"""
import os
import re
import signal
import subprocess

SPEC = {
    "name": "solver_bg",
    "description": (
        "Background solver manager for long runs: action=start launches foamRun "
        "detached (nohup+setsid, MPI) and returns immediately; action=status "
        "polls log.foamRun (latest time, deltaT, Courant, PID alive) without "
        "blocking; action=stop kills it (restarts from latestTime). Lets a user "
        "check in occasionally on a multi-day DDES run. [approval required]"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "case_dir": {"type": "string"},
            "action": {"enum": ["start", "status", "stop"], "type": "string"},
            "nprocs": {"type": "integer", "description": "MPI ranks (start; default 14)"},
            "of_bashrc": {
                "type": "string",
                "description": "OpenFOAM env script (default /opt/openfoam12/etc/bashrc)",
            },
        },
        "required": ["case_dir", "action"],
    },
}

GATED = {"solver_bg"}

PIDFILE = ".bgsolver.pid"


def _pidpath(case_dir):
    return os.path.join(case_dir, PIDFILE)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_pid(case_dir):
    p = _pidpath(case_dir)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return int(f.read().strip())
    except ValueError:
        return None


def _tail(path, n=4000):
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        try:
            f.seek(-n, 2)
        except OSError:
            f.seek(0)
        return f.read().decode("utf-8", "replace")


def _parse_progress(logpath):
    t = _tail(logpath, 8000)
    res = {}
    times = re.findall(r"\nTime = ([\d.eE+-]+)s", t)
    if times:
        res["latest_time"] = times[-1]
    dts = re.findall(r"deltaT = ([\d.eE+-]+)", t)
    if dts:
        res["deltaT"] = dts[-1]
    cos = re.findall(r"Courant Number mean: ([\d.eE+-]+) max: ([\d.eE+-]+)", t)
    if cos:
        res["Co_mean"], res["Co_max"] = cos[-1]
    if "FOAM FATAL" in t:
        res["fatal"] = True
    if "End\n" in t and "Finalising parallel run" in t:
        res["finished"] = True
    return res


def handler(**args):
    case_dir = args["case_dir"]
    action = args["action"]
    if not os.path.isdir(case_dir):
        return {"ok": False, "error": "case_dir not found"}
    logpath = os.path.join(case_dir, "log.foamRun")

    if action == "status":
        pid = _read_pid(case_dir)
        prog = _parse_progress(logpath)
        return {
            "ok": True,
            "action": "status",
            "pid": pid,
            "alive": _alive(pid) if pid else False,
            "progress": prog,
            "has_log": os.path.exists(logpath),
        }

    if action == "stop":
        pid = _read_pid(case_dir)
        if not pid:
            return {"ok": False, "error": "no PID file (%s)" % PIDFILE}
        if not _alive(pid):
            return {"ok": True, "action": "stop", "note": "already stopped", "pid": pid}
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception as e:
                return {"ok": False, "error": "kill failed: %s" % e}
        return {"ok": True, "action": "stop", "pid": pid, "note": "SIGTERM sent"}

    # action == start
    nprocs = int(args.get("nprocs", 14))
    of_bashrc = args.get("of_bashrc", "/opt/openfoam12/etc/bashrc")
    runner = os.path.join(case_dir, "run_bg.sh")
    with open(runner, "w") as f:
        f.write("#!/bin/bash\n")
        f.write("cd %s\n" % case_dir)
        f.write("source %s\n" % of_bashrc)
        f.write("decomposePar -force > log.decomposePar 2>&1\n")
        f.write(
            "mpirun -np %d foamRun -parallel > log.foamRun 2>&1\n" % nprocs
        )
        f.write("reconstructPar -newTimes >> log.reconstruct 2>&1\n")
    os.chmod(runner, 0o755)

    # launch detached: setsid + nohup, fully backgrounded
    try:
        proc = subprocess.Popen(
            ["setsid", "nohup", "bash", runner],
            cwd=case_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        return {"ok": False, "error": "launch failed: %s: %s" % (type(e).__name__, e)}

    with open(_pidpath(case_dir), "w") as f:
        f.write(str(proc.pid))

    return {
        "ok": True,
        "action": "start",
        "pid": proc.pid,
        "runner": runner,
        "nprocs": nprocs,
        "note": (
            "launched detached; call action=status to verify it survived the "
            "handler return (sandbox may reap children)."
        ),
    }
