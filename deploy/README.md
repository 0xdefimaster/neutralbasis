# NeutralBasis V2: setup on another machine or a server

This guide is for anyone moving NeutralBasis V2 to a new computer or server.

**PAPER ONLY.** Nothing here places an order, signs anything or needs a key.

## 1. Code

```
git clone https://github.com/0xdefimaster/neutralbasis.git
cd neutralbasis
git checkout v2/venue-policy
```

| Platform | Setup command |
|---|---|
| Windows | `powershell -ExecutionPolicy Bypass -File deploy\setup.ps1` |
| Linux / macOS | `sh deploy/setup.sh` |

- **Requirements:** Python 3.13. The only third-party package is `ccxt` (`requirements.txt`).
- **What the setup does:** it creates `.venv`, installs the requirements and runs the offline test suite.
- **Expected result on Windows (verified 2026-11-12 on a clean clone):** 1020+ tests OK.
- **Linux / macOS:** NOT verified yet. Tests that rely on Windows drive letters (D:/E:) and DPAPI skip there.

## 2. Data (optional; needed for the offline replays and the OBSERVED-data tests)

**Data pack:** `neutralbasis-data-pack-*.zip`, made with `python -m tools.v2_pack_data`.

1. Unzip it, for example to `/srv/nb/`, which creates `/srv/nb/nb-benchmark/...`.
2. Check each file against `nb-benchmark/MANIFEST.json` (sha256).
3. Set the variables:

| Variable | Value |
|---|---|
| `NB_V2_BENCH_DIR` | `/srv/nb/nb-benchmark` |
| `NB_V2_CLOSES_FILE` | `/srv/nb/nb-benchmark/v2-paper-data/closes-2026-11-13.json.gz` (1 MB; replaces the 5.3 GB pilot DB) |
| `NB_V2_PAPER_DATA_DIR` | a folder on the SAFE disk for new outputs |

Without the data pack, the three OBSERVED-data test classes skip and every other test runs.

## 3. Never

| Never | Why |
|---|---|
| Set `NB_V2_E_WRITE_ACK=1` | E:/D: on the old machine is a failing disk |
| Set `NB_V2_READONLY_WS=1` without the owner's approval | Read-only WebSocket, doc 35 / v2.ws_readonly |
| Set `NB_V2_ESTIMATED_RFQ=1` in a process that also runs the strict desk | ESTIMATED results stay apart |
| Start a 24/7 recorder or the engine before the owner approves the storage | — |

## 4. Storage for continuous recording (pending)

- A bounded stream writes about 26 MB per hour, about 630 MB per day: hash-chained JSON lines in `NB_V2_PAPER_DATA_DIR`.
- Reliable closed PAPER trades need hours of uninterrupted data, so the recorder needs:
  - a healthy disk;
  - log rotation;
  - the owner's approval.
