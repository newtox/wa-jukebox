# wa-jukebox

A Spotify Connect speaker for WorkAdventure. It shows up in your Spotify app as a device ("WorkAdventure Jukebox");
whatever you play on it is streamed as MP3 over HTTP, so an area or a jukebox in WorkAdventure can play it for everyone
standing nearby. Like a jam session: one person with Spotify Premium picks the music, everyone hears it.

```
Spotify app ──(Connect)──▶ librespot ──PCM──▶ pacer ──▶ ffmpeg (MP3) ──HTTP──▶ WorkAdventure players
```

- The stream never breaks off: while nothing plays, listeners get silence, and the music comes back as soon as you press play.
- A few seconds behind the app, like a radio: the jukebox itself adds well under a second, the rest is the browser's buffer.
- Needs a **Spotify Premium** account (librespot only works with Premium).

Note: Spotify's terms don't allow rebroadcasting its music. Keep the stream private (a hard-to-guess `STREAM_PATH`) and
for a few friends only.

## Running

```yaml
services:
  jukebox:
    image: ghcr.io/newtox/wa-jukebox:latest
    container_name: wa-jukebox
    restart: unless-stopped
    environment:
      DEVICE_NAME: "WorkAdventure Jukebox"
      STREAM_PATH: "<random, e.g. from: openssl rand -hex 12>"
      INITIAL_VOLUME: "80"
    ports:
      - "127.0.0.1:8099:8000"
    volumes:
      - /docker_volumes/wa-jukebox:/data
```

Reverse proxy (Caddy), inside the WorkAdventure site block, so no extra domain is needed:

```
handle_path /jukebox/* {
    reverse_proxy 127.0.0.1:8099
}
```

The stream is then at `https://<your WorkAdventure domain>/jukebox/<STREAM_PATH>`.

### Logging in to Spotify (once)

```sh
docker exec -it wa-jukebox jukebox-login
```

Open the printed link, log in with the Premium account, then copy the address of the page that fails to load
(`http://127.0.0.1/login?code=...`) back into the terminal. When it says "Authenticated", press Ctrl+C.
The login is saved in `/data`; the jukebox starts by itself a few seconds later.

### In WorkAdventure

Map editor → draw an area around the jukebox → property **Play audio file** → the stream URL.
Everyone who walks in hears the music, walking out stops it. Then pick "WorkAdventure Jukebox" as the device in Spotify.

## Settings

| Variable | Default | |
|---|---|---|
| `DEVICE_NAME` | `WorkAdventure Jukebox` | Name in the Spotify device list |
| `STREAM_PATH` | `stream` | Path of the stream; make it hard to guess |
| `MP3_BITRATE` | `192k` | Bitrate of the stream |
| `SPOTIFY_BITRATE` | `320` | Quality fetched from Spotify (96, 160, 320) |
| `INITIAL_VOLUME` | `80` | Volume in % after a start |
| `VOLUME_CTRL` | `fixed` | `fixed`: the Spotify volume slider does nothing, every listener sets their own volume in WorkAdventure. `log`: the slider changes the volume for everyone (a few seconds late) |
| `PORT` | `8000` | HTTP port inside the container |

`GET /health` tells whether the jukebox is logged in and how many people listen.

## License

MIT. librespot is MIT licensed, ffmpeg is LGPL/GPL (installed from Debian).
