# Basler GigE network setup

Connect the three cameras to PoE-capable switch ports **if the camera models support PoE**; otherwise use their required external supplies. Connect the switch uplink to the Belkin F2CU040 and the adapter directly to a compatible USB-C data port. The current setup uses a YuanLey YS2083GS-P in Default/Standard mode: Near Field on port 6, Far Field on port 7, Dark Field on port 8, and the computer on uplink port 9. Verify power requirements separately from data connectivity. Software does not configure the unmanaged switch or USB adapter.

```text
Far Field  ─┐
Dark Field ─┼─ YuanLey YS2083GS-P ─ Ethernet ─ Belkin F2CU040 ─ USB-C ─ PC
Near Field ─┘                            shared 1 Gb/s uplink
```

1. Install Basler pylon and its GigE transport/driver components. Verify all cameras in pylon Viewer first, then close Viewer so it does not retain camera ownership.
2. Confirm 1.0 Gb/s negotiation on every link, including the Belkin adapter. Use Cat5e or better cabling. Keep this camera network separate from internet traffic.
3. Put the adapter and all cameras in one IPv4 subnet with unique addresses. Example **only if it does not conflict with existing networks**: adapter 192.168.50.1/24, cameras .11, .12, .13; no gateway is needed on that isolated adapter. Basler's IP Configurator can set persistent camera IPs. This application never changes adapter IPs or the Windows firewall.
4. Permit pylon camera discovery and GigE Vision streaming on the camera interface. Verify discovery with `--list-cameras`, then run **configure_cameras.cmd** to assign the correct Far/Dark/Near Field serials. **check_windows.cmd --cameras** reports visibility without taking camera ownership.
5. Start with 1500-byte packets. Jumbo packets must be supported and enabled along the **entire** path: cameras, exact switch revision, Belkin hardware/driver and NIC settings. Verify packet loss before raising `packet_size`; do not assume USB-C implies jumbo-frame support.
6. Keep exposure shorter than the requested frame interval (83.3 ms at 12 fps; 166.7 ms at 6 fps). Hardware ROI, sensor readout, packet pacing and achievable frame rate also affect delivery. Applied settings and resulting frame rate, when available, are saved under `camera_<role>.json`.

## Shared bandwidth

The Belkin is a **10/100/1000 Mb/s** adapter. Its 1 Gb/s link is a shared limit for all three streams, regardless of switch fabric capacity or USB-C connector speed. The theoretical ceiling is 125 MB/s before overhead. The configuration uses a conservative **90 MB/s image-payload budget**.

```text
payload_MBps = sum(width * height * fps * wire_bytes_per_pixel) / 1e6
wire_bytes_per_pixel = 1.5 for packed Mono12p, 2 for unpacked Mono12/16, 1 for Mono8
```

| Stream configuration | Aggregate payload | Relative to 90 MB/s budget |
|---|---:|---|
| Full 1456×1088 lossless Mono12p at 14/8/12 fps (Far/Dark/Near; supplied configuration) | 71.29 MB/s | within budget; Far/Near need 28.51 MB/s each, Dark needs 14.26 MB/s |
| Full 1456×1088 unpacked Mono12, all three at 10 fps | 95.05 MB/s | exceeds configured budget |
| Three 512×512 unpacked Mono12 streams at 10 fps | 15.73 MB/s | ample headroom, but crops the sensor |
| Three 1024×1024 unpacked Mono12 streams at 10 fps | 62.91 MB/s | measure transport and exposure delay |
| Three 1920×1080 unpacked Mono12 streams at 10 fps | 124.42 MB/s | exceeds budget before protocol overhead |
| 1024×1024 unpacked Mono12 at 20 fps | 125.83 MB/s | exceeds budget |

The application refuses requested payload above the configured budget. This check is an estimate, not a measured link qualification. All three cameras negotiated **1 Gb/s** in the previous hardware test. The current full-frame lossless Mono12p profile requests 12 fps and a 36,000,000 **byte/s** `DeviceLinkThroughputLimit` for Far/Near Field; Dark Field requests 6 fps and 20,000,000 byte/s. This meets the requested targets of at least 10 fps for Far/Near and 5–8 fps for Dark while reducing total payload by one sixth compared with the earlier 14/8/14 profile. The saved profile passed a one-minute live recording test on this setup. Native processing/recording remains uint16; all 12 bits are retained. Without packing, all three full-resolution streams at 10 fps would exceed the aggregate payload budget. If the throughput-limit feature is unavailable, the adapter tries to derive `GevSCPD` from the reported device tick frequency; if neither exists, it reports the need for manual pacing. `GevSCPD` and `GevSCFTD` are camera **ticks**, not microseconds, and tick frequencies vary by camera/PTP state. Manual `inter_packet_delay_ticks` is applied after the throughput limit and can change effective throughput. Keep resulting throughput above `width*height*fps*wire_bytes_per_pixel` with allowance for transport overhead; otherwise the requested frame rate may not be achievable.

The SDK uses `LatestImageOnly` and four grab buffers per camera. This avoids a growing acquisition backlog but can skip frames when overloaded. Saved SDK skip counts do not include all driver overruns or network losses. During hardware qualification also inspect pylon's stream statistics (resends, failed buffers, missing packets), compare delivered FPS and saved sequences, and use the real network's measured latency. Multi-camera packet bursts may require further inter-packet pacing or transmission staggering.

At startup the application checks the receiver's `FrameRetention` deadline against payload size, actual throughput limit and link speed. If needed it increases the deadline to twice the estimated payload transfer time plus 100 ms, preserving an already longer deadline. This also supports slower links or deliberately paced full-frame streams without rejecting a frame before its packets can arrive. The applied deadline is saved in `camera_<role>.json` under `stream_grabber`.

## What the 50 ms timing means

The supplied configuration requests a **83.3 ms frame interval** at 12 fps for Far/Near Field and a **166.7 ms interval** at 6 fps for Dark Field. The processing target is **50 ms after frame retrieval**, independent of frame cadence. Reported timing starts when a completed frame is returned by `RetrieveResult`, before the application copies its pixels. It covers:

- `host_processing`: owned buffer copy plus centroid calculation up to measurement creation.
- `host_to_pid`: retrieval through centroid, loopback transport and nominal PID calculation.
- `host_to_saved`: retrieval through successful raw-file write/close into the OS cache.
- `host_to_display_submission`: retrieval through submission of a new pixmap to Qt.

It does **not** measure exposure age, sensor readout, camera/network buffering, USB scheduling, physical monitor scanout or piezo settling. The display metric is measured only when a new frame is submitted, not on repeated timer ticks. Independent streams are not synchronized. True exposure-to-actuation qualification needs synchronized timestamp conversion (or external instrumentation), the actual camera models and a calibrated actuator driver. Windows/Python provides a soft timing target, not a hard real-time deadline.

Separately, supported cameras now provide an approximate `camera_to_host` age using a read-only clock latch at connection time. `camera_to_display_submission` adds host rendering time. These estimates expose transfer delay that the host-only timing excludes; the GUI's **image age** also includes the time since that image arrived. Clock-latch round-trip uncertainty and subsequent clock drift limit absolute accuracy. These diagnostics are not used to authorize control output or qualify physical actuation.

One full 1456×1088 lossless Mono12p frame contains 2,376,192 wire payload bytes, decoded to 3,168,256 native uint16 bytes. Payload transmission alone takes approximately **66 ms** at the Far/Near Field 36 MB/s caps, or **119 ms** at Dark Field's 20 MB/s cap, before exposure/readout and protocol overhead. These payload-only estimates exclude protocol and scheduling overhead. The host's 50 ms processing target starts after retrieval and does not include this transfer time. Raise per-camera limits only within the shared uplink budget, or reduce frame rate while retaining the complete sensor view.

## Vendor references

- [Basler Python acquisition and grab strategies](https://docs.baslerweb.com/pypylon-image-acquisition)
- [Basler buffer ownership and advanced acquisition](https://docs.baslerweb.com/pylonapi/cpp/pylon_advanced_topics)
- [Basler network parameters and device tick definitions](https://docs.baslerweb.com/network-related-parameters)
- [Official pypylon repository and installation](https://github.com/basler/pypylon)
- [Belkin F2CU040 network-speed specification](https://www.belkin.com/uk/support-article/?articleNum=336963)
- [Intellinet 561792 product family](https://intellinetnetwork.eu/products/intellinet-en-5-port-gigabit-ethernet-poe-switch-561792) (check the physical V2 revision manual for revision-specific details)

