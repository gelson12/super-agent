# JARVIS Super Agent — Mobile App Installation Guide

> Built with Flutter · Delivered via Super Agent's autonomous build pipeline

---

## Android Installation (APK Sideload)

### Prerequisites
- Android phone running Android 8.0 (Oreo) or newer
- The APK download link from Super Agent (`POST /build/mobile` response)

---

### Step 1 — Download the APK

1. Open the download link on your Android phone (use Chrome or the default browser)
2. Tap the download link — Chrome will warn you that this is an APK file
3. Tap **Download anyway**
4. The APK file will appear in your **Downloads** folder

---

### Step 2 — Enable Installation from Unknown Sources

> This only needs to be done once per app / browser

**Android 8.0 and above (per-app permission):**

1. When you tap the downloaded APK, Android will say *"For your security, your phone is not allowed to install unknown apps from this source"*
2. Tap **Settings**
3. Toggle **Allow from this source** ON
4. Tap the back arrow — the install prompt will reappear

**Samsung devices (One UI):**
- Settings → Biometrics and Security → Install Unknown Apps → Chrome → Allow

**Xiaomi / Poco devices:**
- Settings → Privacy → Special App Access → Install Unknown Apps → Chrome → Allow

---

### Step 3 — Install the App

1. Tap the downloaded APK file (find it in **Files → Downloads** or the browser notification)
2. Tap **Install**
3. Wait for the installation to complete (usually 10–30 seconds)
4. Tap **Open** to launch immediately, or **Done** to find it in your app drawer

---

### Step 4 — Grant Permissions

On first launch the app will request:
- **Microphone** — required for voice input ("Hey JARVIS")
- **Internet** — required to connect to Super Agent API

Tap **Allow** for both.

---

### Step 5 — Configure the App

On the Settings screen (gear icon in the top right):

| Setting | What to enter |
|---|---|
| **Server URL** | Your Railway app URL, e.g. `https://your-app.railway.app` |
| **Password** | Your `UI_PASSWORD` environment variable value |
| **ElevenLabs API Key** | Optional — for premium voice TTS (leave blank to use free TTS) |

Tap **Save**. The app will verify the connection.

---

### Troubleshooting (Android)

| Problem | Solution |
|---|---|
| "App not installed" error | Free up storage space (need ~100 MB free) |
| "Parse error" | Re-download the APK — the file may be corrupted |
| Microphone not working | Settings → Apps → JARVIS → Permissions → Microphone → Allow |
| Can't connect to server | Check the Server URL in Settings — include `https://` |
| Voice wake word not triggering | Speak clearly: "Hey JARVIS" — hold phone 20–30 cm from mouth |

---

---

## iOS Installation (AltStore — No Apple Developer Account Required)

> iOS does not allow installing apps outside the App Store without sideloading.
> AltStore is the easiest free method — no developer account, no jailbreak.

### Prerequisites
- iPhone or iPad running iOS 14 or newer
- A Windows PC or Mac (needed once to install AltStore)
- The IPA download link from the GitHub Actions build

---

### Part A — Install AltStore on Your Computer

**Windows:**
1. Download **AltServer for Windows** from [altstore.io/downloads](https://altstore.io/downloads)
2. Run the installer
3. AltServer appears in the system tray (bottom right of taskbar)

**Mac:**
1. Download **AltServer for Mac** from [altstore.io/downloads](https://altstore.io/downloads)
2. Move AltServer to Applications
3. Open AltServer — it appears in the menu bar

---

### Part B — Install AltStore on Your iPhone

1. Connect your iPhone to your computer with a USB cable
2. Open **iTunes** (Windows) or trust the device if prompted on Mac
3. Click the AltServer icon in the tray/menu bar
4. Click **Install AltStore** → select your iPhone
5. Enter your Apple ID and password when prompted *(AltServer uses this only to sign the app — Apple is NOT notified)*
6. AltStore will appear on your iPhone home screen

---

### Part C — Install the JARVIS IPA

1. On your iPhone, open **Safari** and navigate to the IPA download link
2. Tap the share icon → **Open in AltStore**
   - *Alternative:* Download the IPA to iCloud Drive or Files, then open AltStore → My Apps → + → navigate to the IPA
3. AltStore will sign and install the app (takes ~30 seconds)
4. Trust the developer: **Settings → General → VPN & Device Management → [your Apple ID] → Trust**

---

### Part D — Keep the App Running (AltStore Refresh)

> Sideloaded iOS apps expire every 7 days without an Apple Developer account.
> AltStore can auto-refresh when your phone and computer are on the same Wi-Fi.

1. Keep AltServer running on your computer
2. Connect iPhone and computer to the same Wi-Fi
3. AltStore auto-refreshes apps in the background — you'll get a notification

---

### Troubleshooting (iOS)

| Problem | Solution |
|---|---|
| "Untrusted Developer" error | Settings → General → VPN & Device Management → Trust |
| App crashes on launch | Re-install via AltStore — the signing certificate may have expired |
| AltStore can't find iPhone | Ensure iTunes/Finder recognises the device; try a different cable |
| IPA install fails in AltStore | Check AltStore has free app slots (free Apple ID allows 3 sideloaded apps) |
| Microphone permission denied | Settings → Privacy & Security → Microphone → JARVIS → ON |

---

---

## Updating the App

When Super Agent builds a new version:
1. A new download link will be provided
2. **Android:** Download and install the new APK — it updates automatically (same package name)
3. **iOS:** Open AltStore → My Apps → swipe left on JARVIS → Remove, then install the new IPA

---

## App Features

| Feature | Description |
|---|---|
| Wake word | Say **"Hey JARVIS"** to activate hands-free |
| Voice input | Speak your question after activation |
| Text input | Type in the chat box as usual |
| Voice responses | JARVIS speaks answers back (ElevenLabs or free TTS) |
| Session memory | Conversations persist across sessions |
| Multi-model | Routes to Claude / Gemini / DeepSeek automatically |

---

*Guide generated by Super Agent · `GET /install-guide` for the latest version*
