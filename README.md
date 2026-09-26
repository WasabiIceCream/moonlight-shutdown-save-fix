# Moonlight Shutdown Save Fix

Bytecode patch for [Moonlight](https://modrinth.com/mod/moonlight)
(`fabric 26.1/mods/moonlight-26.1.2-4.0.8-fabric.jar`), fixing a real `/stop`
crash that hits any world with Create's Train module loaded, station placed
or not.

## The bug

`MinecraftServer.stopServer()` fires Fabric API's
`ServerLifecycleEvents.SERVER_STOPPING` near the start of the method, well
before it later calls `saveAllChunks()` for the actual final shutdown save.
Moonlight's own `SERVER_STOPPING` handler
(`MoonlightFabric.lambda$onInitialize$2`) immediately nulls its static
`currentServer` field. By the time `saveAllChunks()` runs, that field is
already null.

`Utils.hackyGetRegistryAccess()` (Moonlight's own source literally calls it
"hacky") uses that server reference as its primary way to get
`RegistryAccess` server-side. With it null, and no early-registry-access
`ThreadLocal` populated for the shutdown-save thread either, it falls
through to its last resort:

```
throw new UnsupportedOperationException("Failed to get registry access. This is a bug");
```

Any mod saving `SavedData` through Moonlight's map-data serialization during
that final save hits this. Confirmed here via Create's Train Station Marker
system (`StationMarker$WrapperCodec.encode()`, which saves through
`MapDataInternal.saveCustomData()`) — reproduced 4 times in one session on a
world with **zero actual Train Stations placed**, confirming it's an
unconditional part of Create's own save routine whenever the Train module
is loaded at all, not tied to real station data.

`SavedDataStorage.collectDirtyTagsToSave` iterates every dirty `SavedData`
entry in one `HashMap.forEach` — an uncaught exception from any single
entry's `encode()` aborts the whole save call, so in principle this can
block *other*, unrelated saved data from writing during that same shutdown
save. `stopServer()`'s own outer try/catch logs "Exception stopping the
server" and swallows it, but something downstream never completes: the JVM
process stays alive instead of exiting, RCON stops responding, and the
process needs a force-kill every time. Confirmed reproducible 4 times in one
session.

## The fix

Per this project's established bytecode-patching convention (see
`spyglass-astronomy-crash-fix` and memory `mod-dedicated-server-bytecode-
patching`), inserting new logic isn't safe to hand-patch, but *removing* a
call/assignment is safe as long as it's stack-neutral. The whole bug traces
to one instruction pair in the `SERVER_STOPPING` lambda:

```
aconst_null                                ; push 1
putstatic  MoonlightFabric.currentServer   ; pop 1
```

`aconst_null` pushes exactly what `putstatic` pops, so NOPing out both
(4 bytes -> four `0x00`s) leaves a stack-neutral, still-valid method body.
The rest of the handler (`FakeLevelManager.invalidateAll()`,
`SidedInstance.clearAll()`) is untouched and still runs. The only
behavioral change: `currentServer` keeps its last real value through the
brief shutdown window instead of being nulled at the very start of it -
exactly what lets `hackyGetRegistryAccess()` succeed via its first,
non-throwing path (`server.registryAccess()`) instead of falling through to
the throw. The process exits shortly after regardless, so nothing else
depends on `currentServer` reading null during that window.

## Usage

```
python3 patch.py <input jar> <output jar>
```

The `aconst_null putstatic` sequence occurs exactly once in the class file,
confirmed before patching. If Moonlight ever gets updated, re-derive the
byte offset with `javap -p -c -constants` against the new
`MoonlightFabric.class` before assuming this pattern still matches - check
upstream first too, since Moonlight's own source already flags
`hackyGetRegistryAccess` as fragile and may fix the underlying ordering
issue properly at some point.

## Where the patched jar lives

`fabric 26.1/mods/moonlight-26.1.2-4.0.8-fabric.jar` - replaces the
unmodified jar of the same name/version. Server-loaded mod (not
client-only), so per AGENTS.md's convention this needs deploying to both
the server's own `mods/` and the local test instance's merged `mods/`
folder, and needs a full restart to take effect.

## Status

**Confirmed fixed.** Patched, deployed, restarted onto the patched jar, then
tested a real `/stop`: exited cleanly in 2 seconds, no hang, no force-kill
needed - a stark contrast to the same command hanging 4 times in a row
earlier the same session before this patch. Log confirms a genuinely clean
shutdown (`ThreadedAnvilChunkStorage: All dimensions are saved`, no
`UnsupportedOperationException`, no "Exception stopping the server").
Deployed to both the server's `mods/` and the local test instance's merged
`mods/` folder.
