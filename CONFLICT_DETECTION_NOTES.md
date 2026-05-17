# Modbus Address Conflict Detection Notes

## What This Document Is For

This document describes how a Modbus address conflict (two physical devices
sharing the same Modbus slave/unit ID on the same RS-485 bus or TCP server)
manifests on the wire, as simulated by the PHS Modbus Server Simulator.  Use
these details to implement conflict-detection logic in a separate Modbus
scanning / diagnostic tool.

---

## Background: What Causes an Address Conflict?

On an RS-485 Modbus RTU bus (or a Modbus TCP gateway that multiplexes multiple
physical devices), each device is assigned a unique **Unit ID** (slave address,
1–247).  If a technician accidentally programs two devices with the same Unit ID:

- Both devices listen to the same address.
- Both devices attempt to respond to the same query simultaneously.
- Their RS-485 drivers fight each other on the bus (bus contention).
- The master receives a garbled, combined signal.

On Modbus TCP (as simulated here), the effect is modelled by:
1. **XOR-ing** the register values of both conflicting devices on every read.
2. **Randomly dropping ~15 % of responses** (simulating frames lost to
   collision / CRC failure).

---

## How Conflicts Appear to a Polling Master / Scanner

### 1. Intermittent Timeouts (~15 % of polls)

The most reliable first indicator.  A healthy device responds to every poll
within its configured timeout.  A conflicting address will silently drop
roughly 1 in 7 responses.

**Detection heuristic:**
```
timeout_rate = timeouts / total_polls
if timeout_rate > 0.10:   # >10 % — suspicious
    flag as "possible conflict or unreliable device"
if timeout_rate > 0.05 and register_variance_high:
    flag as "probable address conflict"
```

### 2. Inconsistent / Unstable Register Values

Even when a response arrives, the value is the **bitwise XOR** of both
devices' registers.  This produces values that:

- Change between polls even when neither device's actual value changed
  (because the other device's value changed).
- Are often nonsensical for the expected data type (e.g., a temperature
  sensor reading that oscillates between 72 °F and 65,463 °F).
- Show high variance across consecutive polls with no physical cause.

**Detection heuristic (for holding/input registers):**
```
# Collect N consecutive readings of the same register
readings = [poll() for _ in range(N)]   # e.g. N = 10
variance = stdev(readings)
if variance > EXPECTED_MAX_VARIANCE:
    flag as "register value unstable — possible conflict"
```

**Detection heuristic (for coils / discrete inputs):**
```
# A coil that flips state on every poll with no commanded change
flips = count_state_changes(coil_readings)
if flips / len(coil_readings) > 0.5:
    flag as "coil toggling unexpectedly — possible conflict"
```

### 3. XOR Signature Pattern

If you know the expected value of one device (e.g., from a factory default or
a known-good baseline scan), you can detect the XOR pattern:

```
observed_value XOR known_device_A_value = device_B_value
```

If `device_B_value` is a plausible register value (not random noise), this
strongly suggests a second device is responding on the same address.

### 4. Simultaneous Timeout + Bad Value Correlation

A single timeout is not conclusive.  A single bad value is not conclusive.
But the **combination** of both occurring on the same Unit ID is a strong
signal:

```
score = 0
if timeout_rate > 0.05:       score += 2
if value_variance_high:       score += 2
if coil_flip_rate > 0.3:      score += 2
if crc_error_rate > 0.05:     score += 1   # if your stack exposes CRC errors
if score >= 4:
    flag as "HIGH CONFIDENCE: address conflict on Unit ID {uid}"
elif score >= 2:
    flag as "POSSIBLE address conflict on Unit ID {uid}"
```

---

## Simulator Behaviour Reference

| Condition | Simulator Action |
|-----------|-----------------|
| Two slaves with same Unit ID on same TCP server | `ConflictingDataBlock` is wired for that ID |
| Read request arrives for conflicting ID | Returns `device_A_value XOR device_B_value` |
| Write request arrives for conflicting ID | Both devices' data blocks are written |
| Response drop probability | ~15 % (random, per-request) |
| Coil/DI conflict value | `bool_A XOR bool_B` (i.e., `True XOR True = False`, `True XOR False = True`) |
| Holding/Input register conflict value | `uint16_A XOR uint16_B` per register word |

---

## Recommended Detection Algorithm for a Scanning Tool

```python
POLL_COUNT        = 20      # polls per device per scan cycle
TIMEOUT_THRESHOLD = 0.10    # >10 % timeouts → suspicious
VARIANCE_THRESHOLD = 500    # stdev of uint16 readings → suspicious
FLIP_THRESHOLD    = 0.40    # >40 % coil state changes → suspicious

def assess_unit_id(uid: int, poll_results: list) -> str:
    """
    poll_results: list of dicts, each with keys:
        'timed_out': bool
        'holding_regs': list[int]   (None if timed_out)
        'coils': list[bool]         (None if timed_out)
    """
    total   = len(poll_results)
    timeouts = sum(1 for r in poll_results if r['timed_out'])
    timeout_rate = timeouts / total

    valid = [r for r in poll_results if not r['timed_out']]
    score = 0

    # Timeout score
    if timeout_rate > TIMEOUT_THRESHOLD:
        score += 2

    # Holding register variance score
    if valid:
        for reg_idx in range(len(valid[0]['holding_regs'])):
            vals = [r['holding_regs'][reg_idx] for r in valid]
            if stdev(vals) > VARIANCE_THRESHOLD:
                score += 2
                break   # one unstable register is enough

    # Coil flip score
    if valid and valid[0]['coils']:
        for coil_idx in range(len(valid[0]['coils'])):
            states = [r['coils'][coil_idx] for r in valid]
            flips  = sum(1 for i in range(1, len(states))
                         if states[i] != states[i-1])
            if flips / len(states) > FLIP_THRESHOLD:
                score += 2
                break

    if score >= 4:
        return f"HIGH CONFIDENCE: address conflict on Unit ID {uid}"
    elif score >= 2:
        return f"POSSIBLE address conflict on Unit ID {uid}"
    else:
        return f"Unit ID {uid}: OK"
```

---

## Notes on Modbus TCP vs RTU

| Aspect | Modbus RTU (RS-485) | Modbus TCP (simulated) |
|--------|--------------------|-----------------------|
| Conflict mechanism | Physical bus contention, CRC errors | XOR of register values, dropped responses |
| CRC errors visible? | Yes — framing errors on the wire | No — TCP handles framing; only value corruption |
| Timeout cause | Collision destroys frame | Simulator randomly drops response |
| Distinguishing feature | CRC error rate elevated | No CRC errors, but value instability + timeouts |

On a real RS-485 bus, CRC errors are an additional strong indicator.  On
Modbus TCP (or a TCP gateway), CRC errors are hidden by the transport layer,
so value instability and timeout rate are the primary signals.

---

## Quick Reference: Conflict Fingerprint

A conflicting Unit ID will typically show **all three** of these simultaneously:

1. **Timeout rate 10–20 %** (not 0 %, not 100 %)
2. **Register values that change between polls** with no commanded write
3. **Values that are implausible** for the expected data type / range

Any single symptom alone could have another cause.  All three together on the
same Unit ID is a strong indicator of an RS-485 address conflict.
