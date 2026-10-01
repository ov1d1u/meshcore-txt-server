# MeshCore Teletext server

This Python service sends Markdown pages over MeshCore direct messages through a USB companion. It is an independent project: install and run it from this directory. The companion used by the Android Teletext app must have compatible radio settings and be able to exchange direct messages with the server companion.

## Requirements and installation

- Python 3.10 or newer
- A MeshCore USB companion and its serial port
- `meshcore==2.3.14` (installed with the package)

From the server directory:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
```

On Windows, activate the virtual environment and run `python -m pip install -e .` instead.

## Configuration and running

Edit `config.ini` before starting the service:

```ini
[server]
node_name = Teletext
```

The service adds `-txt` unless the name already ends with that suffix, sets the companion's node name, and requests a flood advertisement. The completed name must fit in 23 UTF-8 bytes so it remains intact in advertisements with location data. The name and advertisement are applied again after a USB reconnect. Use `--config /path/to/config.ini` to select another file.

Start the service from this directory so the default content and configuration paths resolve here:

```sh
.venv/bin/meshcore-teletext --port /dev/ttyACM0 --content content
```

On macOS the port usually looks like `/dev/cu.usbmodem…` or `/dev/cu.usbserial…`. Run one service process per serial companion. Another instance using that port is refused; the service retries if the companion does not answer the serial handshake or disconnects.

The service accepts direct requests from any sender the companion can deliver. Configure the server companion to auto-add Chat contacts, or add the Android companion's full public key as a contact. The Android app sends a flood advertisement on connection so the server companion can learn its key. Hearing the server advertisement on Android alone is insufficient for encrypted direct messages. There is no application allowlist or password.

## Pages

Edit `content/index.md` for page 100, the curated index. Put other pages in `content/pages/NNN.md`, where `NNN` is 101 through 999. Page 100 always uses `content/index.md`, even if `content/pages/100.md` exists. The index is sent exactly as authored and does not automatically list the directory. Add links such as `[101 Welcome](page:101)` for tappable page numbers in the Android app. Pages absent from the index remain reachable by entering their number. File edits take effect on the next request.

The service snapshots each requested file to temporary storage before transmission, so an edit cannot mix old and new chunks. It serves one transfer at a time and replies `BUSY` to other clients. Large pages can consume substantial airtime and temporary disk space.

## Protocol and compatibility

See [PROTOCOL.md](PROTOCOL.md) for the T1 frame format, chunking, compression, repair, and checksum rules. The `fixtures/protocol.tsv` file captures examples shared with the Android project. Keep the fixture and protocol documentation synchronized across repositories. Update the server and Android app together when changing compressed frames.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The tests cover configuration, content paths, Unicode chunk boundaries, malformed frames, protocol fixtures, and simulated server sends. A full USB-to-radio-to-BLE test requires two compatible companions and an Android device.
