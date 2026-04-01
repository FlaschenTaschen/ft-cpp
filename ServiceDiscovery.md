# Service Discovery Strategy for FlaschenTaschen

## Overview

This document describes the recommended approach for discovering FlaschenTaschen displays from client applications (Mac, iPhone), particularly to work around limitations in mDNS reliability on different platforms and network conditions.

## The Problem

mDNS (multicast DNS) discovery is **not 100% reliable**, especially:
- On older Avahi versions (0.8 on Raspberry Pi OS)
- Across heterogeneous networks with multiple mDNS responders
- In WiFi environments with multicast handling variations
- When devices join/leave the network frequently

**Observed behavior:**
- Sometimes displays appear, sometimes disappear from discovery results
- Discovery can be sporadic and timing-dependent
- Cross-instance discovery (Pi-to-Pi) is especially unreliable on Avahi 0.8

## Recommended Strategy

**Use a hybrid approach: mDNS + Persistent Cache + Reachability Check**

### 1. **Persistent Service Cache**

Store discovered services locally with metadata:
```
{
  instance_name: "Polaris",
  address: "192.168.88.30",
  hostname: "polaris.local",
  port: 1337,
  geometry: "64x64",
  last_seen: "2026-03-31T19:15:00Z",
  source: "mdns" | "cache"
}
```

**When to use cache:**
- User opens app → show cached services immediately
- User taps "Scan" → show cache while scanning for updates
- Detected offline → mark as unavailable but keep in cache
- Network restored → services reappear from cache

### 2. **Periodic mDNS Scanning**

Perform fresh mDNS discovery when:
- User explicitly taps "Scan" or "Refresh" button
- App enters foreground after being backgrounded
- Network changes detected (WiFi reconnect, IP change)
- Periodic background refresh (every 5-10 minutes)

**Don't rely on continuous discovery** — treat it as best-effort snapshots.

### 3. **Reachability Checks**

For cached services, verify they're still online:
```
ping <cached_ip>:1337
  → Success: service is online, use it
  → Timeout: service offline, mark as unavailable
  → Timeout + mDNS finds it elsewhere: update IP address
```

### 4. **Merge Results**

When merging fresh mDNS results with cache:
1. Trust fresh mDNS discovery (has latest info)
2. Keep old cache entries for services not in current scan (they might be offline)
3. Update IP addresses if service moved to new address
4. Remove/archive truly gone services after N scans without discovery

## Why This Works

**Resilience:**
- Avahi 0.8 bugs don't block service access (cache provides fallback)
- Sporadic discovery doesn't break functionality (periodic scans retry)
- Network flakiness handled gracefully

**Better UX:**
- App launches instantly with cached services
- Users see "Last seen: 2 hours ago" instead of just "not found"
- Explicit "Scan" button puts discovery in user's control

**Cross-Platform Consistency:**
- Mac (Bonjour), iOS (Network.framework), and Linux (Avahi) all have different discovery behaviors
- This approach abstracts away the differences

**Production Standard:**
- This is how professional apps handle discovery (HomeKit, AirPlay, Chromecast, etc.)
- Bonjour/mDNS is treated as best-effort, not guaranteed

## Implementation Notes

### macOS/Swift
Use `Network.framework` or `dns_sd.h`:
```swift
// Fresh discovery
DNSServiceBrowse(_flaschen-taschen._udp.local)

// Reachability
let monitor = NWPathMonitor()
```

### iOS/Swift
Same as macOS (Network.framework is modern recommendation).

### Linux/C++
Current implementation uses Avahi. For client apps, consider:
- Use Avahi if targeting Linux clients
- Or implement cache + reachability as above
- ft-detect tool already uses Avahi with timeout

## Caching Policy

```
Service State Transitions:

[Discovered]
    ↓ (seen in mDNS)
[Online, Active]
    ↓ (missed in 1-2 scans)
[Online, Stale] (show as available but grayed out)
    ↓ (reachability check fails)
[Offline]
    ↓ (missed in 5+ scans)
[Archived] (remove from UI, keep in history)
```

## Example: iOS App Scanning

```
User taps "Scan"
  ↓
Show cached services immediately
  ↓
Start background mDNS discovery task (5 second timeout)
  ↓
Start reachability checks on cached IPs (2 second timeout each)
  ↓
Update UI with fresh results
  ↓
Merge into cache for next time
```

## Timeout Recommendations

- mDNS browse timeout: 5-10 seconds (balance thoroughness vs responsiveness)
- Reachability (ping) timeout: 2 seconds per service
- Cache validity: Services stay in cache for 24 hours minimum
- Stale threshold: Service missed in 3+ consecutive scans marked as stale

## References

- Avahi 0.8 has cross-instance discovery limitations (see debugging-mdns.md)
- macOS dns-sd tool demonstrates reliable discovery due to Bonjour
- HomeKit and AirPlay use this hybrid approach for resilience
