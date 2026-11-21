# 🎬 sakrecubesdb - ReelShot

Reference Shots for Reels and YouTube Videos.

An ultra-responsive, mobile-first Progressive Web App (PWA) designed for travel filmmakers and content creators to reference shot breakdowns and replicate camera compositions on the go—even in remote areas with zero cellular connectivity.

---

## ✨ Features

- **📱 iOS Native Feel:** Minimalist dark mode aesthetic tailored for mobile screens with tactile haptic feedback.
- **⚡ 100% Offline via IndexedDB (`ReelShotDB`):** Avoids iOS Safari HTTP caching limitations by storing full GIF blobs directly inside IndexedDB.
- **📚 Master Library Catalog:** Browse all historical reels in chronological order, filter by search keywords, and selectively import only active projects to your trip board.
- **🎯 Dynamic Shot Progress:** Smooth checkmark animations that glide completed shots to the bottom of the stack to keep remaining frames front and center.
- **📥 In-App Download Ring:** Visual circular SVG progress indicator (`1/20`, `2/20`...) that smoothly fades away once all assets are saved offline.
- **🤖 Headless Ingestion Pipeline:** Automated scene cut detection and 640p GIF generation powered by FFmpeg.

---

## 🛠️ Tech Stack

- **Frontend:** Vanilla HTML5, CSS3, JavaScript (ES Modules, IndexedDB, Service Worker)
- **Deployment:** GitHub Pages (HTTPS, Global Fastly CDN)
- **Media CDN:** Cloudflare R2 (S3-compatible, $0 egress fees)
- **Processing Engine:** Raspberry Pi 3 (FFmpeg + `yt-dlp` + n8n automation)

---

## 🚀 Local Development

To run locally on your Mac or PC:

```bash
# Serve the repository root over HTTP
python3 -m http.server 8000
```

Open `http://localhost:8000` in your browser.
