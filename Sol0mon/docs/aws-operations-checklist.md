# AWS operations checklist (not yet verified)

Sol0mon's debate workflow uses signed-in ChatGPT and Gemini browser sessions.
The current design must not be described as headless or fully unattended.

Before operating on an AWS instance, an operator must verify:

- Install the Python version documented by the project and install the
  repository requirements in a virtual environment.
- Install a compatible Chrome/Chromium build and Playwright browser support.
- Bind Chrome remote debugging to loopback (`127.0.0.1`) only. Never expose
  port `9222` to a public interface or security group.
- Use a dedicated persistent browser profile for Sol0mon and complete
  ChatGPT/Gemini sign-in interactively. Protect the profile as credential data.
- Keep `.env` outside version control with restrictive file permissions. Do
  not put provider credentials in command-line arguments or logs.
- Place SQLite databases and workspace artifacts on persistent encrypted
  storage and confirm backup/restore procedures. SQLite WAL sidecar files
  must be handled consistently during backup.
- Confirm provider configuration and authenticated browser sessions at startup;
  inspect provider/browser failure logs and recovery after interruption.
- Test restart and resume behavior with a backed-up copy of production data
  before relying on unattended service restarts.

No systemd unit is supplied: this remediation has not established that the
current interactive browser workflow can safely run as a service. No AWS
deployment has been performed.
