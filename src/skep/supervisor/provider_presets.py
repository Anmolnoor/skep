"""v108-F3: the provider preset catalog — Hermes parity as DATA.

Each preset is one row: registry protocol + endpoint + the NAME of the env
var that holds its key + a default model + the egress hosts it needs. Mined
2026-08-05 from the operator's own ``~/.hermes`` install
(``hermes_cli/providers.py`` + its models.dev cache) so the catalog does not
depend on that tree surviving archival.

The lines this catalog will not cross (ADR 0051):

- NO preset ships a credential or an OAuth client id. Subscription
  providers (qwen-portal, nous, minimax) are reachable by pasting a token
  obtained at their portal; presenting another app's registered client id
  to a provider is impersonation and is not done.
- NO implicit hosts: the endpoint host plus every ``extra_hosts`` entry
  land in ``allowed_network_hosts`` and ride the one v19-F2 egress merge
  (I5/I12). Regional hosts are explicit — never a ``*.amazonaws.com``.
- Default models are STARTING POINTS: the health probe says honestly
  whether the configured model exists at the provider (I8).
"""

from __future__ import annotations

from dataclasses import dataclass

from .providers import ProviderError, ProviderProfile, provider_host


@dataclass(frozen=True)
class ProviderPreset:
    preset_id: str
    label: str
    protocol: str  # registry spelling (PROVIDER_PROTOCOLS)
    base_url: str | None  # None: the operator must supply one (per-resource)
    default_model: str
    api_key_env: str | None
    cost_class: str = "paid"
    extra_hosts: tuple[str, ...] = ()
    auth_note: str = ""  # how credentials reach this provider, honestly


def _p(
    preset_id: str,
    label: str,
    base_url: str,
    default_model: str,
    api_key_env: str | None,
    *,
    auth_note: str = "",
) -> ProviderPreset:
    return ProviderPreset(
        preset_id, label, "openai_compat", base_url, default_model, api_key_env, auth_note=auth_note
    )


_PASTE_TOKEN = (
    "subscription token: obtain it at the provider's portal and store it "
    "with `skep provider set-key` (never through chat)"
)

_PRESETS: tuple[ProviderPreset, ...] = (
    # -- Tier 1: OpenAI-compatible chat completions, key auth ----------------
    _p(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "deepseek/deepseek-v4-flash",
        "OPENROUTER_API_KEY",
    ),
    _p("deepseek", "DeepSeek", "https://api.deepseek.com", "deepseek-v4-flash", "DEEPSEEK_API_KEY"),
    _p("zai", "Z.AI (GLM)", "https://api.z.ai/api/paas/v4", "glm-4.7", "ZHIPU_API_KEY"),
    _p(
        "zhipuai-cn",
        "Zhipu AI (bigmodel.cn)",
        "https://open.bigmodel.cn/api/paas/v4",
        "glm-5.2",
        "ZHIPU_API_KEY",
    ),
    _p("openai", "OpenAI", "https://api.openai.com/v1", "gpt-5-mini", "OPENAI_API_KEY"),
    _p("xai", "xAI (Grok)", "https://api.x.ai/v1", "grok-4.3", "XAI_API_KEY"),
    _p(
        "nvidia",
        "NVIDIA NIM",
        "https://integrate.api.nvidia.com/v1",
        "moonshotai/kimi-k2.6",
        "NVIDIA_API_KEY",
    ),
    _p(
        "alibaba",
        "Alibaba Model Studio",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "qwen3-coder-plus",
        "DASHSCOPE_API_KEY",
    ),
    _p(
        "alibaba-coding-plan",
        "Alibaba Coding Plan",
        "https://coding-intl.dashscope.aliyuncs.com/v1",
        "qwen3-coder-plus",
        "ALIBABA_CODING_PLAN_API_KEY",
    ),
    _p("stepfun", "StepFun", "https://api.stepfun.com/v1", "step-3.7-flash", "STEPFUN_API_KEY"),
    _p(
        "moonshotai",
        "Moonshot AI",
        "https://api.moonshot.ai/v1",
        "kimi-k2.7-code",
        "MOONSHOT_API_KEY",
    ),
    _p(
        "huggingface",
        "Hugging Face router",
        "https://router.huggingface.co/v1",
        "moonshotai/Kimi-K2-Instruct-0905",
        "HF_TOKEN",
    ),
    _p(
        "novita",
        "Novita AI",
        "https://api.novita.ai/openai",
        "deepseek/deepseek-v4-flash",
        "NOVITA_API_KEY",
    ),
    _p(
        "kilo",
        "Kilo Gateway",
        "https://api.kilo.ai/api/gateway",
        "deepseek-v4-flash",
        "KILO_API_KEY",
    ),
    _p(
        "opencode-zen",
        "OpenCode Zen",
        "https://opencode.ai/zen/v1",
        "deepseek-v4-flash",
        "OPENCODE_API_KEY",
    ),
    _p(
        "opencode-go",
        "OpenCode Go",
        "https://opencode.ai/zen/go/v1",
        "deepseek-v4-flash",
        "OPENCODE_API_KEY",
    ),
    _p("xiaomi", "Xiaomi MiMo", "https://api.xiaomimimo.com/v1", "mimo-v2.5", "XIAOMI_API_KEY"),
    _p(
        "tencent-tokenhub",
        "Tencent TokenHub",
        "https://tokenhub.tencentmaas.com/v1",
        "hy3",
        "TENCENT_TOKENHUB_API_KEY",
    ),
    _p("arcee", "Arcee AI", "https://api.arcee.ai/api/v1", "coder-large", "ARCEE_API_KEY"),
    _p("gmi", "GMI Cloud", "https://api.gmi-serving.com/v1", "zai-org/GLM-5.1-FP8", "GMI_API_KEY"),
    _p(
        "nous",
        "Nous Research",
        "https://inference-api.nousresearch.com/v1",
        "hermes-3-405b",
        "NOUS_API_KEY",
        auth_note=_PASTE_TOKEN + " (or a portal API key)",
    ),
    _p("ollama-cloud", "Ollama Cloud", "https://ollama.com", "glm-5.2", "OLLAMA_API_KEY"),
    _p(
        "google-gemini",
        "Google Gemini (OpenAI-compatible)",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-2.5-flash",
        "GEMINI_API_KEY",
    ),
    _p(
        "qwen-portal",
        "Qwen portal (subscription)",
        "https://portal.qwen.ai/v1",
        "qwen3-coder-plus",
        None,
        auth_note=_PASTE_TOKEN,
    ),
    ProviderPreset(
        "lmstudio",
        "LM Studio (local)",
        "openai_compat",
        "http://127.0.0.1:1234/v1",
        "local-model",
        None,
        cost_class="local",
        auth_note="local server — no key, nothing leaves this machine",
    ),
    ProviderPreset(
        "azure-foundry",
        "Azure AI Foundry",
        "openai_compat",
        None,
        "gpt-5-mini",
        "AZURE_FOUNDRY_API_KEY",
        auth_note="per-resource endpoint: pass --base-url https://<resource>.openai.azure.com/...",
    ),
    # -- Tier 2: Anthropic Messages wire format ------------------------------
    ProviderPreset(
        "anthropic",
        "Anthropic",
        "anthropic",
        "https://api.anthropic.com",
        "claude-sonnet-4-5",
        "ANTHROPIC_API_KEY",
    ),
    ProviderPreset(
        "minimax",
        "MiniMax (minimax.io)",
        "anthropic",
        "https://api.minimax.io/anthropic",
        "MiniMax-M2.5-highspeed",
        "MINIMAX_API_KEY",
        auth_note="API key, or " + _PASTE_TOKEN + " for the OAuth coding plan",
    ),
    ProviderPreset(
        "minimax-cn",
        "MiniMax (minimaxi.com)",
        "anthropic",
        "https://api.minimaxi.com/anthropic",
        "MiniMax-M2.5-highspeed",
        "MINIMAX_API_KEY",
    ),
    ProviderPreset(
        "kimi-for-coding",
        "Kimi For Coding",
        "anthropic",
        "https://api.kimi.com/coding",
        "kimi-k2-thinking",
        "KIMI_API_KEY",
        auth_note=_PASTE_TOKEN,
    ),
    # -- Tier 3: needs the copilot token exchange (v108-F7) ------------------
    ProviderPreset(
        "github-copilot",
        "GitHub Copilot",
        "openai_compat",
        "https://api.githubcopilot.com",
        "gpt-5-mini",
        "GITHUB_TOKEN",
        extra_hosts=("api.github.com",),
        auth_note=(
            "uses your own GitHub token (GITHUB_TOKEN/GH_TOKEN or set-key); "
            "skep exchanges it at api.github.com for a short-lived Copilot "
            "bearer per request — no borrowed OAuth client id"
        ),
    ),
    # -- Tier 4: the new wire protocols (v108-F5/F6) -------------------------
    ProviderPreset(
        "openai-responses",
        "OpenAI (Responses API)",
        "openai_responses",
        "https://api.openai.com/v1",
        "gpt-5-mini",
        "OPENAI_API_KEY",
    ),
    ProviderPreset(
        "xai-responses",
        "xAI Grok (Responses API)",
        "openai_responses",
        "https://api.x.ai/v1",
        "grok-4.3",
        "XAI_API_KEY",
    ),
    ProviderPreset(
        "bedrock",
        "AWS Bedrock",
        "bedrock",
        "https://bedrock-runtime.us-east-1.amazonaws.com",
        "anthropic.claude-sonnet-4-5-v1:0",
        None,
        auth_note=(
            "credentials come from the daemon's AWS env vars "
            "(AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY[/AWS_SESSION_TOKEN]); "
            "api_key_env is unused. Pass --base-url for another region — the "
            "matching bedrock.<region> control-plane host follows"
        ),
    ),
)

PROVIDER_PRESETS: dict[str, ProviderPreset] = {p.preset_id: p for p in _PRESETS}


def preset_egress_note(preset: ProviderPreset, base_url: str) -> str:
    """One honest line about what selecting this preset means for egress —
    the voice.py PROVIDER_EGRESS_NOTES pattern (I8)."""
    if preset.cost_class == "local":
        return f"LOCAL ({preset.label}) — nothing leaves this machine"
    hosts = [h for h in (provider_host(base_url), *preset.extra_hosts) if h]
    if preset.preset_id == "bedrock":
        from .serve.llm_bedrock import control_base_url

        control = provider_host(control_base_url(base_url))
        if control and control not in hosts:
            hosts.append(control)
    return (
        f"CLOUD ({preset.label}) — every assistant/worker prompt leaves this "
        f"machine for {', '.join(hosts)}"
    )


def profile_from_preset(
    preset_id: str,
    *,
    provider_id: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    cost_class: str | None = None,
    fallback_order: int = 0,
) -> ProviderProfile:
    """Build an (unvalidated) profile from a preset; the store's upsert path
    runs ``validate_provider_profile`` as always. Provenance is recorded as
    ``preset:<id>`` (I8)."""
    preset = PROVIDER_PRESETS.get(preset_id)
    if preset is None:
        raise ProviderError(
            f"unknown preset {preset_id!r} (see `skep provider presets` for the catalog)"
        )
    url = (base_url or preset.base_url or "").strip()
    if not url:
        raise ProviderError(f"preset {preset_id!r} needs an explicit base_url: {preset.auth_note}")
    extra = preset.extra_hosts
    if preset.preset_id == "bedrock":
        # The control-plane host (model listing / health) tracks the chosen
        # region's runtime host — explicit per-region rows, never a wildcard.
        from .serve.llm_bedrock import control_base_url

        control = provider_host(control_base_url(url))
        if control is not None:
            extra = tuple(dict.fromkeys((*extra, control)))
    return ProviderProfile(
        provider_id=(provider_id or preset_id).strip(),
        protocol=preset.protocol,
        base_url=url,
        model=(model or preset.default_model).strip(),
        allowed_network_hosts=extra,
        cost_class=cost_class or preset.cost_class,
        fallback_order=fallback_order,
        api_key_env=preset.api_key_env,
        source=f"preset:{preset_id}",
    )


def preset_view(preset: ProviderPreset) -> dict[str, object]:
    """The catalog row every surface shows (CLI, REST, chat)."""
    base_url = preset.base_url or ""
    return {
        "preset_id": preset.preset_id,
        "label": preset.label,
        "protocol": preset.protocol,
        "base_url": preset.base_url,
        "default_model": preset.default_model,
        "api_key_env": preset.api_key_env,
        "cost_class": preset.cost_class,
        "extra_hosts": list(preset.extra_hosts),
        "auth_note": preset.auth_note,
        "egress": preset_egress_note(preset, base_url) if base_url else "depends on --base-url",
    }
