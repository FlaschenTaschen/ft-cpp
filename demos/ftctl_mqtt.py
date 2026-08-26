#!/usr/bin/env python3
"""Home Assistant, over MQTT. Optional half of ftctl; see its docstring first.

The wall is not a light. It plays generated animations and little clips, and a
light entity with a colour picker that does nothing would be a lie. But it is
also 20,000 lumens on a wall in a workshop, and people want to turn that off
with a switch. So it is both, as several entities on one device:

  light   Betelgeuse      the display: on/off and brightness (ft_server)
  switch  Playing         the rotation, paused or not (ftsched)
  select  Demo            jump to a named effect (ftsched)
  button  Next            advance (ftsched)
  sensor  Now playing     what is on
  sensor  Frame rate      how it is doing (diagnostic)
  image   Now playing     a picture of what is on

Wipe and Restart rotation were here and are not any more: neither did anything
you could see from the room. They remain on the control socket and in the panel.
Not everything a daemon can do earns a place in a room's controls.

On/off and brightness are deliberately separate channels, and off is never
brightness 0 -- which is what both AWTRIX and WLED settled on, for the good
reason that people expect "off" to remember how bright it was. It also matters
more here than on a strip: a HUB75 panel at its minimum duty cycle is not off,
it is dim and banding, whereas all-black pixels are properly dark.

Pause is a separate entity from power for a similar reason: holding a frame on
a lit wall and blanking a wall that is still rotating are both things people
want, and one entity cannot say both. The entities that need ftsched carry
their own availability topic, so when the scheduler is down they go unavailable
on their own and the light stays live -- which is the whole point of ftctl
being a separate daemon.

Nobody models a display like this as a media_player, which is convenient,
because Home Assistant's MQTT discovery does not support one.

Discovery is the device-based form (HA 2024.11 and later): a single retained
payload describing every component at once, so this arrives as one device
rather than eight loose entities. Note `default_entity_id` and not `object_id`
-- the latter was removed in HA 2026.4, and its replacement includes the domain
in the value.

One payload for everything also means one mistake loses everything, and it does
so quietly: a config HA refuses produces no entity, no repair issue, and one
line in a log nobody is reading. See the comment above `base_availability` for
the way that has already happened once.
"""

import json
import sys
import threading
import time

# 1.x and 2.x differ in the callback signatures. Asking 2.x for the version-1
# API keeps one code path, and the version on Raspberry Pi OS is 1.5.1.
try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

RECONNECT_MIN = 1
RECONNECT_MAX = 60

# Republish at least this often even when nothing has changed, so an entity
# cannot sit wrong forever if a message was lost. Overridable; see
# --mqtt-heartbeat.
HEARTBEAT_SECONDS = 60

# The fields a person can see or has just changed. A publish happens the instant
# one of these moves, and otherwise only on the heartbeat.
#
# Everything else in the state document is telemetry that rides along with
# whatever publish happens next. That distinction is the whole reason this is
# quiet: the first version compared the entire document, which included the
# rotation's elapsed seconds, so "has anything changed?" was true every single
# second and the broker got three retained publishes a second forever. Frame
# rate has the same problem more subtly -- it wobbles by a tenth constantly.
SIGNIFICANT = ("on", "bri", "dimmer", "playing", "demo")

# Entities this used to publish and no longer does. They are named here rather
# than simply deleted because Home Assistant will not drop an entity it has
# already registered just because it stopped being described -- see the end of
# _publish_discovery(). Sent on every discovery publish, which costs one extra
# retained message per connect and makes the removal self-healing for anyone
# whose HA was down when they upgraded.
#
# Safe to empty once no installation still has these:
#
#   Restart rotation -- ftsched's restart is invalidate_from(index + 1) plus the
#     same skip() that Next does, so on the wall it was indistinguishable from
#     Next. The queue rebuild it adds only matters after editing the rotation,
#     which happens in the panel, not from a phone.
#   Wipe -- clears every layer in ft_server, and ftsched repaints layer 0 at
#     30-60 fps, so the wall was back inside one frame. It is a real tool for a
#     stuck client, which is a thing you do at the panel with the scheduler
#     stopped, not a button worth a place in a room's controls.
RETIRED = (("restart", "button"), ("wipe", "button"))


def start(bridge, args):
    """Return a running Bridge, or None if MQTT is unavailable."""
    if mqtt is None:
        sys.stderr.write(
            "ftctl: paho-mqtt is not installed (apt install python3-paho-mqtt)\n")
        return None
    published = MqttBridge(bridge, args)
    published.start()
    return published


class MqttBridge(object):

    def __init__(self, bridge, args):
        self.bridge = bridge
        self.args = args
        self.prefix = args.mqtt_prefix.rstrip("/")
        self.node = args.node_id
        self.lock = threading.Lock()
        self._last_key = None
        self._last_options = None
        self._last_avail = None
        self._last_screen = None
        self._last_publish = 0.0
        self._connected = False
        self.heartbeat = getattr(args, "mqtt_heartbeat", HEARTBEAT_SECONDS)

        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                                      client_id="ftctl-" + self.node)
        except AttributeError:
            self.client = mqtt.Client(client_id="ftctl-" + self.node)

        if args.mqtt_user:
            self.client.username_pw_set(args.mqtt_user, args.mqtt_pass)
        # Last will: if this process dies, or the Pi drops off the network, the
        # broker says so and every entity goes unavailable rather than sitting
        # at its last value looking live.
        self.client.will_set(self.t("status"), "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(RECONNECT_MIN, RECONNECT_MAX)

    # -- topics -----------------------------------------------------------

    def t(self, suffix):
        return "%s/%s" % (self.prefix, suffix)

    # -- lifecycle --------------------------------------------------------

    def start(self):
        self.bridge.add_listener(self.publish_state)
        try:
            self.client.connect_async(self.args.mqtt_host, self.args.mqtt_port,
                                      keepalive=60)
        except Exception as exc:
            sys.stderr.write("ftctl: MQTT connect failed (%s)\n" % exc)
        self.client.loop_start()

    def stop(self):
        try:
            # Say it rather than leaving it to the will, so a planned stop looks
            # different from a crash in the broker's logs.
            self.client.publish(self.t("status"), "offline", qos=1, retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc != 0:
            # 4 is bad credentials and 5 is not authorised, which are the two
            # worth naming: both look identical from the outside.
            sys.stderr.write("ftctl: MQTT refused connection, rc=%s (%s)\n"
                             % (rc, {1: "bad protocol version",
                                     2: "client id rejected",
                                     3: "broker unavailable",
                                     4: "bad username or password",
                                     5: "not authorised"}.get(rc, "see paho rc")))
            return
        self._connected = True
        sys.stderr.write("ftctl: MQTT connected to %s:%d, publishing discovery "
                         "to %s/device/%s/config\n"
                         % (self.args.mqtt_host, self.args.mqtt_port,
                            self.args.mqtt_discovery_prefix.rstrip("/"),
                            self.node))
        client.publish(self.t("status"), "online", qos=1, retain=True)
        self._publish_discovery()
        for suffix in ("power", "brightness", "playing", "demo", "next"):
            client.subscribe(self.t("set/" + suffix), qos=1)
        # Whatever we last knew, immediately, so the entities are not blank
        # until the next poll. Everything is re-asserted after a reconnect
        # rather than trusted to the broker's retained copy.
        self._last_key = self._last_avail = self._last_screen = None
        self.publish_state()

    def _on_disconnect(self, client, userdata, rc, properties=None):
        self._connected = False
        # rc 0 is our own disconnect(); anything else is the broker or the
        # network, and paho will reconnect on its own.
        if rc != 0:
            sys.stderr.write("ftctl: MQTT lost the broker (rc=%s), retrying\n"
                             % rc)

    # -- inbound ----------------------------------------------------------

    def _on_message(self, client, userdata, message):
        topic = message.topic.rsplit("/", 1)[-1]
        payload = message.payload.decode("utf-8", "replace").strip()
        try:
            self._handle(topic, payload)
        except Exception as exc:
            sys.stderr.write("ftctl: MQTT %s failed (%s)\n" % (topic, exc))
            return
        # Report back rather than waiting up to a second for the poll: an HA
        # slider that does not move under the finger feels broken.
        self.publish_state()

    def _handle(self, topic, payload):
        b = self.bridge
        if topic == "power":
            b.set_blanked(payload.upper() not in ("ON", "TRUE", "1"))
        elif topic == "brightness":
            from ftctl import ha_to_pct
            b.set_brightness(ha_to_pct(int(float(payload))))
        elif topic == "playing":
            on = payload.upper() in ("ON", "TRUE", "1")
            b.scheduler.command("resume" if on else "pause")
        elif topic == "demo":
            index = self._index_of(payload)
            if index is None:
                sys.stderr.write("ftctl: no demo called %r\n" % payload)
            else:
                b.scheduler.command("jump", index=index)
        elif topic == "next":
            b.scheduler.command("next")
        # No restart or wipe: see RETIRED. Both are still on the control socket
        # and in the panel, which is where they belong.

    def _index_of(self, name):
        sched = self.bridge.snapshot()["scheduler"] or {}
        for entry in sched.get("rotation") or []:
            if entry.get("name") == name:
                return entry.get("position", entry.get("index"))
        return None

    # -- outbound ---------------------------------------------------------

    def _options(self):
        """Names for the Demo select, in rotation order.

        Only the enabled ones: an option that jumps to a switched-off effect
        would either do nothing or silently turn it back on, and neither is
        what the person tapping it meant.
        """
        sched = self.bridge.snapshot()["scheduler"] or {}
        names = []
        for entry in sched.get("rotation") or []:
            if entry.get("enabled", True) and entry.get("name"):
                names.append(entry["name"])
        return names

    def publish_state(self):
        if not self._connected:
            return
        snap = self.bridge.snapshot()
        display, sched = snap["display"], snap["scheduler"]
        from ftctl import pct_to_ha

        with self.lock:
            # The demo list is part of the discovery payload, not the state, so
            # a rotation that gains or loses an entry needs the device
            # republished rather than just a new state message.
            options = self._options()
            if options and options != self._last_options:
                self._last_options = options
                self._publish_discovery()

            now = (sched or {}).get("now") or {}
            fps = ((sched or {}).get("health") or {}).get("actual_fps")
            state = {
                "on": (display is not None and not display["blanked"]),
                "bri": pct_to_ha(display["brightness"]) if display else 0,
                "dimmer": bool(display and display["dimmer"]),
                "playing": bool(sched and not sched.get("paused")),
                "demo": now.get("name"),
                # Whole numbers: a diagnostic sensor does not need tenths, and
                # tenths are exactly what would make this look like it changed.
                "fps": int(round(fps)) if fps is not None else None,
            }

            key = tuple(state[k] for k in SIGNIFICANT)
            due = (time.time() - self._last_publish) >= self.heartbeat
            if key == self._last_key and not due:
                return
            self._last_key = key
            self._last_publish = time.time()

            self.client.publish(self.t("state"), json.dumps(state),
                                qos=0, retain=True)

            # Availability for everything that needs the scheduler, and only
            # when it moves. Separate from the device-wide topic on purpose: the
            # light must stay usable when the rotation is not running. Retained,
            # so republishing it on a timer would have the broker rewriting its
            # retained store for a value that changes about twice a week.
            avail = "online" if sched else "offline"
            if avail != self._last_avail or due:
                self._last_avail = avail
                self.client.publish(self.t("sched/status"), avail,
                                    qos=1, retain=True)

            if self.args.public_url and state["demo"]:
                # Not retained: it names a demo that has probably already
                # changed by the time anything reads a retained copy. Only sent
                # when it actually differs -- the demo changes every 45 seconds,
                # not every second.
                url = "%s/api/thumbnail.png?demo=%s" % (
                    self.args.public_url.rstrip("/"), state["demo"])
                if url != self._last_screen or due:
                    self._last_screen = url
                    self.client.publish(self.t("screen"), url,
                                        qos=0, retain=False)

    # -- discovery --------------------------------------------------------

    def _publish_discovery(self):
        args = self.args
        device = {
            "ids": self.node,
            "name": args.friendly_name,
            "mf": "FlaschenTaschen",
            "mdl": "HUB75 video wall",
        }
        if args.public_url:
            device["cu"] = args.public_url
        display = self.bridge.snapshot()["display"]
        if display and display.get("width"):
            device["mdl"] = "HUB75 video wall, %dx%d" % (display["width"],
                                                         display["height"])

        # Availability is stated on every component and never in the shared
        # block, which looks like needless repetition and is not. Home
        # Assistant's _merge_common_device_options() copies availability_topic
        # from the shared block into any component that does not set it, and
        # its schema declares availability_topic and availability as a
        # vol.Exclusive group -- so a shared availability_topic plus a
        # per-component availability list produces both keys in one config and
        # HA rejects it. Not just that component: this is one payload, so every
        # component in it is dropped, silently, with a single line in HA's log.
        #
        # Stating it per component makes the merge a no-op and the payload
        # immune to which form the shared block happens to use.
        base_availability = [
            {"topic": self.t("status"), "payload_available": "online",
             "payload_not_available": "offline"},
        ]
        # Anything driven by ftsched needs the scheduler up as well, so it goes
        # unavailable on its own while the light stays live. availability_mode
        # "all" is what makes it both topics rather than either.
        sched_availability = base_availability + [
            {"topic": self.t("sched/status"), "payload_available": "online",
             "payload_not_available": "offline"},
        ]

        components = {
            # name: null is the documented way to say "this entity *is* the
            # device", which is what makes it light.betelgeuse rather than
            # light.betelgeuse_display.
            "display": {
                "p": "light",
                "name": None,
                "unique_id": self.node + "_display",
                "default_entity_id": "light." + self.node,
                "command_topic": self.t("set/power"),
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_value_template": "{{ 'ON' if value_json.on else 'OFF' }}",
                "brightness_command_topic": self.t("set/brightness"),
                "brightness_state_topic": self.t("state"),
                "brightness_value_template": "{{ value_json.bri }}",
                "brightness_scale": 255,
                "icon": "mdi:wall",
                "availability": base_availability,
            },
            "playing": {
                "p": "switch",
                "name": "Playing",
                "unique_id": self.node + "_playing",
                "command_topic": self.t("set/playing"),
                "value_template": "{{ 'ON' if value_json.playing else 'OFF' }}",
                "icon": "mdi:play-pause",
                "availability": sched_availability,
                "availability_mode": "all",
            },
            "demo": {
                "p": "select",
                "name": "Demo",
                "unique_id": self.node + "_demo",
                "command_topic": self.t("set/demo"),
                "value_template": "{{ value_json.demo }}",
                "options": self._options() or ["none"],
                "icon": "mdi:animation-play",
                "availability": sched_availability,
                "availability_mode": "all",
            },
            "next": {
                "p": "button",
                "name": "Next",
                "unique_id": self.node + "_next",
                "command_topic": self.t("set/next"),
                "payload_press": "PRESS",
                "icon": "mdi:skip-next",
                "availability": sched_availability,
                "availability_mode": "all",
            },
            "now_playing": {
                "p": "sensor",
                "name": "Now playing",
                "unique_id": self.node + "_now",
                "value_template": "{{ value_json.demo }}",
                "icon": "mdi:movie-open",
                "availability": sched_availability,
                "availability_mode": "all",
            },
            "fps": {
                "p": "sensor",
                "name": "Frame rate",
                "unique_id": self.node + "_fps",
                "value_template": "{{ value_json.fps }}",
                "unit_of_measurement": "fps",
                "state_class": "measurement",
                "entity_category": "diagnostic",
                "availability": sched_availability,
                "availability_mode": "all",
            },
        }
        if args.public_url:
            components["screen"] = {
                # The preview of what is playing, not a capture of the panel --
                # only ft_server knows the composite and it does not hand it
                # out. Named for what it actually is.
                "p": "image",
                "name": "Now playing",
                "unique_id": self.node + "_screen",
                "url_topic": self.t("screen"),
                "availability": sched_availability,
                "availability_mode": "all",
            }

        def send(cmps):
            payload = {
                "dev": device,
                "o": {"name": "ftctl",
                      "url": "https://github.com/FlaschenTaschen/ft-cpp"},
                "state_topic": self.t("state"),
                "qos": 1,
                "cmps": cmps,
            }
            topic = "%s/device/%s/config" % (
                args.mqtt_discovery_prefix.rstrip("/"), self.node)
            self.client.publish(topic, json.dumps(payload), qos=1, retain=True)

        # Dropping a component from the payload does not delete the entity --
        # Home Assistant keeps what it already registered. It has to be told,
        # by an update carrying the component with nothing but its platform,
        # and only then by an update that leaves it out. So retirements are
        # announced once and then forgotten.
        if RETIRED:
            tombstones = dict(components)
            for name, platform in RETIRED:
                tombstones[name] = {"p": platform}
            send(tombstones)
        send(components)
