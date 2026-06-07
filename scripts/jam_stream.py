#!/usr/bin/env python3
"""jam_stream.py — stream ChucK's JACK output to Icecast via GStreamer.

Reads the Icecast source-password from icecast.xml (via sudo) and sets it on
shout2send PROGRAMMATICALLY so the secret never appears in process argv / ps.
Runs on beelink. Connects gststream's jack input ports to ChucK:outport 0/1.
"""
import subprocess
import sys
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

Gst.init(None)

# Pull the source-password out of icecast.xml without ever putting it in argv.
pw = subprocess.check_output(
    ["sudo", "grep", "-oP", r"(?<=<source-password>)[^<]+", "/etc/icecast2/icecast.xml"],
    text=True,
).strip()

pipeline = Gst.parse_launch(
    "jackaudiosrc client-name=gststream connect=none ! "
    "audioconvert ! audioresample ! audio/x-raw,rate=44100,channels=2 ! "
    "volume volume=4.0 ! "  # downstream boost: ChucK master.gain is conservative (0.6)
    "queue ! lamemp3enc target=bitrate bitrate=128 cbr=true ! "
    "shout2send name=cast ip=127.0.0.1 port=8080 mount=/jam.mp3"
)
pipeline.get_by_name("cast").set_property("password", pw)  # not in argv

pipeline.set_state(Gst.State.PLAYING)

# Wait for jack ports to register, then wire ChucK -> gststream.
GLib.timeout_add_seconds(3, lambda: (
    subprocess.run(["jack_connect", "ChucK:outport 0", "gststream:in_jackaudiosrc0_1"]),
    subprocess.run(["jack_connect", "ChucK:outport 1", "gststream:in_jackaudiosrc0_2"]),
    print("connected ChucK -> gststream", flush=True),
    False,
)[-1])

GLib.MainLoop().run()
