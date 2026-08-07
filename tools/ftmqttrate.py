#!/usr/bin/env python3
"""Count what ftctl actually puts on the broker, per topic, over a window."""
import re, sys, time, collections
import paho.mqtt.client as mqtt

env = {}
for path in ("/etc/systemd/system/ftctl.service.d/override.conf",
             "/etc/systemd/system/ftctl.service"):
    try:
        for line in open(path):
            m = re.match(r"\s*Environment=(FTCTL_\w+)=(.*)", line)
            if m and m.group(1) not in env and m.group(2).strip():
                env[m.group(1)] = m.group(2).strip().strip('"')
    except OSError:
        pass

WINDOW = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
counts = collections.Counter()
bytes_ = collections.Counter()
t0 = None

def on_connect(c, u, f, rc):
    c.subscribe("ft/betelgeuse/#", 0)
    c.subscribe("homeassistant/device/betelgeuse/config", 0)

def on_message(c, u, m):
    global t0
    if t0 is None:                 # ignore the retained burst on subscribe
        return
    counts[m.topic] += 1
    bytes_[m.topic] += len(m.payload)

try:
    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="ftctl-rate")
except AttributeError:
    cl = mqtt.Client(client_id="ftctl-rate")
if env.get("FTCTL_MQTT_USER"):
    cl.username_pw_set(env["FTCTL_MQTT_USER"], env.get("FTCTL_MQTT_PASS"))
cl.on_connect, cl.on_message = on_connect, on_message
cl.connect(env["FTCTL_MQTT_HOST"], 1883, 30)
cl.loop_start()
time.sleep(2)                      # let retained messages land and be ignored
t0 = time.monotonic()
time.sleep(WINDOW)
cl.loop_stop()

total = sum(counts.values())
print("over %.0fs, idle (nobody touching anything):" % WINDOW)
for topic in sorted(counts, key=lambda t: -counts[t]):
    print("  %-40s %4d msgs  %6d bytes  %.2f/s"
          % (topic, counts[topic], bytes_[topic], counts[topic] / WINDOW))
print("  %-40s %4d msgs  %6d bytes  %.2f/s"
      % ("TOTAL", total, sum(bytes_.values()), total / WINDOW))
