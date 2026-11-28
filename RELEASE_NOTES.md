# Release Notes — sakrecubesdb (ReelShot)

All notable changes across releases are documented in this file.
From `v7.0` onward, releases follow `major.minor` versioning.

---

## [v7.2] — 2026-11-28
### Added
- **Instagram Carousel & Single Photo Support (UI + Pipeline):**
  - **Feed Carousel Viewer (`index.html`):** Horizontal swipeable carousel track with `scroll-snap-type: x mandatory`, desktop prev/next arrows, slide counter (`1 / N`), pagination dots, `📸 N Photos` pill, and autoplay for mixed video slides.
  - **Photo Checklist Detail View:** Opens carousels/photos without the Reference Video player, supporting tick-mark completion (`0 / N Photos`) and Save to Collection bookmarks with image previews.
  - **3xN Grid & Collections Support:** Displays stacked-cards carousel badges with photo counts in the Grid and renders saved carousel photos as `<img>` in Collections.
  - **Ingestion Pipeline (`scripts/ingest_reel.py` & `scripts/queue_manager.py`):** Auto-detects Instagram carousels (`media_type == 8`) and single photos (`media_type == 1`), downloads slides, transcodes mixed-carousel video slides to iOS-compatible H.264+AAC, and syncs to Cloudflare R2.
- **Home & Grid Tick Progress Indicators:**
  - Added live `X/Y | Z% Done` progress indicator below the `✂️ Scenes` / `📸 Photos` pill on Feed cards.
  - Added completion percentage (`50%`) below the scene/photo count badge on 3xN Grid cards.

### Changed & Fixed
- **iOS WebKit Carousel & Modal Image Rendering:** Forced GPU layer compositing (`translateZ(0)`) and eager image decoding for carousel slides and Library/Sheet modal thumbnails so images render cleanly on iPhone Safari.
- **Trip Board Removal (`toggleReelActive`):** Implemented `toggleReelActive()` so "Remove from Trip Board" in the Feed options menu removes the item, purges its offline cache, and syncs to R2.
- **Header & Library UI Polish:** Replaced `Manage`/`Done` text buttons in the Library modal with Material gear and checkmark icon buttons, and removed the redundant gear icon from the main top header.

---

## [v7.1] — 2026-11-28
### Added
- **Exact Instagram Metrics (`play_count` + `like_count`):** Updated `scripts/ingest_reel.py` with `shortcode_to_pk()` and Instagram media info API extraction to capture real reel view counts (`play_count` / `ig_play_count`) alongside `like_count`.
- **Dual Metrics Display:** Updated the Reels Feed creator row (`index.html`) to display both **View Count (`👁️`)** and **Like Count (`❤️`)** pills side-by-side.
- **Full Caption & Chrome Cookie Fallback:** Added full Instagram caption extraction and automatic `--cookies-from-browser chrome` fallback in `ingest_reel.py` for reels that fail anonymous metadata/media fetches.
- **Clean Catalog Ingestion:** Wiped legacy R2/local database and ingested 6 reels with full captions, view counts, and like counts.

### Fixed
- **iOS Safari / WebKit Video Playback & Autoplay:**
  - Transcoded all videos on Cloudflare R2 and in `ingest_reel.py` to Apple-compatible **H.264 (`libx264`, `yuv420p`, High Profile) + AAC (`+faststart`)**, resolving the crossed-out play icon (`AVFoundationErrorDomain Code=-11828`) caused by VP9 MP4 streams.
  - Updated `sw.js` (bumped to `v5`) to bypass `.mp4` fetch interception so iOS Safari HTTP `206` byte-range requests stream natively from R2.
  - Enforced `playsinline`, `webkit-playsinline`, `muted`, and added `initIOSMediaUnlock()` for reliable mobile autoplay and audio unlocking.
- **Collections Tab Thumbnails:** Replaced autoplaying video elements on Collection cards with static cover thumbnails.

---

## [v7.0] — 2026-11-28
### Added
- **Instagram-Style Reels Feed (Home):** Overhauled the Home active trip board into an immersive 9:16 vertical snap-scrolling Reels Feed with autoplay, mute/unmute toggle, tap-to-pause, and press-and-hold **2X Speed** playback.
- **Left-Swipe to Split Scenes:** Swiping left on any reel in the feed opens its exact scene breakdown view.
- **3xN Grid Mode Toggle:** Added a header toggle to switch between the Reels Feed and a compact 3xN static thumbnail grid.
- **Reel Options Menu:** Added a three-dots options menu with a direct link to the original Instagram reel.

### Fixed
- **2X Speed Restoration:** Ensured releasing touch/mouse, `touchcancel`, or scrolling reliably restores playback speed back to `1.0x`.

---

## [v6.5] — 2026-11-27
### Changed
- **Material Symbols UI Polish:** Adopted Google Material Symbols theme for favorites and long-press context menus, removing the shaded emoji star and circular badge.

---

## [v6.4] — 2026-11-27
### Added
- **Collection Sorting & Pinning:** Added default alphabetical sorting for Saved Collections and the ability to favorite collections so they stay pinned at the top.
- **Home Long-Press Context Menu:** Added long-press actions on collection cards (`Add to Favorite`, `Archive`, `Delete Collection`).

---

## [v6.3] — 2026-11-26
### Added
- **Multi-Device Cloud Sync:** Added Cloudflare R2 cloud synchronization for the active Trip Board and Saved Collections.

---

## [v6.2] — 2026-11-26
### Added
- **Saved Collections:** Added Instagram-style Saved Collections with a 3xN home grid, save overlay sheet, and automatic cleanup on deletion.

### Fixed
- Resolved target reel shots resolution in the Save Sheet.

---

## [v6.0 – v6.1] — 2026-11-25
### Changed
- **MP4-Only Standardization:** Standardized exclusively on MP4 video clips, removed legacy GIF fallbacks, and reset to a clean catalog.

---

## [v5.0 – v5.2] — 2026-11-25
### Added
- **MP4 (GIFV) Migration:** Migrated scene clips from GIF to 640p looping MP4 clips with `IntersectionObserver` playback (`v5`).
- **Master Catalog Sync:** Synced `data/reels.json` with live R2 (`v5.1`).
- **Partial Scene Deletion:** Added partial split-scene deletion modal in `manage.html` and CLI support with R2/JSON updates (`v5.2`).

---

## [v4.0] — 2026-11-25
### Changed
- **480p Default Resolution:** Added `--resolution` ingestion option (`640p`, `480p`, `360p`) and set `480p` as the global default across ingestion and export pipelines.

---

## [v3.0] — 2026-11-23
### Added
- **Fast oEmbed Metadata & Telegram Formatting:** Added fast oEmbed metadata discovery and rich Telegram link formatting.

---

## [v2.0] — 2026-11-23
### Added
- **Background Ingestion Queue:** Added `scripts/queue_manager.py` for FIFO background reel processing with real-time `journalctl` logging (`flush=True`).

---

## [v1.0] — 2026-11-22
### Added
- **Offline Video & Storage Tracking:** Included full reference video in offline download counts, added package size (`MB`) tracking across the PWA and `manage.html`, and used unique reel shortcodes in IDs to prevent collisions.

---

## [v0.0] — 2026-11-21 – 2026-11-22
### Added
- Initial **ReelShot (`sakrecubesdb`)** PWA with Master Catalog, IndexedDB offline storage (`ReelShotDB`), Service Worker caching, Cloudflare R2 CDN integration, `ingest_reel.py` FFmpeg scene-cut pipeline, `manage.html`, and `delete_reel.py`.
