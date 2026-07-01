"""
Channel Stream Merger — Dispatcharr Plugin

Pairs quality-variant streams (e.g. "ESPN FHD" + "ESPN") into a single channel
with ordered fallback. Uses configurable quality tags (comma-separated, first
= primary), supports per-group configuration via ChannelGroupM3UAccount
.custom_properties, dry-run preview, and automatic execution after M3U refresh.
"""

import re as re_builtin
import regex as re_module
from django.db import transaction
from django.db import close_old_connections

# ---------------------------------------------------------------------------
# Logger wrapper — verbose mode promotes debug to info-level output
# ---------------------------------------------------------------------------

class _PluginLogger:
    """Lightweight logger wrapper.  debug() only emits when verbose is True;
    info() and warning() always pass through."""

    def __init__(self, logger, verbose):
        if logger is None:
            import logging
            logger = logging.getLogger(__name__)
        self._logger = logger
        self._verbose = verbose

    def debug(self, msg, *args, **kwargs):
        if self._verbose:
            self._logger.info("[VERBOSE] " + msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self._logger.info(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._logger.warning(msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self._logger.error(msg, *args, **kwargs)


def _match_base_name(stream_name, regex_pattern, tags_list, plog):
    """Check if *regex_pattern* matches a quality word in *stream_name*.

    Returns ``(base_name, matched_tag)``.  The tag is found by simple
    substring search over *tags_list* (avoiding regex ``.group()`` quirks).
    """
    try:
        m = re_builtin.search(regex_pattern, stream_name, re_builtin.IGNORECASE)
        if m is not None:
            base = re_builtin.sub(
                regex_pattern, '', stream_name, count=1,
                flags=re_builtin.IGNORECASE,
            )
            base = re_builtin.sub(r'\s+', ' ', base).strip()
            # Find which tag matched — simple substring, no regex
            name_upper = stream_name.upper()
            matched = "plain"
            for t in tags_list:
                if t.upper() == "[NOTAG]":
                    continue
                if re_builtin.escape(t).upper() in name_upper:
                    matched = t
                    break
            plog.debug("Matched '%s' → tag='%s' → base='%s'",
                       stream_name, matched, base)
            return base, matched
    except re_builtin.error as exc:
        plog.warning("Invalid regex pattern '%s': %s", regex_pattern, exc)
        return None, None

    plain_base = stream_name.strip()
    plog.debug("No tag in '%s' → using plain base='%s'", stream_name, plain_base)
    return plain_base, None


# ---------------------------------------------------------------------------
# Core pairing logic (shared by join + dry_run)
# ---------------------------------------------------------------------------

def _process_pairs(
    plog, groups_qs, tags, selected_set,
    dry_run=False,
):
    """Scan auto-sync groups and find candidate stream pairs.

    Parameters
    ----------
    plog : _PluginLogger
    groups_qs : QuerySet[ChannelGroupM3UAccount]
        Pre-filtered to enabled auto-sync groups.
    tags : list[str]
        Quality tags in priority order (index 0 = primary).
    selected_set : set[str] | None
        Lowercased group-name whitelist, or None for all.
    dry_run : bool
        When True no database writes are performed.

    Returns
    -------
    dict with keys: paired, deleted, groups_processed, errors, report
    """
    from apps.channels.models import Channel, ChannelStream, ChannelGroupM3UAccount

    paired = 0
    deleted = 0
    groups_processed = 0
    errors = []
    report = []  # structured per-group report for dry_run

    for rel in groups_qs.iterator():
        group = rel.channel_group
        group_name = group.name

        # -- group targeting filter ----------------------------------------
        if selected_set is not None and group_name.lower() not in selected_set:
            plog.debug("Skipping group '%s' — not in selected_groups", group_name)
            continue

        # Separate the special "[notag]" keyword from real quality tags.
        # "[notag]" sets the rank for streams with no quality tag at all.
        plain_rank = None
        real_tags = []
        for i, t in enumerate(tags):
            if t.upper() == "[NOTAG]":
                plain_rank = i
            else:
                real_tags.append(t)

        effective_regex = _build_regex_from_tags(real_tags)
        if not effective_regex and not real_tags:
            plog.warning("No quality tags configured — skipping group '%s'", group_name)
            continue

        # Build rank map: real tags + [NOTAG] both get their position
        tag_rank = {t.upper(): i for i, t in enumerate(tags)}
        plog.info("Processing group '%s' with tags: %s (notag rank: %s)",
                  group_name, tags, plain_rank)

        # -- fetch candidates (channels with exactly 1 stream) -------------
        # list() materialises the queryset — no need for .iterator()
        # (which also clashes with prefetch_related unless chunk_size is set)
        channels = list(
            Channel.objects.filter(
                auto_created=True,
                channel_group=group,
            ).prefetch_related("channelstream_set__stream")
        )

        candidates = []  # (channel, base_name, matched_tag_or_None)
        for ch in channels:
            cs_list = list(ch.channelstream_set.all())
            if len(cs_list) != 1:
                plog.debug(
                    "Skipping channel '%s' (id=%d) — has %d stream(s), need exactly 1",
                    ch.name, ch.id, len(cs_list),
                )
                continue

            stream_name = cs_list[0].stream.name
            base, tag = _match_base_name(stream_name, effective_regex, real_tags, plog)
            if base is None:
                plog.debug(
                    "Channel '%s' (id=%d) — invalid regex, skipping",
                    ch.name, ch.id,
                )
                continue
            candidates.append((ch, base, tag))

        # -- group by base name --------------------------------------------
        by_base: dict[str, dict[str, list]] = {}
        for ch, base, tag in candidates:
            entry = by_base.setdefault(base, {"matched": [], "plain": []})
            if tag is not None:
                entry["matched"].append((ch, tag))
            else:
                entry["plain"].append((ch, tag))

        # -- merge all variants per base name ---------------------------------
        # Quality priority = tag position: lower index = higher quality = tried first.

        def _quality_rank(channel):
            """Return the priority rank for a channel based on which quality
            tag appears in its name. Lower number = higher priority.
            Unknown tags rank after all known ones."""
            return _find_tag_rank(channel)

        def _find_tag_rank(channel):
            name_upper = channel.name.upper()
            for tag, rank in sorted(tag_rank.items(), key=lambda kv: kv[1]):
                escaped = re_builtin.escape(tag)
                if re_builtin.search(r"\b" + escaped + r"\b", name_upper):
                    return rank
            return plain_rank if plain_rank is not None else 99

        group_merges = []  # (base_name, surviving_ch, surviving_tag, [streams+tags], [channels+tags])
        for base_name, entry in by_base.items():
            matched_list = entry["matched"]   # [(ch, tag), ...]
            plain_list = entry["plain"]       # [(ch, tag), ...]
            all_candidates = matched_list + plain_list
            if len(all_candidates) < 2:
                plog.debug(
                    "Base '%s': only %d candidate(s) — need ≥2 to merge",
                    base_name, len(all_candidates),
                )
                continue

            # Pick the surviving channel: prefer the highest-quality matched
            # variant (or lowest channel_number as tie-breaker).
            if matched_list:
                matched_list.sort(key=lambda ct: (_quality_rank(ct[0]), ct[0].channel_number or 999999))
                survivor, survivor_tag = matched_list[0]
            else:
                plain_list.sort(key=lambda ct: ct[0].channel_number or 999999)
                survivor, survivor_tag = plain_list[0]

            # Collect streams from all OTHER channels (delete candidates)
            streams_to_add = []
            channels_to_delete = []  # [(ch, tag), ...]
            order_idx = 0  # survivor already has its stream at order=0

            # Add other matched variants as fallback (better quality first)
            for ch, tag in matched_list:
                if ch.id == survivor.id:
                    continue
                cs = list(ch.channelstream_set.all())
                if cs:
                    streams_to_add.append((cs[0].stream, order_idx + 1))
                    order_idx += 1
                    channels_to_delete.append((ch, tag))

            # Add plain variants as lowest-priority fallback
            for ch, tag in plain_list:
                if ch.id == survivor.id:
                    continue
                cs = list(ch.channelstream_set.all())
                if cs:
                    streams_to_add.append((cs[0].stream, order_idx + 1))
                    order_idx += 1
                    channels_to_delete.append((ch, tag))

            if not streams_to_add:
                continue  # nothing to merge (survivor is the only channel)

            def _stream_name(ch):
                cs = list(ch.channelstream_set.all())
                return cs[0].stream.name if cs else "?"

            deleted_names = [f"'{ch.name}'(stream:'{_stream_name(ch)}', tag={tag})"
                           for ch, tag in channels_to_delete]
            group_merges.append((base_name, survivor, survivor_tag, streams_to_add, channels_to_delete))
            plog.debug(
                "Base '%s': merging %d variant(s) → survivor='%s'(stream:'%s',%s,#%d), delete: [%s]",
                base_name, len(channels_to_delete),
                survivor.name, _stream_name(survivor), survivor_tag, survivor.channel_number,
                ", ".join(deleted_names),
            )

        if not group_merges:
            plog.info("No merges needed in group '%s'", group_name)
            continue

        groups_processed += 1
        plog.info(
            "Group '%s': merging %d base(s)", group_name, len(group_merges),
        )

        # -- execute or record merges ------------------------------------------
        for base_name, survivor, survivor_tag, streams_to_add, channels_to_delete in group_merges:
            if dry_run:
                for stream, order in streams_to_add:
                    report.append({
                        "group": group_name,
                        "base": base_name,
                        "primary_channel_id": survivor.id,
                        "primary_channel_name": survivor.name,
                        "primary_channel_number": survivor.channel_number,
                        "secondary_stream_id": stream.id,
                        "secondary_stream_name": stream.name,
                        "order": order,
                    })
                deleted_names = [f"'{ch.name}'(stream:'{_stream_name(ch)}', tag={tag})"
                               for ch, tag in channels_to_delete]
                plog.info(
                    "[DRY RUN] '%s': keep '%s'(stream:'%s',%s,#%d), add %d stream(s), delete: [%s]",
                    base_name, survivor.name, _stream_name(survivor), survivor_tag,
                    survivor.channel_number,
                    len(streams_to_add), ", ".join(deleted_names),
                )
            else:
                try:
                    with transaction.atomic():
                        for stream, order in streams_to_add:
                            ChannelStream.objects.create(
                                channel=survivor,
                                stream=stream,
                                order=order,
                            )
                        for ch, _tag in channels_to_delete:
                            ch.delete()
                    paired += len(streams_to_add)
                    deleted += len(channels_to_delete)
                    deleted_names = [f"'{ch.name}'(stream:'{_stream_name(ch)}', tag={tag})"
                                   for ch, tag in channels_to_delete]
                    plog.info(
                        "MERGED: base='%s' → '%s'(stream:'%s',%s,#%d) +%d stream(s) -%d channel(s): [%s]",
                        base_name, survivor.name, _stream_name(survivor), survivor_tag,
                        survivor.channel_number,
                        len(streams_to_add), len(channels_to_delete),
                        ", ".join(deleted_names),
                    )
                except Exception as exc:
                    errors.append(
                        f"Failed to merge base='{base_name}' "
                        f"(survivor={survivor.id}): {exc}"
                    )
                    plog.error("Merge failed for base='%s': %s", base_name, exc)

    if dry_run:
        return {
            "status": "ok",
            "paired": len(report),
            "groups_processed": groups_processed,
            "report": report,
        }

    return {
        "status": "ok",
        "paired": paired,
        "deleted": deleted,
        "groups_processed": groups_processed,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Shared setup helper — avoids duplication between join / dry_run handlers
# ---------------------------------------------------------------------------

_DEFAULT_TAGS = "2160p,4K,1080p,FHD,720p,HD,480p,SD,[notag]"


def _parse_tags(tags_str):
    """Parse a comma-separated quality-tags string into a cleaned list.
    Strips whitespace, removes empties, preserves order."""
    return [t.strip() for t in tags_str.split(",") if t.strip()]


def _build_regex_from_tags(tags):
    """Build a regex that matches any tag as a standalone token.
    Uses ``\\b`` word boundaries (reliable with Python's ``re`` module).
    Tags are *escaped* so regex metacharacters are treated literally.
    Returns the compiled pattern string or ``None`` if the list is empty."""
    if not tags:
        return None
    escaped = [re_builtin.escape(t) for t in tags]
    return r"\b(" + "|".join(escaped) + r")\b"


def _prepare_run(settings, plog):
    """Resolve settings and build the groups queryset used by both
    join_streams and dry_run.  Returns a dict with all resolved values."""
    from apps.channels.models import ChannelGroupM3UAccount

    tags_raw = settings.get("quality_tags", _DEFAULT_TAGS)
    tags = _parse_tags(tags_raw)
    plog.debug("Quality tags: %s (regex will be built from these)", tags)

    selected_raw = settings.get("selected_groups", "")

    selected_set = None
    if selected_raw and selected_raw.strip():
        selected_set = {
            g.strip().lower()
            for g in selected_raw.split(",")
            if g.strip()
        }

    groups_qs = ChannelGroupM3UAccount.objects.filter(
        auto_channel_sync=True,
        enabled=True,
    ).select_related("channel_group")

    return {
        "tags": tags,
        "selected_set": selected_set,
        "groups_qs": groups_qs,
    }


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _action_join_streams(settings, params, plog):
    """Join paired streams — the main merge action."""

    # Detect event-triggered invocation
    if params.get("event") == "m3u_refresh" and not settings.get("auto_run", False):
        plog.info("Event-triggered but auto_run is disabled — skipping")
        return {"status": "skipped", "reason": "auto_run disabled"}

    prepped = _prepare_run(settings, plog)
    plog.info(
        "Starting join — tags=%s, groups=%s",
        prepped["tags"], prepped["selected_set"] or "ALL",
    )

    result = _process_pairs(
        plog=plog,
        groups_qs=prepped["groups_qs"],
        tags=prepped["tags"],
        selected_set=prepped["selected_set"],
        dry_run=False,
    )

    plog.info(
        "Join complete: %d paired, %d deleted, %d groups processed",
        result.get("paired", 0), result.get("deleted", 0),
        result.get("groups_processed", 0),
    )
    if result.get("errors"):
        plog.warning("Join completed with %d error(s)", len(result["errors"]))

    return result


def _action_dry_run(settings, params, plog):
    """Preview pairing without making any database changes."""

    prepped = _prepare_run(settings, plog)
    plog.info(
        "Dry run — tags=%s, groups=%s",
        prepped["tags"], prepped["selected_set"] or "ALL",
    )

    result = _process_pairs(
        plog=plog,
        groups_qs=prepped["groups_qs"],
        tags=prepped["tags"],
        selected_set=prepped["selected_set"],
        dry_run=True,
    )

    report = result.get("report", [])
    paired = len(report)
    groups = result.get("groups_processed", 0)

    # Build a human-readable summary for the notification
    if paired == 0:
        message = "No streams to merge. All channels already paired or no matches found."
    else:
        lines = [
            f"{paired} stream(s) would be merged across {groups} group(s).",
            "",
        ]
        # Group report entries by group name
        by_group = {}
        for entry in report:
            by_group.setdefault(entry["group"], []).append(entry)
        for grp, entries in by_group.items():
            lines.append(f"[{grp}]")
            for e in entries:
                lines.append(
                    f"  '{e['primary_channel_name']}'(#{e.get('primary_channel_number','?')}) "
                    f"←+ '{e['secondary_stream_name']}' (order {e.get('order','?')})"
                )
        lines.append("")
        lines.append("Run 'Join Paired Streams' to apply these changes.")
        message = "\n".join(lines)

    plog.info("Dry run complete: %d prospective merge(s) in %d group(s)", paired, groups)

    result["message"] = message
    return result




# ===========================================================================
# Plugin entry point
# ===========================================================================

class Plugin:
    """Channel Stream Merger — Dispatcharr Plugin."""

    name = "Channel Stream Merger"
    version = "2.3.0"
    description = (
        "Pairs quality-variant streams into a single channel with ordered "
        "fallback. List your quality tags (e.g. FHD,HD,4K) in "
        "priority order — first is primary, rest are fallbacks."
        "Perfect for Auto channel sync."
    )
    author = "Dispatcharr Plugin"
    help_url = ""

    fields = [
        {"id": "enabled", "label": "Enabled", "type": "boolean", "default": True},
        {
            "id": "quality_tags",
            "label": "Quality Tags (highest priority first)",
            "type": "string",
            "default": "2160p,4K,1080p,FHD,720p,HD,480p,SD,[notag]",
            "help_text": (
                "Comma-separated quality tags to search for in stream names. "
                "Left = highest priority (primary). Right = lowest (last "
                "fallback). Case-insensitive. Use [notag] as a placeholder "
                "for streams with no quality tag — place it where you want "
                "untagged streams to rank."
            ),
        },
        {
            "id": "selected_groups",
            "label": "Selected Groups (comma-separated)",
            "type": "string",
            "default": "",
            "help_text": (
                "Only process channel groups matching these names "
                "(case-insensitive, partial match). Leave blank to process "
                "all auto-sync groups."
            ),
        },
        {
            "id": "auto_run",
            "label": "Auto-Run After M3U Refresh",
            "type": "boolean",
            "default": False,
            "help_text": (
                "When enabled, the join action triggers automatically after "
                "every M3U refresh completes."
            ),
        },
        {
            "id": "verbose_logging",
            "label": "Verbose Logging",
            "type": "boolean",
            "default": False,
            "help_text": (
                "Log every regex match, candidate, pairing decision, and "
                "exclusion reason for troubleshooting."
            ),
        },
    ]

    actions = [
        {
            "id": "join_streams",
            "label": "Join Paired Streams",
            "description": (
                "Find and merge paired streams into single channels with "
                "primary/fallback ordering."
            ),
            "button_label": "Run Join",
            "button_variant": "filled",
            "button_color": "blue",
            "events": ["m3u_refresh"],
            "confirm": {
                "required": True,
                "title": "Join Paired Streams?",
                "message": (
                    "This will merge paired streams into single channels and "
                    "delete redundant channels. Use 'Dry Run' first to preview."
                ),
            },
        },
        {
            "id": "dry_run",
            "label": "Dry Run (Preview)",
            "description": (
                "Preview which streams would be paired without making any changes."
            ),
            "button_label": "Preview",
            "button_variant": "outline",
            "button_color": "gray",
        },
    ]

    # ------------------------------------------------------------------
    def run(self, action: str, params: dict, context: dict):
        """Main entry point — dispatch to the appropriate handler."""
        settings = context.get("settings", {})
        logger = context.get("logger")

        plog = _PluginLogger(logger, settings.get("verbose_logging", False))

        if not settings.get("enabled", True):
            plog.info("Plugin is disabled — skipping action '%s'", action)
            return {"status": "skipped", "reason": "plugin disabled"}

        # Dispatch table
        handlers = {
            "join_streams": _action_join_streams,
            "dry_run": _action_dry_run,
        }

        handler = handlers.get(action)
        if handler is None:
            return {"status": "error", "message": f"Unknown action: {action}"}

        try:
            return handler(settings, params, plog)
        except Exception as exc:
            plog.error("Action '%s' failed: %s", action, exc)
            return {"status": "error", "message": str(exc)}
        finally:
            # Ensure the geventpool checkout is returned after every action
            close_old_connections()

    # ------------------------------------------------------------------
    def stop(self, context: dict):
        """Graceful shutdown — called when plugin is disabled/deleted/reloaded."""
        logger = context.get("logger")
        if logger:
            logger.info("Channel Stream Merger plugin stopped")
