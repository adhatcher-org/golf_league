"""Entrypoint for the Golf League multi-agent workflow.

    uv run python workflow.py --phase status
    uv run python workflow.py --phase all --max-tasks 1

Configuration comes from the environment (see .env.example); no secrets live in
source. Models are routed through the LiteLLM proxy: planning roles on
`claude-sonnet-5-cloud-plan`, the implementer on `qwen3-coder-30b`.
"""

from orchestrator.run import main

if __name__ == "__main__":
    raise SystemExit(main())
