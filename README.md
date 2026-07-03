# Channel Stream Merger Plugin

A **Dispatcharr plugin** that automatically merges quality-variant streams (e.g. `ESPN FHD` + `ESPN HD` + `ESPN`) into a **single channel** with ordered fallback. The proxy tries the highest-quality stream first, then falls back down the chain.

---

## Features

- **Quality-tag based matching** — list your quality tags in priority order (e.g. `2160p,4K,FHD,HD`). First = primary, rest = fallbacks.
- **Multi-variant merge** — handles any number of variants (UHD + FHD + HD + plain → 1 channel with 4 streams).
- **`[notag]` keyword** — control where untagged streams rank (e.g. `[notag],FHD,HD` makes plain streams primary).
- **Dry Run (Preview)** — shows what would be merged without making changes.
- **Auto-run** — automatically executes after every M3U refresh.
- **Verbose Logging** — detailed per-channel match/miss logging for troubleshooting.
- **Group targeting** — process specific channel groups by name.
- **Re-entrancy safe** — already-merged channels are skipped; no double-merging.

---

## Quick Start

### Installation

1. Download the latest `.zip` from [Releases](https://github.com/tomasz86/Dispatcharr-Plugin/releases).
2. In the Dispatcharr UI, go to **Plugins** → **Import** → upload the zip.
3. Click the **refresh** icon to discover the plugin.
4. **Enable** the plugin.

> Or copy the `channel_stream_merger` folder into `data/plugins/` manually and refresh.

### First Run

1. **Enable** Auto Channel Sync on your M3U account groups (if not already).
2. **Refresh M3U** to create channels from all streams.
3. Go to **Plugins** → **Channel Stream Merger** → **Settings** → set your quality tags.
4. Click **Actions** → **Dry Run (Preview)** to see what would merge.
5. Click **Join Paired Streams** to execute.

---

## Settings

| Field | Type | Default | Description |
|---|---|---|---|
| `Enabled` | boolean | `true` | Master switch |
| `Quality Tags` | string | `2160p,4K,1080p,FHD,720p,HD,480p,SD,[notag]` | Comma-separated, first=highest priority. `[notag]` ranks untagged streams |
| `Selected Groups` | string | (blank) | Comma-separated group names to process. Leave blank for all auto-sync groups |
| `Auto-Run After M3U Refresh` | boolean | `false` | Automatically run `Join Paired Streams` after every M3U refresh |
| `Verbose Logging` | boolean | `false` | Log every regex match, candidate, and merge decision |

---

## Actions

| Action | Description |
|---|---|
| **Join Paired Streams** | Merges quality-variant streams into single channels with ordered fallback. Deletes redundant channels |
| **Dry Run (Preview)** | Shows what would merge — check the log for per-stream details |

---
Made by AI, handle with care!
---

## How It Works

### Matching

The plugin matches against the **stream name** (not the channel name), because auto-sync may strip quality tags when creating the channel. The stream retains the original provider name with quality info.

For each 1-stream channel in an auto-sync group:

```
Stream: "ESPN FHD 7/1 [HBO]"   → regex matches tag "FHD"   → base = "ESPN 7/1 [HBO]"
Stream: "ESPN 7/1 [HBO]"       → no match                  → base = "ESPN 7/1 [HBO]"
```

Both share the base `"ESPN 7/1 [HBO]"` → they merge.

### Priority

Tags in your `Quality Tags` setting define the priority order:

```
2160p,4K,FHD,HD,[notag]

"ESPN 2160p" → rank 0 → primary stream (order 0, tried first)
"ESPN 4K"    → rank 1 → fallback (order 1)
"ESPN FHD"   → rank 2 → fallback (order 2)
"ESPN HD"    → rank 3 → fallback (order 3)
"ESPN"       → rank 4 → last resort (order 4)
```

### Auto-cleanup

After merging, the surviving channel's channel number and name are preserved. Redundant channels are **deleted** — the next M3U refresh won't recreate them because the stream is already linked to the surviving channel.

---

## Usage Examples

### Default — highest quality is primary

```
Quality Tags: 2160p,4K,1080p,FHD,720p,HD,480p,SD,[notag]
```

2160p becomes primary, plain (no tag) is last resort.

### Plain streams as primary

```
Quality Tags: [notag],FHD,HD
```

Non-tagged streams are primary, FHD is first fallback, HD is second.

### Custom quality tags

```
Quality Tags: UHD,HDR,FHD,SD
```

Matches your custom tags in your preferred order.

---

## Log Reference

With **Verbose Logging** enabled, the plugin logs:

| Log level | Example | Meaning |
|---|---|---|
| `info` | `Processing group 'Sports SE' with tags: ['FHD','HD','[notag]']` | Group being processed |
| `debug` | `Matched 'ESPN FHD 7/1' → tag='FHD' → base='ESPN 7/1'` | Stream matched a quality tag |
| `debug` | `No tag in 'ESPN 7/1' → using plain base='ESPN 7/1'` | Stream has no quality tag |
| `info` | `MERGED: base='ESPN 7/1' → 'ESPN FHD 7/1'(FHD,#101) +1 stream(s) -1 channel(s)` | Actual merge executed |
| `info` | `[DRY RUN] ...` | Preview of what would merge |
| `info` | `Join complete: 12 paired, 12 deleted, 1 groups processed` | Final summary |

---

## Requirements

- **Dispatcharr** ≥ 0.27.0
- **Auto Channel Sync** enabled on at least one M3U account group

---

## License

[MIT](LICENSE)
