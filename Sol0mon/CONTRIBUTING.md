# Contributing

Contributions are welcome. Before starting a large change, open an issue to discuss the problem and intended approach.

## Development Setup

1. Use Python 3.12, matching the version represented by the supplied environment.
2. Create a virtual environment and install `requirements.txt`.
3. Copy `.env.example` to `.env` and configure only the providers needed for your work. Never commit `.env` or user workspace data.

## Changes and Pull Requests

- Keep changes focused and preserve existing behavior unless the change explicitly updates it.
- Add or update tests for behavior changes.
- Run `python run_all_tests.py` and report its results. Note tests that could not run because of external services or tools.
- Explain the change, relevant configuration updates, and any known limitations in the pull request.

## License Status

This repository does not currently contain a license. The maintainers must select and add a license before contributions can be accepted under an open-source license.
