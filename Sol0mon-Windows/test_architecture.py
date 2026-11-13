from architecture_runner import (
    build_architecture
)

if __name__ == "__main__":
    architecture = build_architecture(
        "openclaw",
        "planet_scale_ai_consensus_systems.md"
    )
    print(architecture[:3000])
