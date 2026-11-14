bl_info = {
    "name": "INVIZ",
    "author": "INVIZ",
    "version": (0, 5, 0),
    "blender": (3, 6, 0),
    "location": "3D Viewport > Sidebar (N) > INVIZ",
    "description": "Use your phone as a motion controller for Blender's camera (live rotation, joystick movement, live camera view, keyframe recording, full remote controls on the phone)",
    "category": "Camera",
}

import base64, collections, datetime, hashlib, ipaddress, json, os, queue, secrets, socket, socketserver
import ssl, struct, subprocess, sys, threading, time, zlib
from urllib.parse import parse_qs, urlparse

import math

import bpy
import gpu
import numpy as np
from mathutils import Quaternion, Vector

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CMDS = ("calibrate", "rec_start", "rec_stop", "live_on", "live_off")
MAX_FRAME = 65536
S = {"running": False, "url": "", "token": "", "webdir": "", "srv": None, "ctx": None,
     "clients": set(), "q": queue.Queue(maxsize=512), "last": {}, "last_t": 0.0,
     "packets": 0, "bad": 0, "rate": 0.0, "_n": 0, "_t0": time.time(),
     "live": False, "cam": "", "orig_mode": "XYZ", "base": None, "q0": None, "off": None, "grip": None, "out": None, "msg": "",
     "_tk": time.time(), "_rd": 0.0,
     "pv": set(), "conns": {}, "drop": "", "raw": None, "ev": None, "goff": None, "goff_size": None, "_pv": 0.0,
     "pv_msg": "", "pv_fail": 0, "h": None, "rec": None, "rmsg": "",
     "cmds": queue.Queue(maxsize=64), "joy": (0.0, 0.0, 0.0), "state_js": "", "_st": 0.0,
     "lockv": [0.0, 0.0, 0.0], "lk_prev": None, "yaw_hint": 0.0}


# ---------- helpers ----------
def lan_ip(override=""):
    if override:
        return override
    try:  # no packet is sent; this only asks the OS which interface it would use
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def mod_dir():
    d = bpy.utils.user_resource('SCRIPTS', path="modules", create=True)
    if d not in sys.path:
        sys.path.append(d)
    return d


def have_crypto():
    mod_dir()
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def make_cert(ip):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    d = bpy.utils.user_resource('CONFIG', path="inviz", create=True)
    cp, kp = os.path.join(d, "cert_%s.pem" % ip), os.path.join(d, "key_%s.pem" % ip)
    if os.path.exists(cp) and os.path.exists(kp):
        return cp, kp
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "INVIZ")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]), critical=False)
            .sign(key, hashes.SHA256()))
    with open(kp, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    with open(cp, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return cp, kp


# ---------- WebSocket framing (RFC 6455, small unfragmented text frames) ----------
def take_frame(buf):
    """Pop one complete frame from the bytearray `buf`, or return None if more bytes are needed.
    Partial data stays in the buffer, so a read timeout can never corrupt the stream."""
    if len(buf) < 2:
        return None
    b1, b2 = buf[0], buf[1]
    op, ln, i = b1 & 15, b2 & 127, 2
    if ln == 126:
        if len(buf) < 4:
            return None
        ln, i = struct.unpack(">H", bytes(buf[2:4]))[0], 4
    elif ln == 127:
        if len(buf) < 10:
            return None
        ln, i = struct.unpack(">Q", bytes(buf[2:10]))[0], 10
    if ln > MAX_FRAME:
        raise ValueError("frame too large")
    masked = bool(b2 & 128)
    need = i + (4 if masked else 0) + ln
    if len(buf) < need:
        return None
    if masked:
        mask = bytes(buf[i:i + 4])
        data = bytes(c ^ mask[k % 4] for k, c in enumerate(buf[i + 4:i + 4 + ln]))
    else:
        data = bytes(buf[i:i + ln])
    del buf[:need]
    return op, data


def send_frame(sock, op, data=b""):
    n = len(data)
    if n < 126:
        ln, ext = n, b""
    elif n < 65536:
        ln, ext = 126, struct.pack(">H", n)
    else:
        ln, ext = 127, struct.pack(">Q", n)
    sock.sendall(bytes([0x80 | op, ln]) + ext + data)


class Conn:
    """Outgoing queue of one phone. Only the connection's own thread touches the TLS socket: reading and
    writing one SSL object from two threads at once is not safe and shows up as random disconnects."""
    def __init__(self):
        self.ctl = collections.deque()  # small control/state frames, in order
        self.pv = None                  # newest preview frame only: an unsent older one is simply replaced


def push(sock, op, data=b""):
    """Any thread: queue a frame for a phone. The connection thread sends it within ~20 ms."""
    c = S["conns"].get(sock)
    if c is None:
        return
    if op == 2:
        c.pv = data
    else:
        c.ctl.append((op, data))


class Handler(socketserver.BaseRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        self.request = S["ctx"].wrap_socket(self.request, server_side=True)

    def handle(self):
        try:
            self._handle()
        except (OSError, ConnectionError, ValueError) as e:
            S["drop"] = "%s: %s" % (type(e).__name__, e)  # shown in the panel, so a bad link can be diagnosed
            print("INVIZ: phone connection closed (%s)" % S["drop"])
        finally:
            S["clients"].discard(self.request)
            S["joy"] = (0.0, 0.0, 0.0)
            S["pv"].discard(self.request)
            S["conns"].pop(self.request, None)
            try:
                self.request.close()
            except OSError:
                pass

    def _http(self, code, ctype, body):
        if isinstance(body, str):
            body = body.encode()
        hdr = ("HTTP/1.1 %d X\r\nContent-Type: %s\r\nContent-Length: %d\r\nCache-Control: no-store\r\n"
               "Connection: close\r\n\r\n" % (code, ctype, len(body))).encode()
        self.request.sendall(hdr + body)

    def _handle(self):
        s, buf = self.request, b""
        while b"\r\n\r\n" not in buf:
            c = s.recv(4096)
            if not c or len(buf) > 8192:
                return
            buf += c
        lines = buf.split(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
        _, target, _ = lines[0].split(" ", 2)
        hd = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
        u = urlparse(target)
        k = parse_qs(u.query).get("k", [""])[0]
        if not secrets.compare_digest(k.encode(), S["token"].encode()):
            return self._http(403, "text/plain", "Forbidden: open the exact URL shown in Blender.")
        if u.path == "/ws" and hd.get("upgrade", "").lower() == "websocket":
            return self._ws(hd)
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(S["webdir"], "index.html"), "rb") as f:
                    return self._http(200, "text/html; charset=utf-8", f.read())
            except OSError:
                return self._http(404, "text/plain", "index.html not found in the INVIZ web folder.")
        self._http(404, "text/plain", "Not found")

    def _ws(self, hd):
        s = self.request
        acc = base64.b64encode(hashlib.sha1(hd.get("sec-websocket-key", "").encode() + GUID).digest()).decode()
        s.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   "Sec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
        conn = S["conns"][s] = Conn()
        S["clients"].add(s)
        rbuf, last_rx = bytearray(), time.time()
        while S["running"]:
            if conn.ctl or conn.pv is not None:
                s.settimeout(10)  # a send that stalls this long means a dead link: the error closes the connection
                while conn.ctl:
                    send_frame(s, *conn.ctl.popleft())
                pv, conn.pv = conn.pv, None
                if pv is not None:
                    send_frame(s, 2, pv)
            s.settimeout(0.02)
            try:
                chunk = s.recv(65536)
            except (socket.timeout, ssl.SSLWantReadError):
                chunk = None
            if chunk == b"":
                raise ConnectionError("phone closed the connection")
            if chunk:
                rbuf += chunk
                last_rx = time.time()
            elif time.time() - last_rx > 30:  # phone sends sensors/heartbeat constantly, so silence = dead link
                raise ConnectionError("no data from phone for 30 s")
            while True:
                fr = take_frame(rbuf)
                if fr is None:
                    break
                self._dispatch(s, *fr)

    def _dispatch(self, s, op, data):
        if op == 8:
            send_frame(s, 8)
            raise ConnectionError("phone closed the connection")
        if op == 9:
            send_frame(s, 10, data)
            return
        if op != 1:
            return
        try:
            m = json.loads(data)
            if m.get("type") == "hb":
                send_frame(s, 1, b'{"type":"hb"}')
                return
            if m.get("type") == "preview":  # phone asks for / stops the camera view
                S["pv_fail"] = 0
                (S["pv"].add if m.get("on") else S["pv"].discard)(s)
                return
            if m.get("type") in ("cmd", "set", "joy"):
                self._control(m)
                return
            pkt = {k: float(m[k]) for k in ("alpha", "beta", "gamma")}
            pkt["t"], pkt["abs"] = float(m.get("t", 0)), bool(m.get("abs"))
            S["q"].put_nowait(pkt)
        except queue.Full:
            pass
        except (ValueError, KeyError, TypeError, AttributeError):
            S["bad"] += 1

    def _control(self, m):
        """Phone remote control. Runs on a socket thread: it only validates and queues, Blender is touched in tick()."""
        t = m["type"]
        if t == "joy":
            x, y = max(-1.0, min(1.0, float(m["x"]))), max(-1.0, min(1.0, float(m["y"])))
            S["joy"] = (x, y, time.time())
        elif t == "cmd":
            if m.get("cmd") in CMDS:
                S["cmds"].put_nowait((m["cmd"], {}))
        else:  # "set": sensitivity / smoothing / speed / axis locks
            a = {}
            for key, lo, hi in (("sens", 0.1, 3.0), ("smooth", 0.0, 0.95), ("speed", 0.05, 100.0)):
                if key in m:
                    a[key] = max(lo, min(hi, float(m[key])))
            if "lock" in m:
                lk = m["lock"]
                a["lock"] = (bool(lk[0]), bool(lk[1]), bool(lk[2]))
            if a:
                S["cmds"].put_nowait(("set", a))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = os.name != "nt"  # on Windows this flag would allow port hijacking
    daemon_threads = True

    def handle_error(self, *a):  # TLS warnings from a phone that rejects the cert are normal
        pass


# ---------- camera control (runs on the main thread only) ----------
def phone_quat(p):
    """W3C DeviceOrientation: R = Rz(alpha) Rx(beta) Ry(gamma) maps device axes to an East-North-Up world.
    Blender world is also X east / Y north / Z up, and a Blender camera's local axes (x right, y up,
    looks down -z) equal a phone's (x right, y up, back camera looks down -z), so R is the camera rotation."""
    return (Quaternion((0, 0, 1), math.radians(p["alpha"])) @ Quaternion((1, 0, 0), math.radians(p["beta"]))
            @ Quaternion((0, 1, 0), math.radians(p["gamma"])))


GRIP_DEG = {'LAND_L': -90.0, 'LAND_R': 90.0, 'PORTRAIT': 0.0}


def heading(q):
    """Compass-style direction (radians) a camera with rotation q looks toward, around the world Z axis."""
    f = q @ Vector((0, 0, -1))
    return math.atan2(f[1], f[0])


def decompose(q, yaw_hint=0.0):
    """Camera rotation -> (pitch, yaw, roll) in radians. Yaw = compass heading, pitch = elevation of the
    view direction, roll = tilt about the view direction (0 = horizon level)."""
    f = q @ Vector((0, 0, -1))
    pitch = math.asin(max(-1.0, min(1.0, f[2])))
    yaw = math.atan2(f[1], f[0]) if math.hypot(f[0], f[1]) > 1e-4 else yaw_hint  # straight up/down: heading undefined
    r = compose(pitch, yaw, 0.0).inverted() @ q  # what is left is a pure turn about local Z
    if r.w < 0:
        r.negate()
    return pitch, yaw, 2.0 * math.atan2(r.z, r.w)


def compose(pitch, yaw, roll):
    return (Quaternion((0, 0, 1), yaw - math.pi / 2) @ Quaternion((1, 0, 0), math.pi / 2 + pitch)
            @ Quaternion((0, 0, 1), roll))


def live_cam():
    ob = bpy.data.objects.get(S["cam"])
    return ob if ob is not None and ob.type == 'CAMERA' else None


def live_on(cam):
    S.update(live=True, cam=cam.name, orig_mode=cam.rotation_mode, base=cam.matrix_basis.to_quaternion(),
             q0=None, out=None, msg="")


def live_off(msg=""):
    stop_record()
    cam = live_cam()
    if S["live"] and cam is not None:
        cam.rotation_mode = S["orig_mode"]  # Blender converts, so the orientation is kept
    S["live"], S["msg"] = False, msg


def live_step(cam, now, sens, smooth, grip_deg, lock):
    p = S["last"]
    if not p or now - S["last_t"] > 1.0:  # stale: hold the last pose
        return
    if S["grip"] != grip_deg:  # grip changed: recalibrate so the camera does not jump
        S["grip"], S["q0"] = grip_deg, None
    # roll the camera about its own Z so the phone's grip matches the camera frame
    q = phone_quat(p) @ Quaternion((0, 0, 1), math.radians(grip_deg))
    if S["q0"] is None:
        # (re)calibrate: pitch and roll stay true to gravity (level phone = level camera);
        # only the heading is anchored to the camera's home heading
        S["off"] = Quaternion((0, 0, 1), heading(S["base"]) - heading(q))
        S["q0"] = S["off"] @ q
        S["out"] = S["q0"].copy()
        S["lk_prev"] = None  # locked axes freeze at the freshly calibrated pose
    cur = S["off"] @ q
    d = cur @ S["q0"].inverted()  # rotation since calibration, in world space
    if d.w < 0:
        d.negate()
    axis, ang = d.to_axis_angle()
    target = Quaternion(axis, ang * sens) @ S["q0"]
    prev = S["lk_prev"] or (False, False, False)
    if any(l and not pv for l, pv in zip(lock, prev)):  # axis just locked: freeze what the camera shows now
        now_v = decompose(S["out"], S["yaw_hint"])
        for i in range(3):
            if lock[i] and not prev[i]:
                S["lockv"][i] = now_v[i]
    S["lk_prev"] = lock
    if any(lock):
        v = list(decompose(target, S["yaw_hint"]))
        for i in range(3):
            if lock[i]:
                v[i] = S["lockv"][i]
        S["yaw_hint"] = v[1]
        target = compose(*v)
    a = 1.0 - smooth ** (max(now - S["_tk"], 0.001) * 60.0)  # frame-rate independent smoothing
    S["out"] = S["out"].slerp(target, a)
    cam.rotation_mode = 'QUATERNION'
    cam.rotation_quaternion = S["out"]


def joy_step(cam, now):
    """Joystick: push forward moves along the direction the camera is looking (so looking up goes up),
    sideways strafes along the camera's own right axis. Uses the smoothed output rotation."""
    x, y, t = S["joy"]
    if S["out"] is None or now - t > 0.4 or (x == 0.0 and y == 0.0):  # stale = phone vanished, so stop
        return
    dt = min(max(now - S["_tk"], 0.0), 0.1)
    sp = bpy.context.scene.inviz.move_speed
    fwd, right = S["out"] @ Vector((0, 0, -1)), S["out"] @ Vector((1, 0, 0))
    d = (fwd * y + right * x) * (sp * dt)
    if cam.parent is not None:
        d = cam.parent.matrix_world.to_3x3().inverted() @ d
    cam.location += d
    if S["rec"]:
        S["rec"]["moved"] = True


# ---------- recording ----------
def record_step(cam, now):
    """While performing, only remember (frame, rotation) in memory: no keyframe writes, so no extra
    depsgraph/animation updates fighting the live pose. Keys are written in one go when you stop.
    Frames come from real elapsed time, so the timing of your move is preserved."""
    r = S["rec"]
    if not r or S["out"] is None:
        return
    f = r["start"] + int(round((now - r["t0"]) * r["fps"]))
    if r["limit"] is not None and f > r["limit"]:
        stop_record()
        return
    if f >= r["last"] + r["step"]:
        r["buf"].append((f, S["out"].copy(), cam.location.copy()))
        r["last"], r["n"] = f, r["n"] + 1


def anim_play(on):
    """Start/stop timeline playback from the timer or an operator (needs a window/screen override in a timer)."""
    wm = bpy.context.window_manager
    if not wm.windows:
        return
    w = wm.windows[0]
    try:
        with bpy.context.temp_override(window=w, screen=w.screen):
            playing = w.screen.is_animation_playing
            if on and not playing:
                bpy.ops.screen.animation_play()
            elif not on and playing:
                bpy.ops.screen.animation_cancel(restore_frame=False)
    except Exception:
        import traceback
        traceback.print_exc()


def live_on_default(sc):
    """Enable live control on the chosen camera. Returns an error string or None."""
    cam = sc.inviz.camera
    if cam is None or cam.type != 'CAMERA':
        return "Pick a camera in the Camera field first"
    live_on(cam)
    return None


def rec_begin(sc):
    """Start a take. Returns an error string or None. Used by the panel button and the phone's Record."""
    p = sc.inviz
    if not S["live"] or live_cam() is None:
        return "Enable live camera control first"
    if S["rec"]:
        return None
    if p.rec_start == 'RANGE':
        sc.frame_set(sc.frame_start)
    start = sc.frame_current
    S["rmsg"] = ""
    S["rec"] = dict(t0=time.time(), start=start, last=start - p.rec_step, step=p.rec_step,
                    fps=sc.render.fps / sc.render.fps_base, n=0, buf=[], act=None, moved=False,
                    limit=sc.frame_end if p.rec_use_end else None, played=False)
    ad = live_cam().animation_data
    if p.rec_play and ad is not None and ad.action is not None:
        S["rec"]["act"], ad.action = ad.action, None  # an old take would override the live pose on playback
    if p.rec_play:
        anim_play(True)
        S["rec"]["played"] = True
    return None


def stop_record(ctx=None):
    r, S["rec"] = S["rec"], None
    if not r:
        return
    cam = live_cam()
    if cam is None:
        S["rmsg"] = "Camera missing: this take was lost"
        return
    try:
        ad = cam.animation_data
        if r["act"] is not None and ad is not None:
            ad.action = r["act"]  # give the camera its earlier animation back
        if r["buf"]:
            cam.rotation_mode = 'QUATERNION'
            live_loc = cam.location.copy()
            for f, q, loc in r["buf"]:
                cam.rotation_quaternion = q
                cam.keyframe_insert(data_path="rotation_quaternion", frame=f)
                if r["moved"]:  # only key position when the joystick was actually used in this take
                    cam.location = loc
                    cam.keyframe_insert(data_path="location", frame=f)
            cam.location = live_loc
            if S["out"] is not None:
                cam.rotation_quaternion = S["out"]
            S["orig_mode"] = 'QUATERNION'  # the keys live on rotation_quaternion, so the camera must stay in it
            S["rmsg"] = "Recorded %d keys, frames %d to %d" % (r["n"], r["start"], r["last"])
        else:
            S["rmsg"] = "Nothing recorded (no data from the phone?)"
    except Exception as e:
        import traceback
        traceback.print_exc()
        S["rmsg"] = "Writing keys failed: %s" % e
    if r["played"]:
        anim_play(False)


# ---------- main-thread timer: the ONLY place Blender state is touched ----------
def tick():
    try:
        return _tick()
    except Exception as e:  # keep the timer alive; show the problem in the panel and the console
        import traceback
        traceback.print_exc()
        S["msg"] = "Error: %s (see System Console)" % e
        return 0.1 if S["running"] else None


def run_cmds():
    sc = bpy.context.scene
    p = sc.inviz
    try:
        while True:
            name, a = S["cmds"].get_nowait()
            if name == "set":
                if "sens" in a:
                    p.sensitivity = a["sens"]
                if "smooth" in a:
                    p.smoothing = a["smooth"]
                if "speed" in a:
                    p.move_speed = a["speed"]
                if "lock" in a:
                    p.lock_pitch, p.lock_yaw, p.lock_roll = a["lock"]
            elif name == "calibrate":
                S["q0"] = None
            elif name == "live_on":
                if not S["live"]:
                    S["msg"] = live_on_default(sc) or ""
            elif name == "live_off":
                live_off()
            elif name == "rec_start":
                if not S["live"]:
                    S["rmsg"] = live_on_default(sc) or ""
                if S["live"]:
                    S["rmsg"] = rec_begin(sc) or ""
            elif name == "rec_stop":
                stop_record()
    except queue.Empty:
        pass


def publish_state(now, sc, p):
    """Snapshot for the phone (sent by the stater thread, so the timer never blocks on a socket)."""
    if now - S["_st"] < 0.2:
        return
    S["_st"] = now
    r = S["rec"]
    S["state_js"] = json.dumps({
        "type": "state", "live": S["live"], "cam": S["cam"], "rec": bool(r), "keys": r["n"] if r else 0,
        "frame": sc.frame_current, "rmsg": S["rmsg"], "msg": S["msg"],
        "sens": round(p.sensitivity, 3), "smooth": round(p.smoothing, 3), "speed": round(p.move_speed, 3),
        "lock": [p.lock_pitch, p.lock_yaw, p.lock_roll]})


def _tick():
    n = 0
    try:
        while True:
            S["last"] = S["q"].get_nowait()
            n += 1
    except queue.Empty:
        pass
    now = time.time()
    if n:
        S["last_t"] = now
        S["packets"] += n
        S["_n"] += n
    if now - S["_t0"] >= 1:
        S["rate"], S["_n"], S["_t0"] = S["_n"] / (now - S["_t0"]), 0, now
    run_cmds()
    pr = bpy.context.scene.inviz
    if S["live"]:
        tgt = pr.camera
        if tgt is None or tgt.type != 'CAMERA':
            live_off("Camera field is empty: live control stopped")
        elif tgt.name != S["cam"]:  # field changed: finish the take on the old camera, then follow the new one
            live_off()
            live_on(tgt)
    if S["live"]:
        cam = live_cam()
        if cam is None:
            live_off("Camera was deleted: live control stopped")
        else:
            live_step(cam, now, pr.sensitivity, pr.smoothing, GRIP_DEG[pr.grip],
                      (pr.lock_pitch, pr.lock_yaw, pr.lock_roll))
            joy_step(cam, now)
            record_step(cam, now)
    publish_state(now, bpy.context.scene, pr)
    S["_tk"] = now
    if S["live"] or S["pv"] or now - S["_rd"] > 0.1:
        S["_rd"] = now
        for w in bpy.context.window_manager.windows:
            for a in w.screen.areas:
                if a.type == 'VIEW_3D':
                    a.tag_redraw()
    return 0.016 if S["running"] else None


# ---------- camera view streaming ----------
def png_bytes(w, h, rgba):
    """Minimal PNG encoder (stdlib zlib). OpenGL rows come bottom-up, so flip them."""
    a = np.frombuffer(rgba, np.uint8).reshape(h, w, 4)[::-1, :, :3]
    raw = np.zeros((h, w * 3 + 1), np.uint8)
    raw[:, 1:] = a.reshape(h, w * 3)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), 1)) + chunk(b"IEND", b""))


def streamer():
    """Background thread: encodes and sends the newest frame, so a slow phone can never stall Blender."""
    while S["running"]:
        S["ev"].wait(0.5)
        if not S["ev"].is_set():
            continue
        S["ev"].clear()
        job, S["raw"] = S["raw"], None
        if not job:
            continue
        try:
            png = png_bytes(*job)
        except Exception:
            continue
        for c in list(S["pv"]):
            try:
                push(c, 2, png)
            except OSError:
                S["pv"].discard(c)


def stater():
    """Background thread: pushes the state snapshot to every phone (on change, on new connection, and at least 1/s)."""
    last, seen, t_last = "", frozenset(), 0.0
    while S["running"]:
        time.sleep(0.1)
        js, cs, now = S["state_js"], frozenset(S["clients"]), time.time()
        if not js or not cs or (js == last and cs == seen and now - t_last < 1.0):
            continue
        last, seen, t_last = js, cs, now
        for c in cs:
            try:
                push(c, 1, js.encode())
            except OSError:
                S["clients"].discard(c)


def buf_bytes(buf, w, h):
    """gpu Buffer -> bytes. Blender's Buffer is multi-dimensional and numpy rejects it as non-contiguous,
    so flatten it first; if that still fails, copy it element-wise (slower, but always works)."""
    try:
        buf.dimensions = w * h * 4
        return np.frombuffer(buf, np.uint8).tobytes()
    except Exception:
        return np.array(buf, np.uint8).reshape(-1).tobytes()


def free_off():
    if S["goff"] is not None:
        try:
            S["goff"].free()
        except Exception:
            pass
    S["goff"], S["goff_size"] = None, None


def grab_frame(pr):
    """Main thread: draw the camera's view offscreen and read the pixels back."""
    scene = bpy.context.scene
    cam = live_cam() or pr.camera
    area, region = bpy.context.area, bpy.context.region
    if cam is None or area is None or region is None:
        S["pv_msg"] = "Preview needs a camera and an open 3D Viewport"
        return
    w = pr.preview_width
    h = max(2, int(w * scene.render.resolution_y / max(1, scene.render.resolution_x)))
    if S["goff_size"] != (w, h):
        free_off()
        S["goff"], S["goff_size"] = gpu.types.GPUOffScreen(w, h), (w, h)
    dg = bpy.context.evaluated_depsgraph_get()
    with S["goff"].bind():
        fb = gpu.state.active_framebuffer_get()
        S["goff"].draw_view3d(scene, bpy.context.view_layer, bpy.context.space_data, region,
                             cam.matrix_world.inverted(), cam.calc_matrix_camera(dg, x=w, y=h),
                             do_color_management=True)
        buf = fb.read_color(0, 0, w, h, 4, 0, 'UBYTE')
    S["raw"], S["pv_msg"] = (w, h, buf_bytes(buf, w, h)), ""
    S["ev"].set()


def draw_preview():
    """Runs inside the 3D Viewport's own redraw, so the GPU context is valid. Errors here never touch the timer."""
    if not (S["pv"] and S["running"]):
        return
    now = time.time()
    pr = bpy.context.scene.inviz
    if not pr.preview or S["pv_fail"] >= 3 or now - S["_pv"] < 1.0 / pr.preview_fps:
        return
    S["_pv"] = now
    try:
        grab_frame(pr)
    except Exception as e:
        import traceback
        traceback.print_exc()
        S["pv_fail"] += 1
        S["pv_msg"] = "Preview error: %s" % e


def start_server(ip, port, webdir):
    """Returns an error string, or None on success."""
    if S["running"]:
        return "Already running"
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(*make_cert(ip))
        S.update(ctx=ctx, token=secrets.token_urlsafe(6), webdir=webdir, packets=0, bad=0, last={}, last_t=0.0)
        srv = Server((ip, port), Handler)  # bound to the LAN address only, never 0.0.0.0
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)
    S.update(srv=srv, running=True, url="https://%s:%d/?k=%s" % (ip, port, S["token"]))
    S.update(ev=threading.Event(), pv=set(), conns={}, drop="", pv_fail=0, pv_msg="", raw=None,
             cmds=queue.Queue(maxsize=64), joy=(0.0, 0.0, 0.0), state_js="", lk_prev=None)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True).start()
    threading.Thread(target=streamer, daemon=True).start()
    threading.Thread(target=stater, daemon=True).start()
    S["h"] = bpy.types.SpaceView3D.draw_handler_add(draw_preview, (), 'WINDOW', 'POST_PIXEL')
    if not bpy.app.timers.is_registered(tick):
        bpy.app.timers.register(tick, first_interval=0.05)
    return None


def stop_server():
    if S["h"] is not None:
        bpy.types.SpaceView3D.draw_handler_remove(S["h"], 'WINDOW')
        S["h"] = None
    live_off()
    S["running"] = False
    srv, S["srv"] = S["srv"], None
    if srv:
        srv.shutdown()
        srv.server_close()
    for c in list(S["clients"]):
        try:
            c.close()
        except OSError:
            pass
    S["clients"].clear()
    S["pv"].clear()
    free_off()
    S["url"] = ""


# ---------- Blender UI ----------
def is_cam(self, ob):
    return ob.type == 'CAMERA'


class INVIZ_Props(bpy.types.PropertyGroup):
    port: bpy.props.IntProperty(name="Port", default=8765, min=1024, max=65535)
    ip_override: bpy.props.StringProperty(name="IP (optional)", description="Leave empty to auto-detect")
    web_dir: bpy.props.StringProperty(name="Web folder", subtype='DIR_PATH',
                                      description="Folder containing index.html")
    camera: bpy.props.PointerProperty(name="Camera", type=bpy.types.Object, poll=is_cam,
                                      description="The camera that is controlled and recorded. Must be set")
    rec_start: bpy.props.EnumProperty(
        name="Start at", default='CURRENT',
        items=[('CURRENT', "Current frame", ""), ('RANGE', "Start of scene frame range", "")])
    rec_step: bpy.props.IntProperty(name="Key every N frames", default=1, min=1, max=10,
                                    description="1 = a key on every frame (smoothest, most keys)")
    rec_play: bpy.props.BoolProperty(name="Play timeline while recording", default=False,
                                     description="Other animation plays along while you perform (can be heavy on big scenes)")
    rec_use_end: bpy.props.BoolProperty(name="Stop at end of scene range", default=False)
    preview: bpy.props.BoolProperty(name="Camera view on phone", default=True)
    preview_width: bpy.props.IntProperty(name="Preview width", default=480, min=160, max=960,
                                         description="Pixels. Smaller = smoother over weak Wi-Fi")
    preview_fps: bpy.props.IntProperty(name="Preview FPS", default=10, min=1, max=30)
    grip: bpy.props.EnumProperty(
        name="Phone grip", default='LAND_L',
        items=[('LAND_L', "Landscape (top to left)", "Matches Blender's wide camera frame"),
               ('LAND_R', "Landscape (top to right)", "Landscape, phone turned the other way"),
               ('PORTRAIT', "Portrait", "For tall / vertical video")])
    move_speed: bpy.props.FloatProperty(name="Move speed", default=1.0, min=0.05, max=100.0, soft_max=20.0,
                                        description="Joystick speed in Blender units per second at full push")
    lock_pitch: bpy.props.BoolProperty(name="Lock pitch", description="Freeze up/down tilt of the camera")
    lock_yaw: bpy.props.BoolProperty(name="Lock yaw", description="Freeze left/right heading of the camera")
    lock_roll: bpy.props.BoolProperty(name="Lock roll", description="Freeze the sideways tilt (keeps the horizon level)")
    sensitivity: bpy.props.FloatProperty(name="Sensitivity", default=1.0, min=0.1, max=3.0,
                                         description="1.0 = camera turns exactly as much as the phone")
    smoothing: bpy.props.FloatProperty(name="Smoothing", default=0.3, min=0.0, max=0.95,
                                       description="0 = raw, higher = smoother but laggier")


class INVIZ_OT_install(bpy.types.Operator):
    bl_idname, bl_label = "inviz.install", "Install certificate helper"
    bl_description = "Installs the 'cryptography' package into Blender (needs internet once; Blender may pause)"

    def execute(self, ctx):
        try:
            subprocess.run([sys.executable, "-m", "ensurepip"], check=False, capture_output=True)
            subprocess.run([sys.executable, "-m", "pip", "install", "--target", mod_dir(), "cryptography"],
                           check=True, capture_output=True)
        except Exception as e:
            self.report({'ERROR'}, "Install failed (internet? run Blender as admin?): %s" % e)
            return {'CANCELLED'}
        self.report({'INFO'}, "Helper installed")
        return {'FINISHED'}


class INVIZ_OT_start(bpy.types.Operator):
    bl_idname, bl_label = "inviz.start", "Start Server"

    def execute(self, ctx):
        p = ctx.scene.inviz
        webdir = bpy.path.abspath(p.web_dir) if p.web_dir else os.path.dirname(os.path.abspath(__file__))
        err = None if have_crypto() else "Install the certificate helper first"
        err = err or start_server(lan_ip(p.ip_override), p.port, webdir)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        return {'FINISHED'}


class INVIZ_OT_stop(bpy.types.Operator):
    bl_idname, bl_label = "inviz.stop", "Stop Server"

    def execute(self, ctx):
        stop_server()
        return {'FINISHED'}


class INVIZ_OT_copy(bpy.types.Operator):
    bl_idname, bl_label = "inviz.copy", "Copy URL"

    def execute(self, ctx):
        ctx.window_manager.clipboard = S["url"]
        return {'FINISHED'}


class INVIZ_OT_live_on(bpy.types.Operator):
    bl_idname, bl_label = "inviz.live_on", "Enable Live Camera Control"

    def execute(self, ctx):
        err = live_on_default(ctx.scene)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        return {'FINISHED'}


class INVIZ_OT_live_off(bpy.types.Operator):
    bl_idname, bl_label = "inviz.live_off", "Disable Live Control"

    def execute(self, ctx):
        live_off()
        return {'FINISHED'}


class INVIZ_OT_calibrate(bpy.types.Operator):
    bl_idname, bl_label = "inviz.calibrate", "Calibrate / Recenter"
    bl_description = "Hold the phone in your starting pose, then press: the camera heading returns to its home heading"

    def execute(self, ctx):
        S["q0"] = None
        return {'FINISHED'}


class INVIZ_OT_rec_start(bpy.types.Operator):
    bl_idname, bl_label = "inviz.rec_start", "Start Recording"

    def execute(self, ctx):
        if S["rec"]:
            return {'CANCELLED'}
        err = rec_begin(ctx.scene)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        return {'FINISHED'}


class INVIZ_OT_rec_stop(bpy.types.Operator):
    bl_idname, bl_label = "inviz.rec_stop", "Stop Recording"

    def execute(self, ctx):
        stop_record(ctx)
        return {'FINISHED'}


class INVIZ_PT_panel(bpy.types.Panel):
    bl_label, bl_idname = "INVIZ", "INVIZ_PT_panel"
    bl_space_type, bl_region_type, bl_category = 'VIEW_3D', 'UI', "INVIZ"

    def draw(self, ctx):
        l, p = self.layout, ctx.scene.inviz
        if not S["running"]:
            if not have_crypto():
                l.label(text="Step 1: one-time setup", icon='INFO')
                l.operator("inviz.install", icon='IMPORT')
            col = l.column(align=True)
            col.prop(p, "port")
            col.prop(p, "ip_override")
            col.prop(p, "web_dir")
            l.operator("inviz.start", icon='PLAY')
            return
        l.operator("inviz.stop", icon='PAUSE')
        box = l.box()
        box.label(text="Open on your phone:")
        box.label(text=S["url"])
        box.operator("inviz.copy", icon='COPYDOWN')
        live = time.time() - S["last_t"] < 1.0
        n = len(S["clients"])
        st = "LIVE" if live else ("Phone connected, no data" if n else ("STALE" if S["packets"] else "Waiting for phone"))
        l.label(text="Status: " + st, icon='CHECKMARK' if live else 'TIME')
        l.label(text="Rate: %.0f Hz   Packets: %d   Bad: %d" % (S["rate"], S["packets"], S["bad"]))
        if S["drop"]:
            l.label(text="Last disconnect: " + S["drop"][:60], icon='INFO')
        d = S["last"]
        if d:
            col = l.column(align=True)
            col.label(text="Pitch (beta):  %7.1f" % d["beta"])
            col.label(text="Yaw (alpha):   %7.1f" % d["alpha"])
            col.label(text="Roll (gamma):  %7.1f" % d["gamma"])
            col.label(text="Compass-referenced: %s" % ("yes" if d["abs"] else "no"))
        l.separator()
        col = l.column(align=True)
        col.prop(p, "camera")
        if p.camera is None:
            l.label(text="Pick the camera to record above", icon='ERROR')
        col.prop(p, "grip")
        col.prop(p, "sensitivity")
        col.prop(p, "smoothing")
        col.prop(p, "move_speed")
        row = l.row(align=True)
        row.prop(p, "lock_pitch", toggle=True)
        row.prop(p, "lock_yaw", toggle=True)
        row.prop(p, "lock_roll", toggle=True)
        col = l.column(align=True)
        col.prop(p, "preview")
        if p.preview:
            col.prop(p, "preview_width")
            col.prop(p, "preview_fps")
            l.label(text="Camera view: " + ("streaming to phone" if S["pv"] else "off (switch on in the phone page)"))
        if S["pv_msg"]:
            l.label(text=S["pv_msg"], icon='ERROR')
        if S["live"]:
            l.label(text="Controlling: " + S["cam"], icon='CAMERA_DATA')
            l.operator("inviz.calibrate", icon='ORIENTATION_GIMBAL')
            box = l.box()
            if S["rec"]:
                box.label(text="REC  %d keys  (frame %d)" % (S["rec"]["n"], ctx.scene.frame_current), icon='REC')
                box.operator("inviz.rec_stop", icon='PAUSE')
            else:
                box.prop(p, "rec_start")
                box.prop(p, "rec_step")
                box.prop(p, "rec_play")
                box.prop(p, "rec_use_end")
                box.label(text="Frame rate: %.2f fps (set in Output properties)"
                          % (ctx.scene.render.fps / ctx.scene.render.fps_base))
                box.operator("inviz.rec_start", icon='REC')
            if S["rmsg"]:
                box.label(text=S["rmsg"], icon='INFO')
            l.operator("inviz.live_off", icon='PAUSE')
        else:
            l.operator("inviz.live_on", icon='CAMERA_DATA')
        if S["msg"]:
            l.label(text=S["msg"], icon='ERROR')


classes = (INVIZ_Props, INVIZ_OT_install, INVIZ_OT_start, INVIZ_OT_stop, INVIZ_OT_copy,
           INVIZ_OT_live_on, INVIZ_OT_live_off, INVIZ_OT_calibrate, INVIZ_OT_rec_start, INVIZ_OT_rec_stop, INVIZ_PT_panel)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.inviz = bpy.props.PointerProperty(type=INVIZ_Props)


def unregister():
    stop_server()
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    del bpy.types.Scene.inviz
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
