# photo-share

Self-hosted event photo sharing. Make an event, put photos in it, send guests a link (or QR code).
Guests can browse by day, see when each shot was taken, view full screen, and download one photo, a selection, or everything as a zip.

## Run
```bash
cp .env.example .env     # set ADMIN_PASSWORD, SECRET_KEY, PUID/PGID (run `id`)
sudo chown -R $(id -u):$(id -g) /mnt/fpv2tb/event-photos
docker compose up -d --build
```
The app listens on `127.0.0.1:8100`. The Cloudflare tunnel points `share.tamairnv.co.uk` at it. Admin is at `/admin`.

## Features
**Guests**
- Photos grouped by day and sorted by time taken (EXIF for photos, recording time for videos, otherwise file date)
- Full screen viewer with swipe/arrow keys and the capture time
- Download one, **select several and download as a zip**, select a whole day, or download everything
- Optional: password and expiry date

**Uploader links**
- Make a named upload link per person (e.g. one for Jess, one for Tom). They can add photos; the normal share link stays view-only
- Every photo gets its uploader's colour as a border, with a legend to filter by person (your own uploads/rsync show as `OWNER_NAME`)
- Turn a link off any time; their photos stay tagged
- Upload links work even if the public link is off or password protected, and live at `/u/...` (outside `/admin`, so not behind Cloudflare Access)

**Admin**
- Upload in the browser with progress bars, 3 files at a time
- Big files (videos, RAW) are sent in 50MB chunks, so Cloudflare's 100MB per request limit is never hit
- Copy-paste **rsync command** for dumping an SD card / DJI / GoPro straight to the server over your LAN, plus a Rescan button
- Cover photo, description, date, sort order, QR code, view and download counts
- Delete single photos or select a bunch and delete them
- Turn a link off, regenerate it, or delete the whole event

## Storage
- Photos are plain files in `PHOTOS_PATH/events/<event>/`, subfolders included. No lock-in.
- Copy files in any way you like (rsync, scp, Samba). New folders become events automatically.
- Event settings live in `PHOTOS_PATH/events.json`, who-uploaded-what in `PHOTOS_PATH/.sources/`. Thumbnails cache in `PHOTOS_PATH/.cache` (safe to delete).
- Formats: jpg, png, webp, gif, heic, tiff, mp4, mov, webm.

## Update
```bash
git pull && docker compose up -d --build
```
