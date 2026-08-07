#!/usr/bin/env python3
"""Check that Home Assistant will actually accept ftctl's discovery payload.

There is no feedback loop here worth relying on. A discovery config HA refuses
produces no entity, no repair issue and no error anywhere ftctl can see -- just
one line in HA's own log. It happened: a shared availability_topic alongside
per-component availability lists invalidated the whole payload, and seven of
nine entities quietly did not exist for weeks.

So this reads what is really retained on the broker and replays the two HA
behaviours that combined to cause that:

  - _merge_common_device_options() copies each of SHARED_OPTIONS out of the
    device-level body into every component that does not set it itself.
  - The MQTT entity schema puts `availability_topic` and `availability` in one
    vol.Exclusive group, so a config carrying both is rejected -- and because a
    device payload is validated as a single document, every component in it
    goes down together.

It also sweeps for stale per-component discovery topics left over from an older
scheme, which drift out of agreement with the device payload and are worth
deleting (publish an empty retained payload to remove one).

Credentials come out of ftctl's systemd drop-in, so nothing is typed on a
command line or left in a shell history. Needs to read that file:

    sudo python3 tools/ftmqttcheck.py
"""

import json
import re
import sys
import time

import paho.mqtt.client as mqtt

# homeassistant/components/mqtt/discovery.py. Device-level values are inherited
# by a component only where the component itself is silent.
SHARED_OPTIONS = ("availability", "availability_mode", "availability_template",
                  "availability_topic", "command_topic", "encoding",
                  "payload_available", "payload_not_available", "state_topic",
                  "qos")

# The retained flood under homeassistant/# arrives slowly on a busy broker, and
# a window that is too short looks exactly like "nothing is published there".
SETTLE_SECONDS = 45


def credentials():
    env = {}
    for path in ("/etc/systemd/system/ftctl.service.d/override.conf",
                 "/etc/systemd/system/ftctl.service"):
        try:
            handle = open(path)
        except OSError:
            continue
        for line in handle:
            match = re.match(r"\s*Environment=(FTCTL_\w+)=(.*)", line)
            if match and match.group(1) not in env and match.group(2).strip():
                env[match.group(1)] = match.group(2).strip().strip('"')
        handle.close()
    return env


def collect(env, node):
    seen = {}

    def on_connect(client, userdata, flags, rc):
        if rc != 0:
            sys.stderr.write("broker refused the connection (rc=%s)\n" % rc)
        client.subscribe("homeassistant/#", 1)

    def on_message(client, userdata, message):
        if node in message.topic.lower():
            seen[message.topic] = message.payload

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                             client_id="ftmqttcheck")
    except AttributeError:
        client = mqtt.Client(client_id="ftmqttcheck")
    if env.get("FTCTL_MQTT_USER"):
        client.username_pw_set(env["FTCTL_MQTT_USER"], env.get("FTCTL_MQTT_PASS"))
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(env["FTCTL_MQTT_HOST"], int(env.get("FTCTL_MQTT_PORT", 1883)),
                   60)
    client.loop_start()
    time.sleep(SETTLE_SECONDS)
    client.loop_stop()
    client.disconnect()
    return seen


def check_config(topic, payload):
    """Print a verdict per component. Returns the number HA would reject."""
    doc = json.loads(payload)
    components = doc.get("cmps") or doc.get("components")
    if components:
        shared = doc
        print("%s -- device payload, %d components" % (topic, len(components)))
    else:
        # A per-component topic: the platform comes from the topic itself, and
        # there is no shared body to merge.
        shared = {}
        components = {"(itself)": doc}
        print("%s -- legacy per-component config" % topic)

    rejected = 0
    for name in sorted(components):
        merged = dict(components[name])
        for option in SHARED_OPTIONS:
            if option in shared and option not in merged:
                merged[option] = shared[option]

        both = "availability" in merged and "availability_topic" in merged
        if "availability" in merged:
            topics = [entry.get("topic") for entry in merged["availability"]]
        else:
            topics = [merged.get("availability_topic")]
        # Drop the ft/<node>/ prefix but keep everything after it, so
        # status and sched/status stay distinguishable -- which is the whole
        # thing being checked.
        short = [t.split("/", 2)[-1] if t else "none" for t in topics]

        print("  %-12s %-7s %-6s availability=%s%s"
              % (name, merged.get("p", "?"),
                 "REJECT" if both else "ok",
                 ",".join(short),
                 "  <-- both availability forms after the merge" if both else ""))
        if both:
            rejected += 1
    return rejected


def main():
    env = credentials()
    if not env.get("FTCTL_MQTT_HOST"):
        print("no broker configured in ftctl's drop-in; nothing to check")
        return 0
    node = (sys.argv[1] if len(sys.argv) > 1 else "betelgeuse").lower()
    print("broker %s, looking for %s (%d s)"
          % (env["FTCTL_MQTT_HOST"], node, SETTLE_SECONDS))

    found = collect(env, node)
    configs = {t: p for t, p in found.items() if t.endswith("/config")}
    if not configs:
        print("no discovery config retained for %s -- HA cannot know it exists"
              % node)
        return 1

    device_topics = [t for t in configs if "/device/" in t]
    legacy = sorted(t for t in configs if "/device/" not in t)

    rejected = 0
    for topic in sorted(device_topics) + legacy:
        if not configs[topic]:
            print("%s -- empty payload, this is a deletion" % topic)
            continue
        rejected += check_config(topic, configs[topic])
        print()

    if legacy:
        print("%d stale per-component topic(s) alongside the device payload:"
              % len(legacy))
        for topic in legacy:
            print("  %s" % topic)
        print("These predate the device-based form and drift out of agreement "
              "with it.\nPublish an empty retained payload to each to remove "
              "them.")
        print()

    if rejected:
        print("%d component(s) would be rejected -- and a device payload is "
              "all-or-nothing,\nso expect none of its entities to exist."
              % rejected)
        return 1
    print("every retained config passes the availability rule")
    return 0


if __name__ == "__main__":
    sys.exit(main())
