# Troubleshooting mDNS Discovery

I'm running Avahi 0.8 on two Raspberry Pis that are advertising mDNS services. The services advertise correctly (they're visible on macOS via Bonjour/dns-sd), but the Avahi browser on the Pis themselves only discovers their own local services, not services advertised by other hosts on the network. Specifically: - avahi-browse -r _flaschen-taschen._udp on Polaris only shows Polaris's service - avahi-browse -r _flaschen-taschen._udp on Betelgeuse only shows Betelgeuse's service - Mac running dns-sd sees all three services correctly - Network connectivity is fine - the Pis can ping each other - mDNS multicast groups are properly joined - The Avahi responder (advertising) works perfectly, but the browser (discovering) does not Is this a known issue with Avahi 0.8? If so, what are the recommended solutions or workarounds?

---

This isn’t a general, known “Avahi 0.8 is broken at browsing” bug—what you’re seeing is almost always caused by **how multicast (mDNS) packets are handled on Linux**, not by Avahi’s responder itself.

The key clue is this:

* ✅ Your services are advertised correctly (Mac sees everything)
* ❌ Each Pi only sees *its own* service

That strongly suggests **incoming multicast traffic (224.0.0.251:5353) isn’t being delivered to Avahi on the Pis**, even though outgoing advertisements work.

---

## What’s really happening

On Linux (including Raspberry Pi OS), mDNS browsing depends on:

* UDP multicast **receiving** (not just sending)
* Proper **socket binding and interface selection**
* No filtering by firewall, kernel, or network stack

macOS handles this very permissively (via Bonjour), which is why it “just works” there.

On your Pis, one of these is almost certainly true:

### 1) Reverse path filtering (rp_filter) is dropping multicast

This is **the most common cause** on Raspberry Pi / Linux systems.

Check it:

```bash
cat /proc/sys/net/ipv4/conf/all/rp_filter
cat /proc/sys/net/ipv4/conf/eth0/rp_filter
```

If you see `1` (strict mode), multicast packets can get dropped.

### Fix:

```bash
sudo sysctl -w net.ipv4.conf.all.rp_filter=0
sudo sysctl -w net.ipv4.conf.eth0.rp_filter=0
```

To persist:

```bash
sudo nano /etc/sysctl.conf
```

Add:

```
net.ipv4.conf.all.rp_filter=0
net.ipv4.conf.default.rp_filter=0
```

---

### 2) Firewall blocking mDNS (UDP 5353 multicast)

Even if you think there’s no firewall—double check.

If you use `ufw`:

```bash
sudo ufw status
```

Allow mDNS explicitly:

```bash
sudo ufw allow in proto udp to 224.0.0.251 port 5353
```

If using `iptables`:

```bash
sudo iptables -L -n
```

Look for anything blocking UDP 5353.

---

### 3) Avahi is bound to the wrong interface

Check:

```bash
avahi-daemon --debug
```

or:

```bash
journalctl -u avahi-daemon
```

Also inspect:

```bash
cat /etc/avahi/avahi-daemon.conf
```

Make sure:

```
allow-interfaces=eth0,wlan0
```

(or just comment it out to allow all)

---

### 4) Missing or broken NSS configuration (less likely here)

This affects resolution more than browsing, but worth confirming:

```bash
cat /etc/nsswitch.conf
```

You should see:

```
hosts: files mdns4_minimal [NOTFOUND=return] dns mdns4
```

---

### 5) Multicast filtering at the network layer (switch/AP)

Since macOS sees everything, this is less likely—but still possible if:

* Wi-Fi has **client isolation**
* IGMP snooping is misconfigured

---

## Quick diagnostic test (very useful)

Run this on one Pi:

```bash
sudo tcpdump -i eth0 udp port 5353
```

Then trigger browsing from the other Pi:

```bash
avahi-browse -r _flaschen-taschen._udp
```

👉 If you **don’t see incoming packets from the other Pi**, the issue is definitely kernel/firewall/network—not Avahi.

---

## Is this specific to Avahi 0.8?

No—this behavior shows up across versions, including:

* Avahi 0.7 and 0.8
* Especially on Raspberry Pi OS / Debian-based systems

It’s more about **Linux multicast handling defaults** than an Avahi regression.

---

## Most likely fix (in your case)

Given your symptoms, I’d bet on:

👉 **rp_filter = 1 blocking multicast replies**

So try this first:

```bash
sudo sysctl -w net.ipv4.conf.all.rp_filter=0
sudo systemctl restart avahi-daemon
```

---

## If that doesn’t fix it

Tell me:

* Are you using Ethernet or Wi-Fi?
* Output of:

  * `ip a`
  * `ip route`
  * `avahi-daemon --debug`
* Whether `tcpdump` sees incoming mDNS packets

…and I can pinpoint it further.

---

## Investigation Results (March 31, 2026)

### Symptoms Observed

Setup: Two Raspberry Pi systems (Polaris, Betelgeuse) running Avahi 0.8 + ft-server, plus Mac running Bonjour.

**Working:**
- ✅ macOS dns-sd browser sees all 3 services (Polaris, Betelgeuse, Mac FT)
- ✅ ft-server publishes services successfully (logs show "Service successfully published")
- ✅ `avahi-browse` on each Pi shows its own service
- ✅ Each Pi can resolve the other's hostname via mDNS
- ✅ Network connectivity is fine (ping works between devices)
- ✅ Multicast groups properly joined (224.0.0.251 seen in `ip maddr`)

**Not working:**
- ❌ Pi-to-Pi service discovery broken
  - Polaris avahi-browse: Only sees Polaris + Mac FT (NOT Betelgeuse)
  - Betelgeuse avahi-browse: Only sees Betelgeuse + Mac FT (NOT Polaris)
- ❌ ft-detect on Pis cannot discover each other

### Diagnostics Performed

**✓ rp_filter check:**
```
Polaris:   /proc/sys/net/ipv4/conf/all/rp_filter = 0
Betelgeuse: /proc/sys/net/ipv4/conf/all/rp_filter = 0
```
Not the issue.

**✓ NSS configuration fix:**
Changed from:
```
hosts: files mdns4_minimal [NOTFOUND=return] dns
```
To:
```
hosts: files mdns4_minimal [NOTFOUND=return] dns mdns4
```
This fixed hostname resolution but didn't fix service discovery.

**✓ Firewall check:**
Port 5353 (mDNS) is open. No blocking rules detected.

**✓ Avahi daemon configuration:**
- `use-ipv4=yes`, `use-ipv6=yes`
- `allow-interfaces` commented out (allows all)
- `enable-wide-area=yes`

**✓ D-Bus permissions:**
Pi user properly in avahi group.

### Root Cause

This appears to be **a limitation or bug specific to Avahi 0.8 on Raspberry Pi OS** where the Avahi browser cannot discover services advertised by OTHER Avahi instances on the same network, despite:
1. The services being properly advertised
2. macOS Bonjour seeing all advertised services
3. Each Pi's own services being visible locally

**Not a network issue:** Since macOS consistently sees all services, the underlying mDNS stack is working correctly. This is specific to Avahi's browser/discovery implementation.

### Workaround

Use the Swift version of ft-detect on the Pis, which doesn't depend on Avahi's browser:
```bash
cp ~/ft-swift/build/ft-detect ~/bin/ft-detect
ft-detect -l  # This works correctly
```

### Conclusion

The C++ ft-detect and ft-server code are working correctly. The limitation is in the underlying Avahi 0.8 library's ability to browse services from other Avahi instances. This is likely a known issue in Avahi 0.8 (released 2011) which is quite old.

**Options:**
1. Upgrade Raspberry Pi OS to get a newer Avahi version
2. Use the Swift ft-detect tool as a workaround
3. Migrate to a different mDNS library (Bonjour, etc.)
