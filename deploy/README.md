# Putting the wall in Home Assistant

The wall is not a light. It plays generated animations and little clips, and if
you model it as one light entity you get a colour picker that does nothing and a
brightness slider that fights whatever is on screen. It *is* also twenty
thousand lumens on a wall in a workshop, and people reasonably want to turn that
off with a switch. So it is both, as several entities on one device.

```
Home Assistant ──MQTT──▶ broker ──▶ ftctl ──unix socket──▶ ft_server ──▶ panel
                                      │                        ▲
   phone/browser ──▶ nginx :80 ───────┤                        │
                       └──▶ ftsched :8081 ───────UDP :1337─────┘
```

Only the broker and Home Assistant are elsewhere. `ftctl` runs on betelgeuse,
because the control socket is a unix socket.

## Why the pieces are where they are

**Global state lives in `ft_server`.** It owns the panel and composites every
client onto it, so how bright the wall is and whether it is showing anything are
properties of the display rather than of whoever happens to be drawing. Put them
in a client and a laptop pushing pixels could override the wall being off.

**`ftctl` is a separate daemon, not routes in `ftsched`.** The whole point of a
global off switch is that it works when the rest does not. `ftsched` is
`BindsTo=ft_server` and restarts with it; an off switch living there would be
missing exactly when somebody is trying to deal with a misbehaving wall.

**`ftctl` is also what remembers.** `ft_server` deliberately persists nothing:
after it drops privileges it can write nowhere, and a dragged slider is dozens of
commands a second, which is not a thing to point at an SD card. `ftctl` keeps
desired state and re-applies it when the `generation` field changes, which is how
a server restart gets noticed rather than guessed at.

**`nginx` replaces `ftindex`.** That was a Python front door doing four things
nginx does in a page of config. The reason it was a separate process — that
pushing megabytes of previews through `ftsched` would run them through the GIL
the render loop waits on — stops applying when the process is nginx.

## Install order

Each step is useful on its own, so stop anywhere.

### 1. The server

```sh
sudo cp server/ft_server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart ft_server.service
```

This adds `--control-socket /run/ft/control.sock` and the `RuntimeDirectory` it
needs, and drops the old `start-ft.sh` wrapper. Keep that script until this has
survived a reboot; going back is one `ExecStart` line.

Then `nc` is the whole client:

```sh
echo get              | nc -U /run/ft/control.sock
echo "brightness 40"  | nc -U /run/ft/control.sock
echo "blank on"       | nc -U /run/ft/control.sock
echo wipe             | nc -U /run/ft/control.sock
```

Nothing listens without the flag, so an unflagged server behaves exactly as
before.

### 2. ftctl

```sh
sudo apt install python3-paho-mqtt        # optional; without it, HTTP only
sudo cp demos/ftctl.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ftctl.service
curl -s localhost:8082/api/display | python3 -m json.tool
```

### 3. MQTT credentials

They do not belong in a file in the repository:

```sh
sudo systemctl edit ftctl.service
```
```ini
[Service]
Environment=FTCTL_MQTT_HOST=mqtt.lan
Environment=FTCTL_MQTT_USER=ftctl
Environment=FTCTL_MQTT_PASS=...
Environment=FTCTL_PUBLIC_URL=http://betelgeuse.local/
```

The wall then appears in Home Assistant by itself — discovery is the
device-based form, one retained payload describing every entity, so it arrives
as one device rather than eight loose entities.

| Entity | What it is |
|---|---|
| `light.betelgeuse` | the display: on/off and brightness |
| `switch` Playing | the rotation, paused or not |
| `select` Demo | jump to a named effect |
| `button` Next / Restart rotation | the rotation |
| `button` Wipe | clear every layer, all clients |
| `sensor` Now playing, Frame rate | what is on, and how it is doing |
| `image` Now playing | a picture of it |

On/off and brightness are separate channels and **off is never brightness 0** —
which is what AWTRIX and WLED both settled on, because people expect off to
remember how bright it was. It matters more here than on a strip: a HUB75 panel
at minimum duty cycle is not off, it is dim and banding.

Pause is a separate entity from power on purpose. Holding a frame on a lit wall
and blanking a wall that is still rotating are both things people want, and one
entity cannot say both. By default turning the light off also pauses the
rotation, which is policy that lives in `ftctl` and nowhere else; the
`--no-pause-when-off` flag turns it off.

Everything that needs `ftsched` carries its own availability topic, so when the
scheduler is down those entities go unavailable on their own and the light stays
usable. That is the whole reason `ftctl` is its own daemon.

### 4. nginx

```sh
sudo apt install nginx
sudo cp deploy/nginx-betelgeuse.conf /etc/nginx/sites-available/betelgeuse
sudo ln -sf ../sites-available/betelgeuse /etc/nginx/sites-enabled/betelgeuse
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl disable --now ftindex.service
```

`ftindex.py`, `ftindex.html` and `ftindex.service` are superseded but still in
the tree — remove them once this has survived a reboot. `tailscale serve --bg
--https=443 http://127.0.0.1:80` is unaffected.

The panel then shows a display row under the transport controls. It is hidden
whenever `/api/display` does not answer, so `ftsched` served straight off :8081
looks exactly as it did.

## Things worth knowing

**A blanked wall is not an idle Pi.** The panel keeps being refreshed with an
all-black frame at full rate on its isolated core. Blank is what the display
shows, not what the machine is doing.

**Brightness below about 20% will band.** `SetBrightness()` scales values before
the CIE1931 curve, which is what makes the percentage perceptually linear, but
the bottom of an 8-bit range on a HUB75 panel is genuinely coarse. `--led-pwm-bits`
and `--led-pwm-dither-bits` are the knobs if it bothers you.

**Brightness does nothing to what is already on screen, by itself.** The library
bakes it in when a pixel is encoded, so the server forces a whole-frame repaint
behind every change. One repaint is enough, because the two canvases are kept
byte-identical rather than a frame apart.

**Wipe is the weakest of the three.** The garbage collector already clears
layers above the background after 15s, and `ftsched` repaints layer 0
continuously, so a live client paints over a wipe immediately. It earns its
place against a stuck client that keeps re-sending.

**Nothing here is authenticated**, deliberately and as before. Anything on the
network can already push pixels over UDP. Keep it on the LAN or the tailnet.

## Rolling back

```sh
# the server, binary and unit
cp /home/pi/ft-server.rollback-pre-dc /home/pi/ft-cpp/build/server/ft-server
sudo systemctl restart ft_server.service

# the front door
sudo rm /etc/nginx/sites-enabled/betelgeuse
sudo systemctl reload nginx
sudo systemctl enable --now ftindex.service
```

`ftctl` can simply be stopped; nothing depends on it, which is the point.
