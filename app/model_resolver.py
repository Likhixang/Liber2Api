import re
from typing import Any, Dict, List, Optional, Tuple, Set

# Models blocked due to upstream severe quantization degradation / adulteration
ADULTERATED_BLOCKED_BASES: Set[str] = {
    "glm-5.3",
    "glm-5.3-flash",
}

# Transparent aliases directing generic requests to official pure-blood checkpoints
MODEL_ALIASES: Dict[str, str] = {
    "deepseek-v4.1-flash": "deepseek-v4.1-flash-1017",
}

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
    "deepseek-v4.1-flash-1017",
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
    Completely filters out adulterated models (GLM-5.3 NVFP4, REAP pruned models).
    """
    seen_bases = set()
    canonical_list = []

    for item in models_data:
        raw_id = item.get("id", "")
        base_id, had_tee, had_thinking = strip_model_suffixes(raw_id)

        # 1. Block adulterated bases
        if base_id in ADULTERATED_BLOCKED_BASES:
            continue

        # 2. Do not expose pruned unversioned deepseek-v4.1-flash (prefer official 1017)
        if base_id == "deepseek-v4.1-flash":
            continue

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
    Order of construction: First attach '-tee', then attach '-thinking'.

    Rule:
    1. Blocks adulterated models (e.g. glm-5.3) with explicit ValueError.
    2. Transparently upgrades aliases (e.g. deepseek-v4.1-flash -> deepseek-v4.1-flash-1017).
    3. reasoning_effort == "none" -> Base model without -thinking
    4. reasoning_effort in ("low", "medium", "high", "xhigh", "max") or omitted -> Defaults to -thinking
    5. TEE request (param tee=true, header x-tee=true, or model has -tee) -> Append -tee
    """
    base_name, had_tee, had_thinking = strip_model_suffixes(requested_model)

    # 1. Check if model is blocked
    if base_name in ADULTERATED_BLOCKED_BASES:
        raise ValueError(
            f"模型 '{requested_model}' 因上游采用 NVFP4 4-bit 极限压制导致严重退化及思考外泄，已被网关彻底屏蔽。"
            f"推荐使用官方纯血模型：'deepseek-v4.1-flash-1017' 或 'qwen3.8-27b'。"
        )

    # 2. Alias resolution
    if base_name in MODEL_ALIASES:
        base_name = MODEL_ALIASES[base_name]

    payload_data = payload or {}
    headers_data = headers or {}

    # Parse explicit security intent rather than treating strings like "false" as true.
    def tee_flag(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
            return value.strip().lower() in ("true", "1")
        raise ValueError("tee must be a boolean or true/false/1/0 string")

    payload_tee = tee_flag(payload_data["tee"]) if "tee" in payload_data else False
    header_tee = False
    for key, value in headers_data.items():
        if key.lower() == "x-tee":
            header_tee = tee_flag(value) or header_tee
    wants_tee = had_tee or payload_tee or header_tee or base_name in TEE_ONLY_BASES
    if wants_tee and base_name not in TEE_CAPABLE_BASES | TEE_ONLY_BASES:
        raise ValueError(f"TEE is not available for model '{requested_model}'; refusing non-TEE fallback")

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
