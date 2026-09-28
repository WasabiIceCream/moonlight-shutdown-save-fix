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

Second patch (Moonlight 4.1.0+, added 2026-09-26): 4.1.0 adds a mixin on
`Identifier.<init>` (`ResourceLocationMixin.moonlight$validateValidNamespace`)
that throws "Some mod disabled vanilla id validation ... This is VERY BAD!"
whenever an Identifier with an invalid namespace is built, and
`Moonlight.commonInit` parses "#invalid:ResourceLocation" on purpose to trip
it at startup. AAA Particles (needed by Archaion) @Overwrites
`Identifier.isValidNamespace` to always return true, so the server crashed at
boot. No config switch exists. The handler body is

    aload_1; aload_2; invokestatic ResourceLocationSanityCheck.validateNamespace(String,String)V; return

and NOPing the first three (5 bytes) is stack-neutral, leaving `return`. This
restores 4.0.8's behaviour exactly (4.0.8 had no such check). Skipped with a
note when the class isn't present (older Moonlight).

Usage:
    python3 patch.py <input jar> <output jar>
"""
import sys
import zipfile
from pathlib import Path

# (class, [(pattern, replacement), ...], required). Each entry lists the known builds'
# byte patterns (the constant-pool index shifts between releases); exactly one must match.
PATCHES = [
    # aconst_null (0x01), putstatic currentServer -> 4x nop.
    # #192 (0x00C0) in 4.1.0, #193 (0x00C1) in 4.1.1.
    ("net/mehvahdjukaar/moonlight/platform/MoonlightFabric.class",
     [(bytes([0x01, 0xB3, 0x00, 0xC0]), bytes(4)),
      (bytes([0x01, 0xB3, 0x00, 0xC1]), bytes(4))], True),
    # aload_1, aload_2, invokestatic #7, return -> 5x nop, return
    ("net/mehvahdjukaar/moonlight/core/mixins/ResourceLocationMixin.class",
     [(bytes([0x2B, 0x2C, 0xB8, 0x00, 0x07, 0xB1]), bytes(5) + b"\xB1")], False),
]


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    src_jar, dst_jar = Path(sys.argv[1]), Path(sys.argv[2])

    with zipfile.ZipFile(src_jar) as zin:
        names = set(zin.namelist())
        patched = {}
        for cls, variants, required in PATCHES:
            if cls not in names:
                if required:
                    raise SystemExit(f"{cls} missing from the jar.")
                print(f"skip: {cls} not in this Moonlight version")
                continue
            data = zin.read(cls)
            hits = [(pat, rep) for pat, rep in variants if data.count(pat) == 1]
            if len(hits) != 1 or sum(data.count(pat) for pat, _ in variants) != 1:
                raise SystemExit(
                    f"Expected exactly one known pattern in {cls}, found none or several -- mod "
                    "was likely recompiled/updated, re-derive with `javap -p -c -constants`.")
            pattern, repl = hits[0]
            patched[cls] = data.replace(pattern, repl)
            print(f"patched: {cls}")
        with zipfile.ZipFile(dst_jar, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                zout.writestr(info, patched.get(info.filename, zin.read(info.filename)))

    print(f"Wrote {dst_jar}")


if __name__ == "__main__":
    main()
