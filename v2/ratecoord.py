"""Cross-process rate-limit coordinator (Phase 15 P2). PAPER / public reads only.

v2.ratelimit keeps one budget per venue PER PROCESS; two tools running at once could
each spend the venue's whole limit. This module shares one budget per SCOPE (venue,
endpoint class, IP scope) between processes on the same machine, through small state
files under a control folder on C: (never D:/E:).

Design
  lock          an OS advisory lock on <scope>.lock (msvcrt.locking on Windows,
                fcntl.flock on POSIX). The kernel releases it when the holder dies, so a
                crashed process can never keep a scope locked; acquisition has a timeout
                (LockTimeout), nobody waits forever.
  state         stored INSIDE <scope>.lock after byte 16 (byte 0 carries the lock), as
                magic + length + SHA-256 + JSON, written through the already open lock
                handle (no new file per request: creating files cost ~5 ms each on
                Windows). A torn or corrupt record fails the checksum and is reset
                CONSERVATIVELY (no tokens; wait one refill) and counted.
  bucket        token bucket: capacity = limit, refill = limit / window per second
                (wall clock; a clock step backwards adds nothing, a jump forward refills
                at most to capacity).
  priority      HIGH may take any token; LOW only while at least `reserve` tokens stay
                for HIGH (order books / current funding before metadata / backfill).
  in-flight     at most `max_in_flight` leases per scope; a lease of a dead process or
                older than `lease_s` is reaped (crash recovery).
  Retry-After   on_429(retry_after) pauses the scope for every process.
  backoff       jittered exponential (failures), capped.
  breaker       CLOSED -> OPEN after `trip_after` consecutive failures (429 / 5xx /
                timeout) for a jittered, growing period -> HALF_OPEN (one probe) ->
                CLOSED on success, OPEN again on failure.
  cache         cross-process single-flight shared answers (one fetch per key at a
                time; others wait for the key lock and read the answer), bounded entries.

What it cannot do: other clients behind the same public IP (another program, another
machine on the same NAT) spend the same venue quota invisibly. That share is UNKNOWN
and the Doctor says so; a 429 is still possible and is handled, never assumed away.

Switch: NB_V2_SHARED_BUDGET=1 enables it for v2.venues.base.http_json (default off: the
per-process v2.ratelimit budget is used, unchanged).
"""
import contextlib
import hashlib
import json
import os
import random
import threading
import time
from pathlib import Path

ENV = "NB_V2_SHARED_BUDGET"
HIGH, LOW = "HIGH", "LOW"
CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"


class LockTimeout(RuntimeError):
    pass


class CircuitOpen(RuntimeError):
    pass


class BudgetTimeout(RuntimeError):
    pass


def enabled(environ=None):
    env = os.environ if environ is None else environ
    raw = (env.get(ENV) or "").strip()
    if raw in ("", "0"):
        return False
    if raw == "1":
        return True
    raise ValueError("%s must be 0 or 1, not %r" % (ENV, raw))


def pid_alive(pid):
    """True if a process with this pid exists (best effort, both platforms)."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == 259     # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class FileLock:
    """Exclusive OS advisory lock on a file; released by the kernel if the holder dies."""

    def __init__(self, path, timeout=5.0, poll=0.005, sleep=time.sleep, fd=None):
        self.path, self.timeout, self.poll, self.sleep = Path(path), timeout, poll, sleep
        self.fd, self.own = fd, fd is None      # a caller-owned fd stays open after use

    def _try(self):
        if os.name == "nt":
            import msvcrt
            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __enter__(self):
        if self.own:
            self.fd = open_fd(self.path)
        end = time.monotonic() + self.timeout
        while True:
            try:
                self._try()
                return self
            except OSError:
                if time.monotonic() >= end:
                    if self.own:
                        os.close(self.fd)
                        self.fd = None
                    raise LockTimeout("lock %s busy for %.1f s" % (self.path.name,
                                                                   self.timeout))
                self.sleep(self.poll)

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            if self.own:
                os.close(self.fd)
                self.fd = None


def open_fd(path):
    """Read/write, created if absent, BINARY (Windows text mode would turn bytes 0x0A
    into CR LF and stop reading at 0x1A)."""
    return os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)


def atomic_write(path, obj, tries=20, sleep=time.sleep):
    tmp = Path(str(path) + ".%d.tmp" % os.getpid())
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    for i in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:          # Windows: a reader holds the target open
            sleep(0.005 * (i + 1))
    tmp.unlink(missing_ok=True)
    raise PermissionError("could not replace %s" % path)


STATE_OFFSET = 16
MAGIC = b"NBRC"


def encode_state(st):
    payload = json.dumps(st).encode()
    return MAGIC + len(payload).to_bytes(4, "big") + hashlib.sha256(payload).digest() + \
        payload


def decode_state(blob):
    """dict, None for an empty record (never written), ValueError if corrupt."""
    if not blob:
        return None
    if len(blob) < 40 or blob[:4] != MAGIC:
        raise ValueError("bad magic")
    n = int.from_bytes(blob[4:8], "big")
    payload = blob[40:40 + n]
    if len(payload) != n or hashlib.sha256(payload).digest() != blob[8:40]:
        raise ValueError("checksum")
    st = json.loads(payload)
    if not (isinstance(st, dict) and "tokens" in st and "counters" in st):
        raise ValueError("shape")
    return st


def read_fd(fd):
    os.lseek(fd, STATE_OFFSET, os.SEEK_SET)
    chunks = []
    while True:
        b = os.read(fd, 65536)
        if not b:
            break
        chunks.append(b)
    return b"".join(chunks)


def write_fd(fd, st):
    blob = encode_state(st)
    os.lseek(fd, STATE_OFFSET, os.SEEK_SET)
    os.write(fd, blob)
    os.ftruncate(fd, STATE_OFFSET + len(blob))


def read_path(path, tries=5, sleep=time.sleep):
    """The state of a scope WITHOUT the lock (Doctor); a torn read is retried."""
    last = None
    for i in range(tries):
        try:
            with open(path, "rb") as f:
                f.seek(STATE_OFFSET)
                return decode_state(f.read())
        except (ValueError, OSError) as ex:
            last = ex
            sleep(0.01 * (i + 1))
    raise ValueError("unreadable: %s" % last)


def backoff_s(failures, base=1.0, cap=60.0, rand=random.random):
    """Jittered exponential delay for `failures` consecutive failures (0 -> 0)."""
    if failures <= 0:
        return 0.0
    return min(cap, base * 2 ** (failures - 1)) * (0.5 + rand() / 2)


def scope_id(exchange_id, endpoint="default", ip_scope="local"):
    raw = "%s.%s.%s" % (exchange_id, endpoint, ip_scope)
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in raw)


class Coordinator:
    """One instance per process; the state is shared through `folder`."""

    def __init__(self, folder, limits, clock=time.time, sleep=time.sleep,
                 rand=random.random, lock_timeout=5.0, trip_after=5, open_base_s=5.0,
                 open_cap_s=300.0, lease_s=120.0, max_in_flight=4, reserve_frac=0.2):
        from v2 import storage
        self.folder = storage.check_dir(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.limits = dict(limits)            # scope prefix (exchange_id) -> (n, window s)
        self.clock, self.sleep, self.rand = clock, sleep, rand
        self.lock_timeout, self.trip_after = lock_timeout, trip_after
        self.open_base_s, self.open_cap_s = open_base_s, open_cap_s
        self.lease_s, self.max_in_flight, self.reserve_frac = lease_s, max_in_flight, \
            reserve_frac
        # one open handle per scope for the life of the process (re-opening a modified
        # file costs ~10 ms on Windows) and one thread lock per scope (POSIX flock does
        # not separate threads that share a handle)
        self._fds, self._tlocks, self._guard = {}, {}, threading.Lock()

    # -- state -------------------------------------------------------------------------
    def _paths(self, scope):
        return self.folder / (scope + ".lock")

    def _limit(self, scope):
        xid = scope.split(".", 1)[0]
        if xid not in self.limits:
            raise KeyError("no budget for %s" % xid)
        return self.limits[xid]

    def _fresh(self, now, n):
        return dict(tokens=float(n), updated=now, pause_until=0.0, failures=0,
                    breaker=CLOSED, open_until=0.0, opens=0, probe=None, leases={},
                    counters=dict(granted=0, waited_s=0.0, http_429=0, failures=0,
                                  breaker_refusals=0, state_resets=0, leases_reaped=0,
                                  low_priority_waits=0, lock_timeouts=0))

    def _load(self, scope, now, fd):
        n, _ = self._limit(scope)
        try:
            st = decode_state(read_fd(fd))
        except Exception:
            st = False
        if st is None:
            return self._fresh(now, n)
        if st is False:
            # torn / corrupt record: start CONSERVATIVELY with no tokens
            st = self._fresh(now, n)
            st["tokens"] = 0.0
            st["counters"]["state_resets"] = 1
        return st

    def _handle(self, scope):
        with self._guard:
            if scope not in self._fds:
                self._fds[scope] = open_fd(self._paths(scope))
                self._tlocks[scope] = threading.Lock()
            return self._fds[scope], self._tlocks[scope]

    def close(self):
        with self._guard:
            for fd in self._fds.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._fds.clear()

    @contextlib.contextmanager
    def _locked(self, scope):
        fd, tl = self._handle(scope)
        if not tl.acquire(timeout=self.lock_timeout):
            raise LockTimeout("thread lock %s busy" % scope)
        try:
            with self._locked_fd(scope, fd) as v:
                yield v
        finally:
            tl.release()

    @contextlib.contextmanager
    def _locked_fd(self, scope, fd):
        with FileLock(self._paths(scope), timeout=self.lock_timeout, sleep=self.sleep,
                      fd=fd) as lk:
            now = self.clock()
            st = self._load(scope, now, lk.fd)
            self._refill(scope, st, now)
            self._reap(st, now)
            try:
                yield st, now
            finally:
                write_fd(lk.fd, st)          # counters survive a CircuitOpen too

    def _refill(self, scope, st, now):
        n, window = self._limit(scope)
        # A clock step back adds nothing AND re-anchors at `now`, so refilling resumes
        # at once (keeping the future anchor would stall the budget until the clock
        # caught up). A jump forward refills at most to capacity.
        dt = max(0.0, now - float(st.get("updated", now)))
        st["tokens"] = min(float(n), float(st["tokens"]) + dt * n / window)
        st["updated"] = now

    def _reap(self, st, now):
        for lid, l in list(st["leases"].items()):
            if now - l["started"] > self.lease_s or not pid_alive(l["pid"]):
                st["leases"].pop(lid)
                st["counters"]["leases_reaped"] += 1
                if st.get("probe") == lid:   # a crashed probe frees the half-open slot
                    st["probe"] = None

    # -- public ------------------------------------------------------------------------
    def acquire(self, scope, priority=HIGH, cost=1.0, timeout=60.0):
        """Take `cost` tokens and an in-flight lease; returns the lease id. Raises
        CircuitOpen (breaker open), BudgetTimeout (no budget within `timeout`)."""
        n, _ = self._limit(scope)
        reserve = 0.0 if priority == HIGH else self.reserve_frac * n
        start = self.clock()
        while True:
            try:
                with self._locked(scope) as (st, now):
                    wait = self._decide(scope, st, now, cost, reserve, priority)
                    if wait == 0.0:
                        lid = "%d-%d-%.6f" % (os.getpid(), st["counters"]["granted"], now)
                        st["leases"][lid] = dict(pid=os.getpid(), started=now)
                        if st["breaker"] == HALF_OPEN:
                            st["probe"] = lid     # the single half-open probe
                        st["tokens"] -= cost
                        st["counters"]["granted"] += 1
                        st["counters"]["waited_s"] += now - start
                        return lid
            except LockTimeout:
                wait = 0.05
                self._count_lock_timeout(scope)
            if self.clock() - start + wait > timeout:
                raise BudgetTimeout("%s: no budget within %.1f s" % (scope, timeout))
            self.sleep(wait)

    def _decide(self, scope, st, now, cost, reserve, priority):
        if st["breaker"] == OPEN:
            if now < st["open_until"]:
                st["counters"]["breaker_refusals"] += 1
                raise CircuitOpen("circuit open until %.0f" % st["open_until"])
            st["breaker"], st["probe"] = HALF_OPEN, None
        if st["breaker"] == HALF_OPEN and st.get("probe"):
            return 0.25                          # one probe at a time
        if now < st["pause_until"]:
            return st["pause_until"] - now
        if len(st["leases"]) >= self.max_in_flight:
            return 0.05
        if st["tokens"] - cost >= reserve:
            return 0.0
        if priority == LOW and st["tokens"] >= cost:
            st["counters"]["low_priority_waits"] += 1
        n, window = self._limit(scope)
        need = cost + reserve - st["tokens"]
        return max(0.005, need * window / n)

    def release(self, scope, lease, ok=True, status=None, retry_after=None):
        """End a request. ok=False with status 429 / 5xx / 'timeout' counts a failure;
        429 pauses the scope for Retry-After (or a jittered backoff)."""
        with self._locked(scope) as (st, now):
            st["leases"].pop(lease, None)
            if ok:
                st["failures"] = 0
                if st["breaker"] == HALF_OPEN:
                    st["breaker"], st["opens"], st["probe"] = CLOSED, 0, None
                return
            st["failures"] += 1
            st["counters"]["failures"] += 1
            if status == 429:
                st["counters"]["http_429"] += 1
                ra = retry_after if retry_after is not None else backoff_s(
                    st["failures"], rand=self.rand)
                st["pause_until"] = max(st["pause_until"], now + float(ra))
            if st["breaker"] == HALF_OPEN or st["failures"] >= self.trip_after:
                st["opens"] += 1
                st["breaker"], st["probe"] = OPEN, None
                st["open_until"] = now + min(self.open_cap_s, self.open_base_s * 2 ** (
                    st["opens"] - 1)) * (0.5 + self.rand() / 2)

    def _count_lock_timeout(self, scope):
        try:
            with self._locked(scope) as (st, now):
                st["counters"]["lock_timeouts"] += 1
        except LockTimeout:
            pass

    @contextlib.contextmanager
    def request(self, scope, priority=HIGH, timeout=60.0):
        """with coord.request(scope) as r: ...; r['status']=429 / r['retry_after']=..
        on failure. An exception inside counts as a failure (status 'error')."""
        lease = self.acquire(scope, priority=priority, timeout=timeout)
        r = dict(ok=True, status=None, retry_after=None)
        try:
            yield r
        except Exception:
            r.update(ok=False, status=r.get("status") or "error")
            raise
        finally:
            self.release(scope, lease, ok=r["ok"], status=r["status"],
                         retry_after=r["retry_after"])

    def snapshot(self):
        """Per-scope telemetry for the Doctor (read without the lock; best effort)."""
        out = {}
        for sp in sorted(self.folder.glob("*.lock")):
            try:
                st = read_path(sp)
            except Exception:
                out[sp.stem] = dict(status="UNREADABLE")
                continue
            if st is None:
                continue
            n, w = self.limits.get(sp.stem.split(".", 1)[0], (None, None))
            out[sp.stem] = dict(tokens=st.get("tokens"), limit=n, window_s=w,
                                breaker=st.get("breaker"), open_until=st.get("open_until"),
                                pause_until=st.get("pause_until"),
                                in_flight=len(st.get("leases") or {}),
                                counters=st.get("counters"),
                                external_ip_share="UNKNOWN: other clients on the same "
                                "public IP spend this quota invisibly")
        return out


# -- cross-process single-flight shared cache -----------------------------------------
class SharedFileCache:
    """Answers shared between processes for `ttl` s. One fetch per key at a time: the
    first process takes the key lock and fetches; others wait for that lock (timeout)
    and then read the stored answer. Bounded: at most `max_entries` files (oldest
    removed). Values must be JSON."""

    def __init__(self, folder, ttl, clock=time.time, max_entries=256, lock_timeout=30.0):
        from v2 import storage
        self.folder = storage.check_dir(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.ttl, self.clock, self.max_entries = ttl, clock, max_entries
        self.lock_timeout = lock_timeout
        self.stats = dict(hits=0, fetches=0, evictions=0, corrupt=0)

    def _p(self, key):
        h = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:32]
        return self.folder / (h + ".json"), self.folder / (h + ".lock")

    def _read(self, p):
        try:
            x = json.loads(p.read_text(encoding="utf-8"))
            if self.clock() - x["fetched_at"] <= self.ttl:
                return x
        except FileNotFoundError:
            return None
        except Exception:
            self.stats["corrupt"] += 1
        return None

    def get(self, key, fetch):
        """(value, from_cache, fetched_at)."""
        p, lp = self._p(key)
        x = self._read(p)
        if x is not None:
            self.stats["hits"] += 1
            return x["value"], True, x["fetched_at"]
        with FileLock(lp, timeout=self.lock_timeout):
            x = self._read(p)                      # filled while we waited
            if x is not None:
                self.stats["hits"] += 1
                return x["value"], True, x["fetched_at"]
            value = fetch()
            now = self.clock()
            atomic_write(p, dict(fetched_at=now, value=value))
            self.stats["fetches"] += 1
        self._bound()
        return value, False, now

    def _bound(self):
        files = sorted(self.folder.glob("*.json"), key=lambda f: f.stat().st_mtime)
        for f in files[:max(0, len(files) - self.max_entries)]:
            try:
                f.unlink()
                self.stats["evictions"] += 1
            except OSError:
                pass


_SHARED = None


def shared(folder=None):
    """The process-wide coordinator over v2.ratelimit.BUDGETS (shared hosts merged),
    state under <bench>/v2-paper-data/control/ratecoord on C:."""
    global _SHARED
    if _SHARED is None:
        from v2 import paths, ratelimit
        folder = folder or paths.bench("v2-paper-data", "control", "ratecoord")
        _SHARED = Coordinator(folder, ratelimit.BUDGETS)
    return _SHARED
