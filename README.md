# INVIZ — Virtual Camera Motion Controller for Blender

**INVIZ** turns any smartphone into a wireless, real-time virtual production camera rig for Blender—with **zero mobile apps to install**. 

Walk around your room to aim Blender's camera using your phone's gyroscope, fly through your scene with on-screen virtual joysticks, monitor Blender's live camera viewport directly on your phone screen, and bake smooth motion keyframes straight onto your timeline.

---

## ✨ Features

* **Zero App Install:** Runs directly inside your phone's mobile browser (Safari on iOS, Chrome/Firefox on Android) over local Wi-Fi.
* **6-DoF Hybrid Camera Control:**
  * **Gyroscope Rotation:** Maps phone orientation (W3C `DeviceOrientation`) to Blender's camera quaternion with frame-rate-independent SLERP smoothing.
  * **Virtual Joystick Movement:** Move forward/backward along the camera's local look vector and strafe side-to-side in real time.
* **Live Camera Viewfinder Streaming:** Streams an offscreen GPU render of your active Blender camera straight to your phone screen so you can frame shots without looking at your monitor.
* **One-Tap Keyframe Recording:** Records rotation (and location when moving) in memory at your scene's exact FPS without lagging the viewport, then bakes clean keyframes onto the timeline when you stop.
* **Axis Locking & Grip Modes:**
  * Independently freeze **Pitch**, **Yaw**, or **Roll** (great for keeping the horizon completely level while panning).
  * Switch seamlessly between **Landscape (Left/Right)** and **Portrait** (9:16 vertical video) phone grips.
* **Built-In Local HTTPS & WebSocket Server:** Automatically generates a local SAN-bound TLS certificate (`cryptography`) so mobile browsers grant full gyroscope sensor permissions without manual SSL setup.

---

## 📦 Installation

1. Download or clone this repository (make sure `inviz.py` and `index.html` stay together in the same folder, or zip the folder).
2. Open **Blender (3.6 or newer)**.
3. Go to **Edit → Preferences → Add-ons → Install...**
4. Select the `.py` file (or `.zip` archive) and enable **Camera: INVIZ**.
5. Open the **3D Viewport Sidebar** by pressing `N` and click the **INVIZ** tab.

---

## 🚀 Quick Start Guide

### Step 1: One-Time Certificate Setup
Mobile browsers strictly block gyroscope/motion sensors over unencrypted `http://` connections. 
* In the **INVIZ** sidebar panel, click **Install certificate helper** (this installs Python's `cryptography` package into Blender's local modules folder; requires internet once).

### Step 2: Start the Local Server
1. Ensure your PC and smartphone are connected to the **same Wi-Fi network** (or mobile hotspot).
2. Click **Start Server** in the INVIZ panel.
3. Click **Copy URL** and send/open the `https://<your-lan-ip>:8765/?k=<token>` link on your phone.

### Step 3: Accept the Local Self-Signed Certificate on Your Phone
Because INVIZ generates a private local TLS certificate for your PC's LAN IP, your phone browser will show a security warning the first time:
* **Android (Chrome):** Tap **Advanced** → **Proceed to `<IP>` (unsafe)**.
* **iOS (Safari):** Tap **Show Details** → **Visit this website** → **Visit Website**.

### Step 4: Connect & Shoot
1. On your phone screen, tap **Start tracking** (and grant Motion/Orientation sensor permission if prompted).
2. In Blender's INVIZ panel, select your target **Camera** in the `Camera` field.
3. Click **Enable Live Camera Control** (or toggle **Camera control** right from your phone).
4. Hold your phone in your comfortable starting pose and tap **Calibrate** to align the camera's heading.
5. Toggle **Camera view** on your phone to see Blender's live viewfinder, then hit **Record** to capture your take!

---

## 🎛️ Controls & Settings Reference

| Setting | Location | Description |
| :--- | :--- | :--- |
| **Phone Grip** | Blender Panel | `Landscape (top to left/right)` for standard 16:9 framing, or `Portrait` for vertical 9:16 shots. |
| **Sensitivity** | Both | `1.0` = 1:1 real-world rotation. Higher values amplify small wrist movements. |
| **Smoothing** | Both | Frame-rate-independent quaternion SLERP damping (`0.0` = raw sensor data, `0.3–0.6` = cinematic handheld). |
| **Move Speed** | Both | Virtual joystick speed in Blender units per second. |
| **Lock Pitch / Yaw / Roll** | Both | Freezes individual rotation axes at their current angle (e.g., lock Roll for tripod-level pans). |
| **Preview Width & FPS** | Blender Panel | Adjusts live viewfinder stream resolution (`160–960 px`) and frame rate (`1–30 FPS`) for smooth performance over weak Wi-Fi. |
| **Key Every N Frames** | Blender Panel | `1` records a keyframe on every single frame; higher values record sparser keys. |

---

## 🛠️ How It Works Under the Hood

* **Zero-Dependency RFC 6455 WebSockets:** Implements raw binary WebSocket framing, masking, heartbeat pings, and constant-time token verification (`secrets.compare_digest`) directly on top of Python's `socketserver` and `ssl`.
* **Thread-Safe Architecture:** Network I/O, state broadcasting, and PNG encoding run on dedicated background daemon threads, while all `bpy` scene mutations and `GPUOffScreen` viewport captures execute safely on Blender's main thread via `bpy.app.timers` and `POST_PIXEL` draw handlers.
* **Non-Blocking Frame Dropping:** Outgoing camera preview frames overwrite older unsent frames per connection so a slow Wi-Fi link never stalls Blender's viewport framerate.

---

## 🔧 Troubleshooting

* **Phone won't connect / page times out:**
  * Check that both devices are on the same subnet (some public/college Wi-Fi networks block device-to-device LAN traffic—using a phone hotspot works great).
  * Allow Blender (or port `8765` TCP) through your OS firewall (`ufw` / Windows Defender Firewall).
  * If auto-IP detection picks a virtual adapter (like Docker or VPN), type your Wi-Fi LAN IP into **IP (optional)** before starting the server.
* **Gyroscope values stay at `0` on phone:**
  * Make sure you opened the **`https://`** link (not `http://`) and accepted the certificate warning.
  * Keep the browser tab active and in the foreground while tracking.
* **Camera view stream is laggy:**
  * Lower **Preview width** to `320` or `480` and set **Preview FPS** to `10` in the Blender panel.

---

## 📄 License

Free and open-source—use it in personal, student, or commercial Blender projects. Built by **userk8**.
