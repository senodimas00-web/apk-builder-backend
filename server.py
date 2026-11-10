#!/usr/bin/env python3
import os, json, shutil, subprocess, tempfile, uuid, base64, io, time, zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from PIL import Image, ImageDraw, ImageFont

# ================= KONFIGURASI =================
BUILD_DIR = "/tmp/apkbuild"
OUT_DIR   = "/tmp/apkout"
ANDROID_HOME = os.getenv("ANDROID_HOME", "/opt/android-sdk")
BUILD_TOOLS = os.path.join(ANDROID_HOME, "build-tools", "34.0.0")
PLATFORM_JAR = os.path.join(ANDROID_HOME, "platforms", "android-34", "android.jar")

AAPT2 = os.path.join(BUILD_TOOLS, "aapt2")
D8 = os.path.join(BUILD_TOOLS, "d8")
ZIPALIGN = os.path.join(BUILD_TOOLS, "zipalign")
APKSIGNER = os.path.join(BUILD_TOOLS, "apksigner")
KEYSTORE = os.path.join(OUT_DIR, "debug.keystore")

os.makedirs(BUILD_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

# Generate Keystore standar Android
if not os.path.exists(KEYSTORE):
    print("[*] Membuat keystore...")
    subprocess.run(["keytool", "-genkey", "-v", "-keystore", KEYSTORE, "-storepass", "android",
                    "-alias", "androiddebugkey", "-keypass", "android", "-keyalg", "RSA",
                    "-keysize", "2048", "-validity", "10000", "-dname", "CN=Android Debug,O=Android,C=US"],
                   capture_output=True)

def generate_logo(app_name, size=192):
    img = Image.new("RGBA", (size, size), (15, 23, 42, 255))
    d = ImageDraw.Draw(img)
    initials = "".join(w[0] for w in app_name.split()[:2]).upper() or "A"
    try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", int(size * 0.45))
    except: font = ImageFont.load_default()
    bbox = d.textbbox((0, 0), initials, font=font)
    d.text(((size - (bbox[2]-bbox[0])) / 2, (size - (bbox[3]-bbox[1])) / 2 - bbox[1]), initials, fill=(56, 189, 248, 255), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def build_apk_direct(p, log_callback):
    start_time = time.time()
    d = tempfile.mkdtemp(dir=BUILD_DIR)
    
    try:
        log_callback("Menyiapkan folder project...")
        pkg = p.get("package", "com.build.app")
        pkg_path = pkg.replace(".", "/")
        java_dir = os.path.join(d, "src", pkg_path)
        res_dir = os.path.join(d, "res")
        mipmap_dir = os.path.join(res_dir, "mipmap-xxxhdpi")
        assets_dir = os.path.join(d, "assets")
        for folder in [java_dir, mipmap_dir, assets_dir]: os.makedirs(folder, exist_ok=True)

        log_callback("Menulis file konfigurasi & HTML...")
        with open(os.path.join(d, "AndroidManifest.xml"), "w") as f:
            f.write(f'''<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="{pkg}">
    <uses-permission android:name="android.permission.INTERNET"/>
    <application android:label="{p.get("app_name", "App")}" android:icon="@mipmap/ic_launcher" android:usesCleartextTraffic="true">
        <activity android:name=".MainActivity" android:exported="true">
            <intent-filter><action android:name="android.intent.action.MAIN"/><category android:name="android.intent.category.LAUNCHER"/></intent-filter>
        </activity>
    </application>
</manifest>''')

        with open(os.path.join(java_dir, "MainActivity.java"), "w") as f:
            f.write(f'''package {pkg};
import android.app.Activity; import android.os.Bundle; import android.webkit.WebView; import android.webkit.WebViewClient; import android.webkit.WebSettings;
public class MainActivity extends Activity {{
    @Override protected void onCreate(Bundle b) {{
        super.onCreate(b); WebView w = new WebView(this); WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true); s.setDomStorageEnabled(true); s.setAllowFileAccess(true);
        w.setWebViewClient(new WebViewClient()); w.loadUrl("file:///android_asset/index.html"); setContentView(w);
    }}
}}''')

        with open(os.path.join(assets_dir, "index.html"), "w") as f:
            f.write(p.get("html", "<h1>Blank</h1>"))

        logo_bytes = base64.b64decode(p["logo_b64"]) if p.get("logo_b64") else generate_logo(p.get("app_name", "App"))
        with open(os.path.join(mipmap_dir, "ic_launcher.png"), "wb") as f: f.write(logo_bytes)
        
        with open(os.path.join(res_dir, "values.xml"), "w") as f:
            f.write(f'<resources><string name="app_name">{p.get("app_name", "App")}</string></resources>')

        log_callback("Compile Resource XML (aapt2)...")
        compiled_res = os.path.join(d, "compiled.zip")
        subprocess.run([AAPT2, "compile", "--dir", res_dir, "-o", compiled_res], check=True, capture_output=True)

        log_callback("Link Resource & Manifest (aapt2)...")
        base_apk = os.path.join(d, "base.apk")
        subprocess.run([AAPT2, "link", "-o", base_apk, "-I", PLATFORM_JAR, 
                        "--manifest", os.path.join(d, "AndroidManifest.xml"),
                        "--java", os.path.join(d, "gen"), compiled_res], check=True, capture_output=True)

        log_callback("Compile Java ke .class (javac)...")
        classes_dir = os.path.join(d, "classes")
        os.makedirs(classes_dir, exist_ok=True)
        java_files = [os.path.join(java_dir, "MainActivity.java"), os.path.join(d, "gen", pkg_path, "R.java")]
        subprocess.run(["javac", "-source", "1.8", "-target", "1.8", "-cp", PLATFORM_JAR, "-d", classes_dir] + java_files, check=True, capture_output=True)

        log_callback("Konversi .class ke .dex (d8)...")
        dex_dir = os.path.join(d, "dex")
        os.makedirs(dex_dir, exist_ok=True)
        class_files = []
        for root, _, files in os.walk(classes_dir):
            for file in files:
                if file.endswith(".class"): class_files.append(os.path.join(root, file))
        
        if class_files:
            subprocess.run([D8, "--output", dex_dir, "--lib", PLATFORM_JAR] + class_files, check=True, capture_output=True)

        log_callback("Menggabungkan file APK & Assets...")
        unsigned_apk = os.path.join(d, "unsigned.apk")
        shutil.copy(base_apk, unsigned_apk)
        
        with zipfile.ZipFile(unsigned_apk, 'a', zipfile.ZIP_DEFLATED) as z:
            if os.path.exists(os.path.join(dex_dir, "classes.dex")):
                z.write(os.path.join(dex_dir, "classes.dex"), "classes.dex")
            for root, _, files in os.walk(assets_dir):
                for file in files:
                    filepath = os.path.join(root, file)
                    z.write(filepath, os.path.relpath(filepath, d))

        log_callback("Optimasi ukuran (zipalign)...")
        aligned_apk = os.path.join(d, "aligned.apk")
        subprocess.run([ZIPALIGN, "-f", "4", unsigned_apk, aligned_apk], check=True, capture_output=True)

        log_callback("Tanda tangan digital APK (apksigner)...")
        final_apk = os.path.join(OUT_DIR, f"{uuid.uuid4().hex}.apk")
        subprocess.run([APKSIGNER, "sign", "--ks", KEYSTORE, "--ks-pass", "pass:android", 
                        "--key-pass", "pass:android", "--out", final_apk, aligned_apk], check=True, capture_output=True)

        size_kb = int(os.path.getsize(final_apk) / 1024)
        log_callback(f"Build Selesai! ({time.time() - start_time:.2f} detik)")
        return final_apk, size_kb

    finally:
        shutil.rmtree(d, ignore_errors=True)

# ================= HTTP HANDLER (STREAMING) =================
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/build": return
        ln = int(self.headers.get("Content-Length", 0))
        p = json.loads(self.rfile.read(ln))
        
        if not p.get("html"):
            self.send_response(400); self.end_headers(); self.wfile.write(b'{"ok":false,"error":"No HTML"}'); return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def log_callback(msg):
            line = f"[LOG] {msg}\n"
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()

        try:
            apk_path, size = build_apk_direct(p, log_callback)
            filename = os.path.basename(apk_path)
            final_msg = f"[DONE] {json.dumps({'ok': True, 'download': f'/dl/{filename}', 'size': f'{size} KB'})}\n"
            self.wfile.write(final_msg.encode("utf-8"))
            self.wfile.flush()
        except Exception as e:
            error_msg = f"[FATAL] {str(e)}\n"
            self.wfile.write(error_msg.encode("utf-8"))
            self.wfile.flush()

    def do_GET(self):
        if self.path.startswith("/dl/"):
            fn = self.path.split("/")[-1]
            path = os.path.join(OUT_DIR, fn)
            if not os.path.exists(path):
                self.send_response(404); self.end_headers(); return
            with open(path, "rb") as f: data = f.read()
            self.send_response(200); self.send_header("Content-Type", "application/vnd.android.package-archive")
            self.send_header("Content-Disposition", f'attachment; filename="{fn}"')
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        else:
            self.send_response(200); self.end_headers(); self.wfile.write(b"Server APK Builder Aktif!")

    def log_message(self, *a): pass

if __name__ == "__main__":
    print("[*] Server berjalan di port 8000 (Streaming Mode)")
    HTTPServer(("0.0.0.0", 8000), H).serve_forever()
