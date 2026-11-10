from architecture_runner import (
    build_architecture
)

architecture = build_architecture(
    "openclaw",
    "planet_scale_ai_consensus_systems.md"
)

print(
    architecture[:3000]
)
