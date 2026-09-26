#!/usr/bin/env python3
"""
Patches Moonlight (https://modrinth.com/mod/moonlight) to fix a real shutdown
crash on any world using Create's Train Station system.

Bug: on server shutdown, `MinecraftServer.stopServer()` fires Fabric API's
`ServerLifecycleEvents.SERVER_STOPPING` near the very start of the method --
well before it later calls `saveAllChunks()` to write the final shutdown
save. Moonlight's own SERVER_STOPPING handler
(`MoonlightFabric.lambda$onInitialize$2`) immediately sets its static
`currentServer` field to null. By the time `saveAllChunks()` actually runs,
`MoonlightFabric.getCurrentServer()` (reached via
`PlatHelperImpl.getCurrentServer()` -> `PlatHelper.getCurrentServer()`) is
already null.

`Utils.hackyGetRegistryAccess()` (aptly named -- Moonlight's own source
calls it "hacky") uses that server reference as its primary way to get
`RegistryAccess` on the dedicated-server side. With it null, and no
"early registry access" ThreadLocal populated for the shutdown-save thread
either, it falls through to its last resort: throwing
`UnsupportedOperationException("Failed to get registry access. This is a
bug")`.

Any mod that saves `SavedData` through Moonlight's map-data serialization
during that final shutdown save hits this. Confirmed here via Create's
Train Station Marker system (`StationMarker$WrapperCodec.encode()`, which
saves through `MapDataInternal.saveCustomData()`), reproduced 4 times in
one session on a world with zero actual Train Stations placed -- meaning
this isn't tied to real station data, it's an unconditional part of
Create's own save routine whenever its Train module is loaded at all.
`SavedDataStorage.collectDirtyTagsToSave` iterates every dirty SavedData
entry in one `HashMap.forEach`; an uncaught exception from any single
entry's encode() aborts the whole save call, so this can in principle
block *other*, unrelated saved data from writing during that same
shutdown save too -- a real data-safety concern, not just a log spam
annoyance. In practice on this server, `stopServer()`'s own outer
try/catch logs "Exception stopping the server" and swallows it, but
something downstream of that never completes, leaving the JVM process
alive instead of exiting (confirmed reproducible: RCON stops responding
and the process needs a force-kill every time this fires).

Fix: per this project's established bytecode-patching convention (see
`spyglass-astronomy-crash-fix` and memory `mod-dedicated-server-bytecode-
patching`), inserting new logic isn't safe to hand-patch directly, but
*removing* a call/assignment is safe as long as it's stack-neutral. The
whole bug traces to one instruction pair:

    aconst_null                          (push 1)
    putstatic  MoonlightFabric.currentServer   (pop 1)

aconst_null pushes exactly what putstatic pops, so NOPing out both (4
bytes -> 4x 0x00) leaves a stack-neutral, still-valid method body. The
rest of the SERVER_STOPPING handler (FakeLevelManager.invalidateAll(),
SidedInstance.clearAll()) is untouched and still runs normally. The only
behavioral change: `currentServer` keeps its last real value through the
brief shutdown window instead of being nulled at the very start of it,
which is exactly what lets `hackyGetRegistryAccess()` succeed via its
first, non-throwing code path (`server.registryAccess()`) instead of
falling through to its last-resort throw. The process is exiting shortly
after regardless, so nothing else depends on `currentServer` reading null
during that window.

This 2-byte `aconst_null putstatic` sequence occurs exactly once in the
whole class file, confirmed before patching.

Usage:
    python3 patch.py <input jar> <output jar>
"""
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

CLASS_PATH = "net/mehvahdjukaar/moonlight/platform/MoonlightFabric.class"
# aconst_null (0x01), putstatic #192 (0xB3 0x00 0xC0)
PATTERN = bytes([0x01, 0xB3, 0x00, 0xC0])


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    src_jar, dst_jar = Path(sys.argv[1]), Path(sys.argv[2])

    with zipfile.ZipFile(src_jar) as zf:
        class_bytes = bytearray(zf.read(CLASS_PATH))

    idx = class_bytes.find(PATTERN)
    if idx == -1:
        raise SystemExit(
            f"Expected byte pattern not found in {CLASS_PATH} -- "
            "mod was likely recompiled/updated, re-derive the offset "
            "with `javap -p -c -constants` before patching a new jar."
        )
    if class_bytes.count(PATTERN) != 1:
        raise SystemExit("Pattern is not unique in the class file, refusing to guess.")

    for i in range(idx, idx + 4):
        class_bytes[i] = 0x00

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        class_dir = tmp / "net/mehvahdjukaar/moonlight/platform"
        class_dir.mkdir(parents=True)
        (class_dir / "MoonlightFabric.class").write_bytes(class_bytes)

        dst_jar.write_bytes(src_jar.read_bytes())
        subprocess.run(["zip", str(dst_jar.resolve()), CLASS_PATH], cwd=tmp, check=True)

    print(f"Patched {dst_jar}")


if __name__ == "__main__":
    main()
