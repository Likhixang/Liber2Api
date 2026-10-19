import re
from typing import Any, Dict, List, Optional, Tuple, Set

# Models that have a TEE-enabled hardware enclave variant on LibertAI
TEE_CAPABLE_BASES: Set[str] = {
    "qwen3.8-27b",
    "hermes-3-8b",
}

# Models that only exist in TEE variant on upstream
TEE_ONLY_BASES: Set[str] = {
    "hermes-3-8b",
}

# Models that have a -thinking reasoning variant on LibertAI
THINKING_CAPABLE_BASES: Set[str] = {
    "qwen3.6-35b-a3b",
    "qwen3.8-27b",
    "qwen3.5-122b-a10b",
    "deepseek-v4.1-flash",
    "deepseek-v4.1-flash-1017",
    "glm-5.3-flash",
    "glm-5.3",
}

# Known thinking effort values that trigger thinking suffix
ACTIVE_REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def strip_model_suffixes(model_name: str) -> Tuple[str, bool, bool]:
    """
    Strips -thinking and -tee suffixes regardless of ordering.
    Returns (base_name, had_tee, had_thinking).
    """
    name = model_name.strip()
    had_thinking = False
    had_tee = False

    changed = True
    while changed:
        changed = False
        if name.endswith("-thinking"):
            had_thinking = True
            name = name[:-9]
            changed = True
        if name.endswith("-tee"):
            had_tee = True
            name = name[:-4]
            changed = True

    return name, had_tee, had_thinking


def canonicalize_models_list(models_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Merges -tee and -thinking variants into a single canonical base model entry.
    Removes redundant duplicate entries in /v1/models listing.
    """
    seen_bases = set()
    canonical_list = []

    for item in models_data:
        raw_id = item.get("id", "")
        base_id, had_tee, had_thinking = strip_model_suffixes(raw_id)

        if base_id in seen_bases:
            continue

        seen_bases.add(base_id)
        # Create a clean unified model object
        merged_item = dict(item)
        merged_item["id"] = base_id
        canonical_list.append(merged_item)

    return canonical_list


def resolve_upstream_model(
    requested_model: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> str:
    """
    Intelligently routes a requested model to the correct upstream model identifier.
    Order of construction specified by user: First attach '-tee', then attach '-thinking'.

    Rule:
    1. reasoning_effort == "none" -> Base model without -thinking
    2. reasoning_effort in ("low", "medium", "high", "xhigh", "max") -> Append -thinking
    3. TEE request (param tee=true, header x-tee=true, or model has -tee) -> Append -tee
    """
    base_name, had_tee, had_thinking = strip_model_suffixes(requested_model)

    payload_data = payload or {}
    headers_data = headers or {}

    # Check TEE intent
    wants_tee = had_tee or bool(payload_data.get("tee")) or (headers_data.get("x-tee", "").lower() == "true")
    if base_name in TEE_ONLY_BASES:
        wants_tee = True

    # Check Thinking / Reasoning intent
    # Default is medium (Thinking enabled) unless client explicitly specifies reasoning_effort == "none"
    reasoning_effort = payload_data.get("reasoning_effort")
    if reasoning_effort is not None:
        effort_str = str(reasoning_effort).strip().lower()
        if effort_str == "none":
            wants_thinking = False
        else:
            wants_thinking = True
    else:
        # Default when reasoning_effort is omitted: default to medium (thinking enabled)
        wants_thinking = True

    # Assemble upstream target model: First -tee, then -thinking
    target_model = base_name

    # 1. Process TEE first
    if wants_tee and (base_name in TEE_CAPABLE_BASES or base_name in TEE_ONLY_BASES):
        target_model = f"{target_model}-tee"

    # 2. Process Thinking next
    if wants_thinking and base_name in THINKING_CAPABLE_BASES:
        target_model = f"{target_model}-thinking"

    return target_model
