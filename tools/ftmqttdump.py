#!/usr/bin/env python3
"""Dump what ftctl is publishing. Reads the broker credentials out of ftctl's
systemd drop-in so they are never typed or echoed."""
import json, re, sys, time
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

host = env.get("FTCTL_MQTT_HOST")
if not host:
    print("no broker configured"); sys.exit(1)
print("broker: %s (user %s)" % (host, "set" if env.get("FTCTL_MQTT_USER") else "none"))

seen = {}
def on_connect(c, u, f, rc):
    print("connected rc=%s; subscribing" % rc)
    c.subscribe("homeassistant/device/betelgeuse/config", 1)
    c.subscribe("ft/betelgeuse/#", 1)

def on_message(c, u, m):
    seen[m.topic] = m.payload

try:
    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="ftctl-dump")
except AttributeError:
    cl = mqtt.Client(client_id="ftctl-dump")
if env.get("FTCTL_MQTT_USER"):
    cl.username_pw_set(env["FTCTL_MQTT_USER"], env.get("FTCTL_MQTT_PASS"))
cl.on_connect, cl.on_message = on_connect, on_message
cl.connect(host, int(env.get("FTCTL_MQTT_PORT", 1883)), 30)
cl.loop_start()
time.sleep(8)
cl.loop_stop()

print("\n%d retained/live topics:\n" % len(seen))
for topic in sorted(seen):
    payload = seen[topic].decode("utf-8", "replace")
    if topic.endswith("/config"):
        doc = json.loads(payload)
        print("  %s  (%d bytes)" % (topic, len(payload)))
        print("      device: %s" % json.dumps(doc.get("dev")))
        print("      origin: %s" % json.dumps(doc.get("o")))
        print("      shared: state_topic=%s availability=%s"
              % (doc.get("state_topic"), doc.get("availability_topic")))
        print("      %d components:" % len(doc.get("cmps", {})))
        for key, c in doc["cmps"].items():
            print("        %-13s %-7s %s" % (key, c["p"],
                  c.get("name") if c.get("name") is not None else "(the device itself)"))
    else:
        print("  %-32s %s" % (topic, payload[:110]))
