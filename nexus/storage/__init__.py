"""Persistence helpers between the collector and SQLite.

The data layer is split by domain into the submodules below; this package
re-exports every name the former single ``nexus/storage.py`` module offered,
so ``from nexus.storage import X`` and ``storage.X`` keep working unchanged.
All SQL lives in this package.

Submodules (leaf modules first; none imports a later one):

- ``items``          single write path, FTS queries, search, read/dismiss state
- ``meta``           key/value meta table, per-source watermarks
- ``entities``       entity decoding/normalisation and aliases
- ``archives``       Wayback archive state for items and cases
- ``lists``          analyst lists and bookmarks (pins)
- ``custom_sources`` user-defined API sources and their health
- ``watchlists``     watchlists and ReDoS-bounded matching
- ``cases``          case CRUD, active case, terms, visits, notes
- ``analysis``       AI enrichment queue and translations
- ``feed``           feed row enrichment, attention queue, threat counts
- ``subscriptions``  topic subscriptions, capsules, scan-term values
- ``requirements``   intelligence requirements and intel views
- ``graph``          entity co-occurrence graph and entity profiles
- ``case_feed``      case-scoped item queries (live/new/reviewed, timeline)
"""

from __future__ import annotations

from nexus.storage.analysis import (
    _reindex_item_entities as _reindex_item_entities,
    pending_analysis as pending_analysis,
    pending_keyless_translation as pending_keyless_translation,
    save_analysis as save_analysis,
    save_keyless_translation as save_keyless_translation,
)
from nexus.storage.archives import (
    ARCHIVE_STATUSES as ARCHIVE_STATUSES,
    case_archive_summary as case_archive_summary,
    case_archive_targets as case_archive_targets,
    case_auto_archive as case_auto_archive,
    fail_stale_archive_jobs as fail_stale_archive_jobs,
    get_item_archive as get_item_archive,
    item_archives_map as item_archives_map,
    set_case_auto_archive as set_case_auto_archive,
    set_item_archive as set_item_archive,
)
from nexus.storage.case_feed import (
    _required_terms_for_case as _required_terms_for_case,
    case_alert_terms as case_alert_terms,
    case_evidence as case_evidence,
    case_items as case_items,
    case_items_collected_since as case_items_collected_since,
    case_live_items as case_live_items,
    case_new_count as case_new_count,
    case_new_items as case_new_items,
    case_question_items as case_question_items,
    case_reviewed_items as case_reviewed_items,
    case_timeline as case_timeline,
    case_unread_count as case_unread_count,
    mark_case_read as mark_case_read,
    related_cases as related_cases,
)
from nexus.storage.cases import (
    _CASE_PRIORITIES as _CASE_PRIORITIES,
    _CASE_STATUSES as _CASE_STATUSES,
    _NO_CASE_FILTER as _NO_CASE_FILTER,
    _PRIORITY_RANK as _PRIORITY_RANK,
    add_case_term as add_case_term,
    add_note as add_note,
    all_case_terms as all_case_terms,
    case_notes as case_notes,
    case_terms as case_terms,
    case_terms_map as case_terms_map,
    create_case as create_case,
    delete_case as delete_case,
    delete_note as delete_note,
    get_active_case as get_active_case,
    get_case as get_case,
    get_case_last_visit as get_case_last_visit,
    list_cases as list_cases,
    remove_case_term as remove_case_term,
    set_active_case as set_active_case,
    touch_case_visit as touch_case_visit,
    update_case as update_case,
    update_case_term as update_case_term,
    update_note as update_note,
)
from nexus.storage.custom_sources import (
    _CUSTOM_AUTH_TYPES as _CUSTOM_AUTH_TYPES,
    _CUSTOM_FIELDS as _CUSTOM_FIELDS,
    _CUSTOM_METHODS as _CUSTOM_METHODS,
    _SAMPLE_MAX_CHARS as _SAMPLE_MAX_CHARS,
    _clean_custom as _clean_custom,
    apply_custom_source_remap as apply_custom_source_remap,
    create_custom_source as create_custom_source,
    delete_custom_source as delete_custom_source,
    get_custom_source as get_custom_source,
    list_custom_sources as list_custom_sources,
    record_custom_source_health as record_custom_source_health,
    toggle_custom_source as toggle_custom_source,
    update_custom_source as update_custom_source,
)
from nexus.storage.entities import (
    _ENTITY_EDGE_CHARS as _ENTITY_EDGE_CHARS,
    _ENTITY_GROUP_KEYS as _ENTITY_GROUP_KEYS,
    _clean_entity_display as _clean_entity_display,
    _decode_entities as _decode_entities,
    _entity_kind_map as _entity_kind_map,
    _entity_norm_set as _entity_norm_set,
    _entity_tokens as _entity_tokens,
    _norm_entity_key as _norm_entity_key,
    add_entity_alias as add_entity_alias,
    decode_entities as decode_entities,
    delete_entity_alias as delete_entity_alias,
    get_entity_aliases as get_entity_aliases,
)
from nexus.storage.feed import (
    _AGG_HINTS as _AGG_HINTS,
    _OFFICIAL_HINTS as _OFFICIAL_HINTS,
    _SOCIAL_HINTS as _SOCIAL_HINTS,
    _THREAT_SIGNAL as _THREAT_SIGNAL,
    ATTENTION_LIMIT as ATTENTION_LIMIT,
    ATTENTION_MIN_SIGNAL as ATTENTION_MIN_SIGNAL,
    _attention_scored as _attention_scored,
    _classify_reliability as _classify_reliability,
    _host as _host,
    _relative_time as _relative_time,
    attention_count as attention_count,
    attention_items as attention_items,
    enrich_feed_rows as enrich_feed_rows,
    feed_item as feed_item,
    threat_counts_since as threat_counts_since,
)
from nexus.storage.graph import (
    ENTITY_PAGE_SIZE as ENTITY_PAGE_SIZE,
    entity_profile as entity_profile,
    topic_entity_graph as topic_entity_graph,
)
from nexus.storage.items import (
    _WORD_RE as _WORD_RE,
    _bump_cluster as _bump_cluster,
    _item_filter_clauses as _item_filter_clauses,
    build_fts_or_query as build_fts_or_query,
    build_fts_query as build_fts_query,
    count_items as count_items,
    count_matching_items as count_matching_items,
    dismiss_item as dismiss_item,
    distinct_sources as distinct_sources,
    is_read as is_read,
    list_items as list_items,
    mark_read as mark_read,
    mark_unread as mark_unread,
    search_items as search_items,
    undismiss_item as undismiss_item,
    upsert_item as upsert_item,
)
from nexus.storage.lists import (
    _LIST_COLORS as _LIST_COLORS,
    add_bookmark as add_bookmark,
    add_to_list as add_to_list,
    create_list as create_list,
    delete_list as delete_list,
    get_list as get_list,
    list_lists as list_lists,
    list_member_items as list_member_items,
    remove_bookmark as remove_bookmark,
    remove_from_list as remove_from_list,
    rename_list as rename_list,
    update_list as update_list,
)
from nexus.storage.meta import (
    _parse_utc as _parse_utc,
    get_meta as get_meta,
    get_meta_value as get_meta_value,
    get_source_watermark as get_source_watermark,
    set_meta as set_meta,
    set_source_watermark as set_source_watermark,
)
from nexus.storage.requirements import (
    active_requirements as active_requirements,
    add_requirement as add_requirement,
    delete_requirement as delete_requirement,
    intel_items as intel_items,
    list_requirements as list_requirements,
    pending_requirement_scoring as pending_requirement_scoring,
    save_requirement_hit as save_requirement_hit,
    set_requirement_enabled as set_requirement_enabled,
    unscored_requirements_for_item as unscored_requirements_for_item,
)
from nexus.storage.subscriptions import (
    add_subscription as add_subscription,
    delete_capsule as delete_capsule,
    delete_subscription as delete_subscription,
    get_case_term_values as get_case_term_values,
    get_subscription_values as get_subscription_values,
    list_query_capsules as list_query_capsules,
    list_subscriptions as list_subscriptions,
    subscription_values as subscription_values,
)
from nexus.storage.watchlists import (
    _MAX_REGEX_HAYSTACK as _MAX_REGEX_HAYSTACK,
    _MAX_REGEX_PATTERN_LEN as _MAX_REGEX_PATTERN_LEN,
    _matches as _matches,
    count_new_watchlist_hits as count_new_watchlist_hits,
    create_watchlist as create_watchlist,
    delete_watchlist as delete_watchlist,
    list_watchlist_hits as list_watchlist_hits,
    list_watchlists as list_watchlists,
    mark_watchlist_hits_seen as mark_watchlist_hits_seen,
    match_watchlists as match_watchlists,
    toggle_watchlist as toggle_watchlist,
)
