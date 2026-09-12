"""Runtime configuration. Secrets come from the environment, never from source."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from crewai import LLM
from crewai.mcp import MCPServerHTTP
from crewai.mcp.filters import StaticToolFilter

# LiteLLM proxy `model_name` values (config.yaml), not upstream provider strings.
PLAN_MODEL = os.getenv("GL_PLAN_MODEL", "claude-sonnet-5-cloud-plan")
ACT_MODEL = os.getenv("GL_ACT_MODEL", "qwen3-coder-30b")

# `claude-sonnet-5` was once a proxy alias pointing at ollama_chat/qwen3-coder:30b,
# which silently downgraded any planning role that selected it. The entry has since
# been removed from the catalog, so the name now just fails to resolve. Keep refusing
# it explicitly: a clear error beats a 404, and it documents the retired trap.
_MISLEADING_PLAN_ALIASES = {"claude-sonnet-5", "anthropic/claude-sonnet-5"}

# Obsidian MCP tools, grouped by what a role legitimately needs. Nothing outside
# these lists reaches an agent: vault_delete, vault_move, vault_copy and
# command_execute are never granted, so no agent can destroy or relocate a note.
OBSIDIAN_READ_TOOLS = [
    "vault_read",
    "vault_list",
    "vault_get_document_map",
    "search_simple",
    "search_query",
    "tag_list",
    "active_file_get_path",
]
#: Section-level edits. Used by the Coordinator to flip a checkbox and append a
#: completion record without rewriting the surrounding note.
OBSIDIAN_EDIT_TOOLS = ["vault_patch", "vault_append"]
#: Whole-note authoring, granted only to the Architect for the Plan notes.
OBSIDIAN_AUTHOR_TOOLS = ["vault_write"]

VAULT_PLAN_DIR = "01 Projects/Golf League/Plan"
VAULT_TASKS_DIR = "01 Projects/Golf League/Tasks"
EXECUTION_GUIDE = f"{VAULT_TASKS_DIR}/00-Execution-Guide.md"


class ConfigError(RuntimeError):
    """Raised when required runtime configuration is missing or unsafe."""


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(
            f"{name} is not set. Export it (see .env.example) before running the workflow."
        )
    return value


@dataclass(frozen=True)
class Runtime:
    """Resolved endpoints and models for one orchestrator run."""

    litellm_base_url: str
    litellm_api_key: str
    obsidian_url: str
    obsidian_token: str
    plan_model: str
    act_model: str
    repo_root: str

    @classmethod
    def from_env(cls) -> Runtime:
        plan_model = PLAN_MODEL
        if plan_model in _MISLEADING_PLAN_ALIASES:
            raise ConfigError(
                f"GL_PLAN_MODEL={plan_model!r} is not a planning model in the LiteLLM "
                "catalog; it was a retired alias for a local coder. Use "
                "'claude-sonnet-5-cloud-plan' for planning roles."
            )
        return cls(
            litellm_base_url=os.getenv("LITELLM_BASE_URL", "http://192.168.50.4:4000/v1"),
            litellm_api_key=_require("LITELLM_API_KEY"),
            obsidian_url=os.getenv(
                "OBSIDIAN_MCP_URL", "https://gringotts.tail441593.ts.net:8445/mcp"
            ),
            obsidian_token=_require("OBSIDIAN_MCP_TOKEN"),
            plan_model=plan_model,
            act_model=ACT_MODEL,
            repo_root=os.getenv("GL_REPO_ROOT", os.getcwd()),
        )

    def llm(self, model: str, temperature: float = 0.1) -> LLM:
        """Build an LLM bound to the LiteLLM proxy.

        The `openai/` prefix selects LiteLLM's OpenAI-compatible client; the proxy
        then routes `model` by its catalog `model_name`.
        """
        return LLM(
            model=f"openai/{model}",
            api_key=self.litellm_api_key,
            base_url=self.litellm_base_url,
            temperature=temperature,
        )

    @property
    def plan_llm(self) -> LLM:
        return self.llm(self.plan_model)

    @property
    def act_llm(self) -> LLM:
        return self.llm(self.act_model)

    def accessible_models(self) -> set[str]:
        """Model names this key may actually use, per the proxy.

        `/v1/model/info` is key-scoped: a narrowly-scoped key sees only its own
        models, so this is the cheapest way to catch a wrong key.
        """
        request = urllib.request.Request(
            self.litellm_base_url.rstrip("/").removesuffix("/v1") + "/v1/model/info",
            headers={"Authorization": f"Bearer {self.litellm_api_key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ConfigError(
                f"Could not reach the LiteLLM proxy at {self.litellm_base_url}: {exc}"
            ) from exc
        return {entry["model_name"] for entry in payload.get("data", [])}

    def verify_access(self) -> None:
        """Fail before any phase runs if the key cannot reach both models.

        More than one LiteLLM key exists on this machine, with different model
        scopes, and the environment does not make it obvious which one is in
        play. Without this check a wrong key surfaces as a 403 partway through
        a crew run, after the vault has already been touched.
        """
        available = self.accessible_models()
        missing = [m for m in (self.plan_model, self.act_model) if m not in available]
        if missing:
            fingerprint = f"{self.litellm_api_key[:7]}...{self.litellm_api_key[-4:]}"
            raise ConfigError(
                f"LiteLLM key {fingerprint} cannot access: {', '.join(missing)}. "
                f"It can reach: {', '.join(sorted(available)) or '(nothing)'}. "
                "Set LITELLM_API_KEY to a key scoped for both the planning and "
                "acting models (note: a launchd-set value shadows your shell profile "
                "for GUI-launched apps)."
            )

    def obsidian_mcp(self, allowed_tools: list[str] | None = None) -> MCPServerHTTP:
        """Obsidian MCP server, restricted to the tools a role actually needs.

        Omitting `allowed_tools` still blocks the destructive surface.
        """
        tool_filter = StaticToolFilter(
            allowed_tool_names=allowed_tools,
            blocked_tool_names=["vault_delete", "vault_move", "vault_copy", "command_execute"],
        )
        return MCPServerHTTP(
            url=self.obsidian_url,
            headers={"Authorization": f"Bearer {self.obsidian_token}"},
            cache_tools_list=True,
            tool_filter=tool_filter,
        )
