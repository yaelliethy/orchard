#!/usr/bin/env python3
"""Talk to macOS's own guest agent from the host.

macOS ships /usr/libexec/AppleQEMUGuestAgent; launchd starts it when the guest
has a virtio-serial port named org.qemu.guest_agent.0, which run-vm.sh adds and
connects to $ROOT/qga.sock. It speaks the QEMU guest agent protocol and runs
commands as root, so this needs neither ssh nor the guest's password.

  scripts/guest.py wait [SECONDS]    wait until the agent answers (default 900)
  scripts/guest.py exec CMD [ARG..]  run CMD as root in the guest, print output
  scripts/guest.py settime           set the guest clock from the host clock
  scripts/guest.py prepare           retire tart-guest-agent (see TECHNICAL.md):
                                     once its disk resize has finished, stop and
                                     move aside its LaunchDaemon and LaunchAgent;
                                     harmless when already done
  scripts/guest.py shutdown [--reboot]

QEMU's socket takes one client at a time; each request here holds it only for
its own round trip, and guest.py processes take turns (qga.sock.lock).
QGA_SOCK overrides the socket path.

Copyright (c) 2026 awaiiro
SPDX-License-Identifier: GPL-2.0-or-later
"""

import base64
import fcntl
import json
import os
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOCK = os.environ.get("QGA_SOCK", os.path.join(ROOT, "qga.sock"))

PREPARE = r"""
d=/Library/LaunchDaemons/org.cirruslabs.tart-guest-daemon.plist
a=/Library/LaunchAgents/org.cirruslabs.tart-guest-agent.plist
if [ ! -e "$d" ] && [ ! -e "$a" ]; then echo "no tart-guest-agent jobs"; exit 0; fi
mkdir -p /Library/LaunchDaemons.disabled /Library/LaunchAgents.disabled
# The LaunchAgent does no disk work: stop it at once.
if [ -e "$a" ]; then
  for u in $(ps -axo uid=,comm= | awk '$2 ~ /loginwindow$/ {print $1}' | sort -u); do
    launchctl bootout "gui/$u/org.cirruslabs.tart-guest-agent" 2>/dev/null
  done
  mv "$a" /Library/LaunchAgents.disabled/
fi
# The daemon repairs and grows the APFS container on its first run. Stop it
# only after its log shows how that run ended, and while no repair or resize
# is running. launchd loads /Library/LaunchDaemons a little after this agent
# answers, so "not loaded" early in the boot means "not yet": moving the plist
# then would skip the resize. Not loaded 10 min into the boot means never.
if [ -e "$d" ]; then
  i=0
  while :; do
    case "$(tail -n 1 /tmp/tart-guest-daemon.log 2>/dev/null)" in
      *"running on Tart"*|*"attempting to resize"*)
        # a run is in progress: never cut it short
        if [ $i -ge 360 ]; then
          echo "tart-guest-daemon is still resizing after 30 min; run prepare again later" >&2
          exit 1
        fi ;;
      "")
        boot=$(sysctl -n kern.boottime | sed 's/^{ sec = \([0-9]*\),.*/\1/')
        if [ $(($(date +%s) - boot)) -ge 600 ] &&
           ! launchctl print system/org.cirruslabs.tart-guest-daemon >/dev/null 2>&1; then
          break
        fi
        # loaded, but no log after 30 min: it is not running either
        [ $i -lt 360 ] || break ;;
      *)
        pgrep -q -f 'diskutil (repairDisk|apfs resizeContainer)' || break ;;
    esac
    i=$((i + 1)); sleep 5
  done
  launchctl bootout system/org.cirruslabs.tart-guest-daemon 2>/dev/null
  mv "$d" /Library/LaunchDaemons.disabled/
fi
echo "tart-guest-agent jobs stopped and moved to /Library/Launch*.disabled"
"""


class AgentError(Exception):
    pass


class NotAnswering(AgentError):
    pass


class Agent:
    """One connection per request: QEMU serves a single client at a time, so
    nothing here holds the socket longer than one round trip."""

    def __init__(self, path):
        self.path = path

    def _open(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        try:
            sock.connect(self.path)
        except OSError:
            sock.close()
            raise
        return sock, sock.makefile("rwb")

    @staticmethod
    def _close(sock, f):
        for obj in (f, sock):
            try:
                obj.close()
            except OSError:
                pass

    @staticmethod
    def _send(f, cmd, args=None):
        msg = {"execute": cmd}
        if args is not None:
            msg["arguments"] = args
        f.write(json.dumps(msg).encode() + b"\n")
        f.flush()

    @staticmethod
    def _read(sock, f, timeout):
        sock.settimeout(timeout)
        line = f.readline()
        if not line:
            raise EOFError("guest agent closed the connection")
        return json.loads(line)

    def _synced(self, deadline):
        """A connection on which the agent has just answered a guest-sync.
        Replies to requests that timed out earlier can still be queued; reading
        up to our own sync id drops them."""
        while time.time() < deadline:
            sock = f = None
            try:
                sock, f = self._open()
                token = int(time.time() * 1000) % 2**31
                self._send(f, "guest-sync", {"id": token})
                while self._read(sock, f, 15).get("return") != token:
                    pass
                return sock, f
            except (OSError, EOFError, ValueError):
                if sock:
                    self._close(sock, f)
                time.sleep(2)
        raise NotAnswering("guest agent not answering on %s (another guest.py, e.g. "
                         "boot-robust.sh's, may be using it; see guest.log)" % self.path)

    def call(self, cmd, args=None, wait=120):
        """Send one request and return its reply. args may be a callable,
        evaluated after the sync (for values that must be fresh)."""
        sock, f = self._synced(time.time() + wait)
        try:
            self._send(f, cmd, args() if callable(args) else args)
            reply = self._read(sock, f, wait)
        finally:
            self._close(sock, f)
        if "error" in reply:
            raise AgentError("%s: %s" % (cmd, reply["error"].get("desc", reply["error"])))
        return reply.get("return")

    def notify(self, cmd, args=None, wait=120):
        """For a request that does not answer on success (guest-shutdown):
        silence means it was accepted, an error reply means it was not."""
        sock, f = self._synced(time.time() + wait)
        try:
            self._send(f, cmd, args)
            try:
                reply = self._read(sock, f, 3)
            except (OSError, EOFError, ValueError):
                return
        finally:
            self._close(sock, f)
        if "error" in reply:
            raise AgentError("%s: %s" % (cmd, reply["error"].get("desc", reply["error"])))

    def run(self, argv, timeout=3600):
        """Run argv as root in the guest, copy its output, return its status.
        guest-exec itself is never retried, so a command cannot run twice."""
        pid = self.call("guest-exec", {"path": argv[0], "arg": argv[1:],
                                       "capture-output": True})["pid"]
        end = time.time() + timeout
        retried = False
        while time.time() < end:
            try:
                st = self.call("guest-exec-status", {"pid": pid})
            except NotAnswering:
                retried = True  # a loaded guest can be slow; ask again
                continue
            except AgentError as e:
                if retried and "pid" in str(e):
                    # The reply that reported the exit was lost, and the agent
                    # has already forgotten the process.
                    print("guest.py: %s finished, but its result was lost" % argv[0],
                          file=sys.stderr)
                    return 3
                raise
            except (OSError, EOFError, ValueError):
                retried = True  # a loaded guest can be slow; ask again
                continue
            if st.get("exited"):
                for key, stream in (("out-data", sys.stdout), ("err-data", sys.stderr)):
                    if st.get(key):
                        stream.write(base64.b64decode(st[key]).decode(errors="replace"))
                        stream.flush()
                if "exitcode" in st:
                    return st["exitcode"]
                if "signal" in st:
                    print("guest.py: %s killed by signal %d" % (argv[0], st["signal"]),
                          file=sys.stderr)
                    return 128 + st["signal"]
                return 1
            time.sleep(1)
        raise AgentError("timed out waiting for %s" % argv[0])


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        sys.exit(__doc__)
    what, rest = args[0], args[1:]
    # One guest.py at a time, so that e.g. a shutdown waits for boot-robust.sh's
    # prepare instead of cutting into the disk resize it is waiting on.
    lock = open(SOCK + ".lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("guest.py: waiting for another guest.py to finish (boot-robust.sh's "
              "setup? see guest.log)", file=sys.stderr)
        fcntl.flock(lock, fcntl.LOCK_EX)
    agent = Agent(SOCK)
    if what == "wait" and len(rest) <= 1:
        sock, f = agent._synced(time.time() + (float(rest[0]) if rest else 900))
        agent._close(sock, f)
        print("guest agent is up")
    elif what == "exec" and rest:
        sys.exit(agent.run(rest))
    elif what == "settime" and not rest:
        agent.call("guest-set-time", lambda: {"time": time.time_ns()})
        sys.exit(agent.run(["/bin/date", "-u"]))
    elif what == "prepare" and not rest:
        sys.exit(agent.run(["/bin/sh", "-c", PREPARE]))
    elif what == "shutdown" and rest in ([], ["--reboot"]):
        mode = "reboot" if rest else "powerdown"
        agent.notify("guest-shutdown", {"mode": mode})
        print("requested", mode)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        main()
    except (AgentError, OSError, EOFError, ValueError) as e:
        sys.exit("guest.py: %s" % e)
