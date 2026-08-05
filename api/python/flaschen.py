# -*- mode: python; c-basic-offset: 2; indent-tabs-mode: nil; -*-
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation version 2.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://gnu.org/licenses/gpl-2.0.txt>
'''A Python API client for Flaschen Taschen.'''

import errno
import socket
import sys
import time

__version__ = "0.2.0"

# Ask for a send buffer big enough that a burst of frames does not hit ENOBUFS.
# The kernel silently clamps this to net.core.wmem_max, so it is safe to ask.
_SNDBUF_BYTES = 1 << 20

# A fast sender can momentarily outrun the network stack. Pausing to let the
# queue drain and trying once more usually gets the frame out.
_RETRY_ERRNOS = (errno.ENOBUFS, errno.EAGAIN, errno.EWOULDBLOCK)

# The server is not there. Because the socket is connected, an ICMP port
# unreachable from an earlier datagram surfaces as an error on a later send,
# so this reports a frame that already failed rather than one that just did.
# A display stream is better off skipping it: the frame is stale a few
# milliseconds later anyway, and the sender should survive the server being
# restarted underneath it. The kernel clears the pending error once it is
# read, so sends resume by themselves when the server comes back.
_UNREACHABLE_ERRNOS = (errno.ECONNREFUSED, errno.EHOSTUNREACH,
                       errno.ENETUNREACH, errno.ENETDOWN)

# Reading the pending error clears it, so the next send succeeds at the syscall
# level and only the one after it fails again: a dead server makes sends fail
# every other time, not every time. Recovery therefore has to be judged on a
# sustained run of successes, or the notices flap once per frame. At 60 fps
# this is half a second.
_RECOVERY_STREAK = 30

# Netpbm header with Flaschen Taschen offset included.
_HEADER_P6_FT = b"""\
P6
%(width)d %(height)d
#FT: %(x)d %(y)d %(z)d
255
"""

class Flaschen(object):
  '''A Framebuffer display interface that sends a frame via UDP.

  Sending tolerates the server being absent: frames sent while nothing is
  listening are counted in `dropped` rather than raising, so a long-running
  demo survives the server being restarted underneath it and resumes on its
  own. Errors that indicate a real problem still propagate.
  '''

  def __init__(self, host, port, width=0, height=0, layer=5, transparent=False):
    '''

    Args:
      host: The flaschen taschen server hostname or ip address.
      port: The flaschen taschen server port number.
      width: The width of the flaschen taschen display in pixels.
      height: The height of the flaschen taschen display in pixels.
      layer: The layer of the flaschen taschen display to write to.
      transparent: If true, black(0, 0, 0) will be transparent and show the layer below.
    '''
    self.width = width
    self.height = height
    self.layer = layer
    self.transparent = transparent
    self.dropped = 0                  # frames lost to an unreachable server
    self._peer = "%s:%d" % (host, port)
    self._unreachable = False         # so the notice prints once, not per frame
    self._ok_streak = 0
    self._dropped_at_notice = 0
    self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _SNDBUF_BYTES)
    self._sock.connect((host, port))
    header = _HEADER_P6_FT % {
      b"width": self.width,
      b"height": self.height,
      b"x": 0,
      b"y": 0,
      b"z": self.layer,
    }
    self._data = bytearray(len(header) + (width * height * 3))
    self._data[0:len(header)] = header
    self._header_len = len(header)

  @property
  def __array_interface__(self):
    '''An array interface to directly access the framebuffer pixel data.
    
    Direct writes to pixel data will ignore the transparent parameter.
    '''
    return {
      "shape": (self.height, self.width, 3),
      "typestr": "|u1",
      "data": memoryview(self._data)[
        self._header_len : self._header_len + (self.height * self.width * 3)
      ],
      "version": 3,
    }

  def set(self, x, y, color):
    '''Set the pixel at the given coordinates to the specified color.

    Args:
      x: x offset of the pixel to set
      y: y offset of the piyel to set
      color: A 3 tuple of (r, g, b) color values, 0-255
    '''
    if x >= self.width or y >= self.height or x < 0 or y < 0:
      return
    if color == (0, 0, 0) and not self.transparent:
      color = (1, 1, 1)

    offset = (x + y * self.width) * 3 + self._header_len
    self._data[offset] = color[0]
    self._data[offset + 1] = color[1]
    self._data[offset + 2] = color[2]
  
  def _send(self, data):
    '''Send one datagram.

    A full send buffer is retried once after a short pause. An unreachable
    server drops the frame and increments `dropped`, so that restarting the
    server does not kill a running demo. Anything else propagates.
    '''
    try:
      self._sock.send(data)
    except OSError as e:
      if e.errno in _RETRY_ERRNOS:
        time.sleep(0.0004)            # let the queue drain
        try:
          self._sock.send(data)
        except OSError as retry_error:
          e = retry_error
        else:
          self._note_reachable()
          return
      if e.errno not in _UNREACHABLE_ERRNOS:
        raise
      self.dropped += 1
      self._ok_streak = 0
      if not self._unreachable:
        self._unreachable = True
        self._dropped_at_notice = self.dropped
        sys.stderr.write("flaschen: %s unreachable (%s); dropping frames "
                         "until it returns\n" % (self._peer, e.strerror))
      return
    self._note_reachable()

  def _note_reachable(self):
    if not self._unreachable:
      return
    self._ok_streak += 1
    if self._ok_streak < _RECOVERY_STREAK:
      return                          # could just be the gap between ICMPs
    self._unreachable = False
    sys.stderr.write("flaschen: %s reachable again, %d frames dropped\n"
                     % (self._peer, self.dropped - self._dropped_at_notice))

  def send(self):
    '''Send the updated pixels to the display.'''
    self._send(self._data)

  def send_array(self, pixels, offset):
    # (numpy.typing.ArrayLike, tuple[int, int, int]) -> None
    '''Send an array of pixels to the given offset.

    Can be used as an alternative to the send method.
    The initial transparent option is respected.
    The initial width, height, and layer is ignored.

    Args:
      pixels: An array-like of RGB pixels, shaped as (height, width, RGB)
              Color values are expected to be from 0 to 255.
      offset: The (x, y, layer) offset.
              X and Y begin at the top-left pixel.
    '''
    import numpy as np
    array = np.asarray(pixels, dtype=np.uint8)
    if len(array.shape) != 3 or array.shape[2] != 3:
      raise TypeError(
        "Pixel array must be shape (height, width, 3), got %r" % (array.shape,)
      )
    if not self.transparent:
      array = array.copy(order="C")  # Prevent modifying the original array.
      array[(array == (0, 0, 0)).all(axis=-1)] = (1, 1, 1)

    header = _HEADER_P6_FT % {
      b"width": array.shape[1],
      b"height": array.shape[0],
      b"x": offset[0],
      b"y": offset[1],
      b"z": offset[2],
    }
    self._send(header + array.tobytes())

  def send_array_banded(self, pixels, offset, band_rows=0):
    # (numpy.typing.ArrayLike, tuple[int, int, int], int) -> None
    '''Send an array of pixels as a stack of horizontal bands.

    Each band is an independent datagram placed with its own y offset, so a
    frame need not fit in one 65,507-byte datagram, and a lost IP fragment
    costs only its band rather than the whole frame (the kernel reassembles
    each datagram all-or-nothing, and the server does no cross-datagram
    reassembly).

    Fewer, larger bands are cheaper: over WiFi, airtime is dominated by
    packets per second rather than bytes. Over loopback there is no fragment
    loss to localize, so band_rows=0 (one datagram) is the cheapest choice.

    Args:
      pixels: An array-like of RGB pixels, shaped as (height, width, RGB)
              Color values are expected to be from 0 to 255.
      offset: The (x, y, layer) offset of the whole array.
      band_rows: Rows per datagram. 0 sends the array as a single band.
    '''
    import numpy as np
    array = np.asarray(pixels, dtype=np.uint8)
    x, y, z = offset
    rows = band_rows or len(array)
    for top in range(0, len(array), rows):
      self.send_array(array[top:top + rows], (x, y + top, z))
